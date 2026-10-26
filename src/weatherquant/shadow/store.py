"""Persistence for the shadow engine: every write, in one place.

Two rules this module exists to keep:

**Natural keys, so a re-run overwrites instead of duplicating.** Every insert
conflicts on the key that identifies the *fact* - a market is identified by its
id, a checkpoint by its path, a prediction by model + version + run + date +
event - so replaying a batch after a crash converges rather than doubling.

**One transaction per batch.** :meth:`WeatherquantStore.commit_batch` writes the
effects *and* the ingest offsets together. Advancing an offset without its
effects loses lines; effects without the offset replays them. Neither is
representable when both are in the same commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from weatherquant.shadow.decision import Decision, Direction
from weatherquant.shadow.events import BookObservation, CatalogMarket
from weatherquant.shadow.ingest import Checkpoint, QualityEvent
from weatherquant.storage.shadow import ShadowDatabase

__all__ = ["ShadowStore", "decision_id", "quality_event_id"]


def decision_id(*, run_id: str, cycle_id: str, market_id: str, direction: Direction) -> str:
    """Deterministic id: recomputing a cycle yields the same row, not a second one."""
    basis = "|".join([run_id, cycle_id, market_id, direction.value])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def quality_event_id(*, run_id: str, kind: str, path: str | None, detail: str) -> str:
    basis = "|".join([run_id, kind, path or "", detail])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class ShadowStore:
    """Thin, explicit write layer over one open :class:`ShadowDatabase`."""

    def __init__(self, db: ShadowDatabase) -> None:
        self.db = db

    # -- hydration ----------------------------------------------------------

    def load_checkpoints(self) -> list[tuple]:
        return self.db.con.execute(
            "SELECT file_path, source, offset_bytes, lines_processed, last_run_id, "
            "last_archive_seq, last_received_at_utc, file_identity, updated_at_utc "
            "FROM ingest_checkpoints"
        ).fetchall()

    def load_catalog_rows(self) -> list[tuple]:
        return self.db.con.execute(
            "SELECT market_id, event_id, slug, question, bracket_label, bracket_kind, "
            "bracket_low, bracket_high, bracket_rounding, token_yes, token_no, target_date, "
            "station, tick_size, min_order_size, fee_rate, fee_exponent, active, closed, "
            "accepting_orders, raw_sha256, source_run_id FROM market_catalog"
        ).fetchall()

    def load_book_rows(self) -> list[tuple]:
        return self.db.con.execute(
            "SELECT token_id, market_id, book_id, book_source, bids_json, asks_json, "
            "venue_ts_utc, venue_ts_origin, collector_received_at_utc, "
            "shadow_received_at_utc, quality, defects, updated_at_utc FROM token_books"
        ).fetchall()

    def load_forecast_rows(self) -> tuple[list[tuple], list[tuple]]:
        runs = self.db.con.execute(
            "SELECT model, run_time_utc, status, http_status, error_text, payload_sha256, "
            "publishable_at_utc, publishable_basis, received_at_utc, n_points "
            "FROM forecast_runs"
        ).fetchall()
        daily = self.db.con.execute(
            "SELECT model, run_time_utc, target_date, station, predicted_max_f, n_hours, "
            "n_missing_hours, is_complete, computed_at_utc FROM forecast_daily_max"
        ).fetchall()
        return runs, daily

    def collector_last_seen(self) -> datetime | None:
        """Newest ``received_at_utc`` the collector has stamped on anything we read.

        Checkpoints, not the in-memory stats: a restarted shadow has no stats
        but does have offsets, and reporting "never seen" after a restart would
        be a lie about the feed."""
        row = self.db.con.execute(
            "SELECT MAX(last_received_at_utc) FROM ingest_checkpoints"
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # -- writes -------------------------------------------------------------

    def upsert_markets(self, markets: list[CatalogMarket], *, now: datetime) -> int:
        if not markets:
            return 0
        sql = (
            "INSERT INTO market_catalog VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (market_id) DO UPDATE SET "
            "question = excluded.question, tick_size = excluded.tick_size, "
            "min_order_size = excluded.min_order_size, fee_rate = excluded.fee_rate, "
            "fee_exponent = excluded.fee_exponent, active = excluded.active, "
            "closed = excluded.closed, accepting_orders = excluded.accepting_orders, "
            "raw_sha256 = excluded.raw_sha256, source_run_id = excluded.source_run_id, "
            "last_seen_utc = excluded.last_seen_utc"
        )
        # Bracket columns are deliberately NOT updated: a bracket is the market's
        # identity, and first-write-wins keeps it from drifting under us. This
        # mirrors the collector's own markets upsert.
        rows = [
            (
                m.market_id,
                m.event_id,
                m.slug,
                m.question,
                m.bracket.label if m.bracket else None,
                m.bracket.kind.value if m.bracket else None,
                m.bracket.low if m.bracket else None,
                m.bracket.high if m.bracket else None,
                m.bracket.rounding.value if m.bracket else None,
                m.token_yes,
                m.token_no,
                m.target_date,
                m.station,
                m.tick_size,
                m.min_order_size,
                m.fee_rate,
                m.fee_exponent,
                m.active,
                m.closed,
                m.accepting_orders,
                m.raw_sha256,
                m.source_run_id,
                now,
                now,
            )
            for m in markets
        ]
        self.db.executemany(sql, rows)
        return len(rows)

    def upsert_books(self, observations: list[BookObservation], *, now: datetime) -> int:
        if not observations:
            return 0
        sql = (
            "INSERT INTO token_books (token_id, market_id, book_id, book_source, bids_json, "
            "asks_json, venue_ts_utc, venue_ts_origin, collector_received_at_utc, "
            "shadow_received_at_utc, quality, defects, updated_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (token_id) DO UPDATE SET "
            "market_id = excluded.market_id, book_id = excluded.book_id, "
            "book_source = excluded.book_source, bids_json = excluded.bids_json, "
            "asks_json = excluded.asks_json, venue_ts_utc = excluded.venue_ts_utc, "
            "venue_ts_origin = excluded.venue_ts_origin, "
            "collector_received_at_utc = excluded.collector_received_at_utc, "
            "shadow_received_at_utc = excluded.shadow_received_at_utc, "
            "quality = excluded.quality, defects = excluded.defects, "
            "updated_at_utc = excluded.updated_at_utc "
            "WHERE excluded.collector_received_at_utc >= token_books.collector_received_at_utc"
        )
        rows = [
            (
                o.token_id,
                o.market_id,
                o.book_id,
                o.book_source,
                json.dumps(
                    [
                        {"price_usd_per_share": lv.price_usd_per_share, "size_shares": lv.size_shares}
                        for lv in o.book.bids
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                json.dumps(
                    [
                        {"price_usd_per_share": lv.price_usd_per_share, "size_shares": lv.size_shares}
                        for lv in o.book.asks
                    ],
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                o.venue_ts,
                o.venue_ts_origin,
                o.collector_received_at_utc,
                o.shadow_received_at_utc,
                o.quality,
                "+".join(o.defects),
                now,
            )
            for o in observations
        ]
        self.db.executemany(sql, rows)
        return len(rows)

    def save_checkpoints(self, checkpoints: dict[str, Checkpoint]) -> int:
        if not checkpoints:
            return 0
        sql = (
            "INSERT INTO ingest_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (file_path) DO UPDATE SET "
            "offset_bytes = excluded.offset_bytes, "
            "lines_processed = excluded.lines_processed, "
            "last_run_id = excluded.last_run_id, "
            "last_archive_seq = excluded.last_archive_seq, "
            "last_received_at_utc = excluded.last_received_at_utc, "
            "file_identity = excluded.file_identity, "
            "updated_at_utc = excluded.updated_at_utc"
        )
        self.db.executemany(
            sql,
            [
                (
                    c.file_path,
                    c.source,
                    c.offset_bytes,
                    c.lines_processed,
                    c.last_run_id,
                    c.last_archive_seq,
                    c.last_received_at_utc,
                    c.file_identity,
                    c.updated_at_utc,
                )
                for c in checkpoints.values()
            ],
        )
        return len(checkpoints)

    def record_quality(self, events: list[QualityEvent], *, run_id: str) -> int:
        if not events:
            return 0
        sql = (
            "INSERT INTO data_quality_events "
            "(event_id, run_id, ts_utc, kind, severity, source, path, offset_bytes, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING"
        )
        self.db.executemany(
            sql,
            [
                (
                    quality_event_id(
                        run_id=run_id, kind=e.kind, path=e.path, detail=e.detail
                    ),
                    run_id,
                    e.ts_utc,
                    e.kind,
                    e.severity,
                    e.source,
                    e.path,
                    e.offset_bytes,
                    e.detail,
                )
                for e in events
            ],
        )
        return len(events)

    def save_forecasts(self, run_rows: list[tuple], daily_rows: list[tuple]) -> None:
        if run_rows:
            self.db.executemany(
                "INSERT INTO forecast_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (model, run_time_utc) DO UPDATE SET "
                "status = excluded.status, http_status = excluded.http_status, "
                "error_text = excluded.error_text, payload_sha256 = excluded.payload_sha256, "
                "publishable_at_utc = excluded.publishable_at_utc, "
                "publishable_basis = excluded.publishable_basis, "
                "received_at_utc = excluded.received_at_utc, n_points = excluded.n_points",
                run_rows,
            )
        if daily_rows:
            self.db.executemany(
                "INSERT INTO forecast_daily_max VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (model, run_time_utc, target_date, station) DO UPDATE SET "
                "predicted_max_f = excluded.predicted_max_f, n_hours = excluded.n_hours, "
                "n_missing_hours = excluded.n_missing_hours, "
                "is_complete = excluded.is_complete, computed_at_utc = excluded.computed_at_utc",
                daily_rows,
            )

    def save_model_versions(self, rows: list[tuple]) -> None:
        if rows:
            self.db.executemany(
                "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (model_name, model_version) DO UPDATE SET "
                "loaded_at_utc = excluded.loaded_at_utc, is_primary = excluded.is_primary",
                rows,
            )

    def save_predictions(self, rows: list[tuple]) -> None:
        if rows:
            self.db.executemany(
                "INSERT INTO model_predictions VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (model_name, model_version, station, target_date, event_id, "
                "forecast_run_time_utc) DO NOTHING",
                rows,
            )

    def save_decisions(self, decisions: list[Decision], *, run_id: str, now: datetime) -> int:
        if not decisions:
            return 0
        decision_sql = (
            "INSERT INTO shadow_decisions ("
            "decision_id, run_id, cycle_id, market_id, event_id, token_id, bracket_label, "
            "target_date, station, direction, state, primary_reason, model_name, model_version, "
            "model_run_id, forecast_run_time_utc, snapshot_id, book_id, venue_ts_utc, "
            "collector_received_at_utc, shadow_received_at_utc, decision_time_utc, "
            "snapshot_age_ms, model_probability, market_price, best_bid, best_ask, spread, "
            "available_depth_usd, proposed_shares, estimated_fill_shares, estimated_fill_price, "
            "estimated_fees_usd, estimated_buffer_usd, estimated_cost_usd, "
            "expected_net_profit_usd, net_edge, market_closed, forecast_age_hours, "
            "thresholds_version, mode, mode_reason, decision_latency_ms, created_at_utc, "
            "export_seq) "
            "VALUES (" + ", ".join(["?"] * 44) + ", nextval('seq_shadow_decision_export')) "
            "ON CONFLICT (cycle_id, market_id, direction) DO UPDATE SET "
            "state = excluded.state, primary_reason = excluded.primary_reason, "
            "mode = excluded.mode, mode_reason = excluded.mode_reason, "
            "net_edge = excluded.net_edge, "
            "created_at_utc = excluded.created_at_utc"
            # export_seq is deliberately absent from the UPDATE above: the
            # sequence value is claimed on insert only, so re-writing a
            # decision (the engine re-observing the same cycle+market+
            # direction) neither changes its export position nor re-exports
            # it. The column is nullable so pre-0005 images can still insert
            # (NULL); the exporter repairs those rows in bounded batches.
        )
        decision_rows = [
            (
                decision_id(
                    run_id=run_id, cycle_id=d.cycle_id, market_id=d.market_id, direction=d.direction
                ),
                run_id,
                d.cycle_id,
                d.market_id,
                d.event_id,
                d.token_id,
                d.bracket_label,
                d.target_date,
                d.station,
                d.direction.value,
                d.state.value,
                d.primary_reason.value if d.primary_reason else None,
                d.model_name,
                d.model_version,
                d.model_run_id,
                d.forecast_run_time_utc,
                d.snapshot_id,
                d.book_id,
                d.venue_ts_utc,
                d.collector_received_at_utc,
                d.shadow_received_at_utc,
                d.decision_time_utc,
                d.snapshot_age_ms,
                d.model_probability,
                d.market_price,
                d.best_bid,
                d.best_ask,
                d.spread,
                d.available_depth_usd,
                d.proposed_shares,
                d.estimated_fill_shares,
                d.estimated_fill_price,
                d.estimated_fees_usd,
                d.estimated_buffer_usd,
                d.estimated_cost_usd,
                d.expected_net_profit_usd,
                d.net_edge,
                d.market_closed,
                d.forecast_age_hours,
                d.thresholds_version,
                d.mode.value,
                d.mode_reason,
                d.decision_latency_ms,
                now,
            )
            for d in decisions
        ]
        self.db.executemany(decision_sql, decision_rows)

        reason_rows = []
        for d in decisions:
            did = decision_id(
                run_id=run_id, cycle_id=d.cycle_id, market_id=d.market_id, direction=d.direction
            )
            for ordinal, reason in enumerate(d.reasons):
                reason_rows.append(
                    (
                        did,
                        ordinal,
                        reason.value,
                        ordinal == 0,
                        d.details.get(reason.value),
                    )
                )
        if reason_rows:
            self.db.executemany(
                "INSERT INTO rejection_reasons VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (decision_id, reason) DO UPDATE SET "
                "ordinal = excluded.ordinal, is_primary = excluded.is_primary, "
                "detail = excluded.detail",
                reason_rows,
            )
        return len(decisions)

    def save_snapshots(self, decisions: list[Decision], *, now: datetime) -> None:
        """Persist the market state each decision looked at, once per snapshot.

        Note the depth columns: a **buy** consumes the ask side and a **sell**
        the bid side, so ``available_depth_usd`` lands in the column for the
        side that was actually walked - recording it in the other one would
        document liquidity the trade could never have touched.
        """
        seen: dict[str, Decision] = {}
        for d in decisions:
            if d.snapshot_id:
                seen.setdefault(d.snapshot_id, d)
        if not seen:
            return
        sql = (
            "INSERT INTO market_snapshots ("
            "snapshot_id, market_id, token_id, book_id, book_source, venue_ts_utc, "
            "venue_ts_origin, collector_received_at_utc, shadow_received_at_utc, "
            "decision_time_utc, snapshot_age_ms, latency_ms, best_bid, best_ask, spread, "
            "bid_depth_usd, ask_depth_usd, levels_bid, levels_ask, defects, quality, "
            "created_at_utc) VALUES ("
            + ", ".join(["?"] * 22)
            + ") ON CONFLICT (snapshot_id) DO NOTHING"
        )
        rows = []
        for sid, d in seen.items():
            is_buy = d.direction is Direction.BUY_YES
            rows.append(
                (
                    sid,
                    d.market_id,
                    d.token_id or "",
                    d.book_id or "",
                    d.book_source or "",
                    d.venue_ts_utc,
                    "venue" if d.venue_ts_utc else "missing",
                    d.collector_received_at_utc,
                    d.shadow_received_at_utc,
                    d.decision_time_utc,
                    d.snapshot_age_ms,
                    _latency_ms(d),
                    d.best_bid,
                    d.best_ask,
                    d.spread,
                    None if is_buy else d.available_depth_usd,
                    d.available_depth_usd if is_buy else None,
                    0,
                    0,
                    "",
                    "ok",
                    now,
                )
            )
        self.db.executemany(sql, rows)

    def save_metrics(self, *, run_id: str, payload: dict, computed_at: datetime) -> None:
        self.db.execute(
            "INSERT INTO shadow_metrics VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [run_id, computed_at, json.dumps(payload, sort_keys=True, default=str)],
        )

    # -- the batch ----------------------------------------------------------

    def commit_batch(
        self,
        *,
        run_id: str,
        now: datetime,
        markets: list[CatalogMarket],
        books: list[BookObservation],
        checkpoints: dict[str, Checkpoint],
        quality: list[QualityEvent],
        forecast_runs: list[tuple] | None = None,
        forecast_daily: list[tuple] | None = None,
        predictions: list[tuple] | None = None,
        decisions: list[Decision] | None = None,
        metrics: dict | None = None,
    ) -> None:
        """Effects and offsets, atomically. Either a restart sees both or neither."""
        with self.db.transaction():
            self.upsert_markets(markets, now=now)
            self.upsert_books(books, now=now)
            self.save_checkpoints(checkpoints)
            self.record_quality(quality, run_id=run_id)
            if forecast_runs or forecast_daily:
                self.save_forecasts(forecast_runs or [], forecast_daily or [])
            if predictions:
                self.save_predictions(predictions)
            if decisions:
                self.save_snapshots(decisions, now=now)
                self.save_decisions(decisions, run_id=run_id, now=now)
            if metrics is not None:
                self.save_metrics(run_id=run_id, payload=metrics, computed_at=now)


def _latency_ms(d: Decision) -> int | None:
    if d.shadow_received_at_utc is None or d.venue_ts_utc is None:
        return None
    return int((d.shadow_received_at_utc - d.venue_ts_utc).total_seconds() * 1000)

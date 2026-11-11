"""Detail snapshots the *shadow engine* can publish from its own connection.

Status: **wired.** The dashboard's row-level views (decision list,
model-vs-market, probability distributions) can only be built by somebody who
is allowed to hold ``shadow.duckdb`` - which is the shadow engine alone, for
its whole life. ``weatherquant.shadow.runner._publish_report`` calls
:func:`publish_detail_snapshots` on the connection it already holds, under the
same throttle as the report and in its own ``try`` (docs/dashboard-data-contract.md §4);
the dashboard degrades to explicit ``unavailable`` if that call ever stops
landing.

Note for anyone touching :func:`build_forecasts_snapshot`: its column list must
match ``migrations_shadow/0001_shadow_schema.sql``. The first version selected
a ``bracket_label`` that only existed in this module's own test fixture, and
the query failed against the real database while every producer test stayed
green.

Design constraints, same as everywhere else:

* the connection is *injected* - this module never imports duckdb and never
  opens a file, so a reader can prove the dashboard package contains no
  database handle;
* every payload carries ``schema_version`` / ``generated_at_utc`` /
  ``source_run_id`` / ``source_version`` / ``stale_after_seconds`` so the API
  can judge it without guessing;
* rows are windowed (decisions: newest N; markets: active window; forecasts:
  open target dates) - a snapshot is a page, never a table dump.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weatherquant.shadow.publish import atomic_write_json

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "build_decisions_snapshot",
    "build_forecasts_snapshot",
    "build_markets_snapshot",
    "publish_detail_snapshots",
]

SNAPSHOT_SCHEMA_VERSION = 1
DECISION_WINDOW = 500
FORECAST_HORIZON_DAYS = 10

#: Markets snapshot: decisions considered "current" enough that we should
#: bother finding the last row per market. Older decisions are outside the
#: engine's retained window anyway (shadow_decisions grows unbounded today
#: but we cap the scan); the window below is generous enough that a market
#: inactive for a while still shows its last decision, and tight enough that
#: the query does not ROW_NUMBER() across a multi-million-row table.
MARKET_DECISION_WINDOW_DAYS = 30

#: Forecasts snapshot: how far back target_date goes before we stop caring.
#: Anything older than today minus this is ancient history the dashboard has
#: never shown and will never need to.
FORECAST_LOOKBACK_DAYS = 1


def _envelope(
    kind: str,
    data: dict[str, Any],
    *,
    now: datetime,
    run_id: str,
    source_version: str,
    stale_after_seconds: float,
) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "kind": kind,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "source_service": "weatherquant-shadow",
        "source_run_id": run_id,
        "source_version": source_version,
        "stale_after_seconds": stale_after_seconds,
        "data_as_of_utc": now.astimezone(UTC).isoformat(),
        "data_quality": "ok",
        "data": data,
    }


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


def build_decisions_snapshot(
    con: Any,
    *,
    now: datetime,
    run_id: str,
    source_version: str,
    window: int = DECISION_WINDOW,
    stale_after_seconds: float = 300.0,
) -> dict[str, Any]:
    """Newest ``window`` decisions, each with its full rejection reason set."""
    rows = con.execute(
        """
        SELECT d.decision_id, d.decision_time_utc, d.market_id, d.bracket_label,
               d.target_date, d.direction, d.state, d.mode, d.primary_reason,
               d.model_name, d.model_version, d.model_probability, d.market_price,
               d.best_bid, d.best_ask, d.net_edge, d.snapshot_age_ms,
               d.forecast_age_hours, d.available_depth_usd, d.thresholds_version,
               d.run_id, c.question
        FROM shadow_decisions d
        LEFT JOIN market_catalog c ON c.market_id = d.market_id
        ORDER BY d.decision_time_utc DESC
        LIMIT ?
        """,
        [int(window)],
    ).fetchall()

    reason_rows = con.execute(
        """
        SELECT decision_id, reason FROM rejection_reasons
        WHERE decision_id IN (SELECT decision_id FROM shadow_decisions ORDER BY decision_time_utc DESC LIMIT ?)
        ORDER BY decision_id, ordinal
        """,
        [int(window)],
    ).fetchall()
    reasons: dict[str, list[str]] = {}
    for decision_id, reason in reason_rows:
        reasons.setdefault(str(decision_id), []).append(str(reason))

    decisions = [
        {
            "decision_id": str(row[0]),
            "decided_at_utc": _iso(row[1]),
            "market_id": str(row[2]),
            "market_question": row[21],
            "bracket_label": row[3],
            "target_date": str(row[4]) if row[4] is not None else None,
            "direction": row[5],
            "state": str(row[6]),
            "mode": row[7],
            "primary_reason": row[8],
            "model_name": row[9],
            "model_version": row[10],
            "model_probability": row[11],
            "market_price": row[12],
            "best_bid": row[13],
            "best_ask": row[14],
            "net_edge_usd": row[15],
            "snapshot_age_ms": row[16],
            "forecast_age_hours": row[17],
            "available_depth_usd": row[18],
            "thresholds_version": row[19],
            "run_id": row[20],
            "reasons": reasons.get(str(row[0]), []),
        }
        for row in rows
    ]
    return _envelope(
        "recent-decisions",
        {"decisions": decisions, "window": int(window)},
        now=now,
        run_id=run_id,
        source_version=source_version,
        stale_after_seconds=stale_after_seconds,
    )


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------


def build_markets_snapshot(
    con: Any,
    *,
    now: datetime,
    run_id: str,
    source_version: str,
    primary_model: str,
    stale_after_seconds: float = 300.0,
) -> dict[str, Any]:
    """Every tracked market with its latest book, model probability and edge.

    Model probability comes from the newest prediction of ``primary_model``
    for the market's bracket - the same row the decision engine itself reads,
    so the dashboard cannot show a number the engine did not use.
    """
    catalog = con.execute(
        """
        SELECT market_id, event_id, question, bracket_label, bracket_low, bracket_high,
               target_date, station, token_yes, active, closed, accepting_orders
        FROM market_catalog
        ORDER BY target_date, market_id
        """
    ).fetchall()

    books = {
        str(row[0]): row
        for row in con.execute(
            """
            SELECT token_id, bids_json, asks_json, venue_ts_utc, shadow_received_at_utc, quality, defects
            FROM token_books
            """
        ).fetchall()
    }

    # Newest prediction per (event_id, target_date) for primary_model, SQL-side.
    # model_predictions grows unbounded (each forecast run adds a row); qualify
    # + a lower bound on target_date keeps this bounded without pulling the
    # whole history into Python.
    lb_target = (now.date() - timedelta(days=FORECAST_LOOKBACK_DAYS))
    latest_predictions_rows = con.execute(
        """
        SELECT event_id, target_date, probabilities_json, forecast_run_time_utc, model_version
        FROM model_predictions
        WHERE model_name = ?
          AND target_date >= ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY event_id, target_date
            ORDER BY forecast_run_time_utc DESC
        ) = 1
        """,
        [str(primary_model), lb_target],
    ).fetchall()
    latest_predictions: dict[tuple[str, str], tuple[Any, ...]] = {
        (str(row[0]), str(row[1])): row for row in latest_predictions_rows
    }

    # Last decision per market_id, SQL-side, only within a rolling window so
    # ROW_NUMBER() does not span the entire history of shadow_decisions.
    decision_window_start = now - timedelta(days=MARKET_DECISION_WINDOW_DAYS)
    last_decisions = {
        str(row[0]): row
        for row in con.execute(
            """
            SELECT market_id, state, decision_time_utc, net_edge, mode, primary_reason
            FROM shadow_decisions
            WHERE decision_time_utc >= ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY decision_time_utc DESC) = 1
            """,
            [decision_window_start],
        ).fetchall()
    }

    markets: list[dict[str, Any]] = []
    for row in catalog:
        (
            market_id,
            event_id,
            question,
            bracket_label,
            _low,
            _high,
            target_date,
            station,
            token_yes,
            active,
            closed,
            accepting,
        ) = row
        market_id = str(market_id)
        book = books.get(str(token_yes))
        quote = _quote_from_book(book, now) if book else {
            "available": False,
            "quality": "missing",
            "reason": "no book recorded for the YES token",
        }
        probs = latest_predictions.get((str(event_id), str(target_date)))
        probability = None
        version = None
        run_time = None
        predicted_max = None
        if probs is not None:
            try:
                distribution = json.loads(probs[2]) if isinstance(probs[2], str) else probs[2]
                probability = distribution.get(str(bracket_label)) if isinstance(distribution, dict) else None
            except (json.JSONDecodeError, AttributeError):
                probability = None
            version = probs[4]
            run_time = _iso(probs[3])
        decision = last_decisions.get(market_id)
        markets.append(
            {
                "market_id": market_id,
                "event_id": str(event_id),
                "question": str(question or ""),
                "target_date": str(target_date) if target_date is not None else None,
                "bracket_label": str(bracket_label or ""),
                "bracket_low": None,
                "bracket_high": None,
                "station": station,
                "active": bool(active),
                "closed": bool(closed),
                "accepting_orders": bool(accepting),
                "tradeable": bool(active) and bool(accepting) and not bool(closed),
                "token_yes": str(token_yes),
                "book": quote,
                "model_probability": probability,
                "model_version": version,
                "forecast_run_time_utc": run_time,
                "predicted_max_f": predicted_max,
                "net_edge_usd": float(decision[3]) if decision and decision[3] is not None else None,
                "last_decision": (
                    {
                        "state": decision[1],
                        "decided_at_utc": _iso(decision[2]),
                        "mode": decision[4],
                        "primary_reason": decision[5],
                    }
                    if decision
                    else None
                ),
                "updated_at_utc": _iso(book[4]) if book else None,
            }
        )

    return _envelope(
        "market-snapshot",
        {"markets": markets, "primary_model": primary_model},
        now=now,
        run_id=run_id,
        source_version=source_version,
        stale_after_seconds=stale_after_seconds,
    )


def _quote_from_book(row: tuple[Any, ...], now: datetime) -> dict[str, Any]:
    """Levels from stored JSON -> the same shape the API's BookQuote has."""
    _token_id, bids_json, asks_json, venue_ts, received_at, quality, defects = row
    bids = _levels(bids_json, descending=True)
    asks = _levels(asks_json, descending=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    as_of = venue_ts or received_at
    age = (now - as_of).total_seconds() if isinstance(as_of, datetime) else None
    return {
        "available": bool(bids or asks),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": (best_ask - best_bid) if best_bid is not None and best_ask is not None else None,
        "bid_depth_usd": round(sum(p * s for p, s in bids), 4) if bids else 0.0,
        "ask_depth_usd": round(sum(p * s for p, s in asks), 4) if asks else 0.0,
        "levels_bid": len(bids),
        "levels_ask": len(asks),
        "defects": [d for d in str(defects or "").split(",") if d],
        "quality": str(quality or "ok"),
        "age_seconds": round(age, 3) if age is not None else None,
        "venue_ts_utc": _iso(venue_ts),
        "as_of_utc": _iso(as_of),
        "reason": None,
    }


def _levels(raw: Any, *, descending: bool) -> list[tuple[float, float]]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        pairs = [
            (float(level[0]), float(level[1]))
            for level in parsed
            if isinstance(level, (list, tuple)) and len(level) >= 2
        ]
    except (json.JSONDecodeError, TypeError, ValueError, IndexError):
        return []
    pairs.sort(key=lambda pair: pair[0], reverse=descending)
    return pairs


# ---------------------------------------------------------------------------
# forecasts
# ---------------------------------------------------------------------------


def build_forecasts_snapshot(
    con: Any,
    *,
    now: datetime,
    run_id: str,
    source_version: str,
    primary_model: str,
    horizon_days: int = FORECAST_HORIZON_DAYS,
    stale_after_seconds: float = 600.0,
) -> dict[str, Any]:
    """Probability distributions for open target dates, all tracked models.

    Point-in-time by construction: only the prediction row's own
    ``forecast_run_time_utc`` / ``training_cutoff_utc`` are carried, so the UI
    can never present a forecast as knowing what happened after it was issued.
    """
    horizon = now.date() + timedelta(days=max(horizon_days, 1))
    lb_target = now.date() - timedelta(days=FORECAST_LOOKBACK_DAYS)
    rows = con.execute(
        """
        SELECT model_name, model_version, target_date, event_id,
               probabilities_json, predicted_max_f, forecast_run_time_utc,
               training_cutoff_utc, forecast_received_at_utc, station
        FROM model_predictions
        WHERE target_date >= ? AND target_date <= ? AND forecast_run_time_utc <= ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY model_name, target_date, event_id
            ORDER BY forecast_run_time_utc DESC
        ) = 1
        ORDER BY target_date, model_name, event_id
        """,
        [lb_target, horizon, now],
    ).fetchall()

    distributions: list[dict[str, Any]] = []
    for row in rows:
        model, version, target_date, _event_id, probs_json, predicted_max, run_time, cutoff, received, station = row
        try:
            buckets_raw = json.loads(probs_json) if isinstance(probs_json, str) else probs_json
        except json.JSONDecodeError:
            continue
        if not isinstance(buckets_raw, dict):
            continue
        distributions.append(
            {
                "model": str(model),
                "version": str(version),
                "target_date": str(target_date),
                "event_id": str(_event_id),
                "forecast_run_time_utc": _iso(run_time),
                "training_cutoff_utc": _iso(cutoff),
                "forecast_received_at_utc": _iso(received),
                "predicted_max_f": predicted_max,
                "station": station,
                "buckets": [
                    {
                        "label": str(label),
                        "probability": float(probability),
                    }
                    for label, probability in sorted(buckets_raw.items())
                    if isinstance(probability, (int, float))
                ],
            }
        )

    return _envelope(
        "forecast-snapshot",
        {"distributions": distributions, "primary_model": primary_model},
        now=now,
        run_id=run_id,
        source_version=source_version,
        stale_after_seconds=stale_after_seconds,
    )


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------


def publish_detail_snapshots(
    con: Any,
    *,
    directory: str | Path,
    run_id: str,
    source_version: str,
    now: datetime | None = None,
    primary_model: str = "calibrated",
    stale_after_seconds: float = 300.0,
) -> list[Path]:
    """Build and atomically publish all three detail snapshots.

    Called with the *already open* connection - the engine's, never a new one.
    Any failure is the caller's to log: an engine must not stop publishing its
    heartbeat because a dashboard snapshot failed.
    """
    now = now or datetime.now(tz=UTC)
    target = Path(directory)
    published: list[Path] = []
    builders_ = (
        ("recent-decisions.json", lambda: build_decisions_snapshot(con, now=now, run_id=run_id, source_version=source_version, stale_after_seconds=stale_after_seconds)),
        ("market-snapshot.json", lambda: build_markets_snapshot(con, now=now, run_id=run_id, source_version=source_version, primary_model=primary_model, stale_after_seconds=stale_after_seconds)),
        ("forecast-snapshot.json", lambda: build_forecasts_snapshot(con, now=now, run_id=run_id, source_version=source_version, primary_model=primary_model, stale_after_seconds=stale_after_seconds)),
    )
    for filename, build in builders_:
        path = target / filename
        atomic_write_json(path, build())
        published.append(path)
    return published


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)

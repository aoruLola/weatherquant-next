"""The collector's single DuckDB writer.

Responsibilities and the reasoning behind each:

**Batched commits.** One transaction per flush rather than per row. A weather
market's book can tick many times a second, and a durability ``fsync`` per row
would dominate the collector's cost while buying nothing - a crash loses at most
``flush_interval`` seconds, which the gap record then documents.

**Content-addressed dedup.** ``book_id`` is a hash of the book's *content*, not
of when we fetched it. Polling an unchanged book therefore produces the same id
and is skipped, so an hour of quiet polling stores one row, not 3600. The
skipped count is still reported, because "we polled and nothing changed" is
itself evidence the poll worked.

**Checkpointing.** ``CHECKPOINT`` folds the write-ahead log into the main file.
It is what keeps the WAL from growing without bound over weeks of uptime and
what makes a subsequent backup meaningful.

**Consistent backup.** The database file is locked for the writer's entire life,
so copying it from outside would either fail or capture a torn state. Backups
are therefore performed *by the writer itself*: inside one transaction it
attaches a second database, replays every table and index into it, then
re-opens the result read-only and compares row counts. A backup is reported as
successful only after that verification passes - a backup you have not read
back is a hope.

**The database is never destroyed.** ``open()`` refuses to proceed if the file
contains tables but no migration ledger, rather than "initialising" over it.
Nothing in this module deletes, truncates or recreates an existing database.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from weatherquant.storage import connect, initialize
from weatherquant.storage.migrator import available_migrations

log = logging.getLogger(__name__)

__all__ = [
    "BackupReport",
    "CollectorWriter",
    "IntegrityReport",
    "ParquetExportReport",
    "book_content_hash",
    "canonical_levels_json",
    "escape_duckdb_string",
    "next_event_id",
]

_BOOK_COLUMNS = (
    "book_id, market_id, token_id, bids_json, asks_json, best_bid, best_ask, spread, "
    "bid_depth_usd, ask_depth_usd, defects, venue_ts, fetched_at_utc, "
    "source, venue_ts_origin, content_hash, quality, run_id"
)
_BOOK_PLACEHOLDERS = ", ".join("?" for _ in range(len(_BOOK_COLUMNS.split(","))))

_PRICE_COLUMNS = (
    "market_id, token_id, ts, mid_usd_per_share, source, fetched_at_utc, venue_ts_origin, run_id"
)
_PRICE_PLACEHOLDERS = ", ".join("?" for _ in range(len(_PRICE_COLUMNS.split(","))))

_COLLECTOR_EVENT_COLUMNS = "event_id, run_id, ts_utc, level, kind, message, details_json"
_COLLECTOR_EVENT_PLACEHOLDERS = ", ".join("?" for _ in range(len(_COLLECTOR_EVENT_COLUMNS.split(","))))

# Which column defines each table's day partition for parquet export.
_PARQUET_PARTITION_COLUMN = {
    "order_books": "fetched_at_utc",
    "price_history": "ts",
    "markets": "fetched_at_utc",
    "events": "fetched_at_utc",
    "settlements": "target_date",
    "observations": "obs_time_utc",
    "forecast_daily": "target_date",
    "collector_events": "ts_utc",
    "collection_gaps": "detected_at_utc",
    "collector_runs": "started_at_utc",
}


def escape_duckdb_string(value: str) -> str:
    """Escape a value for embedding in single-quoted DuckDB SQL."""
    return value.replace("'", "''")


def iter_ddl_statements() -> list[str]:
    """Flatten every migration's DDL into individual, comment-free statements.

    Comments are stripped **line by line before** splitting on ``;``, not after,
    and trailing comments are cut too - not just whole-line ones. Both halves
    matter:

    * a comment containing a semicolon ("...restarts; it lives outside ...")
      would otherwise cut a statement in half;
    * a trailing comment after a semicolon
      (``... DEFAULT 'rest';          -- rest | ws``) would otherwise become the
      *leading text of the next statement*, and the resulting
      ``"-- rest | ws ALTER TABLE ..."`` would no longer match
      ``CREATE INDEX``/``CREATE SEQUENCE`` - producing a backup with no indexes
      and no sequence, silently.

    Our migrations never place a semicolon or a double hyphen inside a string
    literal, so cutting at the first ``--`` is safe here. If that ever changes,
    this function must grow a real lexer rather than a split.
    """
    statements: list[str] = []
    for spec in available_migrations():
        code_lines: list[str] = []
        for line in spec.sql.splitlines():
            comment_at = line.find("--")
            code = line[:comment_at] if comment_at >= 0 else line
            code = code.strip()
            if code:
                code_lines.append(code)
        for raw in "\n".join(code_lines).split(";"):
            statement = " ".join(raw.split())
            if statement:
                statements.append(statement)
    return statements


def canonical_levels_json(levels: Sequence[tuple[float, float]]) -> str:
    """Serialise ``(price, size)`` pairs in a canonical, order-preserving form.

    The venue's level order is meaningful (best first), so it is kept; only the
    *representation* is canonicalised - fixed key order, no incidental
    whitespace - so that two captures of the same book hash identically.
    """
    return json.dumps(
        [{"price_usd_per_share": float(p), "size_shares": float(s)} for p, s in levels],
        separators=(",", ":"),
        sort_keys=True,
    )


def book_content_hash(
    token_id: str, bids_json: str, asks_json: str, venue_ts: datetime | None
) -> str:
    """Content address for a book snapshot.

    Two snapshots with the same hash are the *same observation* as far as
    research is concerned, regardless of when we happened to poll them. When
    ``venue_ts`` is absent the absence is part of the identity rather than
    being replaced by a capture time - otherwise a missing exchange timestamp
    would silently become a fabricated one.
    """
    ts_part = venue_ts.astimezone(UTC).isoformat() if venue_ts else "<no-venue-ts>"
    payload = f"{token_id}|{bids_json}|{asks_json}|{ts_part}".encode()
    return hashlib.sha256(payload).hexdigest()


# Counter folded into a hash of (run_id, sequence). A wall-clock timestamp was
# tried first and is *not* safe: on Windows `time.time_ns()` has roughly 15 ms
# resolution, so two rapid calls - or two processes started in the same tick -
# return the same value and would mint identical ids. Deriving the id from the
# run's UUID instead is clock-independent, deterministic, and collision-free
# across restarts without needing any durable state (see 0004_collector.sql for
# why a DuckDB sequence could not be relied on either).
_EVENT_SEQ = 0
_EVENT_SEQ_LOCK = threading.Lock()


def next_event_id(run_id: str) -> int:
    """A unique BIGINT for one log event, safe across process restarts.

    ``sha256(run_id, counter)`` truncated to a non-negative 63-bit integer.
    Within a run the counter makes every input distinct; across runs the UUIDs
    differ, so the inputs differ. A 64-bit hash collision among a few thousand
    events has probability near 1e-15 - and unlike a clock, it cannot be
    defeated by a coarse timer.
    """
    global _EVENT_SEQ
    with _EVENT_SEQ_LOCK:
        _EVENT_SEQ += 1
        sequence = _EVENT_SEQ
    digest = hashlib.sha256(f"{run_id}:{sequence}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    ok: bool
    row_counts: dict[str, int]
    problems: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "row_counts": self.row_counts, "problems": list(self.problems)},
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class BackupReport:
    destination: Path
    verified: bool
    row_counts: dict[str, int]
    expected_counts: dict[str, int]
    schema_versions: tuple[str, ...]
    error: str | None = None
    bytes_written: int = 0

    @property
    def ok(self) -> bool:
        return self.verified and self.error is None


@dataclass(frozen=True, slots=True)
class ParquetExportReport:
    day: date
    tables: dict[str, Path] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass
class _Batch:
    books: list[tuple[Any, ...]] = field(default_factory=list)
    prices: list[tuple[Any, ...]] = field(default_factory=list)
    events: list[tuple[Any, ...]] = field(default_factory=list)
    markets: list[tuple[Any, ...]] = field(default_factory=list)
    collector_events: list[tuple[Any, ...]] = field(default_factory=list)

    def __len__(self) -> int:
        return sum(
            len(x)
            for x in (self.books, self.prices, self.events, self.markets, self.collector_events)
        )

    def clear(self) -> None:
        self.books.clear()
        self.prices.clear()
        self.events.clear()
        self.markets.clear()
        self.collector_events.clear()


class CollectorWriter:
    """Owns the one writable DuckDB connection the collector is allowed."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        run_id: str,
        flush_rows: int = 500,
        flush_seconds: float = 5.0,
        checkpoint_seconds: float = 300.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self.checkpoint_seconds = checkpoint_seconds
        self._con: duckdb.DuckDBPyConnection | None = None
        self._batch = _Batch()
        self._last_flush = datetime.now(tz=UTC)
        self._last_checkpoint = datetime.now(tz=UTC)

        self.stats: dict[str, int] = {
            "books_inserted": 0,
            "books_deduplicated": 0,
            "prices_inserted": 0,
            "prices_deduplicated": 0,
            "events_inserted": 0,
            "markets_inserted": 0,
            "collector_events_inserted": 0,
            "flushes": 0,
            "checkpoints": 0,
        }

    # -- lifecycle ---------------------------------------------------------

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise RuntimeError("writer is not open; call open() first")
        return self._con

    @property
    def is_open(self) -> bool:
        return self._con is not None

    def open(self) -> list[str]:
        """Open (or create) the database and apply pending migrations.

        Never deletes or truncates an existing file. If the file contains data
        but no migration ledger it is *not* ours, and we refuse rather than
        build a schema on top of someone else's data.
        """
        existed_before = self.db_path.exists() and self.db_path.stat().st_size > 0
        self._guard_foreign_database(existed_before)
        con, applied = initialize(self.db_path)
        self._con = con
        log.info(
            "%s database at %s (%d migration(s) applied this start)",
            "created" if not existed_before else "opened",
            self.db_path,
            len(applied),
        )
        return applied

    def _guard_foreign_database(self, existed_before: bool) -> None:
        if not existed_before:
            return
        # Same configuration as the writer's own connection, deliberately not
        # read_only: DuckDB refuses a second, differently-configured handle
        # to one file within a process, so a read-only probe crashes whenever
        # a same-process read-write connection already exists (the backup
        # job's, or a test's). The probe only ever reads; and the file
        # already exists, so this cannot create anything.
        probe = connect(self.db_path)
        try:
            has_ledger = bool(
                probe.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = 'schema_migrations'"
                ).fetchone()
            )
            tables = int(
                probe.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchone()[0]
            )
            if not has_ledger and tables > 0:
                raise RuntimeError(
                    f"refusing to open {self.db_path}: it contains {tables} table(s) but no "
                    "schema_migrations ledger, so weatherquant-next did not create it. "
                    "Initialising over it would destroy whatever it holds. Move the file "
                    "aside and let the collector create a fresh database, or restore the "
                    "correct migration ledger."
                )
        finally:
            probe.close()

    def close(self, *, status: str = "stopped", reason: str | None = None) -> None:
        """Flush, checkpoint, record the run's exit, then release the handle."""
        if self._con is None:
            return
        try:
            with contextlib.suppress(Exception):
                self.flush(force=True)
            with contextlib.suppress(Exception):
                self.con.execute(
                    "UPDATE collector_runs SET status = ?, stop_reason = ?, stopped_at_utc = ?, "
                    "stats_json = ? WHERE run_id = ?",
                    [
                        status,
                        reason,
                        datetime.now(tz=UTC),
                        json.dumps(self.stats, sort_keys=True),
                        self.run_id,
                    ],
                )
            with contextlib.suppress(Exception):
                self.con.execute("CHECKPOINT")
                self.stats["checkpoints"] += 1
        finally:
            con, self._con = self._con, None
            if con is not None:
                with contextlib.suppress(duckdb.Error):
                    con.close()

    # -- run bookkeeping ---------------------------------------------------

    def start_run(
        self,
        *,
        app_version: str,
        git_sha: str | None,
        image_tag: str | None,
        hostname: str,
        pid: int,
    ) -> None:
        self.con.execute(
            "INSERT OR REPLACE INTO collector_runs "
            "(run_id, started_at_utc, app_version, git_sha, image_tag, hostname, pid, "
            " status, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', '{}')",
            [
                self.run_id,
                datetime.now(tz=UTC),
                app_version,
                git_sha,
                image_tag,
                hostname,
                pid,
            ],
        )

    def record_gap(
        self,
        *,
        reason: str,
        started_at_utc: datetime,
        detail: str,
        ended_at_utc: datetime | None = None,
    ) -> str:
        gap_id = uuid.uuid4().hex
        self.log_event(
            "warning",
            "collection_gap",
            f"gap opened: {reason}",
            {"reason": reason, "detail": detail},
        )
        self.con.execute(
            "INSERT INTO collection_gaps "
            "(gap_id, run_id, reason, started_at_utc, detected_at_utc, ended_at_utc, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                gap_id,
                self.run_id,
                reason,
                started_at_utc,
                datetime.now(tz=UTC),
                ended_at_utc,
                detail,
            ],
        )
        return gap_id

    def close_open_gaps(self, *, detail: str) -> int:
        """Close gaps a previous unclean shutdown left open."""
        cur = self.con.execute(
            "UPDATE collection_gaps SET ended_at_utc = ?, detail = COALESCE(detail, ?) "
            "WHERE ended_at_utc IS NULL RETURNING gap_id",
            [datetime.now(tz=UTC), detail],
        )
        rows = cur.fetchall()
        if rows:
            log.info("closed %d gap(s) left open by a previous run", len(rows))
        return len(rows)

    def mark_abandoned_runs_failed(self) -> list[str]:
        """Fail every run still marked ``running`` - none of them is us.

        A run's row is closed by :meth:`close`. Anything still ``running`` was
        abandoned by a process that died without closing: SIGKILL, a
        container OOM, or a watchdog exit (the container can stay Up through
        all of those, which is exactly when a supervisor needs the database
        to say what the process could not). Auditing "which runs ended
        abnormally" must be a query, not an archaeology project - so the next
        start marks them, with a reason and a timestamp.

        Call this *before* :meth:`start_run` (or it would fail this run too).
        """
        reason = "run did not exit cleanly (crash, kill, or watchdog exit)"
        rows = self.con.execute(
            "UPDATE collector_runs SET status = 'failed', stop_reason = ?, "
            "stopped_at_utc = ? WHERE status = 'running' RETURNING run_id",
            [reason, datetime.now(tz=UTC)],
        ).fetchall()
        if rows:
            log.warning(
                "marked %d abandoned run(s) as failed: %s",
                len(rows),
                ", ".join(str(r[0])[:12] for r in rows),
            )
        return [str(r[0]) for r in rows]

    def log_event(
        self, level: str, kind: str, message: str, details: dict | None = None
    ) -> None:
        event_id = next_event_id(self.run_id)
        self._batch.collector_events.append(
            (
                int(event_id),
                self.run_id,
                datetime.now(tz=UTC),
                level,
                kind,
                message,
                json.dumps(details, sort_keys=True, default=str) if details else None,
            )
        )

    # -- staging -----------------------------------------------------------

    def stage_book(self, row: tuple[Any, ...]) -> None:
        self._batch.books.append(row)

    def stage_price(self, row: tuple[Any, ...]) -> None:
        self._batch.prices.append(row)

    def stage_event(self, row: tuple[Any, ...]) -> None:
        self._batch.events.append(row)

    def stage_market(self, row: tuple[Any, ...]) -> None:
        self._batch.markets.append(row)

    @property
    def pending_rows(self) -> int:
        return len(self._batch)

    def flush_if_due(self) -> bool:
        if not self._batch:
            return False
        due = (
            len(self._batch) >= self.flush_rows
            or (datetime.now(tz=UTC) - self._last_flush).total_seconds() >= self.flush_seconds
        )
        if not due:
            return False
        self.flush(force=True)
        return True

    def checkpoint_if_due(self, *, force: bool = False) -> bool:
        elapsed = (datetime.now(tz=UTC) - self._last_checkpoint).total_seconds()
        if not force and elapsed < self.checkpoint_seconds:
            return False
        self.checkpoint(force=True)
        return True

    # -- commit ------------------------------------------------------------

    def flush(self, *, force: bool = False) -> dict[str, int]:
        """Commit every staged row in one transaction.

        Returns per-table insert/dedup deltas so the caller can attribute
        throughput without re-reading the counters.
        """
        if not self._batch:
            self._last_flush = datetime.now(tz=UTC)
            return {}
        if not force and not self.flush_if_due():
            return {}

        before = dict(self.stats)
        con = self.con
        con.execute("BEGIN TRANSACTION")
        try:
            # Events must land before markets: markets has a foreign key to them.
            self._commit_events()
            self._commit_markets()
            self._commit_books()
            self._commit_prices()
            self._commit_collector_events()
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(duckdb.Error):
                con.execute("ROLLBACK")
            raise

        self._batch.clear()
        self._last_flush = datetime.now(tz=UTC)
        self.stats["flushes"] += 1
        return {key: self.stats[key] - before.get(key, 0) for key in self.stats if key != "flushes"}

    def _commit_events(self) -> None:
        rows = self._batch.events
        if not rows:
            return
        self.con.executemany(
            "INSERT INTO events (event_id, slug, title, target_date, resolution_source, "
            "rules_text, fetched_at_utc, raw, source, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (event_id) DO UPDATE SET "
            "  title = excluded.title, target_date = excluded.target_date, "
            "  resolution_source = excluded.resolution_source, rules_text = excluded.rules_text, "
            "  fetched_at_utc = excluded.fetched_at_utc, raw = excluded.raw, "
            "  source = excluded.source, run_id = excluded.run_id",
            rows,
        )
        self.stats["events_inserted"] += len(rows)

    def _commit_markets(self) -> None:
        rows = self._batch.markets
        if not rows:
            return
        columns = (
            "market_id, event_id, condition_id, question, bracket_label, bracket_kind, "
            "bracket_unit, bracket_low, bracket_high, bracket_low_inclusive, "
            "bracket_high_inclusive, bracket_rounding, token_yes, token_no, target_date, "
            "station, tick_size, min_order_size_shares, fee_rate, fee_exponent, "
            "resolution_source, rules_text, active, closed, accepting_orders, "
            "fetched_at_utc, raw, source, run_id"
        )
        placeholders = ", ".join("?" for _ in range(len(columns.split(","))))
        self.con.executemany(
            f"INSERT INTO markets ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT (market_id) DO UPDATE SET "
            "  condition_id = excluded.condition_id, question = excluded.question, "
            "  token_yes = excluded.token_yes, token_no = excluded.token_no, "
            "  tick_size = excluded.tick_size, min_order_size_shares = excluded.min_order_size_shares, "
            "  fee_rate = excluded.fee_rate, fee_exponent = excluded.fee_exponent, "
            "  active = excluded.active, closed = excluded.closed, "
            "  accepting_orders = excluded.accepting_orders, "
            "  fetched_at_utc = excluded.fetched_at_utc, raw = excluded.raw, "
            "  source = excluded.source, run_id = excluded.run_id",
            rows,
        )
        self.stats["markets_inserted"] += len(rows)

    def _commit_books(self) -> None:
        rows = self._batch.books
        if not rows:
            return
        book_ids = [r[0] for r in rows]
        existing = {
            r[0]
            for r in self.con.execute(
                "SELECT book_id FROM order_books WHERE book_id = ANY(?)", [book_ids]
            ).fetchall()
        }
        fresh = [r for r in rows if r[0] not in existing]
        if fresh:
            self.con.executemany(
                f"INSERT INTO order_books ({_BOOK_COLUMNS}) VALUES ({_BOOK_PLACEHOLDERS}) "
                "ON CONFLICT (book_id) DO NOTHING",
                fresh,
            )
        self.stats["books_inserted"] += len(fresh)
        self.stats["books_deduplicated"] += len(rows) - len(fresh)

    def _commit_prices(self) -> None:
        rows = self._batch.prices
        if not rows:
            return
        # Two-stage dedup:
        #   1. within the batch - a poll loop can stage the same instant twice,
        #      and both would otherwise collide on the primary key;
        #   2. against the table - a repeated observation of an instant already
        #      stored is idempotent.
        # The natural key is (market, token, ts, source); when the batch holds
        # several captures of one key, the earliest capture wins so the row
        # keeps the first time we saw it.
        self.con.execute(
            "CREATE OR REPLACE TEMP TABLE _pending_prices AS "
            "SELECT * FROM (VALUES "
            + ",".join(
                "(?::VARCHAR, ?::VARCHAR, ?::TIMESTAMPTZ, ?::DOUBLE, ?::VARCHAR, "
                "?::TIMESTAMPTZ, ?::VARCHAR, ?::VARCHAR)"
                for _ in rows
            )
            + ") AS t(market_id, token_id, ts, mid_usd_per_share, source, fetched_at_utc, "
              "venue_ts_origin, run_id)",
            [value for row in rows for value in row],
        )
        try:
            self.con.execute(
                "CREATE OR REPLACE TEMP TABLE _pending_prices_dedup AS "
                "SELECT market_id, token_id, ts, mid_usd_per_share, source, fetched_at_utc, "
                "venue_ts_origin, run_id FROM _pending_prices "
                "QUALIFY row_number() OVER ("
                "  PARTITION BY market_id, token_id, ts, source "
                "  ORDER BY fetched_at_utc) = 1"
            )
            inserted_rows = self.con.execute(
                "INSERT INTO price_history (market_id, token_id, ts, mid_usd_per_share, source, "
                "fetched_at_utc, venue_ts_origin, run_id) "
                "SELECT p.market_id, p.token_id, p.ts, p.mid_usd_per_share, p.source, "
                "p.fetched_at_utc, p.venue_ts_origin, p.run_id "
                "FROM _pending_prices_dedup p "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM price_history ph WHERE ph.market_id = p.market_id "
                "  AND ph.token_id = p.token_id AND ph.ts = p.ts AND ph.source = p.source) "
                "RETURNING market_id"
            ).fetchall()
        finally:
            self.con.execute("DROP TABLE IF EXISTS _pending_prices_dedup")
            self.con.execute("DROP TABLE IF EXISTS _pending_prices")

        inserted = len(inserted_rows)
        self.stats["prices_inserted"] += inserted
        # Everything not newly written - already stored, or a repeat inside
        # this same batch - was deduplicated. inserted + dedup == len(rows).
        self.stats["prices_deduplicated"] += len(rows) - inserted

    def _commit_collector_events(self) -> None:
        rows = self._batch.collector_events
        if not rows:
            return
        self.con.executemany(
            f"INSERT INTO collector_events ({_COLLECTOR_EVENT_COLUMNS}) "
            f"VALUES ({_COLLECTOR_EVENT_PLACEHOLDERS})",
            rows,
        )
        self.stats["collector_events_inserted"] += len(rows)

    def checkpoint(self, *, force: bool = False) -> bool:
        if not force and (datetime.now(tz=UTC) - self._last_checkpoint).total_seconds() < self.checkpoint_seconds:
            return False
        self.con.execute("CHECKPOINT")
        self._last_checkpoint = datetime.now(tz=UTC)
        self.stats["checkpoints"] += 1
        return True

    # -- integrity ---------------------------------------------------------

    def integrity_check(self) -> IntegrityReport:
        """Structural checks that must hold for collected data to be usable."""
        problems: list[str] = []
        counts: dict[str, int] = {}

        tables = [
            r[0]
            for r in self.con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        for name in tables:
            counts[name] = int(self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])

        def one(sql: str) -> int:
            return int(self.con.execute(sql).fetchone()[0])

        orphans = one(
            "SELECT COUNT(*) FROM markets m LEFT JOIN events e ON m.event_id = e.event_id "
            "WHERE e.event_id IS NULL"
        )
        if orphans:
            problems.append(f"{orphans} market(s) reference a missing event")

        orphans = one(
            "SELECT COUNT(*) FROM settlements s LEFT JOIN markets m ON s.market_id = m.market_id "
            "WHERE m.market_id IS NULL"
        )
        if orphans:
            problems.append(f"{orphans} settlement(s) reference a missing market")

        null_hash = one("SELECT COUNT(*) FROM order_books WHERE content_hash IS NULL")
        if null_hash:
            problems.append(f"{null_hash} order_book row(s) have no content_hash")

        null_origin = one("SELECT COUNT(*) FROM order_books WHERE venue_ts_origin IS NULL")
        if null_origin:
            problems.append(f"{null_origin} order_book row(s) have no venue_ts_origin")

        # A venue timestamp must never be a local-clock fabrication: origin must
        # agree with presence.
        mismatched = one(
            "SELECT COUNT(*) FROM order_books "
            "WHERE (venue_ts IS NULL) <> (venue_ts_origin = 'missing')"
        )
        if mismatched:
            problems.append(
                f"{mismatched} order_book row(s) disagree about whether venue_ts is present "
                "vs its venue_ts_origin - a timestamp provenance bug"
            )

        bad_price = one(
            "SELECT COUNT(*) FROM price_history WHERE mid_usd_per_share < 0 "
            "OR mid_usd_per_share > 1"
        )
        if bad_price:
            problems.append(f"{bad_price} price_history row(s) outside [0, 1]")

        return IntegrityReport(ok=not problems, row_counts=counts, problems=tuple(problems))

    # -- disk --------------------------------------------------------------

    def disk_usage(self) -> dict[str, float | int]:
        target = self.db_path if self.db_path.parent.exists() else Path(".")
        usage = shutil.disk_usage(target)
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        wal = self.db_path.with_suffix(self.db_path.suffix + ".wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        free_percent = (usage.free / usage.total * 100.0) if usage.total else 100.0
        return {
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "disk_free_percent": free_percent,
        }

    def raw_tree_bytes(self, raw_root: str | Path) -> int:
        root = Path(raw_root)
        if not root.exists():
            return 0
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    # -- backup ------------------------------------------------------------

    def backup(self, dest_dir: str | Path) -> BackupReport:
        """Create a verified, consistent copy of the database.

        The writer performs the copy itself, inside one transaction, so the
        snapshot is taken at a single MVCC point while writes are quiesced.
        The result is re-opened read-only and its row counts compared before
        the file is published under its final name - a backup that fails
        verification never replaces a good one.
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        # The uuid is part of the *final* name, not just the temp name: two
        # backups started in the same second would otherwise publish to the same
        # path, and the second would silently replace a good copy while the
        # caller believed both existed.
        suffix = f"{stamp}-{uuid.uuid4().hex[:8]}"
        tmp = dest / f".incomplete-{suffix}.duckdb"
        final = dest / f"weatherquant-{suffix}.duckdb"

        try:
            self.flush(force=True)
            self.checkpoint(force=True)

            expected = {
                name: int(self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                for name in self._backup_tables()
            }
            versions = tuple(
                sorted(v for (v,) in self.con.execute("SELECT version FROM schema_migrations").fetchall())
            )

            alias = "wq_backup"
            # `ATTACH DATABASE '<path>' AS alias` - DuckDB 1.5 rejects the
            # `(FILE)` option form, and a plain `ATTACH 'path'` is accepted but
            # resolves differently when the file already exists.
            self.con.execute(
                f"ATTACH DATABASE '{escape_duckdb_string(str(tmp))}' AS {alias}"
            )
            try:
                self.con.execute("BEGIN TRANSACTION")
                for name in self._backup_tables():
                    self.con.execute(f'CREATE TABLE {alias}."{name}" AS SELECT * FROM "{name}"')
                for stmt in self._index_statements(alias):
                    self.con.execute(stmt)
                for stmt in self._sequence_statements(alias):
                    self.con.execute(stmt)
                self.con.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(duckdb.Error):
                    self.con.execute("ROLLBACK")
                raise
            finally:
                with contextlib.suppress(duckdb.Error):
                    self.con.execute(f"DETACH {alias}")

            # Verify by reading it back through an independent connection.
            report = self._verify_backup(tmp, expected, versions)
            if not report.ok:
                tmp.unlink(missing_ok=True)
                return BackupReport(
                    destination=final,
                    verified=False,
                    row_counts=report.row_counts,
                    expected_counts=expected,
                    schema_versions=versions,
                    error=report.error,
                )

            tmp.replace(final)
            return BackupReport(
                destination=final,
                verified=True,
                row_counts=report.row_counts,
                expected_counts=expected,
                schema_versions=versions,
                bytes_written=final.stat().st_size,
            )
        except Exception as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            log.error("backup failed: %s", exc)
            return BackupReport(
                destination=final,
                verified=False,
                row_counts={},
                expected_counts={},
                schema_versions=(),
                error=str(exc),
            )

    def _verify_backup(
        self, path: Path, expected: dict[str, int], versions: tuple[str, ...]
    ) -> BackupReport:
        actual: dict[str, int] = {}
        try:
            probe = connect(path, read_only=True)
        except Exception as exc:
            return BackupReport(path, False, {}, expected, versions, error=f"cannot open backup: {exc}")
        try:
            for name in expected:
                row = probe.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
                actual[name] = int(row[0]) if row else -1
            got_versions = tuple(
                sorted(
                    v
                    for (v,) in probe.execute("SELECT version FROM schema_migrations").fetchall()
                )
            )
            got_sequences = {
                r[0] for r in probe.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()
            }
            expected_sequences = {
                r[0] for r in self.con.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()
            }
        except Exception as exc:
            return BackupReport(path, False, {}, expected, versions, error=f"backup read-back failed: {exc}")
        finally:
            probe.close()

        mismatches = {k: (expected[k], actual.get(k, -1)) for k in expected if expected[k] != actual.get(k)}
        if mismatches:
            return BackupReport(
                path,
                False,
                actual,
                expected,
                versions,
                error=f"row count mismatch (expected, actual): {mismatches}",
            )
        if got_versions != versions:
            return BackupReport(
                path, False, actual, expected, versions,
                error=f"schema versions differ: {versions} vs {got_versions}",
            )
        if got_sequences != expected_sequences:
            return BackupReport(
                path, False, actual, expected, versions,
                error=(
                    "sequence set differs: "
                    f"expected {sorted(expected_sequences)}, got {sorted(got_sequences)}"
                ),
            )
        return BackupReport(path, True, actual, expected, versions)

    def _user_tables(self) -> list[str]:
        return [
            r[0]
            for r in self.con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name != 'schema_migrations' "
                "AND table_type = 'BASE TABLE' ORDER BY table_name"
            ).fetchall()
        ]

    def _backup_tables(self) -> list[str]:
        """Every table a backup must contain, including the migration ledger.

        The ledger is excluded from parquet export (it is bookkeeping, not
        research data) but must be copied into a backup: without it a restored
        database would look unmigrated and the migrator would try to rebuild it.
        """
        return [*self._user_tables(), "schema_migrations"]

    def _sequence_statements(self, alias: str) -> list[str]:
        """Recreate sequences on the backup.

        Sequences are not tables, so ``CREATE TABLE ... AS SELECT`` never
        touches them - and a backup missing ``collector_events_seq`` would fail
        the moment the restored collector logged its first event.
        """
        statements: list[str] = []
        pattern = re.compile(r"CREATE\s+SEQUENCE\s+(?P<name>\w+)(?P<rest>.*)$", re.IGNORECASE)
        for clause in iter_ddl_statements():
            if not clause.upper().startswith("CREATE SEQUENCE"):
                continue
            match = pattern.search(clause)
            if not match:
                log.warning("could not parse sequence DDL from migration: %s", clause)
                continue
            statements.append(
                f"CREATE SEQUENCE IF NOT EXISTS {alias}.{match.group('name')}{match.group('rest')}"
            )
        return statements

    def _index_statements(self, alias: str) -> list[str]:
        """Recreate, on the backup, every index declared in our migrations.

        Index names are qualified against the backup's table rather than
        rewritten by string substitution: an unqualified ``ON markets`` would
        create the index on the *source* database, where it already exists, and
        the backup would silently end up unindexed.
        """
        statements: list[str] = []
        pattern = re.compile(
            r"CREATE\s+INDEX\s+(?P<name>\w+)\s+ON\s+(?P<table>\w+)\s*\((?P<cols>.*?)\)\s*$",
            re.IGNORECASE | re.DOTALL,
        )
        for clause in iter_ddl_statements():
            if not clause.upper().startswith("CREATE INDEX"):
                continue
            match = pattern.search(clause)
            if not match:
                log.warning("could not parse index DDL from migration: %s", clause)
                continue
            statements.append(
                f'CREATE INDEX {match.group("name")} ON {alias}."{match.group("table")}" '
                f"({match.group('cols')})"
            )
        return statements

    # -- parquet -----------------------------------------------------------

    def export_parquet_day(self, day: date, out_root: str | Path) -> ParquetExportReport:
        """Write one UTC day of selected tables to partitioned Parquet files.

        Layout: ``<out_root>/<table>/dt=YYYY-MM-DD.parquet``. Partitioning by
        day keeps every file bounded and lets a restore, an audit, or a
        downstream engine operate on one day at a time instead of the whole
        database.
        """
        root = Path(out_root)
        written: dict[str, Path] = {}
        rows: dict[str, int] = {}
        errors: list[str] = []

        for table, column in _PARQUET_PARTITION_COLUMN.items():
            if table not in self._user_tables():
                continue
            target_dir = root / table / f"dt={day.isoformat()}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / "data.parquet"
            try:
                if column == "target_date":
                    predicate = f'"{column}" = DATE \'{day.isoformat()}\''
                else:
                    predicate = f'"{column}" >= TIMESTAMP \'{day.isoformat()} 00:00:00\' ' \
                                f'AND "{column}" < TIMESTAMP \'{(day + timedelta(days=1)).isoformat()} 00:00:00\''
                cur = self.con.execute(
                    f"COPY (SELECT * FROM \"{table}\" WHERE {predicate}) "
                    f"TO '{escape_duckdb_string(str(target))}' (FORMAT PARQUET)"
                )
                cur.fetchall()
                if target.exists() and target.stat().st_size > 0:
                    count = self.con.execute(
                        f"SELECT COUNT(*) FROM read_parquet('{escape_duckdb_string(str(target))}')"
                    ).fetchone()[0]
                    if int(count) == 0:
                        target.unlink(missing_ok=True)
                        continue
                    written[table] = target
                    rows[table] = int(count)
                else:
                    target.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(f"{table}: {exc}")
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)

        return ParquetExportReport(day=day, tables=written, rows=rows, errors=tuple(errors))

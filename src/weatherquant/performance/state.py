"""The research state store for the streaming performance engine.

Why a store at all
------------------
The whole-history builder held every decision as a Python dict (about 6.8 KB a
row) and died at 960,000 rows on a 16 GB host. Nothing about the *statistics*
needed that: what actually has to survive between batches is the open-episode
set, a handful of counters, and a cursor. Everything else is either derivable
from the export or recomputable from what is stored here.

Why DuckDB, in its own file
---------------------------
* It is already a dependency, and it gives ACID appends - which is what makes
  "the watermark advances only after the analysis is committed" a property of
  the storage engine rather than a promise in a comment.
* ``memory_limit`` plus ``temp_directory`` bound the process explicitly and
  spill to disk instead of asking the host for more RAM. A container memory cap
  protects the container; this is what protects the host.
* It answers the questions a fold cannot answer locally (exact order
  statistics, distinct counts) by query over columns, without the history ever
  entering Python.
* SQLite was the alternative. It has the transactions but not the analytical
  spill behaviour, and no way to fetch a percentile's *neighbouring order
  statistics*, which is what this engine needs to stay bit-for-bit equal to the
  in-memory implementation.

Why not the existing research database: it is written by the legacy importer
and the forecast backfill. Sharing a file would mean sharing a write lock with
unrelated jobs, and it would make "delete the performance state and rebuild it"
a decision about someone else's data. This file is derived and disposable.

The invariants this module enforces
-----------------------------------
1. **The watermark never advances ahead of the analysis.** Rows, episode state,
   counters and the cursor are written inside one transaction; a crash rolls
   back all of them, so the next run re-reads the same part and cannot
   double-count.
2. **Parts are immutable once consumed.** Each consumed part records its row
   count, its end cursor and a SHA-256. A part that changes underneath the
   store is refused rather than silently re-read.
3. **A gap is refused, not skipped.** Part indices must be contiguous from 1.
4. **The folded parameters are pinned.** ``algorithm_version``, the gap window
   and the accepted state are stored with the state; changing any of them
   without a rebuild is refused, because the stored aggregates would then be a
   mixture of two algorithms.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from weatherquant.performance.episodes import (
    Episode,
    EpisodeKey,
    FolderState,
    OpenEpisode,
    _episode_id,
)
from weatherquant.storage.db import connect
from weatherquant.storage.migrator import MigrationError, migrate

__all__ = [
    "DEFAULT_PERFORMANCE_STATE_DB_PATH",
    "PERFORMANCE_ALGORITHM_VERSION",
    "IngestState",
    "PartRecord",
    "PerformanceStateError",
    "PerformanceStateStore",
    "StateMismatch",
    "StoredEpisode",
    "episode_key_hash",
    "settlement_key_hash",
]

#: Bumped whenever the *fold* changes in a way that invalidates stored
#: aggregates. The store refuses to be extended across a change: the numbers
#: would be a mixture of two algorithms, which is worse than a rebuild.
PERFORMANCE_ALGORITHM_VERSION = "performance-streaming-1"

PERFORMANCE_MIGRATIONS_DIR = Path(__file__).parent.parent / "storage" / "migrations_performance"

DEFAULT_PERFORMANCE_STATE_DB_PATH = Path("data/research/performance-state.duckdb")

#: Enough for the fold, the batch buffers and DuckDB's operators. Deliberately
#: far below the host's RAM: the failure this replaces was a build that took the
#: whole NAS down while never exceeding its own container cap.
DEFAULT_MEMORY_LIMIT = "1GB"


class PerformanceStateError(RuntimeError):
    """The store cannot be used as asked."""


class StateMismatch(PerformanceStateError):
    """The stored state was produced by a different input or algorithm."""


def episode_key_hash(key: EpisodeKey) -> str:
    """Stable identity for the five-dimensional key, NULL-safe.

    DuckDB primary keys may not be NULL and any of ``target_date``,
    ``model_name``, ``direction`` and ``mode`` can legitimately be absent, so
    identity is hashed. ``_normalise`` collapses None and "" to None, so an
    empty component cannot collide with a real value here.
    """
    parts = (
        key.market_id,
        key.target_date,
        key.model_name,
        key.direction,
        key.mode,
    )
    basis = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def settlement_key_hash(market_id: str, target_date: str | None) -> str:
    basis = f"{market_id}\x1f{'' if target_date is None else target_date}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PartRecord:
    """One consumed export part, as proof that it was consumed."""

    index: int
    rows: int
    end_cursor_time: datetime
    end_cursor_id: str
    sha256: str


@dataclass(slots=True)
class StoredEpisode:
    """One episode as the store holds it, plus how it should be rendered.

    ``is_open`` is a property of the *state*, not of the publication: an open
    episode is published with ``end_reason = end_of_data`` because the stream
    has merely stopped, and it stays open in the store so the next run can
    extend it. Keeping the two apart is what stops a publish from looking like
    an ending.
    """

    open_ep: OpenEpisode
    is_open: bool
    end_reason: str | None


@dataclass(slots=True)
class IngestState:
    """The singleton row: what has been consumed, and under what rules."""

    algorithm_version: str
    input_source: str
    gap_window_seconds: float
    accepted_state: str
    last_part_index: int = 0
    last_end_cursor_time: datetime | None = None
    last_end_cursor_id: str | None = None
    rows_consumed: int = 0
    row_seq: int = 0
    last_instant: datetime | None = None
    prev_instant: datetime | None = None
    decisions_seen: int = 0
    decisions_with_market: int = 0
    accepts_seen: int = 0
    not_accept: int = 0
    accepts_without_edge: int = 0
    accepts_without_edge_market: int = 0
    first_decision_utc: datetime | None = None
    last_decision_utc: datetime | None = None

    @property
    def last_end_cursor(self) -> tuple[str, str] | None:
        if self.last_end_cursor_time is None or self.last_end_cursor_id is None:
            return None
        return (self.last_end_cursor_time.isoformat(), self.last_end_cursor_id)

    @property
    def last_end_cursor_parsed(self) -> tuple[datetime, str] | None:
        """The cursor as a comparable value, for the in-order check."""
        if self.last_end_cursor_time is None:
            return None
        return (self.last_end_cursor_time, self.last_end_cursor_id or "")

    def folder_state(self) -> FolderState:
        return FolderState(
            row_seq=self.row_seq, last_instant=self.last_instant, prev_instant=self.prev_instant
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "input_source": self.input_source,
            "gap_window_seconds": self.gap_window_seconds,
            "accepted_state": self.accepted_state,
            "last_part_index": self.last_part_index,
            "last_end_cursor": list(self.last_end_cursor) if self.last_end_cursor else None,
            "rows_consumed": self.rows_consumed,
            "row_seq": self.row_seq,
            "decisions_seen": self.decisions_seen,
            "decisions_with_market": self.decisions_with_market,
            "accepts_seen": self.accepts_seen,
            "not_accept": self.not_accept,
            "accepts_without_edge": self.accepts_without_edge,
            "accepts_without_edge_market": self.accepts_without_edge_market,
            "first_decision_utc": _iso(self.first_decision_utc),
            "last_decision_utc": _iso(self.last_decision_utc),
        }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


#: Column types the bulk writer understands. Declared rather than inferred:
#: a nullable integer column that pandas infers as float would be cast, and
#: ``NaN`` is a number where the schema says "absent".
_COLUMN_KINDS = {
    "text": "object",
    "int": "Int64",
    "float": "Float64",
    "bool": "boolean",
    "ts": "datetime64[ns, UTC]",
}


def _frame(columns: Sequence[str], specs: Sequence[str], rows: Sequence[Sequence[Any]]) -> Any:
    """Build a typed DataFrame from row tuples, for one bulk write."""
    import pandas as pd

    if len(columns) != len(specs):  # pragma: no cover - a caller bug
        raise PerformanceStateError("column names and column types disagree")
    transposed = list(zip(*rows, strict=True)) if rows else [() for _ in columns]
    data: dict[str, Any] = {}
    for name, kind, values in zip(columns, specs, transposed, strict=True):
        dtype = _COLUMN_KINDS[kind]
        if kind == "ts":
            data[name] = pd.to_datetime(list(values), utc=True)
        elif kind == "text":
            data[name] = pd.Series(list(values), dtype="object")
        else:
            data[name] = pd.array(list(values), dtype=dtype)
    return pd.DataFrame(data)


_EPISODE_SPECS = (
    "text", "text", "text", "text", "text", "text", "text", "ts", "ts",
    "int", "int", "float", "float", "float", "float", "float",
    "float", "float", "float", "text", "text", "text", "float", "bool", "text",
    "int", "int", "ts",
)

_EPISODE_COLUMNS = (
    "episode_id",
    "key_hash",
    "market_id",
    "target_date",
    "model_name",
    "direction",
    "mode",
    "first_seen_utc",
    "last_seen_utc",
    "observation_count",
    "edge_count",
    "sum_edges",
    "first_edge",
    "last_edge",
    "max_edge",
    "min_edge",
    "first_market_price",
    "first_model_probability",
    "first_available_depth_usd",
    "station",
    "bracket_label",
    "market_question",
    "fee_rate",
    "is_open",
    "end_reason",
    "opened_seq",
    "closed_seq",
    "ingested_at_utc",
)


_EDGE_COLUMNS = ("seq", "target_date", "model_name", "direction", "mode", "edge")
_EDGE_SPECS = ("int", "text", "text", "text", "text", "float")

_SETTLEMENT_COLUMNS = (
    "key_hash",
    "market_id",
    "target_date",
    "status",
    "payout",
    "verified",
    "verification_status",
    "resolution_source",
    "fetched_at_utc",
    "updated_at_utc",
)
_SETTLEMENT_SPECS = (
    "text", "text", "text", "text", "float", "bool", "text", "text", "ts", "ts",
)

_PART_COLUMNS = ("part_index", "rows", "end_cursor_time", "end_cursor_id", "sha256", "consumed_at_utc")
_PART_SPECS = ("int", "int", "ts", "text", "text", "ts")


class PerformanceStateStore:
    """Owns the research state file. One writer, one process, at a time."""

    def __init__(
        self,
        path: str | Path = DEFAULT_PERFORMANCE_STATE_DB_PATH,
        *,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        temp_directory: str | Path | None = None,
        read_only: bool = False,
    ) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.memory_limit = memory_limit
        self.temp_directory = Path(temp_directory) if temp_directory else None
        self.read_only = read_only
        self._con: duckdb.DuckDBPyConnection | None = None
        self._in_transaction = False

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> list[str]:
        """Open the file, apply migrations, and pin the resource limits.

        A second process holding the file is reported as a busy store rather
        than a traceback, because "the nightly job is still running" and "the
        state file is corrupt" are different problems.
        """
        try:
            con = connect(self.path, read_only=self.read_only)
        except duckdb.Error as exc:  # pragma: no cover - depends on host state
            raise PerformanceStateError(
                f"could not open the performance state store at {self.path}: {exc}. "
                "Another process may be writing it; the store is single-writer."
            ) from exc
        self._con = con
        if self.read_only:
            return []
        # The limits are the point: a research build must not be able to ask the
        # host for memory it does not have. Spilling is the intended behaviour.
        con.execute(f"SET memory_limit='{self.memory_limit}'")
        spill = self.temp_directory
        if spill is None and str(self.path) != ":memory:":
            # Spill next to the state file rather than into the system temp
            # directory: same volume as the data, and never a tmpfs that the
            # spill itself could fill.
            spill = self.path.parent / "duckdb-tmp"
        if spill is not None:
            Path(spill).mkdir(parents=True, exist_ok=True)
            con.execute(f"SET temp_directory='{spill}'")
        try:
            return migrate(con, PERFORMANCE_MIGRATIONS_DIR)
        except MigrationError as exc:
            raise PerformanceStateError(f"performance state migrations failed: {exc}") from exc

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> PerformanceStateStore:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            raise PerformanceStateError("the performance state store is not open")
        return self._con

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> tuple | None:
        return self.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
        return self.execute(sql, params).fetchall()

    def cursor(self, sql: str, params: Sequence[Any] | None = None):
        return self.execute(sql, params)

    # -- transactions ------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing: rows, episode state, counters and cursor together.

        This is the mechanism behind "the watermark advances only after the
        analysis is committed". A failure anywhere inside leaves the store
        exactly as it was, so the part that was being read is read again next
        time and nothing is counted twice.
        """
        if self._in_transaction:
            raise PerformanceStateError("nested transactions are not supported here")
        self.con.execute("BEGIN TRANSACTION")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            with contextlib.suppress(duckdb.Error):
                self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")
        finally:
            self._in_transaction = False

    # -- ingest state ------------------------------------------------------

    def load_state(self) -> IngestState | None:
        row = self.fetchone(
            "SELECT algorithm_version, input_source, fold_gap_window_seconds, "
            "fold_accepted_state, last_part_index, last_end_cursor_time, "
            "last_end_cursor_id, rows_consumed, row_seq, last_instant, prev_instant, "
            "decisions_seen, decisions_with_market, accepts_seen, not_accept, "
            "accepts_without_edge, accepts_without_edge_market, first_decision_utc, "
            "last_decision_utc FROM performance_ingest WHERE id = 1"
        )
        if row is None:
            return None
        return IngestState(
            algorithm_version=row[0],
            input_source=row[1],
            gap_window_seconds=float(row[2]),
            accepted_state=row[3],
            last_part_index=int(row[4]),
            last_end_cursor_time=row[5],
            last_end_cursor_id=row[6],
            rows_consumed=int(row[7]),
            row_seq=int(row[8]),
            last_instant=row[9],
            prev_instant=row[10],
            decisions_seen=int(row[11]),
            decisions_with_market=int(row[12]),
            accepts_seen=int(row[13]),
            not_accept=int(row[14]),
            accepts_without_edge=int(row[15]),
            accepts_without_edge_market=int(row[16]),
            first_decision_utc=row[17],
            last_decision_utc=row[18],
        )

    def initialize_state(self, state: IngestState) -> None:
        """Write the singleton row for a fresh store."""
        self.write_state(state)

    def write_state(self, state: IngestState) -> None:
        columns = (
            "id, algorithm_version, input_source, fold_gap_window_seconds, "
            "fold_accepted_state, last_part_index, last_end_cursor_time, "
            "last_end_cursor_id, rows_consumed, row_seq, last_instant, prev_instant, "
            "decisions_seen, decisions_with_market, accepts_seen, not_accept, "
            "accepts_without_edge, accepts_without_edge_market, first_decision_utc, "
            "last_decision_utc, updated_at_utc"
        )
        self.con.execute(
            f"INSERT OR REPLACE INTO performance_ingest ({columns}) VALUES "
            "(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                state.algorithm_version,
                state.input_source,
                state.gap_window_seconds,
                state.accepted_state,
                state.last_part_index,
                state.last_end_cursor_time,
                state.last_end_cursor_id,
                state.rows_consumed,
                state.row_seq,
                state.last_instant,
                state.prev_instant,
                state.decisions_seen,
                state.decisions_with_market,
                state.accepts_seen,
                state.not_accept,
                state.accepts_without_edge,
                state.accepts_without_edge_market,
                state.first_decision_utc,
                state.last_decision_utc,
                datetime.now(tz=UTC),
            ],
        )

    # -- parts -------------------------------------------------------------

    def recorded_parts(self) -> dict[int, PartRecord]:
        rows = self.fetchall(
            "SELECT part_index, rows, end_cursor_time, end_cursor_id, sha256 "
            "FROM performance_parts ORDER BY part_index"
        )
        return {
            int(r[0]): PartRecord(
                index=int(r[0]),
                rows=int(r[1]),
                end_cursor_time=r[2],
                end_cursor_id=str(r[3]),
                sha256=str(r[4]),
            )
            for r in rows
        }

    def record_part(self, part: PartRecord) -> None:
        self._insert_rows(
            "performance_parts",
            _PART_COLUMNS,
            _PART_SPECS,
            [[
                part.index,
                part.rows,
                part.end_cursor_time,
                part.end_cursor_id,
                part.sha256,
                datetime.now(tz=UTC),
            ]],
            replace=True,
        )

    # -- episodes ----------------------------------------------------------

    def _insert_rows(
        self,
        table: str,
        columns: Sequence[str],
        specs: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        replace: bool = False,
    ) -> None:
        """Write a batch through a DataFrame, not through ``executemany``.

        Measured on DuckDB 1.5.6, ``executemany`` costs about 9 ms per row
        against a table with a primary key - 6000 episode rows took 60 seconds
        and dominated the whole ingest. Handing the same rows over as one
        DataFrame costs about 12 microseconds per row, and it still runs inside
        the caller's transaction (verified by rolling one back). The types are
        declared rather than inferred, because a nullable integer column
        inferred as float would be silently cast, and ``NaN`` is not ``NULL``.
        """
        if not rows:
            return
        frame = _frame(columns, specs, rows)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        staging = f"_wq_stage_{table}"
        column_list = ", ".join(columns)
        self.con.register(staging, frame)
        try:
            self.con.execute(
                f"{verb} INTO {table} ({column_list}) SELECT {column_list} FROM {staging}"
            )
        finally:
            self.con.unregister(staging)

    def load_open_episodes(self) -> list[OpenEpisode]:
        rows = self.fetchall(
            "SELECT market_id, target_date, model_name, direction, mode, first_seen_utc, "
            "last_seen_utc, observation_count, edge_count, sum_edges, first_edge, last_edge, "
            "max_edge, min_edge, first_market_price, first_model_probability, "
            "first_available_depth_usd, station, bracket_label, market_question, fee_rate, "
            "opened_seq FROM performance_episodes WHERE is_open ORDER BY opened_seq"
        )
        return [
            OpenEpisode(
                key=EpisodeKey(
                    market_id=str(r[0]),
                    target_date=r[1],
                    model_name=r[2],
                    direction=r[3],
                    mode=r[4],
                ),
                first_seen=r[5],
                last_seen=r[6],
                count=int(r[7]),
                edge_count=int(r[8]),
                sum_edges=float(r[9]),
                first_edge=float(r[10]),
                last_edge=float(r[11]),
                max_edge=float(r[12]),
                min_edge=float(r[13]),
                opened_seq=int(r[21]),
                first_market_price=r[14],
                first_model_probability=r[15],
                first_available_depth_usd=r[16],
                station=r[17],
                bracket_label=r[18],
                market_question=r[19],
                fee_rate=r[20],
            )
            for r in rows
        ]

    def upsert_open_episodes(self, episodes: Iterable[OpenEpisode]) -> None:
        """Persist the checkpoint: the episodes that are still being observed."""
        now = datetime.now(tz=UTC)
        rows = [
            [
                _episode_id(ep.key, ep.first_seen),
                episode_key_hash(ep.key),
                ep.key.market_id,
                ep.key.target_date,
                ep.key.model_name,
                ep.key.direction,
                ep.key.mode,
                ep.first_seen,
                ep.last_seen,
                ep.count,
                ep.edge_count,
                ep.sum_edges,
                ep.first_edge,
                ep.last_edge,
                ep.max_edge,
                ep.min_edge,
                ep.first_market_price,
                ep.first_model_probability,
                ep.first_available_depth_usd,
                ep.station,
                ep.bracket_label,
                ep.market_question,
                ep.fee_rate,
                True,
                None,
                ep.opened_seq,
                None,
                now,
            ]
            for ep in episodes
        ]
        self._write_episode_rows(rows)

    def upsert_closed_episode(self, episode: Episode, open_ep: OpenEpisode, closed_seq: int) -> None:
        """Persist a finished episode, replacing any open copy of the same key.

        The running sum is carried over from the open record rather than
        recomputed from ``mean_edge * observation_count``, which is a different
        float. Nothing reads it after closure, but a stored number that is
        quietly not the one that was computed is exactly the kind of drift this
        store exists to avoid.
        """
        self._write_episode_rows([self._closed_row(episode, open_ep, closed_seq)])

    def upsert_closed_episodes(
        self, records: Iterable[tuple[Episode, OpenEpisode, int]]
    ) -> None:
        self._write_episode_rows(
            [self._closed_row(episode, open_ep, seq) for episode, open_ep, seq in records]
        )

    @staticmethod
    def _closed_row(episode: Episode, open_ep: OpenEpisode, closed_seq: int) -> list[Any]:
        return [
            episode.episode_id,
            episode_key_hash(episode.key),
            episode.key.market_id,
            episode.key.target_date,
            episode.key.model_name,
            episode.key.direction,
            episode.key.mode,
            episode.first_seen_utc,
            episode.last_seen_utc,
            episode.observation_count,
            open_ep.edge_count,
            open_ep.sum_edges,
            episode.first_edge,
            episode.last_edge,
            episode.max_edge,
            episode.min_edge,
            episode.first_market_price,
            episode.first_model_probability,
            episode.first_available_depth_usd,
            episode.station,
            episode.bracket_label,
            episode.market_question,
            episode.fee_rate,
            False,
            episode.end_reason,
            open_ep.opened_seq,
            closed_seq,
            datetime.now(tz=UTC),
        ]

    def _write_episode_rows(self, rows: list[list[Any]]) -> None:
        self._insert_rows(
            "performance_episodes", _EPISODE_COLUMNS, _EPISODE_SPECS, rows, replace=True
        )

    def iter_episodes(self) -> Iterator[StoredEpisode]:
        """Walk every episode in the order the whole-history builder published.

        That order is not arbitrary. The original appended episodes as they
        closed and then sorted stably by ``first_seen_utc``, so episodes that
        share a first-seen instant keep the order they were produced - and
        ``unique_opportunity_edge_sum`` is a float left fold over exactly that
        sequence. The three ordering keys reproduce it: first-seen, then
        "closed during the pass" before "still open at the end", then the
        sequence number the fold assigned.
        """
        cursor = self.cursor(
            "SELECT market_id, target_date, model_name, direction, mode, first_seen_utc, "
            "last_seen_utc, observation_count, edge_count, sum_edges, first_edge, last_edge, "
            "max_edge, min_edge, first_market_price, first_model_probability, "
            "first_available_depth_usd, station, bracket_label, market_question, fee_rate, "
            "opened_seq, is_open, end_reason FROM performance_episodes "
            "ORDER BY first_seen_utc, is_open, COALESCE(closed_seq, opened_seq)"
        )
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                return
            for r in rows:
                yield StoredEpisode(
                    open_ep=OpenEpisode(
                        key=EpisodeKey(
                            market_id=str(r[0]),
                            target_date=r[1],
                            model_name=r[2],
                            direction=r[3],
                            mode=r[4],
                        ),
                        first_seen=r[5],
                        last_seen=r[6],
                        count=int(r[7]),
                        edge_count=int(r[8]),
                        sum_edges=float(r[9]),
                        first_edge=float(r[10]),
                        last_edge=float(r[11]),
                        max_edge=float(r[12]),
                        min_edge=float(r[13]),
                        opened_seq=int(r[21]),
                        first_market_price=r[14],
                        first_model_probability=r[15],
                        first_available_depth_usd=r[16],
                        station=r[17],
                        bracket_label=r[18],
                        market_question=r[19],
                        fee_rate=r[20],
                    ),
                    is_open=bool(r[22]),
                    end_reason=r[23],
                )

    def count_episodes(self) -> int:
        return int(self.fetchone("SELECT count(*) FROM performance_episodes")[0])

    # -- edge observations -------------------------------------------------

    def insert_edge_observations(self, rows: Iterable[Sequence[Any]]) -> None:
        self._insert_rows(
            "performance_edge_observations",
            _EDGE_COLUMNS,
            _EDGE_SPECS,
            [list(row) for row in rows],
        )

    # -- distinct sets -----------------------------------------------------

    def upsert_dimensions(
        self,
        *,
        markets: Iterable[str],
        target_dates: Iterable[str],
        models: Iterable[str],
    ) -> None:
        for table, column, values in (
            ("performance_markets_seen", "market_id", markets),
            ("performance_target_dates_seen", "target_date", target_dates),
            ("performance_models_seen", "model_name", models),
        ):
            payload = [[value] for value in values]
            if payload:
                self._insert_rows(table, [column], ["text"], payload, replace=True)

    def distinct_counts(self) -> dict[str, int]:
        return {
            "markets": int(self.fetchone("SELECT count(*) FROM performance_markets_seen")[0]),
            "target_dates": int(
                self.fetchone("SELECT count(*) FROM performance_target_dates_seen")[0]
            ),
            "models": int(self.fetchone("SELECT count(*) FROM performance_models_seen")[0]),
        }

    def distinct_values(self, table: str) -> list[str]:
        if table not in {
            "performance_markets_seen",
            "performance_target_dates_seen",
            "performance_models_seen",
        }:
            raise PerformanceStateError(f"not a dimension table: {table}")
        return [str(r[0]) for r in self.fetchall(f"SELECT * FROM {table}")]

    # -- settlements -------------------------------------------------------

    def upsert_settlements(
        self,
        answers: Mapping[
            tuple[str, str | None],
            tuple[str, float | None, bool, str | None, str | None, datetime | None],
        ],
    ) -> None:
        """``(market, date) -> (status, payout, verified, verdict, source, fetched_at)``.

        The verdict and the source are stored verbatim rather than folded into
        the boolean: "verified" is a conclusion, and a conclusion whose
        evidence has been discarded cannot be re-examined when the rules
        change.
        """
        if not answers:
            return
        now = datetime.now(tz=UTC)
        rows = [
            [
                settlement_key_hash(market_id, target_date),
                market_id,
                target_date,
                status,
                payout,
                verified,
                verification_status,
                resolution_source,
                # NULL, not "now". The row records when it was *stored*
                # (``updated_at_utc``) and, separately, when the answer was
                # *retrieved*. Filling the second with the first would make an
                # answer of unknown age indistinguishable from a fresh one -
                # exactly the failure the sticky settlement layer has to avoid.
                fetched_at,
                now,
            ]
            for (market_id, target_date), (
                status,
                payout,
                verified,
                verification_status,
                resolution_source,
                fetched_at,
            ) in answers.items()
        ]
        self._insert_rows(
            "performance_settlements", _SETTLEMENT_COLUMNS, _SETTLEMENT_SPECS, rows, replace=True
        )

    def replace_settlements(
        self,
        answers: Mapping[
            tuple[str, str | None],
            tuple[str, float | None, bool, str | None, str | None, datetime | None],
        ],
    ) -> int:
        """Make the store's settlement set exactly this one. Returns rows removed.

        The default upsert is a union, because an index that covers today's
        markets must not erase yesterday's. But a union cannot express
        *withdrawal*: a settlement that the platform retracted, or a market
        that should no longer be scored at all, would survive every later run.
        So retraction is an explicit act with its own flag, and it is the
        caller's assertion that the index it is handing over is complete.
        """
        keep = {settlement_key_hash(market_id, date) for market_id, date in answers}
        existing = {str(row[0]) for row in self.fetchall("SELECT key_hash FROM performance_settlements")}
        removed = sorted(existing - keep)
        if removed:
            self.con.execute(
                "DELETE FROM performance_settlements WHERE key_hash IN "
                f"({', '.join(['?'] * len(removed))})",
                removed,
            )
        self.upsert_settlements(answers)
        return len(removed)

    def load_settlements(self) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT market_id, target_date, status, payout, verified, verification_status, "
            "resolution_source, fetched_at_utc FROM performance_settlements "
            "ORDER BY market_id, target_date"
        )
        return [
            {
                "market_id": str(r[0]),
                "target_date": r[1],
                "status": str(r[2]),
                "payout": r[3],
                "verified": bool(r[4]),
                "verification_status": r[5],
                "resolution_source": r[6],
                "fetched_at_utc": _iso(r[7]),
            }
            for r in rows
        ]

    # -- integrity ---------------------------------------------------------

    def verify_consumed_parts(self, parts: Mapping[int, PartRecord]) -> None:
        """Refuse to continue if a part already consumed has changed.

        Parts are published by rename and never rewritten, so a mismatch means
        something outside this process touched the export. Continuing would
        fold the new bytes into aggregates that already counted the old ones.
        """
        recorded = self.recorded_parts()
        for index, part in recorded.items():
            current = parts.get(index)
            if current is None:
                raise PerformanceStateError(
                    f"part {index} was consumed by an earlier run but is missing from the "
                    "export; the export must not be pruned under a live state store"
                )
            if current.sha256 != part.sha256 or current.rows != part.rows:
                raise PerformanceStateError(
                    f"part {index} changed after it was consumed "
                    f"(recorded {part.rows} rows / {part.sha256[:12]}, now "
                    f"{current.rows} rows / {current.sha256[:12]}). Published parts are "
                    "immutable; rebuild the state store from scratch if this was deliberate"
                )

    def summary(self) -> dict[str, Any]:
        """A cheap health view of the store, for logs and tests."""
        state = self.load_state()
        counts = {
            name: int(self.fetchone(f"SELECT count(*) FROM {table}")[0])
            for name, table in (
                ("episodes", "performance_episodes"),
                ("open_episodes", "performance_episodes WHERE is_open"),
                ("edge_observations", "performance_edge_observations"),
                ("parts", "performance_parts"),
                ("settlements", "performance_settlements"),
            )
        }
        return {
            "path": str(self.path),
            "state": state.as_dict() if state else None,
            "counts": counts,
            "distinct": self.distinct_counts(),
        }

    def reset(self) -> None:
        """Drop every row. The store is derived data; the export is the truth."""
        for table in (
            "performance_episodes",
            "performance_edge_observations",
            "performance_parts",
            "performance_settlements",
            "performance_markets_seen",
            "performance_target_dates_seen",
            "performance_models_seen",
            "performance_ingest",
        ):
            self.con.execute(f"DELETE FROM {table}")


def state_fingerprint(payload: Mapping[str, Any]) -> str:
    """A stable digest of a small mapping, for the generation id."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

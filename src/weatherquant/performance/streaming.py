"""The streaming incremental performance engine.

What this replaces, and why
---------------------------
``build_performance`` reads every decision into a Python dict, sorts the lot,
folds episodes in one pass and only then computes six snapshots. It is correct
and it is unaffordable: measured at about 6.8 KB per decision, it OOMed at
960,000 rows under a 6.5 GB cap and took the whole NAS offline for 35 minutes
at 7.5 GB. The export only grows, so no memory cap fixes that.

The pipeline here is:

    shadow export parts -> streaming reader -> persistent state -> snapshots

* **The reader** walks the published parts in cursor order, one line at a time.
  A decision becomes a compact ``_DecisionRow``, is folded, and is dropped -
  the raw dict never survives the loop.
* **The state** is a DuckDB file (see :mod:`weatherquant.performance.state`)
  holding the open-episode checkpoint, the counters, the consumed-part records
  and the edge observations. Rows, episode state, counters and cursor are
  committed in one transaction per part, so the watermark can never run ahead
  of the analysis.
* **The snapshots** are built by querying that store. The two collections that
  grow with the whole history - the edge bands and the episode list - are
  written as they are read, so peak memory does not depend on how long the
  history is.

What is deliberately *not* changed
----------------------------------
Every published number keeps its meaning, and most keep their exact bits:

* sums use ``math.fsum`` over a streamed cursor, which is order-independent and
  therefore identical to ``math.fsum`` over a list;
* the standard deviation uses ``statistics.pstdev`` over the same stream, which
  is a single exact-rational pass;
* quantiles and the median are taken from the *order statistics* the store
  returns by rank, then interpolated by the same functions the list path uses.
  DuckDB's own ``quantile_cont`` is not used: it interpolates with a different
  expression and disagrees in the last bit (74 of 1200 sampled cases);
* the episode fold, the win-rate bootstrap and the portfolio walk are the same
  code the list path runs - only their driver changed.

Two things do change, and both are additive: the settlement index is sticky
(a run without ``--settlements`` keeps the answers already stored instead of
publishing none), and the manifest carries ``algorithm_version``,
``input_source``, ``input_range`` and a ``streaming`` block.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import statistics
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from operator import itemgetter
from pathlib import Path
from typing import Any

from weatherquant.performance.capital import (
    DEFAULT_MAX_ALLOCATION_FRACTION,
    PortfolioResult,
    PortfolioWalk,
    lifetime_portfolio_payload,
)
from weatherquant.performance.edge import (
    ACCEPT_STATE,
    EDGE_BANDS,
    QUANTILES,
    DistributionParts,
    daily_entry,
    distribution_from_parts,
    median_from_ranks,
    quantile_from_ranks,
)
from weatherquant.performance.episodes import (
    DEFAULT_GAP_WINDOW_SECONDS,
    EndReason,
    Episode,
    EpisodeFolder,
    EpisodeKey,
    EpisodeSummary,
    OpenEpisode,
    _field,
    _timestamp,
    episode_from_open,
    row_from_mapping,
)
from weatherquant.performance.snapshot import (
    DETAIL_MAX_EPISODES,
    DETAIL_PARTITION_SIZE,
    DETAIL_WINDOW_DAYS,
    SettlementRecord,
    _envelope,
    _settlement_coverage,
    _settlement_index,
    detail_warnings,
    detail_window_descriptor,
    publish_performance,
    settlement_freshness,
    settlement_payload,
)
from weatherquant.performance.state import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_PERFORMANCE_STATE_DB_PATH,
    PERFORMANCE_ALGORITHM_VERSION,
    IngestState,
    PartRecord,
    PerformanceStateError,
    PerformanceStateStore,
    StateMismatch,
    StoredEpisode,
    state_fingerprint,
)
from weatherquant.performance.winrate import (
    SettlementAnswer,
    WinRateAccumulator,
    group_key,
    outcome_for,
)
from weatherquant.shadow.export import _end_cursor_of, _index_of
from weatherquant.shadow.publish import PartitionedArray, atomic_write_json

__all__ = [
    "ExportPart",
    "IngestResult",
    "StreamingBuild",
    "build_performance_streaming",
    "build_streaming_payloads",
    "ingest_export",
    "scan_export_parts",
]

#: How many rows to pull out of DuckDB at a time when an aggregate has to run
#: in Python. Large enough that the per-fetch overhead disappears, small enough
#: that a fetch buffer is not a memory decision.
FETCH_ROWS = 20_000

#: The group expressions the published payloads need. A whitelist rather than
#: string interpolation from a caller, so no dimension can reach the SQL.
EDGE_GROUP_SQL: dict[str, str] = {
    "model": "COALESCE(model_name, 'unknown')",
    "direction": "COALESCE(direction, 'unknown')",
    "target_date": "COALESCE(target_date, 'unknown')",
}

#: The five dimensions the win-rate payload splits by.
WIN_RATE_DIMENSIONS = ("model", "direction", "mode", "target_date", "edge_band")

#: The contract's edge bands as one SQL expression, so a band is grouped by the
#: same rule ``band_of`` applies in Python. ``edge`` is always finite here -
#: ``_finite`` refuses NaN and infinities before a row is stored - so the
#: comparison chain is exhaustive.
_BAND_SQL = (
    "CASE WHEN edge < 0.02 THEN 'lt_0.02' "
    "WHEN edge < 0.05 THEN '0.02_0.05' "
    "WHEN edge < 0.10 THEN '0.05_0.10' "
    "ELSE 'gte_0.10' END"
)

_OVERALL = "__overall__"


# ---------------------------------------------------------------------------
# reading the export
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExportPart:
    """One published part, as the reader sees it before consuming it."""

    index: int
    path: Path
    end_cursor_time: datetime
    end_cursor_id: str

    @property
    def end_cursor(self) -> tuple[datetime, str]:
        return (self.end_cursor_time, self.end_cursor_id)


def scan_export_parts(directory: str | Path) -> list[ExportPart]:
    """Enumerate the published parts, in order, refusing a gap.

    The same rule the in-memory reader applies, for the same reason: the cursor
    is a high-water mark, so reading past a missing part claims rows that are
    not there. Here it matters even more, because the state store would record
    the claim permanently.

    Parts are published by an atomic rename, so any ``part-*.jsonl`` that is
    visible is complete. The writer's temporary files carry a ``.tmp`` suffix
    and are not matched.
    """
    root = Path(directory) / "parts"
    if not root.is_dir():
        raise PerformanceStateError(f"no published decision parts under {root}")
    paths = sorted(root.glob("part-*.jsonl"))
    if not paths:
        raise PerformanceStateError(
            f"no published decision parts under {root}; run the shadow's export first"
        )
    indices = [_index_of(path) for path in paths]
    if indices != list(range(1, len(indices) + 1)):
        missing = sorted(set(range(1, len(indices) + 1)) - set(indices))
        raise PerformanceStateError(
            f"the decisions export has a gap: part index(es) {missing} are missing "
            f"from {root}. A watermark taken from the highest part would claim "
            "coverage of rows that are not here, so the export is refused rather "
            "than consumed under a cursor it never reached."
        )
    parts: list[ExportPart] = []
    for path in paths:
        end = _end_cursor_of(path)
        if end is None:
            raise PerformanceStateError(
                f"part {path.name} has no readable end cursor; it is empty or "
                "truncated and cannot be consumed"
            )
        parts.append(
            ExportPart(
                index=_index_of(path),
                path=path,
                end_cursor_time=_timestamp(end[0]),
                end_cursor_id=str(end[1]),
            )
        )
    return parts


def _digest_part(path: Path) -> tuple[str, int]:
    """SHA-256 and row count of one part, from a single sequential read.

    The writer emits exactly one ``\\n`` per row and JSON escapes any newline
    inside a value, so counting line feeds counts rows. A truncated tail line
    changes the digest, which is what makes the check meaningful.
    """
    digest = hashlib.sha256()
    rows = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            rows += chunk.count(b"\n")
    return digest.hexdigest(), rows


def _iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Stream one part's decisions. Only one dict exists at a time."""
    import json

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _row_cursor(row: Mapping[str, Any]) -> tuple[datetime, str]:
    """The ``(instant, id)`` a row sits at, for the order check."""
    stamp = _field(row, "decided_at_utc")
    if stamp is None:
        raise PerformanceStateError("a decision in the export carries no decision_time_utc")
    return (_timestamp(stamp), str(row.get("decision_id") or ""))


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestResult:
    """What one ingest run did."""

    parts_available: int
    parts_consumed: int
    rows_consumed: int
    caught_up: bool
    watermark: list[str] | None
    state: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "parts_available": self.parts_available,
            "parts_consumed": self.parts_consumed,
            "rows_consumed": self.rows_consumed,
            "caught_up": self.caught_up,
            "watermark": list(self.watermark) if self.watermark else None,
            "state": self.state,
        }


def _require_compatible(
    state: IngestState,
    *,
    algorithm_version: str,
    input_source: str,
    gap_window_seconds: float,
    accepted_state: str,
) -> None:
    """Refuse to extend a store built by a different input or algorithm.

    Extending it anyway would produce aggregates that are part one algorithm
    and part another, with nothing in the output to say so. A rebuild from the
    export is cheap; a quietly mixed generation is not detectable afterwards.
    """
    if state.algorithm_version != algorithm_version:
        raise StateMismatch(
            f"the state store was built by algorithm {state.algorithm_version!r} but this "
            f"build is {algorithm_version!r}; rebuild the store from the export"
        )
    if state.input_source != input_source:
        raise StateMismatch(
            f"the state store was built from {state.input_source!r} but this run points at "
            f"{input_source!r}; rebuild the store from the export"
        )
    if abs(state.gap_window_seconds - gap_window_seconds) > 1e-9:
        raise StateMismatch(
            f"the state store was built with gap_window_seconds="
            f"{state.gap_window_seconds!r} but this run asks for {gap_window_seconds!r}; "
            "the stored episode boundaries depend on it, so a rebuild is required"
        )
    if state.accepted_state != accepted_state:
        raise StateMismatch(
            f"the state store was built with accepted_state={state.accepted_state!r} but "
            f"this run asks for {accepted_state!r}; rebuild the store from the export"
        )


def _verify_consumed_parts(
    store: PerformanceStateStore, parts: Sequence[ExportPart], *, deep: bool
) -> None:
    """Refuse to continue if a part already consumed no longer matches.

    Published parts are immutable, so a mismatch means something outside this
    process rewrote or truncated the export. Folding the new bytes in would
    double-count the range that was already counted, and no later count would
    reveal it.

    Only the boundary part is re-hashed by default: it is the one a crashed
    writer or a partial copy would damage, and hashing the whole export on every
    run would make the check cost more than the work it protects. ``deep``
    hashes everything, for an operator who wants the stronger answer.
    """
    recorded = store.recorded_parts()
    if not recorded:
        return
    by_index = {part.index: part for part in parts}
    targets = recorded if deep else {max(recorded): recorded[max(recorded)]}
    for index, record in sorted(targets.items()):
        part = by_index.get(index)
        if part is None:
            raise PerformanceStateError(
                f"part {index} was consumed by an earlier run but is missing from the "
                "export; the export must not be pruned under a live state store"
            )
        digest, rows = _digest_part(part.path)
        if digest != record.sha256 or rows != record.rows:
            raise PerformanceStateError(
                f"part {index} changed after it was consumed (recorded {record.rows} rows / "
                f"{record.sha256[:12]}, now {rows} rows / {digest[:12]}). Published parts are "
                "immutable; rebuild the state store if this was deliberate"
            )


def ingest_export(
    store: PerformanceStateStore,
    directory: str | Path,
    *,
    gap_window_seconds: float = DEFAULT_GAP_WINDOW_SECONDS,
    accepted_state: str = ACCEPT_STATE,
    algorithm_version: str = PERFORMANCE_ALGORITHM_VERSION,
    max_parts_per_run: int | None = None,
    deep_verify: bool = False,
    progress: Callable[[str], None] | None = None,
) -> IngestResult:
    """Consume every published part the store has not seen, in order.

    One transaction per part. Rows, the open-episode checkpoint, the counters,
    the distinct-value sets, the part record and the cursor are committed
    together, so a crash anywhere leaves the store at the previous part's
    boundary and the next run simply re-reads from there. That is what makes
    re-input idempotent: the rows of an interrupted part were never committed,
    so they cannot be counted twice.
    """
    directory = Path(directory)
    parts = scan_export_parts(directory)
    input_source = str(directory.resolve())

    state = store.load_state()
    if state is None:
        state = IngestState(
            algorithm_version=algorithm_version,
            input_source=input_source,
            gap_window_seconds=float(gap_window_seconds),
            accepted_state=accepted_state,
        )
        store.initialize_state(state)
    else:
        _require_compatible(
            state,
            algorithm_version=algorithm_version,
            input_source=input_source,
            gap_window_seconds=float(gap_window_seconds),
            accepted_state=accepted_state,
        )

    _verify_consumed_parts(store, parts, deep=deep_verify)

    closed_buffer: list[tuple[Episode, OpenEpisode, int]] = []

    def on_close(episode: Episode, open_ep: OpenEpisode, seq: int) -> None:
        closed_buffer.append((episode, open_ep, seq))

    folder = EpisodeFolder(
        gap_window_seconds=float(gap_window_seconds),
        accepted_state=accepted_state,
        on_close=on_close,
    )
    folder.restore(store.load_open_episodes(), state=state.folder_state())

    pending = [part for part in parts if part.index > state.last_part_index]
    if max_parts_per_run is not None:
        pending = pending[:max_parts_per_run]

    rows_this_run = 0
    for part in pending:
        if part.index != state.last_part_index + 1:
            # Only reachable if the store's part records and its cursor disagree,
            # which would mean the state was edited by hand.
            raise PerformanceStateError(
                f"the state store is at part {state.last_part_index} but the next part "
                f"available is {part.index}; the export was pruned or the state edited"
            )
        digest, expected_rows = _digest_part(part.path)

        edge_rows: list[list[Any]] = []
        touched: dict[EpisodeKey, bool] = {}
        markets: set[str] = set()
        target_dates: set[str] = set()
        models: set[str] = set()
        closed_buffer.clear()

        base_index = state.rows_consumed
        rows_in_part = 0
        previous_cursor = state.last_end_cursor_parsed

        with store.transaction():
            for raw in _iter_rows(part.path):
                seq = base_index + rows_in_part
                cursor = _row_cursor(raw)
                if previous_cursor is not None and cursor < previous_cursor:
                    raise PerformanceStateError(
                        f"part {part.index} is not in cursor order at row {rows_in_part} "
                        f"({cursor[0].isoformat()} after {previous_cursor[0].isoformat()}). "
                        "The fold assumes the export is non-decreasing in "
                        "(decision_time_utc, decision_id); refusing rather than folding "
                        "an out-of-order part."
                    )
                if previous_cursor is not None and cursor == previous_cursor:
                    raise PerformanceStateError(
                        f"part {part.index} repeats the cursor "
                        f"{cursor[0].isoformat()}/{cursor[1]} at row {rows_in_part}. The "
                        "cursor is the identity of a consumed row, so a repeat is either a "
                        "duplicated export line or a missing decision_id; either way it "
                        "would be counted twice."
                    )
                previous_cursor = cursor

                row = row_from_mapping(raw)
                is_accept = row.state == accepted_state
                has_edge = is_accept and row.net_edge_usd is not None

                state.decisions_seen += 1
                if is_accept:
                    state.accepts_seen += 1
                else:
                    state.not_accept += 1
                if is_accept and row.net_edge_usd is None:
                    state.accepts_without_edge += 1
                    if row.market_id:
                        state.accepts_without_edge_market += 1

                if row.market_id:
                    state.decisions_with_market += 1
                    markets.add(row.market_id)
                if row.target_date:
                    target_dates.add(row.target_date)
                models.add(str(raw.get("model_name") or "unknown"))

                moment = row.decided_at_utc
                if state.first_decision_utc is None or moment < state.first_decision_utc:
                    state.first_decision_utc = moment
                if state.last_decision_utc is None or moment > state.last_decision_utc:
                    state.last_decision_utc = moment

                if has_edge:
                    assert row.net_edge_usd is not None
                    edge_rows.append(
                        [
                            seq,
                            row.target_date,
                            row.model_name,
                            row.direction,
                            row.mode,
                            float(row.net_edge_usd),
                        ]
                    )

                if row.market_id:
                    if has_edge or row.key in folder.open_by_key:
                        touched[row.key] = True
                    folder.add(row)

                rows_in_part += 1

            if rows_in_part != expected_rows:
                raise PerformanceStateError(
                    f"part {part.index} yielded {rows_in_part} rows but its bytes contain "
                    f"{expected_rows}; refusing to record a part whose content disagrees "
                    "with its size"
                )

            store.insert_edge_observations(edge_rows)
            store.upsert_closed_episodes(closed_buffer)
            store.upsert_open_episodes(
                [folder.open_by_key[key] for key in touched if key in folder.open_by_key]
            )
            store.upsert_dimensions(
                markets=markets, target_dates=target_dates, models=models
            )
            store.record_part(
                PartRecord(
                    index=part.index,
                    rows=rows_in_part,
                    end_cursor_time=part.end_cursor_time,
                    end_cursor_id=part.end_cursor_id,
                    sha256=digest,
                )
            )
            # Only now does the cursor move, and it moves inside the same
            # transaction as the rows it describes.
            state.last_part_index = part.index
            state.last_end_cursor_time = part.end_cursor_time
            state.last_end_cursor_id = part.end_cursor_id
            state.rows_consumed = base_index + rows_in_part
            state.row_seq = folder.state.row_seq
            state.last_instant = folder.state.last_instant
            state.prev_instant = folder.state.prev_instant
            store.write_state(state)

        rows_this_run += rows_in_part
        if progress is not None:
            progress(
                f"part {part.index}: {rows_in_part} rows, watermark "
                f"{part.end_cursor_time.isoformat()}"
            )

    return IngestResult(
        parts_available=len(parts),
        parts_consumed=len(pending),
        rows_consumed=rows_this_run,
        caught_up=not pending,
        watermark=list(state.last_end_cursor) if state.last_end_cursor else None,
        state=state.as_dict(),
    )


# ---------------------------------------------------------------------------
# aggregates over the store
# ---------------------------------------------------------------------------


def _row_stream(store: PerformanceStateStore, sql: str) -> Iterator[tuple]:
    cursor = store.cursor(sql)
    while True:
        rows = cursor.fetchmany(FETCH_ROWS)
        if not rows:
            return
        yield from rows


def _counts_and_extremes(
    store: PerformanceStateStore, expression: str | None
) -> dict[str, tuple[int, float | None, float | None]]:
    """``count``, ``min`` and ``max`` per group, asked of SQL.

    Exact for all three, and it keeps the Python passes below to one job each.
    """
    if expression is None:
        count, low, high = store.fetchone(
            "SELECT count(*), min(edge), max(edge) FROM performance_edge_observations"
        )
        return {_OVERALL: (int(count), low, high)} if count else {}
    sql = (
        f"SELECT {expression} AS g, count(*), min(edge), max(edge) "
        "FROM performance_edge_observations GROUP BY 1"
    )
    return {
        str(key): (int(count), low, high)
        for key, count, low, high in _row_stream(store, sql)
    }


def _fsums(store: PerformanceStateStore, expression: str | None) -> dict[str, float]:
    """``math.fsum`` per group, streamed.

    Order-independent and correctly rounded, so this is the same number the
    in-memory builder produced from a list - and the values never all exist.
    """
    if expression is None:
        return {
            _OVERALL: math.fsum(
                value for (value,) in _row_stream(store, "SELECT edge FROM performance_edge_observations")
            )
        }
    sql = f"SELECT {expression} AS g, edge FROM performance_edge_observations ORDER BY 1"
    out: dict[str, float] = {}
    rows = _row_stream(store, sql)
    for key, group in itertools.groupby(rows, key=lambda row: str(row[0])):
        out[str(key)] = math.fsum(map(itemgetter(1), group))
    return out


def _population_stdev(
    store: PerformanceStateStore,
    expression: str | None,
    counts: Mapping[str, tuple[int, float | None, float | None]],
) -> dict[str, float]:
    """``statistics.pstdev`` per group, streamed.

    Its own pass, because the mean it divides by has to be known first and it
    is computed exactly (from rational partials) rather than from the rounded
    ``sum / count``. Passing the values through ``statistics.pstdev`` - the
    same function the list path calls - is what keeps the published ``std``
    identical instead of merely close.
    """
    out: dict[str, float] = {}
    if expression is None:
        if counts.get(_OVERALL, (0,))[0] > 1:
            out[_OVERALL] = statistics.pstdev(
                value for (value,) in _row_stream(store, "SELECT edge FROM performance_edge_observations")
            )
        return out

    sql = f"SELECT {expression} AS g, edge FROM performance_edge_observations ORDER BY 1"
    rows = _row_stream(store, sql)
    for key, group in itertools.groupby(rows, key=lambda row: str(row[0])):
        if counts.get(str(key), (0,))[0] > 1:
            out[str(key)] = statistics.pstdev(map(itemgetter(1), group))
        else:
            # The group still has to be drained, or groupby would hand the next
            # key an iterator that is already positioned inside this one.
            deque(group, maxlen=0)
    return out


def _rank_filter(count_expression: str) -> str:
    """The ranks the published quantiles and the median need, as SQL.

    The arithmetic is the same expression :func:`_rank_pair` evaluates, so the
    store selects exactly the two order statistics the interpolation will use.
    """
    clauses: list[str] = []
    for quantile in QUANTILES:
        for bound in ("floor", "ceil"):
            clauses.append(
                f"rn = CAST({bound}({quantile!r} * ({count_expression} - 1)) AS BIGINT)"
            )
    return " OR ".join(clauses)


def _order_statistics(
    store: PerformanceStateStore, expression: str | None
) -> dict[str, tuple[int, dict[int, float]]]:
    """``rank -> value`` for the ranks the quantiles interpolate between."""
    partition = f"PARTITION BY {expression} " if expression is not None else ""
    grouped = expression is not None
    cte_select = f"{expression} AS g, " if grouped else ""
    sql = (
        f"WITH ranked AS (SELECT {cte_select}edge, "
        f"row_number() OVER ({partition}ORDER BY edge) - 1 AS rn, "
        f"count(*) OVER ({partition}) AS n FROM performance_edge_observations) "
        f"SELECT {'g, ' if grouped else ''}rn, edge, n FROM ranked WHERE {_rank_filter('n')} "
        f"ORDER BY {'g, ' if grouped else ''}rn"
    )
    out: dict[str, tuple[int, dict[int, float]]] = {}
    if expression is None:
        ranks: dict[int, float] = {}
        count = 0
        for rn, edge, n in _row_stream(store, sql):
            count = int(n)
            ranks[int(rn)] = float(edge)
        if count:
            out[_OVERALL] = (count, ranks)
        return out
    for key, group in itertools.groupby(_row_stream(store, sql), key=lambda row: str(row[0])):
        ranks = {}
        count = 0
        for row in group:
            count = int(row[3])
            ranks[int(row[1])] = float(row[2])
        out[key] = (count, ranks)
    return out


def _parts_for(
    key: str,
    counts: Mapping[str, tuple[int, float | None, float | None]],
    sums: Mapping[str, float],
    stdevs: Mapping[str, float],
    ranks: Mapping[str, tuple[int, dict[int, float]]],
) -> DistributionParts:
    count, low, high = counts[key]
    rank_count, rank_values = ranks.get(key, (count, {}))
    if rank_count != count:  # pragma: no cover - guards the two queries agreeing
        raise PerformanceStateError(
            f"the order-statistic query and the counting query disagree about group {key!r} "
            f"({rank_count} vs {count}); the distribution would be built on the wrong ranks"
        )
    return DistributionParts(
        count=count,
        total=sums[key],
        minimum=low,
        maximum=high,
        std=stdevs.get(key, 0.0) if count > 1 else 0.0,
        median=median_from_ranks(count, rank_values),
        quantiles={q: quantile_from_ranks(count, rank_values, q) for q in QUANTILES},
    )


def _distributions(
    store: PerformanceStateStore,
    expression: str | None,
    *,
    ensure_keys: Sequence[str] | None = None,
) -> dict[str, dict[str, float | int | None]]:
    """Every group's distribution, in four passes over the edge table.

    ``count``/``min``/``max`` come from SQL, the exact sum and the exact
    standard deviation each need their own streamed pass, and the quantiles
    need the order statistics by rank. None of the four holds more than a
    group's worth of anything.

    ``ensure_keys`` names groups that must appear even with no observations.
    A group whose key comes from the data (a model, a date) simply is not
    there when it has none; the contract's edge bands are a fixed vocabulary,
    and a band with nothing in it is a fact the payload states rather than
    omits.
    """
    counts = _counts_and_extremes(store, expression)
    sums = _fsums(store, expression)
    stdevs = _population_stdev(store, expression, counts)
    ranks = _order_statistics(store, expression)
    out = {
        key: distribution_from_parts(_parts_for(key, counts, sums, stdevs, ranks))
        for key in counts
    }
    for key in ensure_keys or ():
        out.setdefault(key, distribution_from_parts(DistributionParts(count=0)))
    return dict(sorted(out.items()))


def _daily(store: PerformanceStateStore) -> list[dict[str, object]]:
    """The per-target-date trend, ordered by date."""
    counts = {
        str(key): int(count)
        for key, count in _row_stream(
            store,
            "SELECT target_date, count(*) FROM performance_edge_observations "
            "WHERE target_date IS NOT NULL GROUP BY 1",
        )
    }
    sql = (
        "SELECT target_date, edge FROM performance_edge_observations "
        "WHERE target_date IS NOT NULL ORDER BY 1"
    )
    out: list[dict[str, object]] = []
    rows = _row_stream(store, sql)
    for key, group in itertools.groupby(rows, key=lambda row: str(row[0])):
        out.append(
            daily_entry(str(key), counts[str(key)], math.fsum(map(itemgetter(1), group)))
        )
    return out


def _band_distributions(
    store: PerformanceStateStore,
) -> dict[str, dict[str, float | int | None]]:
    """A distribution per band, not the band's values.

    The raw values were the whole of ``edge.json``'s size - 2.2 million floats
    at five million decisions - and the only consumer counted them. A band is
    just another grouping, so it is computed by the same four passes as the
    model, direction and target-date groupings, and it is exact in the same
    way.
    """
    return _distributions(store, _BAND_SQL, ensure_keys=list(EDGE_BANDS))


def _render(stored: StoredEpisode) -> Episode:
    """The published record for a stored episode.

    An episode that is still open is rendered as ``end_of_data`` - the stream
    has stopped, not the opportunity - while staying open in the store. The
    rendering and the state are deliberately separate: publishing must not look
    like an ending the fold then has to undo.
    """
    reason = stored.end_reason if not stored.is_open else EndReason.END_OF_DATA
    return episode_from_open(stored.open_ep, end_reason=reason or EndReason.END_OF_DATA)


def _stored_settlements(
    store: PerformanceStateStore,
) -> tuple[
    dict[tuple[str, str | None], SettlementAnswer],
    frozenset[tuple[str, str | None]],
    list[SettlementRecord],
]:
    """The store's settlement layer, in the shape both engines publish."""
    rows = store.load_settlements()
    answers: dict[tuple[str, str | None], SettlementAnswer] = {}
    verified: set[tuple[str, str | None]] = set()
    records: list[SettlementRecord] = []
    for row in rows:
        key = (row["market_id"], row["target_date"])
        answers[key] = SettlementAnswer(status=row["status"], payout=row["payout"])
        if row["verified"]:
            verified.add(key)
        records.append(
            SettlementRecord(
                market_id=row["market_id"],
                target_date=row["target_date"],
                status=row["status"],
                payout=row["payout"],
                verified=bool(row["verified"]),
                verification_status=row["verification_status"],
                resolution_source=row["resolution_source"],
                fetched_at_utc=_timestamp(row["fetched_at_utc"]) if row["fetched_at_utc"] else None,
            )
        )
    return answers, frozenset(verified), records


# ---------------------------------------------------------------------------
# building the six payloads
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamingBuild:
    """The six payloads plus what the manifest needs to describe them."""

    payloads: dict[str, dict[str, Any]]
    coverage: dict[str, Any]
    watermark: list[str] | None
    manifest_extra: dict[str, Any]
    state: dict[str, Any]
    summary: dict[str, Any] = field(default_factory=dict)
    settlement_records: list[SettlementRecord] = field(default_factory=list)


def build_streaming_payloads(
    store: PerformanceStateStore,
    *,
    now: datetime,
    source: str = "weatherquant-research",
    dataset: str = "live",
    capital_usdc: float = 100.0,
    max_allocation_fraction: float = DEFAULT_MAX_ALLOCATION_FRACTION,
    book_for: Callable[[Episode], Any] | None = None,
    gap_window_seconds: float = DEFAULT_GAP_WINDOW_SECONDS,
    detail_max_episodes: int = DETAIL_MAX_EPISODES,
    detail_window_days: int = DETAIL_WINDOW_DAYS,
) -> StreamingBuild:
    """Build all six snapshots from the store, holding nothing that grows.

    The episode set is walked three times - once for the summary and the win
    rates together, once to stream the opportunities, once to stream the
    reference-capital results - and the edge table ten times. That is a
    deliberate trade: each pass is bounded, and re-reading a columnar store is
    the cheap half of the trade. What is *not* traded is exactness: every pass
    runs the same functions the in-memory builder runs.
    """
    state = store.load_state()
    if state is None:
        raise PerformanceStateError(
            "the state store is empty; run an ingest before building snapshots"
        )

    answers, verified, settlement_records = _stored_settlements(store)
    freshness = settlement_freshness(settlement_records)
    distinct = store.distinct_counts()

    # -- one pass over the episodes: the summary and all six win rates ------
    episode_summary = EpisodeSummary()
    overall_rate = WinRateAccumulator()
    grouped_rates: dict[str, dict[str, WinRateAccumulator]] = {
        dimension: {} for dimension in WIN_RATE_DIMENSIONS
    }
    episode_count = 0
    for stored in store.iter_episodes():
        episode = _render(stored)
        episode_count += 1
        episode_summary.add(episode)
        outcome, _reason = outcome_for(episode, answers=answers, verified=verified)
        overall_rate.add(
            outcome=outcome,
            market_id=episode.key.market_id,
            target_date=episode.key.target_date,
        )
        for dimension in WIN_RATE_DIMENSIONS:
            key = group_key(episode, outcome, dimension)
            accumulator = grouped_rates[dimension].get(key)
            if accumulator is None:
                accumulator = WinRateAccumulator()
                grouped_rates[dimension][key] = accumulator
            accumulator.add(
                outcome=outcome,
                market_id=episode.key.market_id,
                target_date=episode.key.target_date,
            )

    episode_summary_dict = episode_summary.as_dict()
    rate = overall_rate.result()
    grouped_rate_results = {
        dimension: {
            key: accumulator.result()
            for key, accumulator in sorted(buckets.items())
        }
        for dimension, buckets in grouped_rates.items()
    }

    # -- the edge distributions --------------------------------------------
    overall_distributions = _distributions(store, None)
    # An empty edge table is a real state - a history with no priced ACCEPT -
    # and it publishes a distribution of nothing rather than raising.
    overall_edge = overall_distributions.get(_OVERALL) or distribution_from_parts(
        DistributionParts(count=0)
    )
    grouped_edges = {
        name: _distributions(store, expression)
        for name, expression in EDGE_GROUP_SQL.items()
    }
    daily = _daily(store)
    bands = _band_distributions(store)
    models = sorted(store.distinct_values("performance_models_seen"))

    # -- reference capital: totals first, then the same walk streamed -------
    capital = _walk_capital(
        store,
        capital_usdc=capital_usdc,
        max_allocation_fraction=max_allocation_fraction,
        book_for=book_for,
    )

    build_watermark = state.last_end_cursor
    lifetime_window = {
        "episodes_all": episode_count,
        "first_seen_utc_from": _iso(
            store.fetchone("SELECT min(first_seen_utc) FROM performance_episodes")[0]
        ),
        "last_seen_utc_to": _iso(
            store.fetchone("SELECT max(first_seen_utc) FROM performance_episodes")[0]
        ),
    }

    scope = ["observation_all_accepts", "opportunity_episodes", "database_lifetime"]
    summary_data = {
        "observation_edge_sum": overall_edge["sum"],
        "unique_opportunity_edge_sum": episode_summary_dict["unique_opportunity_edge_sum"],
        "average_net_edge": overall_edge["mean"],
        "realized_directional_win_rate": rate.win_rate,
        "verified_opportunities": rate.valid_samples,
        "settled_markets": rate.distinct_markets,
        "counts": rate.counts,
        "reference_capital_usdc": capital.capital_usdc,
        "reference_capital": lifetime_portfolio_payload(
            capital,
            watermark=build_watermark,
            window=lifetime_window,
            settlement_coverage=_settlement_coverage(settlement_records, rate),
        ),
        "accept_observations": episode_summary_dict["accept_observations"],
        "opportunity_episodes": episode_summary_dict["opportunity_episodes"],
        "accept_opportunity_distinct_markets": episode_summary_dict["distinct_markets"],
        "accept_opportunity_distinct_target_dates": episode_summary_dict[
            "distinct_target_dates"
        ],
        "distinct_markets": episode_summary_dict["distinct_markets"],
        "distinct_target_dates": episode_summary_dict["distinct_target_dates"],
        "episode_build": {
            "decisions_seen": state.decisions_with_market,
            "episodes_built": episode_count,
            "accepts_without_edge": state.accepts_without_edge_market,
            "open_at_end": _open_episode_count(store),
        },
        # The settlement layer is kept across runs, so when it was retrieved is
        # part of the number: a win rate whose settlements were fetched weeks
        # ago is not the same claim as one fetched this morning, and the reader
        # cannot tell the two apart from the rate alone.
        "settlement_freshness": freshness,
        "confidence_interval": list(rate.confidence_interval) if rate.confidence_interval else None,
        "confidence_method": rate.method,
        "insufficient_reason": rate.reason,
        "statistical_scope": scope,
        "warnings": [
            "Observation Edge Sum counts repeated evaluations of one opportunity; "
            "it is not profit, not asset growth and not the expected return of independent trades.",
            "Unique Opportunity Edge Sum is a model expectation, not realised profit.",
            "Actual profit is N/A: this system places no orders.",
            *rate.warnings,
        ],
    }

    edge_payload = {
        "overall": overall_edge,
        "by_model": grouped_edges["model"],
        "by_direction": grouped_edges["direction"],
        "by_target_date": grouped_edges["target_date"],
        "daily": daily,
        "by_edge_band": bands,
        "refused": {
            "accept_without_usable_edge": state.accepts_without_edge,
            "not_accept": state.not_accept,
        },
        "scope": "observation_all_accepts",
        "statistical_scope": ["observation_all_accepts", "database_lifetime"],
        "warnings": [
            "Observation Edge Sum sums repeated evaluations of the same "
            "opportunity: it is not profit, not asset growth and not the "
            "expected return of independent trades.",
            "Quantiles over repeated evaluations describe how often the "
            "engine re-priced a bracket, not how often a market behaved this way.",
        ],
    }

    win_rate_payload = {
        "overall": rate.as_dict(),
        "by_model": {k: v.as_dict() for k, v in grouped_rate_results["model"].items()},
        "by_direction": {k: v.as_dict() for k, v in grouped_rate_results["direction"].items()},
        "by_mode": {k: v.as_dict() for k, v in grouped_rate_results["mode"].items()},
        "by_target_date": {
            k: v.as_dict() for k, v in grouped_rate_results["target_date"].items()
        },
        "by_edge_band": {k: v.as_dict() for k, v in grouped_rate_results["edge_band"].items()},
        "statistical_scope": rate.statistical_scope,
        "warnings": rate.warnings,
    }

    since, window = _detail_window(
        store,
        now=now,
        max_episodes=detail_max_episodes,
        days=detail_window_days,
    )
    opportunities = {
        "episodes": _opportunity_stream(
            store,
            since=since,
            limit=detail_max_episodes,
            answers=answers,
            verified=verified,
            book_for=book_for,
        ),
        "total": window["episodes"],
        "total_all": window["episodes_all"],
        "window": window,
        "statistical_scope": ["opportunity_episodes"]
        + ([] if window["complete"] else [f"window:{window['days']}d"]),
        "warnings": [*episode_summary_dict["warnings"], *detail_warnings(window)],
    }

    settlements_payload = settlement_payload(settlement_records, counts=rate.counts)

    models_payload = {
        "models": models,
        "by_model_edge": grouped_edges["model"],
        "by_model_win_rate": {
            k: v.as_dict() for k, v in grouped_rate_results["model"].items()
        },
        "note": (
            "Brier, Log Loss, ECE, MAE, RMSE and Top-1 are served from the "
            "research evaluation module; see /api/v1/performance/models"
        ),
        "statistical_scope": ["out_of_sample_chronological"],
        "warnings": ["Scores are out-of-sample and chronological; no future data is used."],
    }

    common = dict(now=now, source=source, dataset=dataset)
    sample = overall_edge["count"] or 0
    payloads = {
        "summary.json": _envelope(
            logical_name="performance-summary", data=summary_data,
            sample_size=sample, statistical_scope=scope,
            warnings=summary_data["warnings"], **common,
        ),
        "edge.json": _envelope(
            logical_name="performance-edge", data=edge_payload,
            sample_size=sample, statistical_scope=scope,
            warnings=edge_payload["warnings"], **common,
        ),
        "win-rate.json": _envelope(
            logical_name="performance-win-rate", data=win_rate_payload,
            sample_size=rate.valid_samples, statistical_scope=rate.statistical_scope,
            warnings=rate.warnings, **common,
        ),
        "opportunities.json": _envelope(
            logical_name="performance-opportunities", data=opportunities,
            sample_size=window["episodes"],
            statistical_scope=opportunities["statistical_scope"],
            warnings=opportunities["warnings"], **common,
        ),
        "settlements.json": _envelope(
            logical_name="performance-settlements", data=settlements_payload,
            sample_size=len(settlement_records), statistical_scope=["resolved_verified_only"],
            warnings=settlements_payload["warnings"], **common,
        ),
        "models.json": _envelope(
            logical_name="performance-models", data=models_payload,
            sample_size=sample, statistical_scope=["out_of_sample_chronological"],
            warnings=models_payload["warnings"], **common,
        ),
    }

    coverage = {
        "decisions": state.decisions_seen,
        "all_decision_distinct_markets": distinct["markets"],
        "all_decision_distinct_target_dates": distinct["target_dates"],
        "first_decision_utc": _iso(state.first_decision_utc),
        "last_decision_utc": _iso(state.last_decision_utc),
    }
    manifest_extra = {
        "algorithm_version": state.algorithm_version,
        "input_source": state.input_source,
        "input_range": {
            "first_decision_utc": _iso(state.first_decision_utc),
            "last_decision_utc": _iso(state.last_decision_utc),
        },
        "streaming": {
            "parts_consumed": state.last_part_index,
            "rows_consumed": state.rows_consumed,
            "row_seq": state.row_seq,
            "episodes": episode_count,
            "open_episodes": _open_episode_count(store),
            "gap_window_seconds": state.gap_window_seconds,
            "accepted_state": state.accepted_state,
            "state_db": str(store.path),
        },
    }
    return StreamingBuild(
        payloads=payloads,
        coverage=coverage,
        watermark=list(state.last_end_cursor) if state.last_end_cursor else None,
        manifest_extra=manifest_extra,
        state=state.as_dict(),
        summary=summary_data,
        settlement_records=settlement_records,
    )


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _open_episode_count(store: PerformanceStateStore) -> int:
    return int(
        store.fetchone("SELECT count(*) FROM performance_episodes WHERE is_open")[0]
    )


def _walk_capital(
    store: PerformanceStateStore,
    *,
    capital_usdc: float,
    max_allocation_fraction: float,
    book_for: Callable[[Episode], Any] | None,
) -> PortfolioResult:
    """Run the portfolio walk to completion, keeping only the totals."""
    walk = PortfolioWalk(
        capital_usdc=capital_usdc,
        max_allocation_fraction=max_allocation_fraction,
        collect_results=False,
    )
    for stored in store.iter_episodes():
        episode = _render(stored)
        walk.add(episode, book_for(episode) if book_for is not None else None)
    return walk.finish()


def _detail_window(
    store: PerformanceStateStore,
    *,
    now: datetime,
    max_episodes: int,
    days: int,
) -> tuple[datetime, dict[str, Any]]:
    """The window the browsable detail covers, and its descriptor.

    The store's twin of ``snapshot.detail_window``: the same cutoff, the same
    order and the same cap, so the two engines publish the same window and the
    equivalence test compares them rather than trusting them.
    """
    since = now - timedelta(days=days)
    in_window = store.count_episodes_since(since)
    window_episodes = min(in_window, max_episodes)
    first = store.fetchone(
        "SELECT min(first_seen_utc), max(first_seen_utc) FROM "
        "(SELECT first_seen_utc FROM performance_episodes WHERE first_seen_utc >= ? "
        "ORDER BY first_seen_utc DESC, episode_id DESC LIMIT ?) AS w",
        [since, max_episodes],
    )
    descriptor = detail_window_descriptor(
        window_episodes=window_episodes,
        all_episodes=store.count_episodes(),
        max_episodes=max_episodes,
        days=days,
        first_seen_utc=_iso(first[1]) if first and first[1] else None,
        last_seen_utc=_iso(first[0]) if first and first[0] else None,
    )
    return since, descriptor


def _opportunity_stream(
    store: PerformanceStateStore,
    *,
    since: datetime,
    limit: int,
    answers: Mapping[tuple[str, str | None], SettlementAnswer],
    verified: frozenset[tuple[str, str | None]],
    book_for: Callable[[Episode], Any] | None,
) -> PartitionedArray:
    """The windowed detail, written as it is read, with outcomes attached.

    Published as parts rather than as one array: a reader that wants fifty rows
    reads one part, and the parts record their own sort keys so a cursor can be
    located without opening them.
    """

    def factory() -> Iterator[dict[str, Any]]:
        for stored in store.iter_detail_window(since=since, limit=limit):
            episode = _render(stored)
            outcome, _reason = outcome_for(episode, answers=answers, verified=verified)
            row: dict[str, Any] = {**episode.as_dict(), "outcome": outcome}
            if book_for is not None:
                book = book_for(episode)
                if book is not None:
                    row["book"] = {
                        "bids": [[lvl.price_usd_per_share, lvl.size_shares] for lvl in book.bids],
                        "asks": [[lvl.price_usd_per_share, lvl.size_shares] for lvl in book.asks],
                    }
            yield row

    return PartitionedArray(
        factory,
        directory="opportunities",
        collection="episodes",
        partition_size=DETAIL_PARTITION_SIZE,
        key_of=lambda row: [row["first_seen_utc"], row["episode_id"]],
        label="opportunities.episodes",
    )


# ---------------------------------------------------------------------------
# the whole run
# ---------------------------------------------------------------------------


def _require_dataset(dataset: str) -> str:
    """A run is labelled or it does not ship. There is no default."""
    from weatherquant.performance.backfill import DATASETS

    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {DATASETS}, got {dataset!r}")
    return dataset


def streaming_generation_id(
    now: datetime,
    *,
    state: Mapping[str, Any],
    coverage: Mapping[str, Any],
    settlements: Sequence[SettlementRecord],
    capital_usdc: float,
    max_allocation_fraction: float,
    dataset: str,
    source: str,
) -> str:
    """A stable id for one batch, from what actually went into it.

    The in-memory builder derives its id from the payload bytes. The streaming
    builder cannot: the payloads contain arrays that are never materialised, so
    hashing them would mean reading the whole history twice. It hashes the
    *inputs* instead - the cursor, the counters, the settlement answers, the
    parameters - which is every degree of freedom the output has. Two batches
    that differ anywhere in their content differ here too, and two runs over an
    unchanged store produce the same digest (the timestamp prefix still marks
    when it was published).
    """
    digest = state_fingerprint(
        {
            "algorithm": state.get("algorithm_version"),
            "input_source": state.get("input_source"),
            "rows_consumed": state.get("rows_consumed"),
            "row_seq": state.get("row_seq"),
            "last_end_cursor": state.get("last_end_cursor"),
            "decisions_seen": state.get("decisions_seen"),
            "accepts_seen": state.get("accepts_seen"),
            "accepts_without_edge": state.get("accepts_without_edge"),
            "coverage": dict(coverage),
            "settlements": [
                {
                    "market_id": record.market_id,
                    "target_date": record.target_date,
                    "status": record.status,
                    "payout": record.payout,
                    "verified": record.verified,
                    "verification_status": record.verification_status,
                    "resolution_source": record.resolution_source,
                    "fetched_at_utc": (
                        record.fetched_at_utc.astimezone(UTC).isoformat()
                        if record.fetched_at_utc is not None
                        else None
                    ),
                }
                for record in settlements
            ],
            "capital_usdc": capital_usdc,
            "max_allocation_fraction": max_allocation_fraction,
            "dataset": dataset,
            "source": source,
        }
    )
    return f"{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"


def _write_retraction_backup(
    rows: list[dict[str, Any]],
    *,
    state_db: str | Path,
    source: str | None,
    reason: str | None,
    now: datetime,
) -> Path:
    """Write out the settlement answers a total retraction is about to delete.

    Beside the state database, named for the instant, and returned so the
    caller can print the path. `reason` is recorded as ``None`` when the
    operator did not give one, rather than guessed at: an audit line that
    invents a motive is worse than one that admits it does not have it.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    state_path = Path(state_db)
    path = state_path.with_name(f"{state_path.stem}.retracted-{stamp}.json")
    atomic_write_json(
        path,
        {
            "retracted_at_utc": now.astimezone(UTC).isoformat(),
            "rows_retracted": len(rows),
            "input_source": source,
            "reason": reason,
            "state_db": str(state_path),
            "settlements": rows,
        },
    )
    return path


def build_performance_streaming(
    *,
    export: str | Path,
    out_dir: str | Path,
    dataset: str,
    state_db: str | Path = DEFAULT_PERFORMANCE_STATE_DB_PATH,
    settlements: Sequence[Any] = (),
    settlements_replace: bool = False,
    confirm_empty_retraction: bool = False,
    settlements_source: str | None = None,
    retraction_reason: str | None = None,
    capital_usdc: float = 100.0,
    max_allocation_fraction: float = DEFAULT_MAX_ALLOCATION_FRACTION,
    gap_window_seconds: float = DEFAULT_GAP_WINDOW_SECONDS,
    accepted_state: str = ACCEPT_STATE,
    detail_max_episodes: int = DETAIL_MAX_EPISODES,
    detail_window_days: int = DETAIL_WINDOW_DAYS,
    book_for: Callable[[Episode], Any] | None = None,
    now: datetime | None = None,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    temp_directory: str | Path | None = None,
    reset: bool = False,
    ingest: bool = True,
    publish: bool = True,
    deep_verify: bool = False,
    max_parts_per_run: int | None = None,
    source: str = "weatherquant-research",
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Ingest whatever is new, then publish the six snapshots.

    ``ingest=False`` rebuilds the snapshots from the store without touching the
    export, which is the "partial recompute" path: a settlement that arrives
    days later changes outcomes and therefore the win rate, but it does not
    change a single episode, so nothing has to be re-read.

    ``settlements_replace`` makes the index authoritative rather than additive.
    The default is a union - an index covering today's markets must not erase
    yesterday's answers - but a union cannot express a withdrawal, so
    retraction is an explicit act.
    """
    dataset = _require_dataset(dataset)
    if capital_usdc <= 0:
        raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")
    export_path = Path(export)
    if export_path.is_file():
        raise PerformanceStateError(
            f"the streaming engine consumes the shadow's published parts, not a single "
            f"file ({export_path}). Point --export at the decisions-export directory, or "
            "omit --state-db to use the in-memory builder on a hand-carried file."
        )
    now = now or datetime.now(tz=UTC)

    # Resolved here, and the empty-index refusal raised here, both *before* the
    # store is opened. A rejection that had already ingested parts would have
    # advanced the watermark and recorded a generation, so the run would look
    # like it had done something - and "refused" has to mean nothing moved.
    answers, verified = _settlement_index(settlements)
    if settlements_replace and not answers and not confirm_empty_retraction:
        raise PerformanceStateError(
            "refusing to replace the settlement set with an empty index: that "
            "would retract every answer. An empty index is what a failed "
            "backfill looks like at least as often as it is what a genuine "
            "total retraction looks like - the platform may have been "
            "unreachable, or no market may have matched - and the settlement "
            "layer cannot be rebuilt from the export, because rebuilding it "
            "needs the network. Pass --confirm-empty-retraction to mean it. "
            "Nothing was read, written or retracted."
        )

    with PerformanceStateStore(
        state_db, memory_limit=memory_limit, temp_directory=temp_directory
    ) as store:
        if reset:
            store.reset()
            if progress is not None:
                progress(f"reset {store.path}")

        ingest_result: IngestResult | None = None
        if ingest:
            ingest_result = ingest_export(
                store,
                export_path,
                gap_window_seconds=gap_window_seconds,
                accepted_state=accepted_state,
                max_parts_per_run=max_parts_per_run,
                deep_verify=deep_verify,
                progress=progress,
            )

        # Settlements are upserted, never replaced wholesale: a settlement is an
        # external fact that gets revised, and a run without an index must not
        # erase the answers an earlier run verified. `replace=True` is the
        # caller asserting that this index is *complete*, which is the only way
        # to express a withdrawal - see PerformanceStateStore.replace_settlements.
        if settlements_replace:
            if not answers:
                # The one operation that cannot be undone from anywhere else:
                # the export carries decisions, not settlements, and
                # re-deriving them needs the platform. So everything about to
                # be deleted is written out first, with the count, where the
                # index came from and why - a retraction nobody can reverse is
                # not a retraction, it is a loss. Written before the delete,
                # and written even when there is nothing to delete, so the
                # audit line exists either way.
                backup = _write_retraction_backup(
                    store.load_settlements(),
                    state_db=state_db,
                    source=settlements_source,
                    reason=retraction_reason,
                    now=now,
                )
                if progress is not None:
                    progress(f"retraction backup written to {backup}")
            provenance = _settlement_provenance_map(settlements)
            removed = store.replace_settlements(
                {
                    key: (
                        answer.status,
                        answer.payout,
                        key in verified,
                        provenance.get(key, (None, None, None))[0],
                        provenance.get(key, (None, None, None))[1],
                        provenance.get(key, (None, None, None))[2],
                    )
                    for key, answer in answers.items()
                }
            )
            if progress is not None:
                progress(
                    f"replaced the settlement set: {len(answers)} kept, {removed} retracted"
                )
        elif answers:
            provenance = _settlement_provenance_map(settlements)
            payload = {
                key: (
                    answer.status,
                    answer.payout,
                    key in verified,
                    provenance.get(key, (None, None, None))[0],
                    provenance.get(key, (None, None, None))[1],
                    provenance.get(key, (None, None, None))[2],
                )
                for key, answer in answers.items()
            }
            store.upsert_settlements(payload)

        build = build_streaming_payloads(
            store,
            now=now,
            source=source,
            dataset=dataset,
            capital_usdc=capital_usdc,
            max_allocation_fraction=max_allocation_fraction,
            book_for=book_for,
            gap_window_seconds=gap_window_seconds,
            detail_max_episodes=detail_max_episodes,
            detail_window_days=detail_window_days,
        )
        generation_id = streaming_generation_id(
            now,
            state=build.state,
            coverage=build.coverage,
            settlements=build.settlement_records,
            capital_usdc=capital_usdc,
            max_allocation_fraction=max_allocation_fraction,
            dataset=dataset,
            source=source,
        )

        written: list[Path] = []
        if publish:
            written = publish_performance(
                build.payloads,
                out_dir,
                generation_id=generation_id,
                watermark=build.watermark,
                coverage=build.coverage,
                extra_manifest=build.manifest_extra,
            )

    result = {
        "generation_id": generation_id,
        "written": [str(path) for path in written],
        "watermark": build.watermark,
        "coverage": build.coverage,
        "manifest_extra": build.manifest_extra,
        "summary": build.summary,
        "ingest": ingest_result.as_dict() if ingest_result is not None else None,
    }
    return result


def _settlement_provenance_map(
    settlements: Sequence[Any],
) -> dict[tuple[str, str | None], tuple[str | None, str | None, datetime | None]]:
    """What each settlement index entry says about its own evidence.

    ``(verification_status, resolution_source, fetched_at_utc)`` per
    ``(market, target date)``. Built once rather than searched per key: the
    index is one row per market and date, and looking each one up in the list
    would be quadratic in the number of markets.
    """
    out: dict[tuple[str, str | None], tuple[str | None, str | None, datetime | None]] = {}
    for item in settlements:
        if not isinstance(item, Mapping):
            continue
        market_id = str(item.get("market_id") or "")
        if not market_id:
            continue
        raw = item.get("target_date")
        key = (market_id, str(raw) if raw else None)
        stamp = item.get("fetched_at_utc")
        fetched: datetime | None = None
        if stamp is not None:
            try:
                fetched = _timestamp(stamp)
            except (TypeError, ValueError):
                fetched = None
        verdict = item.get("verification_status")
        source = item.get("resolution_source")
        out[key] = (
            str(verdict) if verdict is not None else None,
            str(source) if source is not None else None,
            fetched,
        )
    return out

"""Historical decision export: the single-writer bridge to the research side.

The performance module could consume an export; nothing produced one. The
shadow engine is the only process allowed to open its own database, so the
export has to be written here - by the holder of the lease, on its own
schedule, without ever letting a slow backfill stall a decision cycle.

Three properties are load-bearing.

**The cursor is `(decision_time_utc, decision_id)`, never a timestamp.**
Timestamps repeat: two decisions in one cycle share an instant, and a
cursor that only stores the instant re-reads or skips one of them.
`decision_id` is a deterministic hash and therefore unique, so pairing it
with the timestamp gives a strict total order. It is *not* itself ordered,
which is why it is the tiebreaker and not the key.

**The parts are the watermark.** Each part records the cursor range it
covers. State is a cache for fast startup, not the authority: if the
process dies between publishing a part and updating state, the next run
re-derives the watermark from the last part instead of re-exporting a
range and duplicating rows. A watermark that advances before the data is
durable is the one failure that silently corrupts a backfill.

**Nothing here is all-or-nothing across the whole history.** One bounded
batch per call, in size and in wall clock, so an initial backfill of a
large table finishes over many cycles rather than blocking one.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "EXPORT_COLUMNS",
    "DecisionExporter",
    "ExportResult",
    "export_watermark",
    "read_export",
    "read_export_with_watermark",
]

#: Everything a decision carries that Episode, Edge, win rate and reference
#: capital need - plus the market's own fee schedule, which the pricing
#: module refuses to assume is zero. Joined in rather than left to the
#: reader, because by the time the reader has it the catalog may have moved.
EXPORT_COLUMNS = (
    "d.decision_id",
    "d.run_id",
    "d.cycle_id",
    "d.market_id",
    "d.event_id",
    "d.token_id",
    "d.bracket_label",
    "d.target_date",
    "d.station",
    "d.direction",
    "d.state",
    "d.primary_reason",
    "d.model_name",
    "d.model_version",
    "d.model_run_id",
    "d.forecast_run_time_utc",
    "d.snapshot_id",
    "d.book_id",
    "d.venue_ts_utc",
    "d.collector_received_at_utc",
    "d.shadow_received_at_utc",
    "d.decision_time_utc",
    "d.snapshot_age_ms",
    "d.model_probability",
    "d.market_price",
    "d.best_bid",
    "d.best_ask",
    "d.spread",
    "d.available_depth_usd",
    "d.net_edge",
    "d.estimated_fees_usd",
    "d.estimated_buffer_usd",
    "d.estimated_cost_usd",
    "d.expected_net_profit_usd",
    "d.proposed_shares",
    "d.estimated_fill_shares",
    "d.estimated_fill_price",
    "d.forecast_age_hours",
    "d.thresholds_version",
    "d.decision_latency_ms",
    "d.created_at_utc",
    "d.mode",
    "d.mode_reason",
    "c.fee_rate",
    "c.fee_exponent",
)


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass(slots=True)
class ExportResult:
    """One bounded batch of export work."""

    rows_exported: int = 0
    parts_written: int = 0
    watermark: tuple[str, str] | None = None
    duration_seconds: float = 0.0
    caught_up: bool = False
    state_file: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_exported": self.rows_exported,
            "parts_written": self.parts_written,
            "watermark": list(self.watermark) if self.watermark else None,
            "duration_seconds": self.duration_seconds,
            "caught_up": self.caught_up,
            "notes": list(self.notes),
        }


class DecisionExporter:
    """Append-only, resumable export of the decision table.

    Owns no lifecycle: the caller supplies the connection it already holds
    and decides when to call. Every method is bounded, so a call that runs
    long is a bug rather than a design.
    """

    def __init__(
        self,
        con: Any,
        directory: str | Path,
        *,
        batch_rows: int = 20_000,
        max_seconds: float = 5.0,
        max_parts_per_run: int | None = None,
    ) -> None:
        if batch_rows <= 0:
            raise ValueError(f"batch_rows must be > 0, got {batch_rows!r}")
        if max_seconds <= 0:
            raise ValueError(f"max_seconds must be > 0, got {max_seconds!r}")
        self.con = con
        self.directory = Path(directory)
        self.batch_rows = batch_rows
        self.max_seconds = max_seconds
        self.max_parts_per_run = max_parts_per_run
        self._cursor: tuple[str, str] | None = None

    # -- state -------------------------------------------------------------

    @property
    def parts_dir(self) -> Path:
        return self.directory / "parts"

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    def _part_path(self, index: int) -> Path:
        return self.parts_dir / f"part-{index:09d}.jsonl"

    def _load(self) -> dict[str, Any]:
        """Read state, then reconcile it against the parts that actually exist.

        State is a cache. If a part was published and the process died
        before state was rewritten, trusting state would re-export that
        range and duplicate every row in it. The last line of the newest
        part carries the cursor it ended on, so the watermark is recovered
        from the durable artifact rather than from the assumption.

        Recovery is written back. It is paid for once rather than on every
        tick: a caught-up run never reaches `_save()` - saving happens after
        writing a part - so a reconciliation left in memory alone would be
        redone forever, rereading the whole export each time.
        """
        state: dict[str, Any] = {
            "next_part": 1,
            "rows_exported": 0,
            "watermark": None,
            "last_run_at_utc": None,
        }
        on_disk: dict[str, Any] | None = None
        if self.state_path.exists():
            # A corrupt state file must not stop the engine; the parts are
            # still the authority and the watermark is re-derived below.
            with contextlib.suppress(OSError, ValueError):
                on_disk = json.loads(self.state_path.read_text(encoding="utf-8"))
                state.update(on_disk)

        self.directory.mkdir(parents=True, exist_ok=True)
        self.parts_dir.mkdir(parents=True, exist_ok=True)

        existing = sorted(self.parts_dir.glob("part-*.jsonl"))
        if existing:
            last = existing[-1]
            end = _end_cursor_of(last)
            if end is not None:
                stored = tuple(state["watermark"]) if state.get("watermark") else None
                if stored is None or _ahead(end, stored):
                    state["watermark"] = list(end)
                # Everything below `accounted` is already inside rows_exported.
                #
                # A state that names parts but counts no rows is not one this
                # can trust - the two are written together - so it is treated as
                # absent and the count is rebuilt.
                accounted = (
                    int(state.get("next_part") or 1) if state.get("rows_exported") else 1
                )
                state["next_part"] = max(accounted, _index_of(last) + 1)
                # Only the parts the state does not already cover are counted.
                #
                # Counting them all means reading every part, and this call
                # happens inside the tick's wall-clock budget - so the count grew
                # with the export until it outran the budget, and every tick broke
                # out before writing anything. The export stalled at 700,000 rows
                # with no error, no watermark move and no quality event, because
                # a tick that writes nothing is indistinguishable from a tick
                # that is caught up.
                #
                # The unaccounted tail is normally empty, and after a crash
                # between publishing a part and saving state it is that one part,
                # so recovery stays exact without a full recount. A state lost
                # entirely counts everything - once: the next write records the
                # total again, and that tick still exports, because the budget is
                # not consulted until a part has been written.
                tail = [path for path in existing if _index_of(path) >= accounted]
                if tail:
                    state["rows_exported"] = int(
                        state.get("rows_exported") or 0
                    ) + _count_rows(tail)
        if on_disk != state:
            self._save(state)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    def state(self) -> dict[str, Any]:
        return self._load()

    # -- export ------------------------------------------------------------

    def run_batch(self) -> ExportResult:
        """Export at most one bounded batch. Never raises into the caller's loop.

        Any failure is returned as a result with notes rather than raised:
        a bad export must be *observable*, not fatal. The engine's decision
        loop is the mission; this is a side channel.
        """
        started = time.monotonic()
        result = ExportResult()
        try:
            state = self._load()
            watermark = tuple(state["watermark"]) if state.get("watermark") else None
            rows_before = int(state.get("rows_exported") or 0)
            index = int(state.get("next_part") or 1)

            parts_written = 0
            rows_written = 0
            while True:
                if self.max_parts_per_run is not None and parts_written >= self.max_parts_per_run:
                    result.notes.append(
                        f"stopped after {parts_written} part(s) by max_parts_per_run"
                    )
                    break
                # The budget bounds the *writing*, so it is only consulted once
                # a part has been written. Checking it first means any slow
                # setup - reading state, recovering the watermark - can consume
                # the whole budget and starve the tick, which is what made a
                # five-second budget stop a five-minute job from ever starting.
                if parts_written and time.monotonic() - started >= self.max_seconds:
                    result.notes.append(
                        f"stopped after {parts_written} part(s) by the "
                        f"{self.max_seconds:g}s budget"
                    )
                    break

                rows = self._fetch_batch(watermark, self.batch_rows)
                if not rows:
                    result.caught_up = True
                    break

                _write_part(self._part_path(index), rows)
                watermark = self._cursor_of(rows[-1])
                rows_written += len(rows)
                parts_written += 1
                index += 1

                # Only now does the watermark move. A part published without
                # its state update is recovered on the next run by re-reading
                # the part's last line; a state update without its part would
                # be data loss that nothing could detect.
                state.update(
                    {
                        "next_part": index,
                        "rows_exported": rows_before + rows_written,
                        "watermark": list(watermark),
                        "last_run_at_utc": datetime.now(tz=UTC).isoformat(),
                    }
                )
                self._save(state)

            result.rows_exported = rows_written
            result.parts_written = parts_written
            result.watermark = watermark  # type: ignore[assignment]
            if result.caught_up and rows_written == 0:
                result.notes.append("already at the watermark; nothing new to export")
        except Exception as exc:  # a side channel must not raise
            # The engine keeps deciding. The caller turns this into an
            # observable state (a quality event) rather than a crash.
            result.notes.append(f"export failed: {type(exc).__name__}: {exc}")
        result.duration_seconds = time.monotonic() - started
        return result

    def _fetch_batch(self, watermark: tuple[str, str] | None, limit: int) -> list[Any]:
        """One strictly ordered page of decisions.

        ``decision_id`` is a hash, so it has no order of its own; it is used
        only to break ties on ``decision_time_utc``. That is what makes the
        cursor total: two decisions recorded in one cycle share an instant,
        and a cursor holding only the instant would re-read or drop one.
        """
        columns = ", ".join(EXPORT_COLUMNS)
        join = " LEFT JOIN market_catalog c ON c.market_id = d.market_id "
        if watermark is None:
            sql = f"SELECT {columns} FROM shadow_decisions d{join}"                 "ORDER BY d.decision_time_utc, d.decision_id LIMIT ?"
            params: list[Any] = [limit]
        else:
            sql = f"SELECT {columns} FROM shadow_decisions d{join}"                 "WHERE d.decision_time_utc > ? "                 "   OR (d.decision_time_utc = ? AND d.decision_id > ?) "                 "ORDER BY d.decision_time_utc, d.decision_id LIMIT ?"
            params = [watermark[0], watermark[0], watermark[1], limit]
        return self.con.execute(sql, params).fetchall()

    @staticmethod
    def _cursor_of(row: Any) -> tuple[str, str]:
        stamp, decision = row[21], row[0]
        if isinstance(stamp, datetime):
            stamp = stamp.astimezone(UTC).isoformat()
        return (str(stamp), str(decision))


    def verify(self) -> dict[str, Any]:
        """Recount what is on disk and compare it with what state claims.

        An export that cannot be reconciled is worse than no export: the
        research side would score against a set of rows nobody can audit.
        """
        state = self._load()
        parts = sorted(self.parts_dir.glob("part-*.jsonl")) if self.parts_dir.exists() else []
        counted = _count_rows(parts)
        claimed = int(state.get("rows_exported") or 0)
        return {
            "parts": len(parts),
            "rows_on_disk": counted,
            "rows_in_state": claimed,
            "consistent": counted == claimed,
            "watermark": state.get("watermark"),
        }


def _index_of(path: Path) -> int:
    try:
        return int(path.stem.split("-")[1])
    except (IndexError, ValueError):
        return 0


def _read_last_line(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return None
            handle.seek(max(size - 65_536, 0), os.SEEK_SET)
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in tail.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _end_cursor_of(path: Path) -> tuple[str, str] | None:
    line = _read_last_line(path)
    if not line:
        return None
    try:
        row = json.loads(line)
    except ValueError:
        return None
    ts, did = row.get("decision_time_utc"), row.get("decision_id")
    if not ts or not did:
        return None
    return (str(ts), str(did))


def _ahead(candidate: tuple[str, str], stored: tuple[str, str]) -> bool:
    return (candidate[0], candidate[1]) > (stored[0], stored[1])


def _count_rows(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            with path.open(encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return total


def _write_part(path: Path, rows: list[Any]) -> Path:
    """Write one immutable part, durably, before anyone may depend on it."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    # delete=False plus an explicit replace: the rename is what makes the
    # publish atomic, and it needs a closed descriptor on Windows.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
    ) as handle:
        for row in rows:
            handle.write(json.dumps(_row_as_dict(row), ensure_ascii=False, default=str))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    Path(temporary).replace(path)
    return path


def _row_as_dict(row: Any) -> dict[str, Any]:
    names = [c.split(".", 1)[-1] for c in EXPORT_COLUMNS]
    return {name: _iso(value) for name, value in zip(names, row, strict=False)}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    Path(temporary).replace(path)


def read_export(directory: str | Path) -> list[dict[str, Any]]:
    """Read every published part, in cursor order.

    The research side's only view of the decision history. Parts are already
    written in order, so this is a concatenation rather than a merge.
    """
    rows, _cursor = read_export_with_watermark(directory)
    return rows


def read_export_with_watermark(
    directory: str | Path,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """Read the parts, and the cursor they reached, from one enumeration.

    Reading them in two passes races the engine, which keeps publishing while
    the research side reads: the second pass can see a part the first did not.
    A manifest built that way advertises a watermark past the last decision
    its snapshots actually contain - claiming coverage of rows it never read.

    Parts are immutable once published, so the last line of the newest one in
    this enumeration is the cursor of exactly the set just read.

    A gap in the part sequence is refused rather than read past. The cursor is
    a high-water mark: with part 3 missing, the remaining parts still produce a
    cursor from part 4 onward, and the manifest would claim coverage through it
    while the snapshots silently omit everything part 3 held. No count in
    `coverage` would reveal which range disappeared, so this is the one case
    where refusing is the only honest answer.
    """
    root = Path(directory) / "parts"
    if not root.exists():
        return [], None
    parts = sorted(root.glob("part-*.jsonl"))
    indices = [_index_of(path) for path in parts]
    if indices != list(range(1, len(indices) + 1)):
        missing = sorted(set(range(1, len(indices) + 1)) - set(indices))
        raise ValueError(
            f"the decisions export has a gap: part index(es) {missing} are "
            f"missing from {root}. A watermark taken from the highest part "
            "would claim coverage of rows that are not here, so the export is "
            "refused rather than published under a cursor it never reached."
        )
    rows: list[dict[str, Any]] = []
    for path in parts:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    if not parts:
        return rows, None
    end = _end_cursor_of(parts[-1])
    return rows, list(end) if end is not None else None


def export_watermark(directory: str | Path) -> list[str] | None:
    """The cursor the export has reached, for a caller that wants only that.

    Not to be paired with a separate `read_export`: the two would enumerate the
    parts at different instants, and the pair could describe a range no single
    read ever covered. A manifest must use
    :func:`read_export_with_watermark` instead.

    Taken from the last line of the newest part rather than from `state.json`,
    for the same reason the writer recovers it that way: state is a cache, and
    a watermark read from a cache would be a claim about the data rather than a
    fact about it.

    ``None`` when there is no export here - a hand-carried JSONL has no cursor,
    and inventing one would put a range in the manifest that nothing backs.
    """
    parts = Path(directory) / "parts"
    if not parts.is_dir():
        return None
    existing = sorted(parts.glob("part-*.jsonl"))
    if not existing:
        return None
    end = _end_cursor_of(existing[-1])
    return list(end) if end is not None else None

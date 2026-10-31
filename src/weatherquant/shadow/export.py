"""Historical decision export: the single-writer bridge to the research side.

The performance module could consume an export; nothing produced one. The
shadow engine is the only process allowed to open its own database, so the
export has to be written here - by the holder of the lease, on its own
schedule, without ever letting a slow backfill stall a decision cycle.

Four properties are load-bearing.

**The positioning cursor is ``export_seq``, a monotonic write sequence.**
Business time is revisable by the data: a REPLAY run re-reads yesterday's
archive and writes decisions whose business instant is in the past, *after*
the export's cursor has already moved past that instant. Positioned on
``(decision_time_utc, decision_id)`` such a row is unreachable forever - it
exists in the database and no batch can ever select it. ``export_seq`` is
claimed once at insert (migration 0005 backfills the history), never moves
on re-write, and every row written now - however old its business time -
lands strictly above the cursor. Within one instant ``decision_id`` still
breaks ties, so rows sharing a timestamp are all exported exactly once.

**The parts are the watermark.** Each part records the cursor range it
covers. State is a cache for fast startup, not the authority: if the
process dies between publishing a part and updating state, the next run
re-derives the watermark from the last part instead of re-exporting a
range and duplicating rows. A watermark that advances before the data is
durable is the one failure that silently corrupts a backfill.

**A v1 export directory is continued, not discarded.** Old parts carry no
``export_seq`` and were selected in business-time order, so their rows may
interleave - in sequence space - with rows the v1 cursor silently skipped.
The one-time transition scans the published parts for the ids they actually
contain, floors the new cursor at the highest sequence among them, and
re-sequences the skipped rows above that floor. No part is rewritten, the
production cursor is never cleared, and the work is bounded per tick. See
:meth:`DecisionExporter._transition_step`.

**Nothing here is all-or-nothing across the whole history.** One bounded
batch per call, in rows and in wall clock, so an initial backfill of a
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
#: ``export_seq`` rides along as the row's position in the write sequence:
#: the research side uses it to validate part order and to tell a late row
#: (business time below what was already consumed) from a reordered one.
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
    "d.export_seq",
)

#: Column offsets used to read a fetched row without naming it twice.
_SEQ_INDEX = EXPORT_COLUMNS.index("d.export_seq")
_TIME_INDEX = EXPORT_COLUMNS.index("d.decision_time_utc")
_ID_INDEX = EXPORT_COLUMNS.index("d.decision_id")

#: The state schema this module writes. State without it was written by the
#: v1 business-time cursor and must go through the one-time transition before
#: any v2 batch runs against it.
_STATE_VERSION = 2

#: How many published parts the one-time transition scans per tick, as a
#: ceiling on top of the wall-clock budget. A part holds at most
#: ``batch_rows`` lines, so this bounds the per-tick work the same way the
#: batch itself is bounded.
_TRANSITION_PARTS_PER_TICK = 8

#: The table the transition accumulates published ids in. It is a *real*
#: table, not a temp one: the scan must survive an engine restart without
#: rescanning from part 1 - at production scale the rescan alone cost more
#: per restart than the scan itself - and the ids are only needed until the
#: transition completes, at which point the table is dropped. It is runtime
#: state owned by the exporter, like the export directory's state.json, and
#: deliberately not part of the migration schema.
_TRANSITION_IDS_TABLE = "_export_transition_ids"


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
    cursor_seq: int | None = None
    late_rows: int = 0
    transition_done: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_exported": self.rows_exported,
            "parts_written": self.parts_written,
            "watermark": list(self.watermark) if self.watermark else None,
            "duration_seconds": self.duration_seconds,
            "caught_up": self.caught_up,
            "cursor_seq": self.cursor_seq,
            "late_rows": self.late_rows,
            "transition_done": self.transition_done,
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
            "cursor_seq": None,
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
                # The business watermark is the *furthest* instant the export
                # reached, so a late row in a newer part must not drag it
                # backwards; `max` rather than "the newest part wins".
                if stored is None or end > stored:
                    state["watermark"] = list(end)
            end_seq = _end_seq_of(last)
            if end_seq is not None:
                stored_seq = state.get("cursor_seq")
                if stored_seq is None or int(end_seq) > int(stored_seq):
                    state["cursor_seq"] = int(end_seq)
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

    # -- the one-time v1 -> v2 transition -----------------------------------

    def _transition_pending(self, state: dict[str, Any]) -> bool:
        return int(state.get("version") or 1) < _STATE_VERSION

    def _repair_rollback_tail(self, state: dict[str, Any]) -> list[str]:
        """Pin the rows a rolled-back image exported, so they export once.

        The supported rollback path is "run the old image, then upgrade
        again". The old image writes rows with ``export_seq IS NULL`` and
        publishes them in v1-format parts - no sequence on the line -
        appended *after* the v2 parts this exporter already wrote. Left
        alone, the v2 exporter would later repair those NULLs to fresh,
        above-cursor values and export every one of them a second time; the
        acceptance drill measured exactly that (15 duplicated lines, and a
        downstream consumer that refuses the mixed format forever).

        The repair pins each such row's sequence to the cursor: a value at
        or below the cursor means "already exported", which is precisely
        what the v1 part on disk says about it. The pin is deliberately the
        cursor value itself rather than a fresh distinct number - nothing
        ever queries the interior of the exported prefix, and inventing
        free values below the floor would suggest an ordering that never
        existed. Idempotent (only NULL rows are touched), bounded per part,
        and recorded in the state so the repair is auditable after the fact.
        """
        cursor = state.get("cursor_seq")
        if cursor is None:
            # v2 parts exist but the state lost its cursor: the transition
            # will recover it from the parts, and the repair runs next tick.
            return []
        parts = sorted(self.parts_dir.glob("part-*.jsonl"))
        for index in range(len(parts) - 1, -1, -1):
            if _end_seq_of(parts[index]) is not None:
                break
        else:
            # A pure v1 history: the one-time transition owns it.
            return []
        cursor = int(cursor)
        pinned = 0
        tail = parts[index + 1 :]
        for path in tail:
            row = self.con.execute(
                "SELECT COUNT(*) FROM shadow_decisions d WHERE d.export_seq IS NULL "
                "AND d.decision_id IN (SELECT decision_id FROM read_json_auto(?, "
                "columns={'decision_id': 'VARCHAR'}))",
                [str(path)],
            ).fetchone()
            if int(row[0]):
                self.con.execute(
                    "UPDATE shadow_decisions SET export_seq = ? "
                    "WHERE export_seq IS NULL AND decision_id IN ("
                    "SELECT decision_id FROM read_json_auto(?, "
                    "columns={'decision_id': 'VARCHAR'}))",
                    [cursor, str(path)],
                )
                pinned += int(row[0])
        if not pinned:
            # The tail is already pinned (a previous tick repaired it); the
            # parts stay on disk forever, so re-reporting every tick would
            # turn a one-time event into noise. The state marker written by
            # the first repair is the durable record.
            return []
        state["last_rollback_repair"] = {
            "parts": len(tail),
            "rows_pinned": pinned,
            "cursor_seq": cursor,
            "at_utc": datetime.now(tz=UTC).isoformat(),
        }
        self._save(state)
        return [
            f"rollback repair: pinned {pinned} row(s) from {len(tail)} v1 "
            f"part(s) written by a rolled-back image at cursor_seq={cursor}"
        ]

    def _transition_step(self, state: dict[str, Any]) -> ExportResult | None:
        """Continue the one-time move from the business-time cursor to ``export_seq``.

        A v1 export selected rows in business-time order, so the rows it
        skipped - the late REPLAY rows this whole change exists for - sit
        *interleaved* with exported rows in sequence space. No single
        sequence floor can both exclude the exported rows and include the
        skipped ones, so the skipped rows are moved above the floor instead:
        re-sequenced to fresh, highest values that position them as ordinary
        pending rows. The published parts are never rewritten.

        The floor is derived from what the parts actually contain, not from
        the v1 watermark: the watermark only says what range was *claimed*,
        and the defect being fixed is precisely that the claim skipped rows.

        Bounded per tick by the wall clock first and the part ceiling second:
        the deadline is checked before every part and before the
        re-sequencing pass, so a tick of this side channel can never block
        the decision loop the way the first implementation did (240s+ per
        tick against a 700k-row history, 18 minutes end to end). Part ids
        are loaded set-based with ``read_json`` rather than row-by-row
        executemany, which is where those 240s went.

        The ids accumulate in a *real* table, so a restart resumes from the
        recorded part instead of rescanning; the one unreachable case - the
        table dropped by hand while progress claims a scan - resets to part
        1 rather than trusting a floor over vanished ids. Every step is
        idempotent, so a crash mid-transition leaves nothing half-applied
        that a rerun would mishandle.

        Returns ``None`` only for the empty-directory case: there is nothing
        to scan and nothing to floor, so the transition completes inline and
        the caller's tick may go straight on to exporting.
        """
        started = time.monotonic()
        result = ExportResult()
        self.con.execute(
            f"CREATE TABLE IF NOT EXISTS {_TRANSITION_IDS_TABLE} "
            "(decision_id VARCHAR PRIMARY KEY)"
        )
        scanned = int(self.con.execute(
            f"SELECT COUNT(*) FROM {_TRANSITION_IDS_TABLE}"
        ).fetchone()[0])

        progress = state.get("transition") or {}
        phase = progress.get("phase") or "scan_parts"
        next_scan = int(progress.get("next_part") or 1)
        if next_scan > 1 and scanned == 0:
            # Progress claims a scan whose accumulated ids are gone (the
            # table was dropped by hand). The floor must never be derived
            # over ids that are not there, so the scan restarts instead.
            next_scan = 1

        existing = sorted(self.parts_dir.glob("part-*.jsonl"))

        if phase == "scan_parts" and not existing:
            # Nothing was ever published: there is nothing exported to floor
            # the cursor at and nothing to reconcile. Start the v2 export
            # from the beginning of the sequence, and let this tick carry on
            # into the first batch - the transition cost nothing, so holding
            # the tick back would only add a pointless empty call.
            state["version"] = _STATE_VERSION
            state["cursor_seq"] = 0
            state["transition"] = {
                "phase": "done",
                "exported_ids": 0,
                "floor_seq": 0,
                "rows_resequenced": 0,
                "completed_at_utc": datetime.now(tz=UTC).isoformat(),
            }
            self._save(state)
            self.con.execute(f"DROP TABLE IF EXISTS {_TRANSITION_IDS_TABLE}")
            return None

        did_scan_work = False
        over_budget_after_scan = False
        if phase == "scan_parts":
            remaining = existing[next_scan - 1 :]
            for batch_start in range(0, len(remaining), _TRANSITION_PARTS_PER_TICK):
                batch = remaining[batch_start : batch_start + _TRANSITION_PARTS_PER_TICK]
                # At least one batch per tick, then the deadline governs: a
                # transition that makes no progress every call would look
                # exactly like a stall, which is the failure this budget
                # exists to prevent. One statement loads the whole batch -
                # read_json takes a list of paths - because a statement per
                # part is a transaction per part, and on the acceptance
                # hardware those commits dominated the tick.
                if did_scan_work and time.monotonic() - started >= self.max_seconds:
                    result.notes.append(
                        f"transition: stopped the scan at the {self.max_seconds:g}s budget"
                    )
                    break
                # For tiny parts the setup cost of a DuckDB JSON reader per
                # file dominates. Keep this in-memory path bounded by bytes.
                if sum(path.stat().st_size for path in batch) <= 256 * 1024:
                    ids = []
                    for path in batch:
                        with path.open("rb") as lines:
                            ids.extend(json.loads(line).get("decision_id") for line in lines if line.strip())
                    self.con.execute(
                        f"INSERT OR IGNORE INTO {_TRANSITION_IDS_TABLE} "
                        "SELECT id FROM unnest(?::VARCHAR[]) AS t(id) WHERE id IS NOT NULL",
                        [ids],
                    )
                else:
                    self.con.execute(
                        f"INSERT OR IGNORE INTO {_TRANSITION_IDS_TABLE} "
                        "SELECT DISTINCT decision_id FROM read_json_auto(?, "
                        "columns={'decision_id': 'VARCHAR'}) WHERE decision_id IS NOT NULL",
                        [[str(path) for path in batch]],
                    )
                next_scan += len(batch)
                did_scan_work = True
            state["transition"] = {"phase": "scan_parts", "next_part": next_scan}
            self._save(state)
            if next_scan <= len(existing):
                result.notes.append(
                    f"transition: scanned {min(next_scan - 1, len(existing))}"
                    f"/{len(existing)} part(s)"
                )
                result.duration_seconds = time.monotonic() - started
                return result
            state["transition"] = {"phase": "finalize"}
            self._save(state)
            over_budget_after_scan = (
                did_scan_work and time.monotonic() - started >= self.max_seconds
            )

        # -- finalize: floor, re-sequence the skipped rows, switch to v2 ----
        floor_row = self.con.execute(
            "SELECT COALESCE(MAX(d.export_seq), 0) FROM shadow_decisions d "
            f"JOIN {_TRANSITION_IDS_TABLE} e ON e.decision_id = d.decision_id"
        ).fetchone()
        floor = int(floor_row[0])

        # The re-sequencing loop assumes nextval lands above the floor; if the
        # sequence head somehow sits below it (a recreated sequence, a skipped
        # migration step), every reassigned row would still match the
        # selection and the loop would spin. One probe value tells us where
        # the head is; if it is too low, the difference is consumed so the
        # next nextval is floor + 1. The probe itself becomes a harmless gap.
        probe = int(self.con.execute(
            "SELECT nextval('seq_shadow_decision_export')"
        ).fetchone()[0])
        if probe <= floor:
            self.con.execute(
                "SELECT nextval('seq_shadow_decision_export') FROM range(?)",
                [floor - probe],
            )

        reassigned = 0
        if over_budget_after_scan:
            # The scan used this tick's budget: defer the finalize pass to a
            # tick of its own rather than overrun. Entry with phase already
            # 'finalize' does the work immediately - the pass is one bounded
            # statement now, and deferring it forever on a small budget was
            # the first version's stall (a fresh tick must make progress).
            state["transition"] = {
                "phase": "finalize",
                "floor_seq": None,
                "rows_resequenced": 0,
            }
            self._save(state)
            result.notes.append(
                "transition: re-sequencing deferred to the next tick by the "
                f"{self.max_seconds:g}s budget"
            )
            result.duration_seconds = time.monotonic() - started
            return result
        # One set-based pass, not a per-row loop: the selection and the
        # UPDATE are each a single statement, so a large skipped set costs
        # one query instead of one executemany per chunk. A re-sequenced row
        # moves above the floor and stops matching, which is what makes a
        # rerun after a crash exact rather than approximate.
        missed_row = self.con.execute(
            "SELECT COUNT(*) FROM shadow_decisions d "
            "WHERE d.export_seq IS NOT NULL AND d.export_seq <= ? AND NOT EXISTS ("
            f"SELECT 1 FROM {_TRANSITION_IDS_TABLE} e "
            "WHERE e.decision_id = d.decision_id)",
            [floor],
        ).fetchone()
        reassigned = int(missed_row[0])
        if reassigned:
            self.con.execute(
                "UPDATE shadow_decisions SET export_seq = "
                "nextval('seq_shadow_decision_export') "
                "WHERE export_seq IS NOT NULL AND export_seq <= ? AND NOT EXISTS ("
                f"SELECT 1 FROM {_TRANSITION_IDS_TABLE} e "
                "WHERE e.decision_id = shadow_decisions.decision_id)",
                [floor],
            )

        exported_ids = int(self.con.execute(
            f"SELECT COUNT(*) FROM {_TRANSITION_IDS_TABLE}"
        ).fetchone()[0])

        watermark = state.get("watermark")
        state["version"] = _STATE_VERSION
        state["cursor_seq"] = floor
        if watermark is None:
            # A lost state file has no business watermark left; the furthest
            # exported instant is recoverable from the table the same way the
            # parts themselves would tell it.
            furthest = self.con.execute(
                "SELECT d.decision_time_utc, d.decision_id FROM shadow_decisions d "
                f"JOIN {_TRANSITION_IDS_TABLE} e ON e.decision_id = d.decision_id "
                "ORDER BY d.decision_time_utc DESC, d.decision_id DESC LIMIT 1"
            ).fetchone()
            if furthest is not None:
                watermark = list(_cursor_strings(furthest[0], furthest[1]))
                state["watermark"] = watermark
        state["transition"] = {
            "phase": "done",
            "parts_scanned": len(existing),
            "exported_ids": exported_ids,
            "floor_seq": floor,
            "rows_resequenced": reassigned,
            "completed_at_utc": datetime.now(tz=UTC).isoformat(),
        }
        self._save(state)
        self.con.execute(f"DROP TABLE IF EXISTS {_TRANSITION_IDS_TABLE}")
        result.transition_done = True
        result.cursor_seq = floor
        result.notes.append(
            f"transition complete: floor_seq={floor}, "
            f"{reassigned} skipped row(s) re-sequenced above it"
        )
        result.duration_seconds = time.monotonic() - started
        return result

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
            repair_notes = self._repair_rollback_tail(state)
            result.notes.extend(repair_notes)
            if self._transition_pending(state):
                # A tick that moves the transition does only that. Running a
                # normal batch in the same call would swallow the transition
                # result, and the caller could no longer tell "the one-time
                # work finished" from "an ordinary batch ran" - the runner
                # records the completion as a quality event, which only this
                # return can carry. The empty-directory case returns None:
                # its transition is free, so the tick goes straight on.
                step = self._transition_step(state)
                if step is not None:
                    step.notes = repair_notes + step.notes
                    return step
                result.transition_done = True

            cursor_seq = state.get("cursor_seq")
            cursor_seq = int(cursor_seq) if cursor_seq is not None else 0
            previous_watermark = (
                tuple(state["watermark"]) if state.get("watermark") else None
            )
            rows_before = int(state.get("rows_exported") or 0)
            index = int(state.get("next_part") or 1)

            parts_written = 0
            rows_written = 0
            late_rows = 0
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

                self._assign_missing_seq()
                rows = self._fetch_batch(cursor_seq, self.batch_rows)
                if not rows:
                    result.caught_up = True
                    break

                _write_part(self._part_path(index), rows)
                cursor_seq = int(rows[-1][_SEQ_INDEX])
                rows_written += len(rows)
                late_rows += _count_late_rows(rows, previous_watermark)
                parts_written += 1
                index += 1

                # Only now does the watermark move. A part published without
                # its state update is recovered on the next run by re-reading
                # the part's last line; a state update without its part would
                # be data loss that nothing could detect.
                stored = tuple(state["watermark"]) if state.get("watermark") else None
                for row in rows:
                    business = _cursor_strings(row[_TIME_INDEX], row[_ID_INDEX])
                    if stored is None or business > stored:
                        stored = business
                if stored is not None:
                    state["watermark"] = list(stored)
                state.update(
                    {
                        "version": _STATE_VERSION,
                        "next_part": index,
                        "rows_exported": rows_before + rows_written,
                        "cursor_seq": cursor_seq,
                        "last_run_at_utc": datetime.now(tz=UTC).isoformat(),
                    }
                )
                self._save(state)

            result.rows_exported = rows_written
            result.parts_written = parts_written
            result.late_rows = late_rows
            result.cursor_seq = cursor_seq
            result.watermark = (
                tuple(state["watermark"]) if state.get("watermark") else None
            )
            if result.caught_up and rows_written == 0:
                result.notes.append("already at the watermark; nothing new to export")
        except Exception as exc:  # a side channel must not raise
            # The engine keeps deciding. The caller turns this into an
            # observable state (a quality event) rather than a crash.
            result.notes.append(f"export failed: {type(exc).__name__}: {exc}")
        result.duration_seconds = time.monotonic() - started
        return result

    def _assign_missing_seq(self) -> None:
        """Give sequence values to rows written without one, in write order.

        Rows can lack ``export_seq`` only when they were written by an image
        that predates migration 0005 - typically an operator rolled the
        engine back and forward again. The repair is bounded by the batch
        size, ordered by ``created_at_utc`` so it follows write order, and
        idempotent: an interrupted repair simply leaves NULLs for the next
        batch to continue.
        """
        try:
            missing = self.con.execute(
                "SELECT decision_id FROM shadow_decisions WHERE export_seq IS NULL "
                "ORDER BY created_at_utc, decision_id LIMIT ?",
                [self.batch_rows],
            ).fetchall()
        except Exception:
            # A database without the column at all (pre-0005, exporter run
            # against it by mistake) must not take the engine down; the fetch
            # below will fail the same way and be reported as a failed batch.
            return
        if not missing:
            return
        self.con.executemany(
            "UPDATE shadow_decisions SET export_seq = "
            "nextval('seq_shadow_decision_export') WHERE decision_id = ?",
            [[row[0]] for row in missing],
        )

    def _fetch_batch(self, cursor_seq: int, limit: int) -> list[Any]:
        """One strictly ordered page of decisions, positioned by write sequence.

        ``export_seq`` is unique and claimed at insert, so ordering by it
        alone is a strict total order: a row re-written after export keeps
        its sequence and is never fetched again, and a row written late -
        however far its business time lies in the past - carries a fresh,
        high sequence and is always reachable above the cursor.
        """
        columns = ", ".join(EXPORT_COLUMNS)
        join = " LEFT JOIN market_catalog c ON c.market_id = d.market_id "
        sql = (
            f"SELECT {columns} FROM shadow_decisions d{join}"
            "WHERE d.export_seq > ? "
            "ORDER BY d.export_seq, d.decision_id LIMIT ?"
        )
        return self.con.execute(sql, [cursor_seq, limit]).fetchall()

    # -- reconciliation ------------------------------------------------------

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
            "cursor_seq": state.get("cursor_seq"),
            "state_version": state.get("version"),
        }

    def reconcile(self) -> dict[str, Any]:
        """Prove the export against the source table: nothing lost, nothing twice.

        Check the counts, sequence order and identity of the exported prefix.
        Every ID must occur exactly once on disk and exist in the source
        prefix. For v2 lines its sequence must also match the source row.
        """
        state = self._load()
        cursor_seq = state.get("cursor_seq")
        parts = sorted(self.parts_dir.glob("part-*.jsonl"))
        report: dict[str, Any] = {
            "parts": len(parts),
            "watermark": state.get("watermark"),
            "state_version": state.get("version"),
            "cursor_seq": cursor_seq,
            "rows_in_state": int(state.get("rows_exported") or 0),
            "source_rows_unsequenced": None,
            "source_rows_at_or_below_cursor": None,
            "rows_on_disk": None,
            "max_seq_on_disk": None,
            "seq_monotone": None,
            "legacy_lines": 0,
            "first_divergence": None,
            "consistent": False,
        }
        if int(state.get("version") or 1) < _STATE_VERSION:
            report["first_divergence"] = (
                "state is still at v1 (business-time cursor); the one-time "
                "transition has not completed"
            )
            return report

        try:
            unsequenced = self.con.execute(
                "SELECT COUNT(*) FROM shadow_decisions WHERE export_seq IS NULL"
            ).fetchone()[0]
            at_or_below = self.con.execute(
                "SELECT COUNT(*) FROM shadow_decisions WHERE export_seq <= ?",
                [int(cursor_seq or 0)],
            ).fetchone()[0]
        except Exception as exc:
            report["first_divergence"] = (
                f"cannot read the source table: {type(exc).__name__}: {exc}"
            )
            return report
        report["source_rows_unsequenced"] = int(unsequenced)
        report["source_rows_at_or_below_cursor"] = int(at_or_below)

        rows_on_disk = 0
        max_seq: int | None = None
        monotone = True
        divergence: str | None = None
        for path in parts:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    rows_on_disk += 1
                    try:
                        row = json.loads(line)
                    except ValueError:
                        monotone = False
                        divergence = f"{path.name}: a line is not valid JSON"
                        break
                    seq = row.get("export_seq")
                    if seq is None:
                        # Legacy lines predate the column. Leading ones are
                        # the pre-transition history; trailing ones were
                        # written by a rolled-back image and their rows are
                        # pinned under the cursor (see _repair_rollback_tail).
                        # Either way they are legal *if* their ids sit in the
                        # exported prefix - verified against the source below,
                        # which is the honest check; position alone was not.
                        report["legacy_lines"] += 1
                        continue
                    seq = int(seq)
                    if max_seq is not None and seq <= max_seq:
                        monotone = False
                        divergence = (
                            f"{path.name}: export_seq {seq} follows {max_seq}; "
                            "the parts are not strictly increasing in write order"
                        )
                        break
                    max_seq = seq
                if divergence:
                    break
        report["rows_on_disk"] = rows_on_disk
        report["max_seq_on_disk"] = max_seq
        report["seq_monotone"] = monotone

        if divergence:
            report["first_divergence"] = divergence
        elif not monotone:
            report["first_divergence"] = "parts are not monotone in export_seq"
        elif rows_on_disk != int(state.get("rows_exported") or 0):
            report["first_divergence"] = (
                f"state claims {state.get('rows_exported')} row(s) but the parts "
                f"hold {rows_on_disk}"
            )
        elif int(at_or_below) != rows_on_disk:
            report["first_divergence"] = (
                f"the source holds {at_or_below} row(s) at or below cursor_seq "
                f"{cursor_seq} but the parts hold {rows_on_disk}; rows were lost "
                "or duplicated between the database and the export"
            )
        elif max_seq is not None and int(cursor_seq or 0) != max_seq:
            report["first_divergence"] = (
                f"state cursor_seq is {cursor_seq} but the newest part ends at "
                f"{max_seq}"
            )
        elif rows_on_disk:
            mismatch = self.con.execute(
                """
                WITH exported AS (
                    SELECT decision_id, COUNT(*) AS copies, MAX(export_seq) AS seq
                    FROM read_json(?, format='newline_delimited',
                                   columns={decision_id: 'VARCHAR', export_seq: 'BIGINT'})
                    GROUP BY decision_id
                )
                SELECT e.decision_id, e.copies, e.seq, d.export_seq
                FROM exported e LEFT JOIN shadow_decisions d USING (decision_id)
                WHERE e.copies <> 1 OR d.export_seq IS NULL OR d.export_seq > ?
                   OR (e.seq IS NOT NULL AND e.seq <> d.export_seq)
                LIMIT 1
                """,
                [[str(path) for path in parts], int(cursor_seq or 0)],
            ).fetchone()
            if mismatch:
                decision_id, copies, disk_seq, source_seq = mismatch
                report["first_divergence"] = (
                    f"decision_id {decision_id!r}: {copies} line(s), exported seq "
                    f"{disk_seq}, source seq {source_seq}, cursor {cursor_seq}"
                )
        report["consistent"] = report["first_divergence"] is None
        return report


def _cursor_strings(stamp: Any, decision: Any) -> tuple[str, str]:
    if isinstance(stamp, datetime):
        stamp = stamp.astimezone(UTC).isoformat()
    return (str(stamp), str(decision))


def _count_late_rows(rows: list[Any], watermark: tuple[str, str] | None) -> int:
    """Rows whose business cursor sits at or below the pre-batch watermark.

    The count makes late REPLAY delivery observable without reading
    anything twice: after the batch, the operator (and the tests) can see
    how many rows arrived out of business order, rather than inferring it
    from a watermark that no longer moves monotonically.
    """
    if watermark is None:
        return 0
    return sum(
        1
        for row in rows
        if _cursor_strings(row[_TIME_INDEX], row[_ID_INDEX]) <= watermark
    )


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


def _last_row_of(path: Path) -> dict[str, Any] | None:
    line = _read_last_line(path)
    if not line:
        return None
    with contextlib.suppress(ValueError):
        return json.loads(line)
    return None


def _end_cursor_of(path: Path) -> tuple[str, str] | None:
    row = _last_row_of(path)
    if not row:
        return None
    ts, did = row.get("decision_time_utc"), row.get("decision_id")
    if not ts or not did:
        return None
    return (str(ts), str(did))


def _end_seq_of(path: Path) -> int | None:
    """The write-sequence position a part ended at, for v2 recovery.

    ``None`` for v1 parts (they predate the column) and for anything
    unreadable; the caller decides what that means.
    """
    row = _last_row_of(path)
    if not row:
        return None
    seq = row.get("export_seq")
    if seq is None:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return int(seq)
    return None


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

    The returned watermark is the business cursor of the newest part's last
    line. Since v2 parts are ordered by write sequence, a part holding late
    rows can end at an earlier business instant than its predecessor; the
    watermark still describes exactly where this enumeration stopped.
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

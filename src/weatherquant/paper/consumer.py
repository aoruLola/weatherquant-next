"""The LIVE signal consumer: the paper engine's only door, and how it locks.

The shadow engine publishes its decision history as append-only JSONL parts
(:mod:`weatherquant.shadow.export`). This module reads those parts - *only*
those parts - and turns their rows into recorded signals, refusals and
quality events. It never opens ``shadow.duckdb``: the export exists precisely
so that a reader does not have to.

Four properties are load-bearing.

**The cursor advances with its effects, in one transaction.** Each bounded
batch commits the signal rows it produced and the ``(part, lines)`` position
it read them from together. A crash between the two either replays the batch
or advances nothing - and because a signal's primary key is the shadow's own
deterministic ``decision_id``, a replay inserts nothing new. "The same
signal must not become a second trade" survives any crash interleaving.

**Parts are immutable and contiguity is checked, not assumed.** The exporter
writes ``part-000000001.jsonl`` upward, once each. A missing index means
someone lost a file, and a cursor that read past the gap would silently
claim decisions that are not in the account. A gap is a loud refusal.

**Malformed is recorded, never fatal, never skipped-quietly.** A line that
does not parse becomes a quality event and the cursor moves past it - one
bad row must not wedge the channel, and it must not vanish either.

**Mode is the gate.** Only ``LIVE`` + ``ACCEPT`` may enter the account.
``REPLAY`` rows are refused with ``NOT_LIVE`` and *recorded*: the refusal is
an auditable outcome, not an absence.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.paper.events import (
    EventError,
    Mode,
    RefusalReason,
    SignalEvent,
    parse_signal_event,
)
from weatherquant.paper.store import PaperStore

__all__ = [
    "CURSOR_KEY",
    "ConsumerResult",
    "ExportGapError",
    "ExportReader",
    "SignalConsumer",
]

log = logging.getLogger("weatherquant.paper")

#: The ``paper_state`` key the consumer's read cursor lives under.
CURSOR_KEY = "consume_cursor"


class ExportGapError(RuntimeError):
    """The published part sequence has a hole in it.

    Refused rather than read past: the cursor is a high-water mark, and
    reading past a missing part would put decisions in the account that the
    export never carried while the cursor claimed them.
    """

    def __init__(self, missing: list[int], directory: Path) -> None:
        self.missing = missing
        super().__init__(
            f"the decisions export has a gap: part index(es) {missing} are missing "
            f"from {directory}. Reading past a gap would count decisions the "
            "export does not contain, so the consumer refuses and waits."
        )


def quality_event_id(*, kind: str, path: str, line_no: int | None, detail: str) -> str:
    """Deterministic id: replaying the same batch re-inserts nothing."""
    basis = "|".join([kind, path, str(line_no if line_no is not None else ""), detail])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Cursor:
    """Read position in the published export.

    ``part`` is the next part index to read (1-based, matching the
    exporter's naming); ``lines`` is how many lines of that part are already
    consumed. ``(1, 0)`` is the beginning.
    """

    part: int = 1
    lines: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"part": self.part, "lines": self.lines}

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> Cursor:
        if not value:
            return cls()
        return cls(part=int(value.get("part") or 1), lines=int(value.get("lines") or 0))


class ExportReader:
    """Read published decisions-export parts from a cursor, bounded.

    The reader owns *no* durable state: it takes a :class:`Cursor` and
    returns lines plus the next cursor. Keeping it stateless is what lets
    the consumer commit the cursor and the effects of the lines in one
    transaction - a stateful reader would be a second cursor to keep in
    sync.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    @property
    def parts_dir(self) -> Path:
        return self.directory / "parts"

    def list_parts(self) -> list[Path]:
        """Every published part, ordered, with contiguity enforced.

        A gap raises :class:`ExportGapError` rather than being read past -
        see the class docstring. Parts below index 1 (should never exist)
        are ignored the way the exporter's reader ignores them.
        """
        if not self.parts_dir.is_dir():
            return []
        parts = sorted(self.parts_dir.glob("part-*.jsonl"))
        indices = [_index_of(path) for path in parts]
        expected = list(range(1, len(indices) + 1))
        if indices != expected:
            missing = sorted(set(expected) - set(indices))
            raise ExportGapError(missing, self.parts_dir)
        return parts

    def read_from(self, cursor: Cursor, limit: int) -> tuple[list[tuple[int, str, int]], Cursor]:
        """Read up to ``limit`` lines from the cursor onward.

        Returns ``(items, next_cursor)`` where each item is
        ``(part_index, line_text, line_no)`` with 1-based ``line_no``.
        """
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit!r}")
        items: list[tuple[int, str, int]] = []
        position = cursor
        for path in self.list_parts():
            index = _index_of(path)
            if index < position.part:
                continue
            consumed_in_part = position.lines if index == position.part else 0
            took_from_this_part = False
            with path.open(encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if line_no <= consumed_in_part:
                        continue
                    if len(items) >= limit:
                        break
                    text = line.rstrip("\n")
                    if not text.strip():
                        # The exporter never writes blank lines; one here is
                        # not a record, and the cursor skips it with the
                        # rest.
                        consumed_in_part = line_no
                        took_from_this_part = True
                        continue
                    items.append((index, text, line_no))
                    consumed_in_part = line_no
                    took_from_this_part = True
            if took_from_this_part:
                position = Cursor(part=index, lines=consumed_in_part)
            if len(items) >= limit:
                break
        return items, position


@dataclass
class ConsumerResult:
    """One bounded consumption pass, in counts an operator can read."""

    lines_read: int = 0
    signals_recorded: int = 0
    accepted: int = 0
    refused: int = 0
    duplicates: int = 0
    malformed: int = 0
    quality_events: int = 0
    caught_up: bool = False
    cursor: Cursor = field(default_factory=Cursor)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines_read": self.lines_read,
            "signals_recorded": self.signals_recorded,
            "accepted": self.accepted,
            "refused": self.refused,
            "duplicates": self.duplicates,
            "malformed": self.malformed,
            "quality_events": self.quality_events,
            "caught_up": self.caught_up,
            "cursor": self.cursor.as_dict(),
            "notes": list(self.notes),
        }


class SignalConsumer:
    """Consume published decision events into the paper engine's records.

    One instance per process, over one open :class:`PaperStore`. The account
    must already exist (``weatherquant-paper init``): signals are accepted
    *into* an account, and an engine without one is misconfigured rather
    than free-form.
    """

    def __init__(
        self,
        store: PaperStore,
        *,
        events_dir: str | Path,
        max_signal_age_seconds: float,
        clock: Any = None,
        batch_lines: int = 5000,
    ) -> None:
        if max_signal_age_seconds <= 0:
            raise ValueError("max_signal_age_seconds must be > 0")
        self.store = store
        self.reader = ExportReader(events_dir)
        self.max_signal_age_seconds = float(max_signal_age_seconds)
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.batch_lines = int(batch_lines)

    # -- one pass -----------------------------------------------------------

    def consume_once(self) -> ConsumerResult:
        """Read at most one bounded batch. Never raises into the caller's loop.

        A gap in the export, a vanished directory, a disk error - each is
        returned as a result with notes rather than raised: consumption is a
        side channel of the engine, and its failures must be observable, not
        fatal.
        """
        result = ConsumerResult()
        try:
            stored = Cursor.from_dict(self.store.load_state(CURSOR_KEY))
            result.cursor = stored
            items, next_cursor = self.reader.read_from(stored, self.batch_lines)
            if not items:
                result.caught_up = True
                return result
            result.lines_read = len(items)
            batch = ConsumerResult(cursor=next_cursor, lines_read=len(items))
            now = self.clock()
            quality: list[tuple[str, str, str | None, int | None, str]] = []
            with self.store.db.transaction():
                for part_index, text, line_no in items:
                    self._consume_line(part_index, text, line_no, now, batch, quality)
                for kind, severity, path, line, detail in quality:
                    self.store.record_quality_event(
                        kind=kind,
                        severity=severity,
                        detail=detail,
                        path=path,
                        line_no=line,
                        ts_utc=now,
                    )
                batch.quality_events = len(quality)
                self.store.save_state(CURSOR_KEY, next_cursor.as_dict(), now=now)
            return batch
        except ExportGapError as exc:
            result.notes.append(f"export gap: {exc}")
        except Exception as exc:  # a side channel must not raise
            result.notes.append(f"consume failed: {type(exc).__name__}: {exc}")
        return result

    # -- one line -----------------------------------------------------------

    def _consume_line(
        self,
        part_index: int,
        text: str,
        line_no: int,
        now: datetime,
        result: ConsumerResult,
        quality: list[tuple[str, str, str | None, int | None, str]],
    ) -> None:
        path = f"part-{part_index:09d}.jsonl"

        def note(kind: str, severity: str, detail: str) -> None:
            quality.append((kind, severity, path, line_no, detail))

        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            result.malformed += 1
            note("malformed_line", "error", f"line is not JSON: {exc}")
            return
        if not isinstance(row, dict):
            result.malformed += 1
            note("malformed_line", "error", f"line is {type(row).__name__}, expected an object")
            return

        try:
            event = parse_signal_event(row)
        except EventError as exc:
            result.malformed += 1
            note("malformed_event", "error", f"{exc.code}: {exc.detail}")
            return

        reason, detail = self._gate(event, now)
        outcome = "ACCEPTED" if reason is None else "REFUSED"
        inserted = self.store.record_signal(
            event,
            tradeable=event.tradeable,
            outcome=outcome,
            refusal_reason=reason.value if reason else None,
            refusal_detail=detail,
            consumed_at_utc=now,
        )
        if inserted:
            result.signals_recorded += 1
            if reason is None:
                result.accepted += 1
            else:
                result.refused += 1
        else:
            # The primary key absorbed this row: the engine has seen the
            # decision before (a rewound cursor, a restored backup, a double
            # publication). No second row, no second trade - and a recorded
            # fact instead of a silent skip.
            result.duplicates += 1
            note(
                "signal_duplicate",
                "warning",
                f"decision {event.decision_id} was redelivered and ignored; "
                "the existing record stands",
            )

    def _gate(self, event: SignalEvent, now: datetime) -> tuple[RefusalReason | None, str]:
        """The consumption gates, in causal order. ``None`` means accepted.

        These are the gates of *consumption* - whether this event may enter
        the account's pipeline at all. The fill engine (WQN-41) and the risk
        gates (WQN-43) refuse further downstream, on their own reasons.
        """
        if event.mode is not Mode.LIVE:
            return RefusalReason.NOT_LIVE, (
                f"mode is {event.mode.value}; only LIVE decisions enter the live "
                "account - a REPLAY row is history re-derived now"
            )
        if event.state.value != "ACCEPT":
            return RefusalReason.NOT_ACCEPT, (
                f"state is {event.state.value}; only ACCEPT was proposed as a trade"
            )
        age = (now - event.decision_time_utc).total_seconds()
        if age > self.max_signal_age_seconds:
            return RefusalReason.STALE_SIGNAL, (
                f"decision is {age:.0f}s old at consumption (limit "
                f"{self.max_signal_age_seconds:.0f}s); the market it priced has moved on"
            )
        return None, ""


def _index_of(path: Path) -> int:
    try:
        return int(path.stem.split("-")[1])
    except (IndexError, ValueError):
        return 0


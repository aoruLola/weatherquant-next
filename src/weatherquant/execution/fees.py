"""A dated fallback fee schedule for markets that report no fee rate.

The venue-reported ``fee_rate`` always wins. This module is only consulted when
that field is absent, so an unreported schedule is a *known* assumption at a
known instant rather than a refusal that stops every trade.

The schedule is deliberately dated rather than a single number: Polymarket's
weather taker rate changes over time (0.05 -> 0.01 at 2026-11-20T23:59Z), and a
decision must be priced with the rate that was actually in force at its own
instant. Using "today's" rate for a replay would invent an edge that never
existed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_FEE_SCHEDULE_PATH",
    "FEE_SOURCE_FALLBACK",
    "FEE_SOURCE_VENUE",
    "DatedFeeSchedule",
    "FeeScheduleEntry",
    "load_fee_schedule",
    "resolve_fee_rate",
]

DEFAULT_FEE_SCHEDULE_PATH = Path("configs/fee-schedule.json")

#: What a decision records when it had to fall back to this file.
FEE_SOURCE_VENUE = "venue"
FEE_SOURCE_FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class FeeScheduleEntry:
    effective_from_utc: datetime | None
    rate: float
    basis: str = ""


@dataclass(frozen=True, slots=True)
class DatedFeeSchedule:
    version: str
    venue: str
    category: str
    role: str
    entries: tuple[FeeScheduleEntry, ...]

    def rate_at(self, instant: datetime) -> float | None:
        """The rate in force at ``instant``, or ``None`` if no entry covers it."""
        chosen: FeeScheduleEntry | None = None
        for entry in self.entries:
            start = entry.effective_from_utc
            if start is None or start <= instant:
                chosen = entry
            else:
                break
        return None if chosen is None else chosen.rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "venue": self.venue,
            "category": self.category,
            "role": self.role,
            "entries": [
                {
                    "effective_from_utc": (
                        entry.effective_from_utc.isoformat()
                        if entry.effective_from_utc
                        else None
                    ),
                    "rate": entry.rate,
                }
                for entry in self.entries
            ],
        }


def load_fee_schedule(path: str | Path = DEFAULT_FEE_SCHEDULE_PATH) -> DatedFeeSchedule:
    """Load and validate the dated schedule.

    A missing file or an empty schedule is an error: the whole point of the
    fallback is that it is configured in the repository, so silently treating an
    absent file as "no fallback" would recreate the refusal this change removes.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"fee schedule file not found: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not payload.get("version"):
        raise ValueError(f"{target} has no version; a fee schedule cannot be audited without one")

    raw_entries = payload.get("entries") or []
    if not raw_entries:
        raise ValueError(f"{target} has no fee entries")

    entries: list[FeeScheduleEntry] = []
    for index, raw in enumerate(raw_entries):
        rate = raw.get("rate")
        if rate is None:
            raise ValueError(f"{target}: entry {index} has no rate")
        rate = float(rate)
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"{target}: entry {index} rate {rate} is outside [0, 1]")
        raw_start = raw.get("effective_from_utc")
        start = None if raw_start in (None, "") else _parse_instant(str(raw_start), target, index)
        entries.append(
            FeeScheduleEntry(
                effective_from_utc=start,
                rate=rate,
                basis=str(raw.get("basis") or ""),
            )
        )

    # Order matters, and a mis-ordered file would silently pick the wrong rate.
    entries.sort(
        key=lambda entry: (
            entry.effective_from_utc is not None,
            entry.effective_from_utc or datetime.min.replace(tzinfo=UTC),
        )
    )
    if entries[0].effective_from_utc is not None:
        raise ValueError(f"{target}: the first entry must have no effective_from_utc")

    return DatedFeeSchedule(
        version=str(payload["version"]),
        venue=str(payload.get("venue") or ""),
        category=str(payload.get("category") or ""),
        role=str(payload.get("role") or "taker"),
        entries=tuple(entries),
    )


def resolve_fee_rate(
    venue_rate: float | None,
    instant: datetime,
    schedule: DatedFeeSchedule | None,
) -> tuple[float | None, str | None]:
    """``(rate, source)`` for a decision.

    ``source`` is ``"venue"`` when the market reported a rate and ``"fallback"``
    when the dated schedule supplied it. ``(None, None)`` means neither source
    could price the trade, and the caller must refuse it.
    """
    if venue_rate is not None:
        return float(venue_rate), FEE_SOURCE_VENUE
    if schedule is None:
        return None, None
    rate = schedule.rate_at(instant)
    if rate is None:
        return None, None
    return rate, FEE_SOURCE_FALLBACK


def _parse_instant(raw: str, path: Path, index: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: entry {index} has an invalid effective_from_utc {raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

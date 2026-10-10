"""Aggregate hourly forecasts into a target-day maximum, and judge point-in-time eligibility.

Two things live here because they share the same calendar arithmetic.

**Daily aggregation.** The market settles on the *New York civil date*, so
hourly points are bucketed by converting each ``valid_time`` to
``America/New_York`` and selecting that date. This is the only correct way to
do it: a fixed ``[00:00Z, 24:00Z)`` window is wrong by an hour on both ends of
every DST transition, and New York has two of them a year.

Local days are 23, 24 or 25 hours long:

* spring forward (e.g. 2026-03-08): 23 hours - 02:00 -> 03:00 does not exist
* fall back (e.g. 2026-11-01): 25 hours - 01:00 happens twice

``expected_local_hours()`` returns that number so a run that covers "the whole
day" is judged against the right denominator rather than 24.

**Point-in-time eligibility.** A forecast is usable for a decision only if it
was publishable *before the day being predicted began*. Not before the day
ended, and not merely before some decision we wish we had made: a run issued
during the target day can already contain that day's observed weather, which is
exactly the leakage the whole research design exists to prevent. Runs whose
publication time is unknown are ineligible for strict use rather than assumed
available.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "MARKET_TIMEZONE",
    "DailyMaxResult",
    "PointInTimeResult",
    "aggregate_daily_max",
    "expected_local_hours",
    "local_day_bounds_utc",
    "point_in_time",
]

MARKET_TIMEZONE = ZoneInfo("America/New_York")


def local_day_bounds_utc(target_date: date) -> tuple[datetime, datetime]:
    """``(start, end)`` of the target civil day in New York, as UTC instants.

    ``end`` is the first instant of the *next* day, so the interval is
    half-open ``[start, end)`` and an hourly point stamped exactly at midnight
    belongs to the day it starts.
    """
    start = datetime.combine(target_date, time(0, 0), tzinfo=MARKET_TIMEZONE).astimezone(UTC)
    end = datetime.combine(target_date + timedelta(days=1), time(0, 0), tzinfo=MARKET_TIMEZONE).astimezone(
        UTC
    )
    return start, end


def expected_local_hours(target_date: date) -> int:
    """Hours in the New York civil day: 23, 24 or 25.

    Computed from the timezone rather than assumed, so a DST transition can
    never be silently treated as an ordinary day.
    """
    start, end = local_day_bounds_utc(target_date)
    return int((end - start).total_seconds() // 3600)


@dataclass(frozen=True, slots=True)
class DailyMaxResult:
    """The maximum of one forecast run over one local target day."""

    target_date: date
    predicted_max_f: float | None
    n_hours: int
    n_missing_hours: int
    first_valid_utc: datetime | None
    last_valid_utc: datetime | None
    expected_hours: int
    covers_full_local_day: bool
    discarded_out_of_day: int = 0
    discarded_malformed: int = 0

    @property
    def is_complete(self) -> bool:
        return self.covers_full_local_day


def aggregate_daily_max(
    hourly: Iterable[dict],
    *,
    target_date: date,
    temp_key: str = "temperature_2m",
    time_key: str = "valid_time_utc",
    input_unit: str = "degF",   # unit of the incoming temperatures
) -> DailyMaxResult:
    """Max one run's hourly points over one local day.

    ``hourly`` items need a UTC ``time_key`` and a temperature ``temp_key`` in
    ``input_unit`` (default degF); a degC input is converted, and an unknown
    unit raises rather than being guessed.
    Points are accepted in either order and may be unsorted; points outside the
    target day are counted but ignored, which lets a caller pass a whole run.

    Returns ``predicted_max_f=None`` when the day has no usable readings at
    all - it is never filled from another day, another run, or an observation.
    """
    start, end = local_day_bounds_utc(target_date)
    expected = expected_local_hours(target_date)

    in_day: list[tuple[datetime, float]] = []
    outside = malformed = 0

    for item in hourly:
        raw_time = item.get(time_key)
        raw_temp = item.get(temp_key)
        try:
            when = raw_time if isinstance(raw_time, datetime) else datetime.fromisoformat(str(raw_time))
        except (TypeError, ValueError):
            malformed += 1
            continue
        if when.tzinfo is None:
            # A naive timestamp has no defined instant; refusing it is the only
            # safe reading, because guessing UTC would shift the DST window.
            malformed += 1
            continue
        when = when.astimezone(UTC)
        try:
            value = float(raw_temp)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if value != value:  # NaN
            malformed += 1
            continue

        if not (start <= when < end):
            outside += 1
            continue
        if input_unit.upper() in ("DEGC", "C", "°C"):
            value = value * 9.0 / 5.0 + 32.0
        elif input_unit.upper() not in ("DEGF", "F", "°F"):
            raise ValueError(f"input_unit must be degF or degC, got {input_unit!r}")
        in_day.append((when, value))

    if not in_day:
        return DailyMaxResult(
            target_date=target_date,
            predicted_max_f=None,
            n_hours=0,
            n_missing_hours=expected,
            first_valid_utc=None,
            last_valid_utc=None,
            expected_hours=expected,
            covers_full_local_day=False,
            discarded_out_of_day=outside,
            discarded_malformed=malformed,
        )

    in_day.sort(key=lambda pair: pair[0])
    peak_temp = max(v for _, v in in_day)
    distinct_hours = {w.replace(minute=0, second=0, microsecond=0) for w, _ in in_day}

    return DailyMaxResult(
        target_date=target_date,
        predicted_max_f=peak_temp,
        n_hours=len(distinct_hours),
        n_missing_hours=max(0, expected - len(distinct_hours)),
        first_valid_utc=in_day[0][0],
        last_valid_utc=in_day[-1][0],
        expected_hours=expected,
        covers_full_local_day=len(distinct_hours) >= expected,
        discarded_out_of_day=outside,
        discarded_malformed=malformed,
    )


# ---------------------------------------------------------------------------
# Point-in-time eligibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PointInTimeResult:
    run_time_utc: datetime | None
    publishable_at_utc: datetime | None
    publishable_basis: str            # 'official' | 'estimated' | 'unknown'
    publishable_lag_hours: float | None
    publishable_note: str | None
    decision_deadline_utc: datetime
    eligible: bool
    ineligible_reason: str | None

    @property
    def known_before_day_started(self) -> bool:
        return (
            self.publishable_at_utc is not None
            and self.publishable_at_utc <= self.decision_deadline_utc
        )


def point_in_time(
    *,
    run_time_utc: datetime | None,
    publishable_at_utc: datetime | None,
    publishable_basis: str,
    target_date: date,
    publishable_lag_hours: float | None = None,
    publishable_note: str | None = None,
) -> PointInTimeResult:
    """Decide whether a run may enter a strict point-in-time history.

    The deadline is the **start** of the target New York day, not its end. A
    run issued during the day being predicted can already contain that day's
    observations, so using it for a decision "during the day" is leakage no
    matter how the decision instant is chosen.

    ``publishable_basis='unknown'`` is always ineligible: a publication time we
    cannot bound is not a publication time we can rely on, and treating it as
    available is how a strict backtest quietly becomes an in-sample one.
    """
    day_start, _ = local_day_bounds_utc(target_date)

    if publishable_at_utc is None or publishable_basis == "unknown":
        reason = (
            "publication time unknown"
            if publishable_at_utc is None
            else "publication time not established (basis='unknown')"
        )
        return PointInTimeResult(
            run_time_utc=run_time_utc,
            publishable_at_utc=publishable_at_utc,
            publishable_basis=publishable_basis,
            publishable_lag_hours=publishable_lag_hours,
            publishable_note=publishable_note,
            decision_deadline_utc=day_start,
            eligible=False,
            ineligible_reason=reason,
        )

    publishable = publishable_at_utc
    if publishable.tzinfo is None:
        publishable = publishable.replace(tzinfo=UTC)

    if publishable > day_start:
        lag = (publishable - day_start).total_seconds() / 3600.0
        return PointInTimeResult(
            run_time_utc=run_time_utc,
            publishable_at_utc=publishable,
            publishable_basis=publishable_basis,
            publishable_lag_hours=publishable_lag_hours,
            publishable_note=publishable_note,
            decision_deadline_utc=day_start,
            eligible=False,
            ineligible_reason=(
                f"publishable {publishable.isoformat()} is {lag:.1f}h after the target "
                f"day began ({day_start.isoformat()})"
            ),
        )

    if run_time_utc is not None and publishable < run_time_utc:
        # A publication time before the model even ran is a derivation error,
        # not an early forecast.
        return PointInTimeResult(
            run_time_utc=run_time_utc,
            publishable_at_utc=publishable,
            publishable_basis=publishable_basis,
            publishable_lag_hours=publishable_lag_hours,
            publishable_note=publishable_note,
            decision_deadline_utc=day_start,
            eligible=False,
            ineligible_reason="publishable_at precedes run_time",
        )

    return PointInTimeResult(
        run_time_utc=run_time_utc,
        publishable_at_utc=publishable,
        publishable_basis=publishable_basis,
        publishable_lag_hours=publishable_lag_hours,
        publishable_note=publishable_note,
        decision_deadline_utc=day_start,
        eligible=True,
        ineligible_reason=None,
    )

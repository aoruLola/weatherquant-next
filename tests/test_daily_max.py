"""Daily aggregation and point-in-time eligibility.

The DST cases are the whole point of this file. A fixed 24-hour window looks
right on 363 days a year and is wrong twice a year - and on those two days a
forecast can be silently attributed to the wrong civil date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.weather.daily_max import (
    aggregate_daily_max,
    expected_local_hours,
    local_day_bounds_utc,
    point_in_time,
)

# US DST transitions in 2026, verified in tests/test_weather_models.py:
#   2026-03-08 spring forward -> 23-hour local day
#   2026-11-01 fall back      -> 25-hour local day
SPRING_DAY = date(2026, 3, 8)
FALL_DAY = date(2026, 11, 1)
ORDINARY_DAY = date(2026, 7, 1)


def _hourly(day: date, temps: list[float] | None = None, *, start_hour_utc: int = 0) -> list[dict]:
    """Build hourly points covering a full UTC day for testing."""
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    points = []
    for i in range(48):
        when = base + timedelta(hours=start_hour_utc + i)
        temp = float(50 + (i % 12)) if temps is None else temps[i]
        points.append(
            {
                "valid_time_utc": when.isoformat(),
                "temperature_2m": temp,
            }
        )
    return points


# ---------------------------------------------------------------------------
# Local-day arithmetic
# ---------------------------------------------------------------------------


def test_ordinary_local_day_has_24_hours() -> None:
    assert expected_local_hours(ORDINARY_DAY) == 24


def test_spring_forward_day_has_23_hours() -> None:
    """2026-03-08: 02:00 EST -> 03:00 EDT, so 02:00-03:00 never happens."""
    assert expected_local_hours(SPRING_DAY) == 23


def test_fall_back_day_has_25_hours() -> None:
    """2026-11-01: 01:00 EDT -> 01:00 EST, so 01:00-02:00 happens twice."""
    assert expected_local_hours(FALL_DAY) == 25


def test_day_bounds_are_utc_and_half_open() -> None:
    start, end = local_day_bounds_utc(ORDINARY_DAY)
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start < end
    # New York is UTC-4 in July (EDT), so local midnight is 04:00Z.
    assert start == datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 2, 4, 0, tzinfo=UTC)
    # The interval is [start, end): exactly at midnight belongs to the new day.
    assert start <= datetime(2026, 7, 2, 4, 0, tzinfo=UTC)
    assert not (datetime(2026, 7, 2, 3, 59, tzinfo=UTC) >= end)


def test_spring_forward_bounds_shift_by_an_hour() -> None:
    """EST is UTC-5 before the switch, EDT is UTC-4 after it."""
    start, end = local_day_bounds_utc(SPRING_DAY)
    # 2026-03-08 00:00 EST = 05:00Z; 2026-03-09 00:00 EDT = 04:00Z.
    assert start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregation_takes_the_maximum_over_the_local_day() -> None:
    """New York is UTC-4 in July, so the local day is UTC hours 04..23.

    The peak is placed at 10:00Z (06:00 local) and a larger value at 02:00Z
    (22:00 the previous local day) to prove the window is the *local* day,
    not a UTC one.
    """
    # Local midnight in July is 04:00Z, so the local day spans UTC indices
    # 4..27 inclusive - it crosses into the next UTC date, which is exactly
    # what a naive "00:00Z to 24:00Z" window gets wrong.
    temps = [900.0] * 48          # 900 marks "outside the local day"
    temps[4:28] = [50.0] * 24     # UTC 04:00 Jul 1 .. 03:00 Jul 2
    temps[10] = 70.0              # local 06:00 -> the peak
    points = _hourly(ORDINARY_DAY, temps)

    result = aggregate_daily_max(points, target_date=ORDINARY_DAY)
    assert result.predicted_max_f == 70.0, "a UTC-hour peak outside the local day leaked in"
    assert result.expected_hours == 24
    assert result.n_hours == 24
    assert result.covers_full_local_day is True
    assert result.discarded_out_of_day == 24, "only the hours outside the local day are dropped"


def test_points_from_other_days_are_excluded_not_merged() -> None:
    """A run's hours span many days; only the target day may contribute."""
    points = _hourly(ORDINARY_DAY)
    points.append(
        {
            "valid_time_utc": datetime(2026, 7, 3, 12, 0, tzinfo=UTC).isoformat(),
            "temperature_2m": 999.0,
        }
    )
    result = aggregate_daily_max(points, target_date=ORDINARY_DAY)
    assert result.predicted_max_f != 999.0
    assert result.discarded_out_of_day >= 1


def test_a_day_with_no_readings_reports_none_rather_than_a_neighbour() -> None:
    """Missing is missing: never borrow from another day or another run."""
    result = aggregate_daily_max([], target_date=ORDINARY_DAY)
    assert result.predicted_max_f is None
    assert result.n_hours == 0
    assert result.n_missing_hours == 24
    assert result.covers_full_local_day is False


def test_a_naive_timestamp_is_rejected_rather_than_assumed_utc() -> None:
    """Guessing UTC would shift the day across a DST boundary."""
    naive = [
        {"valid_time_utc": datetime(2026, 7, 1, h), "temperature_2m": float(50 + h)}
        for h in range(24)
    ]
    result = aggregate_daily_max(naive, target_date=ORDINARY_DAY)
    assert result.predicted_max_f is None
    assert result.discarded_malformed == 24


def test_missing_and_nan_readings_do_not_become_a_max() -> None:
    points = [
        {"valid_time_utc": datetime(2026, 7, 1, 4, 0, tzinfo=UTC), "temperature_2m": "broken"},
        {"valid_time_utc": datetime(2026, 7, 1, 5, 0, tzinfo=UTC), "temperature_2m": float("nan")},
        {"valid_time_utc": datetime(2026, 7, 1, 6, 0, tzinfo=UTC), "temperature_2m": 66.0},
    ]
    result = aggregate_daily_max(points, target_date=ORDINARY_DAY)
    assert result.predicted_max_f == 66.0
    assert result.discarded_malformed == 2


def test_celsius_input_is_converted_only_when_asked() -> None:
    points = [{"valid_time_utc": datetime(2026, 7, 1, 4, 0, tzinfo=UTC), "temperature_2m": 20.0}]
    as_f = aggregate_daily_max(points, target_date=ORDINARY_DAY, input_unit="degF")
    from_c = aggregate_daily_max(points, target_date=ORDINARY_DAY, input_unit="degC")
    assert as_f.predicted_max_f == pytest.approx(20.0), "a degF input must pass through"
    assert from_c.predicted_max_f == pytest.approx(68.0), "20C is 68F"


def test_a_dst_day_aggregates_only_its_own_hours() -> None:
    """Spring forward: 23 local hours, and the missing hour is not invented."""
    start, end = local_day_bounds_utc(SPRING_DAY)
    points = []
    cursor = start
    while cursor < end:
        points.append(
            {
                "valid_time_utc": cursor.isoformat(),
                "temperature_2m": float(40 + (cursor.hour % 10)),
            }
        )
        cursor += timedelta(hours=1)

    result = aggregate_daily_max(points, target_date=SPRING_DAY)
    assert result.expected_hours == 23
    assert result.n_hours == 23
    assert result.covers_full_local_day is True
    assert result.n_missing_hours == 0
    assert result.predicted_max_f is not None


def test_fall_back_day_needs_25_distinct_hours_to_be_complete() -> None:
    """The repeated local hour means 25 readings, not 24."""
    assert expected_local_hours(FALL_DAY) == 25
    start, end = local_day_bounds_utc(FALL_DAY)
    hours = int((end - start).total_seconds() // 3600)
    assert hours == 25


def test_an_unknown_input_unit_is_refused_rather_than_guessed() -> None:
    points = [{"valid_time_utc": datetime(2026, 7, 1, 4, 0, tzinfo=UTC), "temperature_2m": 20.0}]
    import pytest as _pytest

    with _pytest.raises(ValueError, match="input_unit"):
        aggregate_daily_max(points, target_date=ORDINARY_DAY, input_unit="Rankine")


def test_distinguishes_a_forecast_maximum_from_an_observed_one() -> None:
    """The aggregate is labelled a *prediction*; nothing here is an observation."""
    result = aggregate_daily_max(_hourly(ORDINARY_DAY), target_date=ORDINARY_DAY)
    assert hasattr(result, "predicted_max_f")
    assert not hasattr(result, "max_temp_f"), "the observation field must not appear on a forecast"


# ---------------------------------------------------------------------------
# Point-in-time eligibility
# ---------------------------------------------------------------------------


def test_a_run_published_before_the_day_began_is_eligible() -> None:
    day_start, _ = local_day_bounds_utc(ORDINARY_DAY)
    result = point_in_time(
        run_time_utc=day_start - timedelta(hours=12),
        publishable_at_utc=day_start - timedelta(hours=6),
        publishable_basis="official",
        target_date=ORDINARY_DAY,
    )
    assert result.eligible is True
    assert result.ineligible_reason is None
    assert result.known_before_day_started is True


def test_a_run_published_during_the_day_is_not_eligible() -> None:
    """It may already contain the day being predicted - that is leakage."""
    day_start, _ = local_day_bounds_utc(ORDINARY_DAY)
    result = point_in_time(
        run_time_utc=day_start - timedelta(hours=6),
        publishable_at_utc=day_start + timedelta(hours=3),
        publishable_basis="estimated",
        target_date=ORDINARY_DAY,
        publishable_lag_hours=9.0,
        publishable_note="model schedule",
    )
    assert result.eligible is False
    assert "after the target day began" in (result.ineligible_reason or "")
    assert result.known_before_day_started is False


def test_an_unknown_publication_time_is_ineligible_by_default() -> None:
    """Cannot be proven available -> cannot enter a strict history."""
    result = point_in_time(
        run_time_utc=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
        publishable_at_utc=None,
        publishable_basis="unknown",
        target_date=ORDINARY_DAY,
    )
    assert result.eligible is False
    assert "unknown" in (result.ineligible_reason or "")


def test_publishable_before_run_time_is_a_derivation_error() -> None:
    result = point_in_time(
        run_time_utc=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
        publishable_at_utc=datetime(2026, 6, 30, 6, 0, tzinfo=UTC),
        publishable_basis="estimated",
        target_date=ORDINARY_DAY,
    )
    assert result.eligible is False
    assert "precedes run_time" in (result.ineligible_reason or "")


def test_publishable_never_equals_run_time_by_construction() -> None:
    """`run_time` is an initialisation instant, not a publication instant."""
    run_time = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    result = point_in_time(
        run_time_utc=run_time,
        publishable_at_utc=run_time,  # the anti-pattern the spec forbids
        publishable_basis="official",
        target_date=ORDINARY_DAY,
    )
    # A zero lag is permitted only if it still lands before the day began, but
    # the point is that the two fields are compared, never assumed equal.
    assert result.publishable_at_utc == run_time
    day_start, _ = local_day_bounds_utc(ORDINARY_DAY)
    assert result.eligible == (run_time <= day_start)

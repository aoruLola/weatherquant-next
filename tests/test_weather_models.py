"""Weather domain model invariants: provenance ordering, units, DST."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from weatherquant.weather.models import (
    DEFAULT_MODEL_LAG,
    DailyForecast,
    DailyMaxTemperature,
    ForecastRun,
    HourlyForecastPoint,
    Observation,
)

RUN = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def test_publishable_time_must_not_precede_the_run_time() -> None:
    with pytest.raises(ValidationError, match="cannot be public"):
        ForecastRun(
            model_id="ecmwf_ifs025",
            run_time_utc=RUN,
            publishable_at_utc=RUN - timedelta(hours=1),
            fetched_at_utc=RUN + timedelta(hours=1),
        )


def test_fetch_time_must_not_precede_the_run_time() -> None:
    with pytest.raises(ValidationError, match="before it existed"):
        ForecastRun(
            model_id="ecmwf_ifs025",
            run_time_utc=RUN,
            publishable_at_utc=RUN + timedelta(hours=6),
            fetched_at_utc=RUN - timedelta(minutes=5),
        )


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValidationError, match="UTC"):
        ForecastRun(
            model_id="gfs",
            run_time_utc=datetime(2026, 10, 9, 12, 0),  # no tz
            publishable_at_utc=RUN + timedelta(hours=6),
            fetched_at_utc=RUN + timedelta(hours=7),
        )


def test_standard_lag_is_six_hours() -> None:
    run = ForecastRun.with_standard_lag(
        model_id="ecmwf_ifs025",
        run_time_utc=RUN,
        fetched_at_utc=RUN + timedelta(hours=7),
    )
    assert run.publishable_at_utc == RUN + DEFAULT_MODEL_LAG
    assert run.publishable_at_utc == RUN + timedelta(hours=6)


def test_run_is_unusable_before_its_publication_time_and_usable_after() -> None:
    run = ForecastRun.with_standard_lag(
        model_id="ecmwf_ifs025",
        run_time_utc=RUN,
        fetched_at_utc=RUN + timedelta(hours=7),
    )
    assert run.is_usable_at(RUN + timedelta(hours=5)) is False
    assert run.is_usable_at(RUN + timedelta(hours=6)) is True
    assert run.is_usable_at(RUN + timedelta(hours=6, seconds=1)) is True


def test_a_run_carries_no_target_date() -> None:
    """Regression: one run publishes forecasts for many days, so it has no single date."""
    run = ForecastRun.with_standard_lag(
        model_id="gfs",
        run_time_utc=RUN,
        fetched_at_utc=RUN + timedelta(hours=7),
    )
    assert not hasattr(run, "target_date") or "target_date" not in run.model_fields


# ---------------------------------------------------------------------------
# DST handling of the New York civil date
# ---------------------------------------------------------------------------


def test_target_day_deadline_handles_spring_forward() -> None:
    """2026-03-08 is the US spring-forward day: midnight next day is 04:00 UTC."""
    assert ForecastRun.end_of_target_day_utc(date(2026, 3, 8)) == datetime(
        2026, 3, 9, 4, 0, tzinfo=UTC
    )


def test_target_day_deadline_handles_a_normal_winter_day() -> None:
    """EST is UTC-5, so midnight New York is 05:00 UTC."""
    assert ForecastRun.end_of_target_day_utc(date(2026, 3, 7)) == datetime(
        2026, 3, 8, 5, 0, tzinfo=UTC
    )


def test_target_day_deadline_handles_summer_and_fall_back() -> None:
    # EDT is UTC-4.
    assert ForecastRun.end_of_target_day_utc(date(2026, 7, 1)) == datetime(
        2026, 7, 2, 4, 0, tzinfo=UTC
    )
    # 2026-11-01 is the US fall-back day; midnight the 2nd is still EDT (UTC-4)
    # because the transition happens at 02:00 local on the 1st... clocks go back
    # at 02:00 EDT -> 01:00 EST, so by midnight of the 2nd we are in EST (UTC-5).
    assert ForecastRun.end_of_target_day_utc(date(2026, 11, 1)) == datetime(
        2026, 11, 2, 5, 0, tzinfo=UTC
    )


def test_lead_time_is_computed_against_the_deadline_not_a_flat_24h() -> None:
    run = ForecastRun.with_standard_lag(
        model_id="gfs",
        run_time_utc=datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
        fetched_at_utc=datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
    )
    # 2026-03-08T00:00Z -> 2026-03-09T04:00Z = 28h, not 24h.
    assert run.lead_time_hours_to(date(2026, 3, 8)) == pytest.approx(28.0)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_hourly_point_rejects_internally_inconsistent_units() -> None:
    with pytest.raises(ValidationError, match="inconsistent"):
        HourlyForecastPoint(
            model_id="gfs",
            run_time_utc=RUN,
            valid_time_utc=RUN + timedelta(hours=1),
            temp_c=10.0,
            temp_f=70.0,  # 10C is 50F, not 70F
        )


def test_hourly_point_rejects_a_valid_time_before_its_run() -> None:
    with pytest.raises(ValidationError, match="valid time cannot precede"):
        HourlyForecastPoint(
            model_id="gfs",
            run_time_utc=RUN,
            valid_time_utc=RUN - timedelta(hours=1),
            temp_c=10.0,
            temp_f=50.0,
        )


def test_observation_rejects_inconsistent_units() -> None:
    with pytest.raises(ValidationError, match="inconsistent"):
        Observation(
            station="KLGA",
            obs_time_utc=RUN,
            temp_c=20.0,
            temp_f=50.0,  # 20C is 68F
            source="iem_asos",
            fetched_at_utc=RUN,
        )


def test_observation_has_no_official_flag() -> None:
    """An observation is never the official result; only settlement is."""
    obs = Observation(
        station="KLGA",
        obs_time_utc=RUN,
        temp_c=20.0,
        temp_f=68.0,
        source="iem_asos",
        fetched_at_utc=RUN,
    )
    assert "is_official" not in obs.model_fields


def test_daily_forecast_requires_a_positive_number_of_hourly_points() -> None:
    with pytest.raises(ValidationError, match="zero hourly points"):
        DailyForecast(
            model_id="gfs",
            run_time_utc=RUN,
            publishable_at_utc=RUN + timedelta(hours=6),
            target_date=date(2026, 10, 10),
            predicted_max_f=71.0,
            n_hourly_points=0,
            fetched_at_utc=RUN + timedelta(hours=7),
        )


def test_daily_temperature_records_how_the_maximum_was_derived() -> None:
    d = DailyMaxTemperature(
        station="KLGA",
        target_date=date(2026, 10, 10),
        max_temp_f=73.0,
        n_readings=24,
        rule="max of hourly METAR readings",
        source="iem_asos",
        computed_at_utc=datetime(2026, 10, 11, 4, 30, tzinfo=UTC),
    )
    assert d.rule
    assert "is_official" not in d.model_fields

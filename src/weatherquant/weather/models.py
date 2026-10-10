"""Weather forecast and observation domain models.

Provenance rules encoded here (see ``docs/architecture.md``)
------------------------------------------------------------
1. **A model run time is not a publication time.** ``ForecastRun.run_time_utc``
   is when the forecast was *initialised*; ``publishable_at_utc`` is the
   earliest instant the system may act on it. ``publishable_at_utc`` is
   validated to be >= ``run_time_utc``, because a run cannot be public before
   it exists. Any sample whose decision instant precedes ``publishable_at_utc``
   is a lookahead and is rejected downstream.
2. **Observations are never official results.** :class:`Observation` and
   :class:`DailyMaxTemperature` describe what a station measured. Only
   :class:`~weatherquant.markets.models.Settlement` describes what the market
   resolved to. There is deliberately no ``is_official`` flag here - the
   distinction is structural, not a boolean that a later edit could flip.
3. **Every timestamp is absolute and UTC.** Civil-date attribution to
   ``America/New_York`` is a conversion step performed at the boundary, never a
   naive local-time field.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DailyForecast",
    "DailyMaxTemperature",
    "ForecastRun",
    "HourlyForecastPoint",
    "Observation",
]

# Default lag between a model's initialisation time and the moment its output is
# safe to act on. Publication pipelines batch and redistribute runs, so using
# `run_time_utc` as an availability instant would systematically make the system
# act on information too early.
DEFAULT_MODEL_LAG = timedelta(hours=6)

# New York civil date, the date a Polymarket "highest temperature in NYC on <date>"
# market actually settles on. DST is handled by zoneinfo, not a fixed offset.
MARKET_TIMEZONE = "America/New_York"


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC, got {value!r}")
    return value


class ForecastRun(BaseModel):
    """One initialization of one forecast model.

    A single run publishes forecasts for *several* target dates, so the run
    itself carries no ``target_date`` - that lives on :class:`DailyForecast`.
    Modelling them separately is what keeps "one run, many days" from being
    stored as many apparently independent runs.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str  # e.g. "ecmwf_ifs025", "gfs"
    run_time_utc: Annotated[datetime, Field(description="model initialisation instant (UTC)")]
    publishable_at_utc: Annotated[datetime, Field(description="earliest instant this run may be used")]
    fetched_at_utc: datetime
    status: str = "ok"
    reason: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> ForecastRun:
        _require_utc(self.run_time_utc, "run_time_utc")
        _require_utc(self.publishable_at_utc, "publishable_at_utc")
        _require_utc(self.fetched_at_utc, "fetched_at_utc")
        if self.publishable_at_utc < self.run_time_utc:
            raise ValueError(
                "publishable_at_utc must not precede run_time_utc: a forecast cannot be public "
                f"before it was initialised ({self.publishable_at_utc} < {self.run_time_utc})"
            )
        if self.fetched_at_utc < self.run_time_utc:
            raise ValueError(
                "fetched_at_utc must not precede run_time_utc: we cannot have retrieved a run "
                f"before it existed ({self.fetched_at_utc} < {self.run_time_utc})"
            )
        return self

    @classmethod
    def with_standard_lag(
        cls,
        *,
        model_id: str,
        run_time_utc: datetime,
        fetched_at_utc: datetime,
        lag: timedelta = DEFAULT_MODEL_LAG,
        status: str = "ok",
        reason: str | None = None,
    ) -> ForecastRun:
        return cls(
            model_id=model_id,
            run_time_utc=run_time_utc,
            publishable_at_utc=run_time_utc + lag,
            fetched_at_utc=fetched_at_utc,
            status=status,
            reason=reason,
        )

    @classmethod
    def end_of_target_day_utc(cls, target_date: date) -> datetime:
        """The last instant of ``target_date`` in New York, expressed in UTC.

        Used as the scoring deadline for a forecast of that day. Derived with
        ``zoneinfo`` so the 23-hour spring-forward and 25-hour fall-back days are
        handled correctly instead of assuming a 24-hour offset.
        """
        from zoneinfo import ZoneInfo

        end_of_day = datetime.combine(
            target_date + timedelta(days=1), datetime.min.time(), ZoneInfo(MARKET_TIMEZONE)
        )
        return end_of_day.astimezone(UTC)

    def lead_time_hours_to(self, target_date: date) -> float:
        """Hours from initialisation to the end of ``target_date`` in New York.

        This is the horizon a lead-time-dependent error model conditions on.
        """
        return (self.end_of_target_day_utc(target_date) - self.run_time_utc).total_seconds() / 3600.0

    def is_usable_at(self, instant_utc: datetime) -> bool:
        """True if ``instant_utc`` is a legal decision instant for this run."""
        _require_utc(instant_utc, "instant_utc")
        return instant_utc >= self.publishable_at_utc


class HourlyForecastPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    run_time_utc: datetime
    valid_time_utc: datetime
    temp_c: float
    temp_f: float

    @model_validator(mode="after")
    def _check_times(self) -> HourlyForecastPoint:
        _require_utc(self.run_time_utc, "run_time_utc")
        _require_utc(self.valid_time_utc, "valid_time_utc")
        if self.valid_time_utc < self.run_time_utc:
            raise ValueError(
                "a forecast valid time cannot precede its own run time "
                f"({self.valid_time_utc} < {self.run_time_utc})"
            )
        if abs(self.temp_c * 9.0 / 5.0 + 32.0 - self.temp_f) > 1e-6:
            raise ValueError(
                f"temp_f ({self.temp_f}) is inconsistent with temp_c ({self.temp_c}); "
                "both units must describe the same value"
            )
        return self


class DailyForecast(BaseModel):
    """A model's prediction of a target day's maximum temperature.

    This is a **point forecast**. It is not a probability and must never be
    passed to anything that expects one - see
    :mod:`weatherquant.models.probability`.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str
    run_time_utc: datetime
    publishable_at_utc: datetime
    target_date: date
    predicted_max_f: float
    n_hourly_points: Annotated[int, Field(ge=0)]
    fetched_at_utc: datetime

    @model_validator(mode="after")
    def _check(self) -> DailyForecast:
        if self.n_hourly_points == 0:
            raise ValueError("a daily forecast derived from zero hourly points is not a forecast")
        return self


class Observation(BaseModel):
    """A single station reading.

    ``is_official`` does not exist here by design: an observation is never the
    official result. Settlement is a separate type.
    """

    model_config = ConfigDict(frozen=True)

    station: str  # ICAO, e.g. "KLGA"
    obs_time_utc: datetime
    temp_c: float
    temp_f: float
    source: str  # e.g. "iem_asos", "nws_api", "metar"
    report_type: str | None = None
    fetched_at_utc: datetime

    @model_validator(mode="after")
    def _check(self) -> Observation:
        _require_utc(self.obs_time_utc, "obs_time_utc")
        if abs(self.temp_c * 9.0 / 5.0 + 32.0 - self.temp_f) > 1e-6:
            raise ValueError(f"temp_f ({self.temp_f}) inconsistent with temp_c ({self.temp_c})")
        return self


class DailyMaxTemperature(BaseModel):
    """A station's observed maximum temperature for one market civil date.

    ``rule`` records *how* the maximum was derived (e.g.
    ``"max of hourly METAR readings"``) so that two aggregations of the same
    raw readings can be compared. It is stored, not assumed.
    """

    model_config = ConfigDict(frozen=True)

    station: str
    target_date: date
    max_temp_f: float
    n_readings: Annotated[int, Field(ge=0)]
    rule: str
    source: str
    computed_at_utc: datetime

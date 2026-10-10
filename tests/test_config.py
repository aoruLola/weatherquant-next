"""Configuration defaults and validation.

The defaults are load-bearing: each one is a value verified against an upstream
service or derived from the spec. A silent change to any of them would change
system behaviour without a failing test elsewhere, so they are pinned here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from weatherquant.config import Settings
from weatherquant.weather.models import DEFAULT_MODEL_LAG


def test_defaults_are_the_verified_values() -> None:
    s = Settings(_env_file=None)
    assert s.station == "KLGA"
    assert s.market_timezone == "America/New_York"
    assert s.ecmwf_model_id == "ecmwf_ifs025"
    assert s.gfs_model_id == "gfs"
    # Weather category taker rate per docs.polymarket.com/trading/fees.
    assert s.weather_fee_rate == pytest.approx(0.05)
    assert s.execution_buffer_fraction == pytest.approx(0.005)
    assert s.model_lag_hours == pytest.approx(DEFAULT_MODEL_LAG.total_seconds() / 3600.0)


def test_station_is_normalised_to_uppercase_icao() -> None:
    assert Settings(_env_file=None, station="klga").station == "KLGA"


def test_station_rejects_a_non_icao_value() -> None:
    with pytest.raises(ValidationError, match="ICAO"):
        Settings(_env_file=None, station="NYC")


def test_model_lag_cannot_be_negative() -> None:
    """A negative lag would make a forecast usable before it exists - pure leakage."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_lag_hours=-1.0)


def test_model_lag_above_72h_is_refused_as_a_probable_typo() -> None:
    with pytest.raises(ValidationError, match="72h"):
        Settings(_env_file=None, model_lag_hours=100.0)


def test_negative_fee_rate_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, weather_fee_rate=-0.01)


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_STATION", "kjfk")
    monkeypatch.setenv("WQ_MODEL_LAG_HOURS", "9")
    s = Settings(_env_file=None)
    assert s.station == "KJFK"
    assert s.model_lag_hours == pytest.approx(9.0)

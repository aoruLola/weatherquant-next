"""Runtime configuration.

Values come from (in order of precedence): explicit constructor arguments,
``WQ_*`` environment variables, then the defaults below. Every field's default
is a value this project has actually verified against the upstream services -
there are no placeholders.

The point of centralising these is that several of them are *assumptions*, and
an assumption you have to grep for is an assumption you will not question:

* ``model_lag_hours`` - how long after initialisation a forecast becomes usable.
  Getting this wrong makes the system act on information too early, which is
  time leakage rather than a tuning parameter.
* ``weather_fee_rate`` - the Polymarket taker rate for the Weather category.
* ``station`` - the market's resolution station. Audited open-source bots got
  this wrong (KNYC vs KLGA), which silently invalidates every probability.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage -----------------------------------------------------------
    db_path: Path = Path("data/weatherquant.duckdb")
    legacy_db_path: Path = Path("../backend/data/weatherquant.duckdb")
    raw_dir: Path = Path("data/raw")

    # Research runs in its OWN file. The collector's schema and lock must never
    # be touched by a research backfill, and research tables are re-derived
    # often enough that sharing a file with the order-book history would be a
    # poor trade. See weatherquant.storage.research.
    research_db_path: Path = Path("data/research/weather-research.duckdb")
    research_raw_dir: Path = Path("data/research/raw")

    # --- market definition -------------------------------------------------
    station: str = "KLGA"
    """ICAO station the markets resolve on. Not KNYC - see docs/open-source-review.md §5.3."""

    market_timezone: str = "America/New_York"
    """Civil date the market settles on. DST is handled by zoneinfo, not a fixed offset."""

    # --- forecasts ---------------------------------------------------------
    ecmwf_model_id: str = "ecmwf_ifs025"
    # A bare `gfs` is rejected by Open-Meteo (400 "Cannot initialize
    # MultiDomains..."); the working ids are `gfs_seamless` and `gfs_global`.
    # Verified 2026-10-10 - see docs/weather-data-sources.md.
    gfs_model_id: str = "gfs_seamless"
    model_lag_hours: float = Field(
        default=6.0,
        ge=0.0,
        description="Hours after run_time before a forecast may be used. "
        "Raising it is conservative; lowering it risks acting before publication.",
    )

    # --- pricing -----------------------------------------------------------
    weather_fee_rate: float = Field(
        default=0.05,
        ge=0.0,
        description="Polymarket taker fee rate for the Weather category (maker = 0).",
    )
    execution_buffer_fraction: float = Field(
        default=0.005,
        ge=0.0,
        description="Execution buffer as a fraction of fill notional (0.5%).",
    )

    # --- http --------------------------------------------------------------
    http_timeout_seconds: float = Field(default=20.0, gt=0)
    http_retries: int = Field(default=4, ge=0)

    @field_validator("station")
    @classmethod
    def _station_is_icao(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 4 or not v.isalpha():
            raise ValueError(f"station must be a 4-letter ICAO code, got {v!r}")
        return v

    @field_validator("model_lag_hours")
    @classmethod
    def _lag_is_sane(cls, v: float, info: ValidationInfo) -> float:
        # A negative lag would mean a forecast is usable before it exists, which
        # pydantic already rejects via ge=0; this guards the other end.
        if v > 72:
            raise ValueError(
                f"model_lag_hours={v} exceeds 72h; no forecast is useful that stale. "
                "If this is intentional, raise the bound deliberately rather than "
                "by accident."
            )
        return v

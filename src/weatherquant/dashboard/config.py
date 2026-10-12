"""Dashboard configuration.

Paths point at *directories that are mounted read-only* (health/, reports/,
raw/) - never at a ``.duckdb`` file, and the container compose enforces that
by mounting only those subdirectories. Nothing here is a credential; the
dashboard has none.

``WQ_DASHBOARD_DATASET`` exists for one reason: development and CI screenshot
runs have no live NAS in reach. When it is not ``live``, every response is
stamped ``dataset: "fixture"`` and the UI shows a permanent banner - a fixture
must never be able to pass itself off as production data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DashboardSettings"]


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- sources (read-only directories) -----------------------------------
    collector_data_dir: Path = Path("data")
    """Collector data root: <dir>/health/heartbeat.json and <dir>/raw/."""

    shadow_data_dir: Path = Path("data/shadow")
    """Shadow data root: <dir>/health/heartbeat.json and <dir>/reports/."""

    # --- dataset provenance -------------------------------------------------
    # Env: WQ_DATASET (``fixture`` stamps every response and shows a banner).
    dataset: Literal["live", "fixture"] = "live"
    """``fixture`` stamps every response and shows a banner in the UI."""

    station: str = "KLGA"
    """ICAO station the markets resolve on; only used when parsing archive
    metadata that does not carry one. Same env var (``WQ_STATION``) and same
    default as the collector and shadow, so all three agree by construction."""

    # --- freshness thresholds (seconds) ------------------------------------
    heartbeat_stale_seconds: float = Field(default=120.0, gt=0)
    """A heartbeat older than this is STALE. Mirrors the container probes."""

    report_stale_grace_seconds: float = Field(default=60.0, ge=0)
    """Added to the report's own stale_after_seconds before calling it stale."""

    book_stale_seconds: float = Field(default=900.0, gt=0)
    """An order book older than this is shown as STALE, not as a live quote."""

    # --- bounded archive reads ---------------------------------------------
    archive_tail_bytes: int = Field(default=512 * 1024, ge=4096)
    """How much of each archive file is read per refresh. Never a full scan."""

    archive_days_back: int = Field(default=1, ge=0, le=7)
    """How many day-files back (including today) the tail considers."""

    archive_cache_seconds: float = Field(default=4.0, ge=0)
    """Minimum interval between archive re-reads; requests reuse the cache."""

    # --- api ---------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(default=8787, ge=1, le=65535)
    static_dir: Path | None = None
    """Built frontend (dashboard/frontend/dist). None in dev (Vite proxies)."""

    rate_limit_per_minute: int = Field(default=600, ge=0)
    """0 disables the limiter. Generous: only the dashboard's own UI polls."""

    trend_sample_seconds: float = Field(default=10.0, gt=0)
    trend_history_samples: int = Field(default=360, ge=10)

    @field_validator("dataset", mode="before")
    @classmethod
    def _dataset_is_known(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() not in {"live", "fixture"}:
            raise ValueError(f"dataset must be live or fixture (got {value!r})")
        return value

    # --- resolved paths (never serialised into responses) ------------------
    @property
    def collector_health_path(self) -> Path:
        return self.collector_data_dir / "health" / "heartbeat.json"

    @property
    def collector_raw_dir(self) -> Path:
        return self.collector_data_dir / "raw"

    @property
    def shadow_health_path(self) -> Path:
        return self.shadow_data_dir / "health" / "heartbeat.json"

    @property
    def shadow_report_path(self) -> Path:
        return self.shadow_data_dir / "reports" / "report.json"

    @property
    def shadow_snapshots_dir(self) -> Path:
        """Where the shadow-side producer will publish detail snapshots.

        Part of the data contract (docs/dashboard-data-contract.md); the files
        appear once the shadow engine integrates the producer. Until then the
        API reports them as ``unavailable`` with that exact reason.
        """
        return self.shadow_data_dir / "reports"

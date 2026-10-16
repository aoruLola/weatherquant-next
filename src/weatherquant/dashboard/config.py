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

    performance_data_dir: Path = Path("data/performance")
    """Research analytics output: the six performance snapshots.

    Written by the offline research process, read-only here. Kept separate
    from the shadow's directory so a slow backfill can never block the
    engine's own publishes, and so this container still never sees a
    database - only JSON that someone else already computed.
    """

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

    # --- performance multi-layer freshness (seconds) -----------------------
    # These judge the *layers*, not the snapshot's age, so a healthy publish
    # interval is never reported as a fault. See performance_freshness.py.
    performance_publish_interval_seconds: float = Field(default=1200.0, gt=0)
    """How often the publish job runs. A snapshot up to this old is normal."""

    performance_publish_grace_seconds: float = Field(default=300.0, ge=0)
    """Slack added to the publish interval before snapshot age reads as stale."""

    shadow_export_stale_seconds: float = Field(default=900.0, gt=0)
    """No new export data for this long means the shadow export has stopped."""

    performance_ingest_stale_seconds: float = Field(default=900.0, gt=0)
    """The engine falling this far behind the export means ingest has stopped."""

    performance_publication_stale_seconds: float = Field(default=1500.0, gt=0)
    """How far the published snapshot may trail what ingest has consumed.

    Defaults to the publish interval plus grace: with publish running every 20
    minutes the snapshot is *expected* to be up to that far behind the ingest
    position, so a smaller threshold would flag every healthy interval."""

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

    @property
    def shadow_export_dir(self) -> Path:
        """The shadow's decisions export, read live for upstream freshness.

        Only ``state.json`` is read, and only to answer "is the shadow still
        producing data". The Dashboard never infers this from a snapshot's
        manifest: a manifest watermark records what was consumed at build
        time, and treating it as the export's current position would make a
        stopped export look caught up.
        """
        return self.shadow_data_dir / "reports" / "decisions-export"

    @property
    def performance_snapshots_dir(self) -> Path:
        """Where research analytics publishes the performance snapshots.

        The six filenames are fixed by
        ``weatherquant.performance.snapshot.SNAPSHOT_FILES``; a missing file
        is reported as ``missing`` with a reason rather than as zeros, so a
        dashboard with no backfill cannot look like a dashboard with no edge.
        """
        return self.performance_data_dir

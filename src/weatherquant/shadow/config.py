"""Shadow-runtime configuration: paths, cadence, and the risk gates.

Two sources, deliberately different in kind:

*Paths and cadence* are environment variables, like the rest of the project -
they change per host and belong in ``.env``.

*Risk thresholds* are a versioned JSON file (:mod:`configs/shadow-thresholds`),
because a gate that can be widened by an environment variable on the box that
is also producing the results cannot be audited afterwards. Promoting a
threshold means editing a file in the repository, with a version bump and a
rationale attached.

:class:`ShadowSettings` declares only shadow-specific fields so that it never
drifts from :class:`weatherquant.config.Settings`; the market definition, fee
rate and model ids have exactly one owner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ShadowSettings", "Thresholds", "load_thresholds"]


class ShadowSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- storage (the shadow's own directory; never the collector's) -------
    shadow_db_path: Path = Path("data/shadow/shadow.duckdb")
    shadow_health_dir: Path = Path("data/shadow/health")
    shadow_thresholds_path: Path = Path("configs/shadow-thresholds.json")

    # Where the engine publishes the report it computes from its OWN connection.
    # Readers take this snapshot instead of opening DuckDB: the engine holds an
    # exclusive lease for its whole life, and DuckDB blocks a second process
    # from opening the file at all - read-only included. The container sets
    # this to /data/reports/report.json.
    shadow_report_path: Path = Path("data/shadow/reports/report.json")

    # Model artifacts live in a *committed* directory, not under data/: an
    # artifact that ships with the code is one whose version can be reviewed in
    # the same diff as the code that reads it, and one the image can bake in so
    # a pinned image carries pinned parameters. The container overrides this to
    # /app/models; an operator can point it at a mounted directory to use
    # artifacts fitted elsewhere, without rebuilding.
    shadow_models_dir: Path = Path("models")

    # The collector's raw archive, mounted READ-ONLY by the shadow container.
    # Only complete JSONL lines are ever read, and nothing here writes to it.
    collector_raw_dir: Path = Path("data/raw")

    # --- models ------------------------------------------------------------
    shadow_primary_model: str = Field(
        default="calibrated",
        description="Model whose edge decides ACCEPT/REJECT. The others still "
        "produce prediction records so forecast coverage can be compared.",
    )
    shadow_model_names: str = Field(
        default="ecmwf_raw,gfs_raw,fusion,calibrated",
        description="Comma-separated models to compute predictions for.",
    )

    # --- LIVE vs REPLAY ----------------------------------------------------
    shadow_mode: str = Field(
        default="auto",
        description="auto | live | replay. 'live' tails a feed that should be "
        "current: decision time is the wall clock and a silent feed is "
        "COLLECTOR_DISCONNECTED. 'replay' processes history: decision time is "
        "the instant the archive represents, so only forecasts publishable "
        "then may be used, and silence is not a fault. 'auto' decides once at "
        "startup from the age of the newest archive line and then holds that "
        "decision for the whole run - a feed that dies mid-run must surface as "
        "COLLECTOR_DISCONNECTED, not silently become a replay.",
    )

    # --- cadence -----------------------------------------------------------
    shadow_tick_seconds: float = Field(default=5.0, gt=0, description="Main loop period.")
    shadow_forecast_refresh_seconds: float = Field(
        default=600.0, gt=0, description="How often to look for newly publishable runs."
    )
    shadow_metrics_seconds: float = Field(default=300.0, gt=0, description="Metric snapshot period.")
    shadow_report_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How often the report snapshot is recomputed and republished. "
        "Uses the connection already open - it never opens a second one.",
    )
    shadow_report_stale_seconds: float = Field(
        default=300.0,
        gt=0,
        description="A snapshot older than this is printed with an expiry notice. "
        "The report is still shown: a stale answer is more useful than none, "
        "provided it says so.",
    )
    shadow_export_seconds: float = Field(
        default=300.0,
        gt=0,
        description="How often a bounded batch of historical decisions is "
        "appended to the export the research side reads. Never blocks the "
        "decision cycle: one batch, then back to deciding.",
    )
    shadow_export_rows: int = Field(
        default=20_000,
        gt=0,
        description="Hard cap on rows read and written in one export batch.",
    )
    shadow_export_max_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Wall-clock budget for one export batch, independent of "
        "the row cap. A large batch on a slow disk must still yield.",
    )
    shadow_export_max_parts: int = Field(
        default=5,
        gt=0,
        description="Parts written per export tick. Bounds how far one tick "
        "can run on a cold start; the next tick continues from the watermark.",
    )
    shadow_collector_silence_seconds: float = Field(
        default=900.0,
        gt=0,
        description="No archive line for this long raises COLLECTOR_DISCONNECTED "
        "and a data-quality event. 15 min tolerates the collector's own 5s flush "
        "plus a restart, without pretending a silent feed is a quiet market.",
    )
    shadow_backoff_max_seconds: float = Field(default=300.0, gt=0)
    shadow_max_new_runs_per_cycle: int = Field(
        default=2, ge=1, description="Cap on upstream forecast fetches per cycle."
    )
    shadow_run_lookback_hours: float = Field(
        default=72.0, gt=0, description="How far back to look for unfetched issue hours."
    )

    # LIVE vs REPLAY. `auto` decides once per process from the archive's own
    # age: a feed younger than the silence window is LIVE, an archive older
    # than it is history being re-read. Deciding once matters - flipping to
    # REPLAY mid-run because the collector stalled would relabel a genuine
    # COLLECTOR_DISCONNECTED as an archival exercise.
    shadow_mode: str = Field(
        default="auto",
        description="auto | live | replay. Every decision records which applied.",
    )

    @field_validator("shadow_model_names")
    @classmethod
    def _models_not_empty(cls, v: str) -> str:
        names = [n.strip() for n in v.split(",") if n.strip()]
        if not names:
            raise ValueError("shadow_model_names must name at least one model")
        return ",".join(names)

    @field_validator("shadow_mode")
    @classmethod
    def _mode_is_known(cls, v: str) -> str:
        value = str(v).strip().lower()
        if value not in {"auto", "live", "replay"}:
            raise ValueError(
                f"shadow_mode must be auto, live or replay (got {v!r}); "
                "an unknown value would fall through to a default and that "
                "default decides whether output claims to be contemporaneous"
            )
        return value

    @property
    def model_names(self) -> list[str]:
        return [n.strip() for n in self.shadow_model_names.split(",") if n.strip()]


# --- risk gates ------------------------------------------------------------

_REQUIRED_THRESHOLDS = (
    "min_net_edge_usd_per_share",
    "max_snapshot_age_ms",
    "max_forecast_age_hours",
    "proposed_shares",
    "require_complete_fill",
    "min_depth_usd",
    "max_decision_latency_ms",
)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The risk gates in force, with the version they were agreed under."""

    version: str
    basis: str
    min_net_edge_usd_per_share: float
    max_snapshot_age_ms: float
    max_forecast_age_hours: float
    proposed_shares: float
    require_complete_fill: bool
    min_depth_usd: float
    max_decision_latency_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "basis": self.basis,
            "min_net_edge_usd_per_share": self.min_net_edge_usd_per_share,
            "max_snapshot_age_ms": self.max_snapshot_age_ms,
            "max_forecast_age_hours": self.max_forecast_age_hours,
            "proposed_shares": self.proposed_shares,
            "require_complete_fill": self.require_complete_fill,
            "min_depth_usd": self.min_depth_usd,
            "max_decision_latency_ms": self.max_decision_latency_ms,
        }


def load_thresholds(path: str | Path) -> Thresholds:
    """Load the gates, refusing a file that is missing or silently incomplete.

    A missing key is an error rather than a default: the failure mode of a
    "permissive default" here is a gate that is off without anyone turning it
    off, which is indistinguishable in the results from a gate that was never
    written.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"threshold file not found: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))

    missing = [key for key in _REQUIRED_THRESHOLDS if key not in payload.get("thresholds", {})]
    if missing:
        raise ValueError(f"{target} is missing threshold(s): {missing}")
    if not payload.get("version"):
        raise ValueError(f"{target} has no version; a gate set without a version cannot be audited")

    entries = payload["thresholds"]
    values = {key: entries[key]["value"] for key in _REQUIRED_THRESHOLDS}
    thresholds = Thresholds(
        version=str(payload["version"]),
        basis=str(payload.get("basis", "")),
        **values,
    )
    _validate(thresholds, target)
    return thresholds


def _validate(t: Thresholds, path: Path) -> None:
    problems: list[str] = []
    if t.min_net_edge_usd_per_share < 0:
        problems.append("min_net_edge_usd_per_share must be >= 0")
    if t.max_snapshot_age_ms <= 0:
        problems.append("max_snapshot_age_ms must be > 0")
    if t.max_forecast_age_hours <= 0:
        problems.append("max_forecast_age_hours must be > 0")
    if t.proposed_shares <= 0:
        problems.append("proposed_shares must be > 0")
    if t.min_depth_usd < 0:
        problems.append("min_depth_usd must be >= 0")
    if problems:
        raise ValueError(f"{path} contains invalid gate value(s): {problems}")

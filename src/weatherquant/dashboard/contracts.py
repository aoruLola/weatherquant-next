"""Pydantic response models: the API contract the frontend mirrors in TypeScript.

Shape of every endpoint::

    {
      "schema_version": 1,
      "api": "overview",
      "generated_at_utc": "...",   # when the API computed THIS response
      "dataset": "live",           # "fixture" in dev/CI - UI banners on it
      "freshness": {...},          # verdict about the underlying data
      "data": {...}
    }

Two deliberate rules:

* **No absolute paths, ever.** Sources are identified by logical names
  (``collector-heartbeat``), so a response can be pasted into an issue without
  leaking the NAS layout.
* **Tolerant of producer growth.** The other sessions are actively adding
  counters to heartbeats; fields we do not model land in catch-all dicts
  rather than failing validation - a new upstream field must never take the
  dashboard down.
"""

# Chinese UI copy deliberately uses fullwidth CJK punctuation - correct
# typography for these strings, same rationale as the per-file RUF001
# ignores on scripts/forecast_coverage.py and scripts/baseline_report.py.
# ruff: noqa: RUF001

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from weatherquant.dashboard import API_SCHEMA_VERSION

__all__ = [
    "BookQuote",
    "CollectorHealthData",
    "CollectorMetricsData",
    "DecisionRow",
    "DecisionsData",
    "Envelope",
    "ForecastDistribution",
    "ForecastModelInfo",
    "ForecastsData",
    "Freshness",
    "FreshnessState",
    "HealthzData",
    "MarketDetailData",
    "MarketSummary",
    "MarketsData",
    "OverviewData",
    "ShadowHealthData",
    "ShadowReportData",
    "SystemData",
    "SystemSourceHealth",
]


class FreshnessState(StrEnum):
    """What the dashboard can honestly claim about one source."""

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"  # file not there (service never ran, or not mounted)
    CORRUPT = "corrupt"  # file exists but does not parse
    UNAVAILABLE = "unavailable"  # contract defined, producer not integrated yet


class Freshness(BaseModel):
    """How current one source is, and - for performance snapshots - which
    layer of the pipeline is responsible when it is not.

    The four ``*_lag_seconds`` fields are populated for performance snapshots
    and left ``None`` for every other source, whose freshness really is just
    "how old is this file". ``age_seconds``/``as_of_utc`` keep their original
    meaning so a consumer written before the layers existed still reads a
    correct answer.

    ``lagging_layer`` names which layer the verdict is about:
    ``shadow_export``, ``ingestion``, ``publication`` or ``publish_interval``.
    ``None`` means nothing is behind.
    """

    state: FreshnessState
    age_seconds: float | None = None
    as_of_utc: datetime | None = None
    stale_after_seconds: float | None = None
    reason: str | None = None

    # -- multi-layer performance freshness ---------------------------------
    #: How long since the shadow export produced new data (live read).
    export_data_lag_seconds: float | None = None
    #: Export watermark minus the watermark this snapshot consumed.
    ingestion_lag_seconds: float | None = None
    #: Publish time minus the watermark this snapshot consumed.
    publication_lag_seconds: float | None = None
    #: Now minus the publish time. Expected to reach the publish interval.
    snapshot_age_seconds: float | None = None
    #: The live shadow export watermark, as read from the export itself.
    export_watermark_utc: datetime | None = None
    #: The watermark the current snapshot consumed (from its manifest).
    consumed_watermark_utc: datetime | None = None
    #: When the current snapshot was published.
    published_at_utc: datetime | None = None
    #: Which layer the verdict names, or None when nothing is behind.
    lagging_layer: str | None = None
    #: The publish interval this snapshot is judged against, when known.
    publish_interval_seconds: float | None = None


class Envelope[T](BaseModel):
    """Uniform wrapper every endpoint returns. ``data`` holds the payload."""

    schema_version: int = API_SCHEMA_VERSION
    api: str
    generated_at_utc: datetime
    dataset: Literal["live", "fixture"]
    freshness: Freshness
    data: T


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


class CollectorHealthData(BaseModel):
    """Heartbeat + Docker-probe verdict, side by side.

    ``health_status`` is produced by :func:`weatherquant.data.heartbeat.
    evaluate_health` - the same function the container probe runs - so the
    dashboard can never disagree with ``docker ps`` about the same file.
    """

    model_config = ConfigDict(extra="allow")

    present: bool
    health_status: str = "unknown"
    health_reasons: list[str] = Field(default_factory=list)
    process_status: str = "unknown"
    run_id: str | None = None
    app_version: str | None = None
    hostname: str | None = None
    uptime_seconds: float | None = None
    started_at_utc: datetime | None = None
    updated_at_utc: datetime | None = None
    last_upstream_contact_utc: datetime | None = None
    upstream_contact_age_seconds: float | None = None
    last_data_change_utc: datetime | None = None
    data_change_age_seconds: float | None = None
    ws_connected: bool | None = None
    ws_last_message_utc: datetime | None = None
    ws_message_age_seconds: float | None = None
    subscribed_tokens: int | None = None
    rest_last_success_utc: datetime | None = None
    rest_success_age_seconds: float | None = None
    rest_consecutive_failures: int | None = None
    stats: dict[str, int] = Field(default_factory=dict)
    db_bytes: int | None = None
    raw_bytes: int | None = None
    disk_free_percent: float | None = None
    disk_total_bytes: int | None = None
    last_error: str | None = None
    last_error_utc: datetime | None = None
    last_defect: str | None = None
    fatal_error: str | None = None
    telemetry: dict[str, Any] = Field(default_factory=dict)
    """Rolling operational telemetry (Collector >= v0.1.2 heartbeat):
    ``ws_queue {depth, high_water, capacity, frames_dropped}``,
    ``rest_latency_*``, ``poll_budget`` ... An empty dict means the running
    collector does not publish it - which is a different statement from zero."""
    reason: str | None = None
    """Present only when the file is missing or unparseable."""


class CollectorMetricsData(BaseModel):
    """Cumulative counters plus a short observed trend per counter.

    Counters are monotonic totals from the heartbeat; rates are deliberately
    NOT invented here - the trend buffer holds what this process has actually
    observed, and is empty until it has. An empty trend renders as N/A.
    """

    present: bool
    counters: dict[str, int] = Field(default_factory=dict)
    trends: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    trend_window_seconds: float | None = None
    updated_at_utc: datetime | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# shadow
# ---------------------------------------------------------------------------


class ShadowHealthData(BaseModel):
    model_config = ConfigDict(extra="allow")

    present: bool
    health_status: str = "unknown"
    health_reasons: list[str] = Field(default_factory=list)
    process_status: str = "unknown"
    run_id: str | None = None
    updated_at_utc: datetime | None = None
    started_at_utc: datetime | None = None
    uptime_seconds: float | None = None
    last_decision_at_utc: datetime | None = None
    last_decision_age_seconds: float | None = None
    collector_last_seen_utc: str | None = None
    cycles: int | None = None
    mode: str | None = None
    configured_mode: str | None = None
    primary_model: str | None = None
    thresholds_version: str | None = None
    fatal_error: str | None = None
    stop_reason: str | None = None
    disk_free_percent: float | None = None
    last_cycle: dict[str, Any] | None = None
    reason: str | None = None
    # Absent (None) on heartbeats published before these fields existed - the
    # dashboard reports "暂未接入" rather than guessing at a value.
    app_version: str | None = None
    image_tag: str | None = None
    db_bytes: int | None = None


class ShadowReportData(BaseModel):
    """The published report, passed through with its own envelope checked.

    Modelled loosely on purpose: report.py is under active development by
    another session, so every section is optional and unknown keys are kept.
    The frontend reads only what it needs and treats absence as N/A.
    """

    model_config = ConfigDict(extra="allow")

    present: bool
    generated_at_utc: datetime | None = None
    source: str | None = None
    schema_version: int | None = None
    stale_after_seconds: float | None = None
    run: dict[str, Any] | None = None
    decisions: dict[str, Any] | None = None
    theoretical_edge: dict[str, Any] | None = None
    prediction_quality: dict[str, Any] | None = None
    liquidity: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    liveness: dict[str, Any] | None = None
    data_quality: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    equity_curve: Any = None
    equity_curve_note: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------


class BookQuote(BaseModel):
    """One side's view of liquidity. ``best_*`` are quotes, NOT executable
    prices - the label says so on the UI as well."""

    available: bool
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    bid_depth_usd: float | None = None
    ask_depth_usd: float | None = None
    levels_bid: int = 0
    levels_ask: int = 0
    defects: list[str] = Field(default_factory=list)
    quality: str = "missing"
    stale: bool = True
    age_seconds: float | None = None
    venue_ts_utc: datetime | None = None
    received_at_utc: datetime | None = None
    source: str = "archive-tail"
    reason: str | None = None
    """Why the book is missing/stale, in plain words - never a silent null."""


class MarketSummary(BaseModel):
    market_id: str
    event_id: str | None = None
    question: str = ""
    target_date: date | None = None
    bracket_label: str = ""
    bracket_low: float | None = None
    bracket_high: float | None = None
    station: str | None = None
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    tradeable: bool = False
    # Derived, not read from the venue: the market's *civil day in New York*
    # has already ended. A market can still be accepting orders after its
    # target date (the exchange has not resolved it yet), and presenting that
    # next to a current market is the mix this field exists to prevent.
    target_day_ended: bool = False
    token_yes: str | None = None
    book: BookQuote | None = None
    model_probability: float | None = None
    net_edge_usd: float | None = None
    last_decision: dict[str, Any] | None = None
    model_source: Literal["market-snapshot", "unavailable"] = "unavailable"
    model_reason: str | None = None
    updated_at_utc: datetime | None = None


class MarketsData(BaseModel):
    items: list[MarketSummary] = Field(default_factory=list)
    total: int = 0
    next_cursor: str | None = None
    source: str = "archive-tail"
    source_status: FreshnessState = FreshnessState.FRESH
    reason: str | None = None
    filters_applied: dict[str, Any] = Field(default_factory=dict)


class MarketDetailData(BaseModel):
    model_config = ConfigDict(extra="allow")

    market: MarketSummary | None = None
    depth: dict[str, Any] | None = None
    recent_decisions: list[dict[str, Any]] = Field(default_factory=list)
    recent_decisions_status: FreshnessState = FreshnessState.UNAVAILABLE
    recent_decisions_reason: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# forecasts
# ---------------------------------------------------------------------------


class ForecastModelInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    is_primary: bool = False
    predictions: int = 0
    latest_run_utc: datetime | None = None
    training_cutoff_utc: datetime | None = None
    version: str | None = None


class ForecastBucket(BaseModel):
    label: str
    low: float | None = None
    high: float | None = None
    probability: float


class ForecastDistribution(BaseModel):
    model: str
    version: str | None = None
    target_date: date | None = None
    forecast_run_time_utc: datetime | None = None
    training_cutoff_utc: datetime | None = None
    forecast_received_at_utc: datetime | None = None
    predicted_max_f: float | None = None
    station: str | None = None
    buckets: list[ForecastBucket] = Field(default_factory=list)
    source: str = "forecast-snapshot"


class ForecastsData(BaseModel):
    station: str | None = None
    target_dates: list[date] = Field(default_factory=list)
    models: list[ForecastModelInfo] = Field(default_factory=list)
    coverage: dict[str, Any] | None = None
    distributions: list[ForecastDistribution] = Field(default_factory=list)
    distributions_status: FreshnessState = FreshnessState.UNAVAILABLE
    distributions_reason: str | None = None
    observation_status: FreshnessState = FreshnessState.UNAVAILABLE
    observation_reason: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


class DecisionRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    decision_id: str
    decided_at_utc: datetime | None = None
    market_id: str
    market_question: str | None = None
    bracket_label: str | None = None
    target_date: date | None = None
    direction: str | None = None
    state: str
    mode: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_probability: float | None = None
    market_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    net_edge_usd: float | None = None
    primary_reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    snapshot_age_ms: int | None = None
    forecast_age_hours: float | None = None
    available_depth_usd: float | None = None
    thresholds_version: str | None = None
    run_id: str | None = None


class DecisionsData(BaseModel):
    """Row window (when the producer is integrated) plus always-on aggregates.

    ``rows_status`` is independent of the aggregate totals: totals come from
    the shadow report snapshot and are real even while the row list is not
    available. The UI shows both states separately.
    """

    rows: list[DecisionRow] = Field(default_factory=list)
    rows_status: FreshnessState = FreshnessState.UNAVAILABLE
    rows_reason: str | None = None
    total_rows: int | None = None
    next_cursor: str | None = None

    totals: dict[str, Any] | None = None
    totals_status: FreshnessState = FreshnessState.MISSING
    totals_source: str | None = None

    last_cycle: dict[str, Any] | None = None
    last_cycle_status: FreshnessState = FreshnessState.MISSING

    disclaimer: str = (
        "ACCEPT 表示符合当前模型与风险过滤条件，不代表已验证的盈利机会。"
        " ACCEPT means the filters passed; it is not evidence of a profitable trade."
    )
    filters_applied: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# overview / system
# ---------------------------------------------------------------------------


class SystemSourceHealth(BaseModel):
    name: str
    state: FreshnessState
    status: str = "unknown"
    reasons: list[str] = Field(default_factory=list)
    age_seconds: float | None = None


class HealthzData(BaseModel):
    """Liveness payload: the dashboard is serving, plus source states."""

    status: str = "ok"
    uptime_seconds: float = 0.0
    sources: dict[str, str] = Field(default_factory=dict)


class OverviewData(BaseModel):
    collector: CollectorHealthData | None = None
    shadow: ShadowHealthData | None = None
    report: ShadowReportData | None = None
    active_markets: int | None = None
    active_markets_source: str | None = None
    subscribed_tokens: int | None = None
    latest_book_age_seconds: float | None = None
    current_cycle_decisions: dict[str, Any] | None = None
    decision_totals: dict[str, Any] | None = None
    decision_totals_source: str | None = None
    decision_totals_status: FreshnessState = FreshnessState.MISSING
    model_vs_market: list[MarketSummary] = Field(default_factory=list)
    model_vs_market_status: FreshnessState = FreshnessState.UNAVAILABLE
    model_vs_market_reason: str | None = None
    system_health: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class SystemData(BaseModel):
    collector: CollectorHealthData | None = None
    collector_metrics: CollectorMetricsData | None = None
    shadow: ShadowHealthData | None = None
    report: ShadowReportData | None = None
    trends: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)


class PerformanceData(BaseModel):
    """A performance snapshot, passed through with its own scope attached.

    Deliberately open: the payload carries ``statistical_scope`` and
    ``warnings`` as *data*, because those two fields are what stop an edge
    total being repeated as a return. Narrowing them into a fixed schema
    would drop the caveat that travels with the number.

    ``reason`` appears instead of ``data`` when the snapshot has not been
    published - a missing backfill reads as missing, never as zeros.
    """

    model_config = ConfigDict(extra="allow")

    reason: str | None = None
    statistical_scope: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sample_size: int | None = None
    source: str | None = None
    generated_at_utc: datetime | None = None
    as_of_utc: datetime | None = None
    dataset: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

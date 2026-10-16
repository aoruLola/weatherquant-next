"""Multi-layer freshness for the performance snapshots.

A published performance snapshot is old for four different reasons, and the
Dashboard has to say *which* one is happening:

``export_data_lag``
    How long since the Shadow last produced decision data. A large value means
    the *upstream* pipeline stopped - the research engine cannot be fresher
    than the export it reads. Read live from the export's own ``state.json``.
``ingestion_lag``
    How far the research engine is behind the export: the export watermark
    minus the watermark the current snapshot consumed. A large value means
    ingest is not running.
``publication_lag``
    How long the snapshot waited between consuming its data and being
    published. A large value means the publish job is not running.
``snapshot_age``
    How long since the snapshot was published. This one is *expected* to be
    as large as the publish interval - it is not on its own evidence of a
    fault, which is why it is judged against the configured interval rather
    than a fixed 300 seconds.

The distinction matters because the obvious implementation - "stale if the
snapshot is older than N seconds" - reports a fault for the entire interval
between two healthy publishes. With a 20-minute publish cadence and a 300s
threshold the page would read ``stale`` for 17 minutes out of every 20 while
everything was working.

The live export watermark is read from the export directory, never inferred
from the snapshot's own manifest: a manifest watermark is a record of what was
consumed *at build time*, and presenting it as the current export position
would make a stopped export look caught-up. When the export state cannot be
read, ``export_data_lag`` is ``None`` and the reason says so - it is not
silently substituted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from weatherquant.dashboard.contracts import FreshnessState

__all__ = [
    "ExportState",
    "IngestState",
    "PerformanceLayers",
    "evaluate_layers",
    "read_export_state",
    "read_ingest_state",
]


@dataclass(frozen=True, slots=True)
class ExportState:
    """What the Shadow's export says about itself, read live.

    ``watermark`` is ``(business_time, cursor_hash)``. ``readable`` is False
    when the file is absent, unparseable, or carries no usable watermark -
    and then ``reason`` says which, because "no export mounted" and "export
    mounted but silent" are different faults.
    """

    readable: bool
    watermark_time: datetime | None = None
    watermark_hash: str | None = None
    last_run_at_utc: datetime | None = None
    rows_exported: int | None = None
    next_part: int | None = None
    reason: str | None = None

    @classmethod
    def unreadable(cls, reason: str) -> ExportState:
        return cls(readable=False, reason=reason)


def read_export_state(directory: str | Path | None) -> ExportState:
    """Read the export's ``state.json``.

    Returns an unreadable ExportState rather than raising: a Dashboard has to
    render something when the export is not mounted, and "I cannot see the
    export" is itself the finding.
    """
    if directory is None:
        return ExportState.unreadable("no shadow export directory is configured")
    path = Path(directory) / "state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ExportState.unreadable(f"the shadow export state is not present at {path.name}")
    except (OSError, ValueError) as exc:
        return ExportState.unreadable(f"the shadow export state could not be read: {exc}")

    if not isinstance(raw, dict):
        return ExportState.unreadable("the shadow export state is not a JSON object")

    watermark = raw.get("watermark")
    if not isinstance(watermark, (list, tuple)) or not watermark:
        return ExportState.unreadable("the shadow export state carries no watermark")

    watermark_time = _parse(watermark[0])
    if watermark_time is None:
        return ExportState.unreadable("the shadow export watermark is not a timestamp")

    watermark_hash = str(watermark[1]) if len(watermark) > 1 and watermark[1] else None
    return ExportState(
        readable=True,
        watermark_time=watermark_time,
        watermark_hash=watermark_hash,
        last_run_at_utc=_parse(raw.get("last_run_at_utc")),
        rows_exported=_as_int(raw.get("rows_exported")),
        next_part=_as_int(raw.get("next_part")),
    )


@dataclass(frozen=True, slots=True)
class IngestState:
    """How far ingest has got, read from ``ingest-state.json``.

    Distinct from the published snapshot's watermark on purpose: the snapshot's
    is frozen at publish time, so between two publishes it is *expected* to
    trail the live export by a whole publish interval. Judging ingest by that
    would report a stall every interval.
    """

    readable: bool
    watermark_time: datetime | None = None
    committed_at_utc: datetime | None = None
    rows_consumed: int | None = None
    reason: str | None = None

    @classmethod
    def unreadable(cls, reason: str) -> IngestState:
        return cls(readable=False, reason=reason)


def read_ingest_state(directory: str | Path | None) -> IngestState:
    """Read the ingest position the ingest runs maintain."""
    if directory is None:
        return IngestState.unreadable("no performance directory is configured")
    path = Path(directory) / "ingest-state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return IngestState.unreadable(
            "no ingest has recorded its position yet (ingest-state.json is absent)"
        )
    except (OSError, ValueError) as exc:
        return IngestState.unreadable(f"the ingest state could not be read: {exc}")
    if not isinstance(raw, dict):
        return IngestState.unreadable("the ingest state is not a JSON object")

    watermark = raw.get("watermark")
    if not isinstance(watermark, (list, tuple)) or not watermark:
        return IngestState.unreadable("the ingest state carries no watermark")
    watermark_time = _parse(watermark[0])
    if watermark_time is None:
        return IngestState.unreadable("the ingest watermark is not a timestamp")

    return IngestState(
        readable=True,
        watermark_time=watermark_time,
        committed_at_utc=_parse(raw.get("committed_at_utc")),
        rows_consumed=_as_int(raw.get("rows_consumed")),
    )


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    from datetime import UTC

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class PerformanceLayers:
    """The four lags, and the verdict they add up to.

    Every lag is ``None`` when its inputs are unavailable, and ``None`` is
    never treated as zero: "I cannot measure this" must not read as "this is
    fine".
    """

    export_data_lag_seconds: float | None = None
    ingestion_lag_seconds: float | None = None
    publication_lag_seconds: float | None = None
    snapshot_age_seconds: float | None = None

    export_watermark_time: datetime | None = None
    consumed_watermark_time: datetime | None = None
    published_at_utc: datetime | None = None

    state: FreshnessState = FreshnessState.FRESH
    reason: str | None = None
    lagging_layer: str | None = None


def evaluate_layers(
    *,
    now: datetime,
    export_state: ExportState,
    ingest_state: IngestState,
    snapshot_consumed_time: datetime | None,
    published_at_utc: datetime | None,
    publish_interval_seconds: float,
    export_stale_after_seconds: float,
    ingest_stale_after_seconds: float,
    publication_stale_after_seconds: float,
) -> PerformanceLayers:
    """Compute the four lags and decide which layer, if any, is behind.

    Each lag is measured against the *live* position of the layer upstream of
    it, never against the published snapshot's frozen watermark:

    ``export_data_lag``
        now minus the live export watermark - is the shadow still producing?
    ``ingestion_lag``
        live export watermark minus the live ingest position - is ingest
        keeping up with the export?
    ``publication_lag``
        live ingest position minus the watermark the current snapshot
        consumed - is publish keeping up with ingest?
    ``snapshot_age``
        now minus the publish time, judged against the publish interval,
        because a snapshot one interval old is the normal state of a healthy
        system.
    """
    export_lag = _lag(now, export_state.watermark_time)
    ingestion_lag = (
        _diff(export_state.watermark_time, ingest_state.watermark_time)
        if export_state.readable and ingest_state.readable
        else None
    )
    publication_lag = (
        _diff(ingest_state.watermark_time, snapshot_consumed_time)
        if ingest_state.readable
        else None
    )
    snapshot_age = _lag(now, published_at_utc)

    layers = PerformanceLayers(
        export_data_lag_seconds=export_lag,
        ingestion_lag_seconds=ingestion_lag,
        publication_lag_seconds=publication_lag,
        snapshot_age_seconds=snapshot_age,
        export_watermark_time=export_state.watermark_time,
        consumed_watermark_time=ingest_state.watermark_time,
        published_at_utc=published_at_utc,
    )

    # Which layer is behind, most upstream first: a stopped export explains
    # every lag downstream of it, and naming the upstream one is the actionable
    # finding.
    if export_state.readable and export_lag is not None and export_lag > export_stale_after_seconds:
        return _verdict(
            layers,
            "shadow_export",
            f"the shadow export has produced no new data for {export_lag:.0f}s "
            f"(limit {export_stale_after_seconds:.0f}s)",
        )

    if ingestion_lag is not None and ingestion_lag > ingest_stale_after_seconds:
        return _verdict(
            layers,
            "ingestion",
            f"ingest is {ingestion_lag:.0f}s behind the shadow export "
            f"(limit {ingest_stale_after_seconds:.0f}s)",
        )

    if publication_lag is not None and publication_lag > publication_stale_after_seconds:
        return _verdict(
            layers,
            "publication",
            f"the snapshot is {publication_lag:.0f}s behind what ingest has consumed "
            f"(limit {publication_stale_after_seconds:.0f}s)",
        )

    # The snapshot's own age is only a fault once it exceeds the interval it is
    # *supposed* to be republished on.
    if snapshot_age is not None and snapshot_age > publish_interval_seconds:
        return _verdict(
            layers,
            "publish_interval",
            f"the snapshot is {snapshot_age:.0f}s old, beyond the "
            f"{publish_interval_seconds:.0f}s publish interval",
        )

    # An unreadable export or ingest state is not a freshness verdict on the
    # snapshot - the snapshot may be perfectly current. It is reported, but it
    # does not make the snapshot stale, because the Dashboard cannot know that
    # it is.
    unknown = _unknown_reason(export_state, ingest_state)
    if unknown is not None:
        return PerformanceLayers(
            **{
                **_as_kwargs(layers),
                "state": FreshnessState.FRESH,
                "reason": f"snapshot is current, but {unknown}",
                "lagging_layer": None,
            }
        )

    return layers


def _unknown_reason(export_state: ExportState, ingest_state: IngestState) -> str | None:
    """What could not be measured, or None when everything was measurable."""
    parts = []
    if not export_state.readable:
        parts.append(f"upstream freshness is unknown: {export_state.reason}")
    if not ingest_state.readable:
        parts.append(f"ingest freshness is unknown: {ingest_state.reason}")
    return "; ".join(parts) if parts else None


def _as_kwargs(layers: PerformanceLayers) -> dict[str, Any]:
    return {
        "export_data_lag_seconds": layers.export_data_lag_seconds,
        "ingestion_lag_seconds": layers.ingestion_lag_seconds,
        "publication_lag_seconds": layers.publication_lag_seconds,
        "snapshot_age_seconds": layers.snapshot_age_seconds,
        "export_watermark_time": layers.export_watermark_time,
        "consumed_watermark_time": layers.consumed_watermark_time,
        "published_at_utc": layers.published_at_utc,
    }


def _verdict(layers: PerformanceLayers, layer: str, reason: str) -> PerformanceLayers:
    return PerformanceLayers(
        **{**_as_kwargs(layers), "state": FreshnessState.STALE, "reason": reason, "lagging_layer": layer}
    )


def _lag(now: datetime, then: datetime | None) -> float | None:
    if then is None:
        return None
    return round((now - then).total_seconds(), 3)


def _diff(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 3)

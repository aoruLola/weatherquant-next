"""Multi-layer freshness: which layer is behind, and how that is decided.

The property under test throughout is that a *healthy* system is never
reported as stale. Each lag is measured against the live position of the layer
upstream of it, so the publish interval does not leak into the ingest verdict -
the bug that made a working 20-minute cadence read as permanently stale.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from weatherquant.dashboard.contracts import FreshnessState
from weatherquant.dashboard.performance_freshness import (
    ExportState,
    IngestState,
    evaluate_layers,
    read_export_state,
    read_ingest_state,
)

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

# Thresholds mirroring the DashboardSettings defaults.
PUBLISH_INTERVAL = 1200.0 + 300.0  # interval + grace
EXPORT_STALE = 900.0
INGEST_STALE = 900.0
PUBLICATION_STALE = 1500.0  # == interval + grace


def _evaluate(
    *,
    export_state: ExportState,
    ingest_ago: float | None = 60.0,
    consumed_ago: float | None = 1200.0,
    published_ago: float | None = 1140.0,
):
    ingest_state = (
        IngestState(readable=True, watermark_time=NOW - timedelta(seconds=ingest_ago))
        if ingest_ago is not None
        else IngestState.unreadable("no ingest state recorded")
    )
    return evaluate_layers(
        now=NOW,
        export_state=export_state,
        ingest_state=ingest_state,
        snapshot_consumed_time=(
            NOW - timedelta(seconds=consumed_ago) if consumed_ago is not None else None
        ),
        published_at_utc=(
            NOW - timedelta(seconds=published_ago) if published_ago is not None else None
        ),
        publish_interval_seconds=PUBLISH_INTERVAL,
        export_stale_after_seconds=EXPORT_STALE,
        ingest_stale_after_seconds=INGEST_STALE,
        publication_stale_after_seconds=PUBLICATION_STALE,
    )


def _healthy_export(ago: float = 60.0) -> ExportState:
    return ExportState(
        readable=True,
        watermark_time=NOW - timedelta(seconds=ago),
        watermark_hash="abc123",
    )


# --------------------------------------------------------------------------
# reading the live export state
# --------------------------------------------------------------------------


def test_read_export_state_parses_a_real_state_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        (directory / "state.json").write_text(
            json.dumps(
                {
                    "next_part": 444,
                    "rows_exported": 2074358,
                    "watermark": ["2026-10-16T16:30:53.911095+00:00", "fc908dfa"],
                    "last_run_at_utc": "2026-10-16T16:31:05.275245+00:00",
                }
            )
        )
        state = read_export_state(directory)
        assert state.readable
        assert state.watermark_time is not None
        assert state.watermark_time.isoformat() == "2026-10-16T16:30:53.911095+00:00"
        assert state.watermark_hash == "fc908dfa"
        assert state.rows_exported == 2074358
        assert state.next_part == 444


def test_read_export_state_reports_absent_file_rather_than_raising():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = read_export_state(Path(tmpdir))
        assert not state.readable
        assert "not present" in (state.reason or "")


def test_read_export_state_rejects_a_state_without_a_watermark():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        (directory / "state.json").write_text(json.dumps({"next_part": 1}))
        state = read_export_state(directory)
        assert not state.readable
        assert "no watermark" in (state.reason or "")


def test_read_export_state_rejects_unparseable_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        (directory / "state.json").write_text("{ not json")
        state = read_export_state(directory)
        assert not state.readable
        assert "could not be read" in (state.reason or "")


def test_no_configured_export_directory_is_unreadable():
    state = read_export_state(None)
    assert not state.readable
    assert "no shadow export directory is configured" in (state.reason or "")


# --------------------------------------------------------------------------
# reading the ingest position
# --------------------------------------------------------------------------


def test_read_ingest_state_parses_the_recorded_position():
    with tempfile.TemporaryDirectory() as tmpdir:
        directory = Path(tmpdir)
        (directory / "ingest-state.json").write_text(
            json.dumps(
                {
                    "watermark": ["2026-10-16T11:59:00+00:00", "hash"],
                    "committed_at_utc": "2026-10-16T11:59:05+00:00",
                    "rows_consumed": 1584,
                }
            )
        )
        state = read_ingest_state(directory)
        assert state.readable
        assert state.watermark_time is not None
        assert state.watermark_time.isoformat() == "2026-10-16T11:59:00+00:00"
        assert state.rows_consumed == 1584


def test_read_ingest_state_reports_absence_with_a_reason():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = read_ingest_state(Path(tmpdir))
        assert not state.readable
        assert "ingest-state.json is absent" in (state.reason or "")


# --------------------------------------------------------------------------
# the four lags
# --------------------------------------------------------------------------


def test_healthy_pipeline_is_fresh_with_all_four_lags_measured():
    """A snapshot one publish interval old is NORMAL, not stale."""
    layers = _evaluate(
        export_state=_healthy_export(ago=60),
        ingest_ago=60,
        consumed_ago=1200,
        published_ago=1140,
    )
    assert layers.state is FreshnessState.FRESH
    assert layers.lagging_layer is None
    assert layers.export_data_lag_seconds == 60.0
    assert layers.ingestion_lag_seconds == 0.0
    # ingest is 60s old, the snapshot consumed 1200s-old data -> 1140s behind
    assert layers.publication_lag_seconds == 1140.0
    assert layers.snapshot_age_seconds == 1140.0


def test_a_snapshot_just_under_the_interval_is_fresh():
    """The regression this whole module exists for: 19 minutes old with a
    20-minute cadence must not read as stale."""
    layers = _evaluate(
        export_state=_healthy_export(ago=30),
        ingest_ago=30,
        consumed_ago=1140,
        published_ago=1140,
    )
    assert layers.state is FreshnessState.FRESH
    assert layers.lagging_layer is None


def test_publish_interval_judges_the_snapshot_age_when_ingest_cannot_be_read():
    """Without an ingest position the only measurable thing is how old the
    snapshot is, and then the publish interval is the right yardstick."""
    layers = _evaluate(
        export_state=_healthy_export(ago=30),
        ingest_ago=None,
        consumed_ago=None,
        published_ago=1600,
    )
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "publish_interval"
    assert "publish interval" in (layers.reason or "")


def test_stopped_shadow_export_is_named_as_the_upstream_cause():
    layers = _evaluate(
        export_state=_healthy_export(ago=4000),
        ingest_ago=4000,
        consumed_ago=4000,
        published_ago=100,
    )
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "shadow_export"
    assert "shadow export" in (layers.reason or "")


def test_ingest_falling_behind_the_export_is_named_as_ingestion():
    """Export is current but ingest is far behind it: ingest is the layer that
    stopped, and calling it an export stall would be wrong."""
    layers = _evaluate(
        export_state=_healthy_export(ago=60),
        ingest_ago=4000,
        consumed_ago=4000,
        published_ago=100,
    )
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "ingestion"
    assert "ingest is" in (layers.reason or "")


def test_export_lag_is_reported_before_ingestion_lag():
    """Both are behind; the upstream one is the actionable finding."""
    layers = _evaluate(
        export_state=_healthy_export(ago=5000),
        ingest_ago=9000,
        consumed_ago=9000,
        published_ago=100,
    )
    assert layers.lagging_layer == "shadow_export"


def test_publish_falling_behind_ingest_is_named_as_publication():
    """Ingest is current but the snapshot consumed much older data: publish is
    the layer that stopped."""
    layers = _evaluate(
        export_state=_healthy_export(ago=60),
        ingest_ago=60,
        consumed_ago=5000,
        published_ago=100,
    )
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "publication"
    assert "behind what ingest has consumed" in (layers.reason or "")


# --------------------------------------------------------------------------
# unmeasurable is not the same as fine
# --------------------------------------------------------------------------


def test_unreadable_export_does_not_claim_the_snapshot_is_stale():
    """The snapshot may be perfectly current. We cannot see the export, so we
    report that - we do not invent a verdict from the manifest."""
    layers = _evaluate(
        export_state=ExportState.unreadable("export not mounted"),
        ingest_ago=60,
        consumed_ago=100,
        published_ago=100,
    )
    assert layers.export_data_lag_seconds is None
    assert layers.ingestion_lag_seconds is None
    assert layers.state is FreshnessState.FRESH
    assert "upstream freshness is unknown" in (layers.reason or "")
    assert layers.lagging_layer is None


def test_unreadable_export_still_reports_a_stale_snapshot():
    """An unreadable export does not mask a snapshot that is genuinely old."""
    layers = _evaluate(
        export_state=ExportState.unreadable("export not mounted"),
        ingest_ago=None,
        consumed_ago=None,
        published_ago=5000,
    )
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "publish_interval"


def test_missing_ingest_state_is_reported_but_not_invented():
    layers = _evaluate(
        export_state=_healthy_export(),
        ingest_ago=None,
        consumed_ago=100,
        published_ago=100,
    )
    assert layers.ingestion_lag_seconds is None
    assert layers.publication_lag_seconds is None
    assert layers.state is FreshnessState.FRESH
    assert "ingest freshness is unknown" in (layers.reason or "")


def test_missing_publish_time_leaves_snapshot_age_unmeasured():
    layers = _evaluate(
        export_state=_healthy_export(), ingest_ago=60, consumed_ago=100, published_ago=None
    )
    assert layers.snapshot_age_seconds is None
    # Nothing measurable is behind, so it does not manufacture a stale verdict.
    assert layers.state is FreshnessState.FRESH


def test_export_lag_is_measured_even_when_ingest_is_unreadable():
    """Each lag stands on its own inputs: a stopped export is still visible
    when the ingest state cannot be read."""
    layers = _evaluate(
        export_state=_healthy_export(ago=4000),
        ingest_ago=None,
        consumed_ago=None,
        published_ago=100,
    )
    assert layers.export_data_lag_seconds == 4000.0
    assert layers.ingestion_lag_seconds is None
    assert layers.state is FreshnessState.STALE
    assert layers.lagging_layer == "shadow_export"

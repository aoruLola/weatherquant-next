"""Layered health API: Export -> Performance Consume -> Publish -> Snapshot.

The properties these tests hold:

* a healthy 20-minute publish cadence is not reported as stale;
* each layer reports the watermark it actually read, its age and its limit;
* the first layer that crossed its limit is named, most upstream first;
* a manifest watermark never stands in for the live export position;
* missing sources are ``UNKNOWN`` with a reason, never invented values;
* publish and snapshot come from one generation in one response.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import build_tree, write_json

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.performance.snapshot import (
    build_performance,
    publish_performance,
    resolve_generation,
)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


def _publish_generation(
    root: Path,
    *,
    now: datetime,
    published_ago: float,
    consumed_ago: float,
    interval: float = 1200.0,
    watermark_ago: float | None = None,
) -> dict[str, str]:
    """Publish one generation and backdate its clocks by controlled amounts."""
    built_at = now - timedelta(seconds=consumed_ago)
    payloads = build_performance(
        decisions=[],
        settlements=[],
        now=built_at,
        dataset="fixture",
    )
    publish_performance(payloads, root)
    generation = resolve_generation(root)
    assert generation.directory is not None

    manifest_path = generation.directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["published_at_utc"] = _iso(now - timedelta(seconds=published_ago))
    manifest["watermark"] = [
        _iso(now - timedelta(seconds=watermark_ago if watermark_ago is not None else consumed_ago)),
        "fixture-cursor",
    ]
    write_json(manifest_path, manifest)

    summary_path = generation.directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["generated_at_utc"] = _iso(now - timedelta(seconds=published_ago))
    summary["as_of_utc"] = _iso(built_at)
    summary["stale_after_seconds"] = interval
    write_json(summary_path, summary)
    return {
        "generation_id": str(manifest["generation_id"]),
        "published_at": manifest["published_at_utc"],
        "consumed_at": summary["as_of_utc"],
    }


def _write_export(root: Path, *, now: datetime, ago: float) -> None:
    write_json(
        root / "reports" / "decisions-export" / "state.json",
        {
            "next_part": 1,
            "rows_exported": 10,
            "watermark": [_iso(now - timedelta(seconds=ago)), "export-cursor"],
            "last_run_at_utc": _iso(now),
        },
    )


def _write_ingest(root: Path, *, now: datetime, ago: float) -> None:
    write_json(
        root / "ingest-state.json",
        {
            "watermark": [_iso(now - timedelta(seconds=ago)), "ingest-cursor"],
            "last_run_at_utc": _iso(now),
        },
    )


def _client(tmp_path: Path) -> TestClient:
    settings = DashboardSettings(
        collector_data_dir=tmp_path / "collector",
        shadow_data_dir=tmp_path / "shadow",
        performance_data_dir=tmp_path / "performance",
        paper_api_timeout_seconds=0.05,
        rate_limit_per_minute=0,
    )
    return TestClient(create_app(settings))


def _layers(client: TestClient) -> dict:
    response = client.get("/api/v1/health/layers")
    assert response.status_code == 200, response.text
    return response.json()


def _by_name(data: dict) -> dict[str, dict]:
    return {layer["name"]: layer for layer in data["layers"]}


# ---------------------------------------------------------------------------
# healthy cadence
# ---------------------------------------------------------------------------


def test_healthy_20_minute_cadence_is_not_stale(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    _publish_generation(
        performance,
        now=now,
        published_ago=1140,
        consumed_ago=1200,
    )
    _write_export(tmp_path / "shadow", now=now, ago=60)
    _write_ingest(performance, now=now, ago=60)

    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    assert data["lagging_layer"] is None
    assert data["lagging_basis"] is None
    assert layers["export"]["status"] == "OK"
    assert layers["performance_consume"]["status"] == "OK"
    assert layers["publish"]["status"] == "OK"
    # 19 minutes old: normal for a 20-minute publish interval with grace.
    assert layers["snapshot"]["status"] == "OK"
    assert layers["snapshot"]["age_seconds"] == pytest.approx(1140, abs=5)
    assert layers["snapshot"]["threshold_seconds"] == pytest.approx(1500)
    assert body["freshness"]["state"] == "fresh"


# ---------------------------------------------------------------------------
# one layer crossing its limit
# ---------------------------------------------------------------------------


def test_stale_export_is_named_and_downstream_layers_lag(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=4000,
    )
    _write_export(tmp_path / "shadow", now=now, ago=4000)
    _write_ingest(performance, now=now, ago=4000)

    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    assert layers["export"]["status"] == "STALE"
    assert data["lagging_layer"] == "export"
    assert "shadow export" in data["lagging_basis"]
    # They are within their own limits but cannot advance past the export.
    assert layers["performance_consume"]["status"] == "LAGGING"
    assert layers["publish"]["status"] == "LAGGING"
    assert layers["snapshot"]["status"] == "LAGGING"
    assert body["freshness"]["state"] == "stale"
    # The envelope keeps the pipeline vocabulary the existing UI labels.
    assert body["freshness"]["lagging_layer"] == "shadow_export"


def test_stale_ingest_is_named_before_publish(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=2000,
    )
    _write_export(tmp_path / "shadow", now=now, ago=10)
    _write_ingest(performance, now=now, ago=2000)

    with _client(tmp_path) as client:
        body = _layers(client)

    layers = _by_name(body["data"])
    assert layers["export"]["status"] == "OK"
    assert layers["performance_consume"]["status"] == "STALE"
    assert layers["performance_consume"]["lag_seconds"] == pytest.approx(1990, abs=5)
    assert body["data"]["lagging_layer"] == "performance_consume"
    assert body["freshness"]["lagging_layer"] == "ingestion"


def test_stale_publish_is_named(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=5000,
    )
    _write_export(tmp_path / "shadow", now=now, ago=10)
    _write_ingest(performance, now=now, ago=10)

    with _client(tmp_path) as client:
        body = _layers(client)

    layers = _by_name(body["data"])
    assert layers["performance_consume"]["status"] == "OK"
    assert layers["publish"]["status"] == "STALE"
    assert layers["publish"]["lag_seconds"] == pytest.approx(4990, abs=5)
    assert body["data"]["lagging_layer"] == "publish"
    assert body["freshness"]["lagging_layer"] == "publication"


def test_old_snapshot_is_named_when_ingest_is_unreadable(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    _publish_generation(
        performance,
        now=now,
        published_ago=1600,
        consumed_ago=1600,
    )
    _write_export(tmp_path / "shadow", now=now, ago=60)
    # No ingest-state.json: publication lag is unknowable, but snapshot age is.
    with _client(tmp_path) as client:
        body = _layers(client)

    layers = _by_name(body["data"])
    assert layers["publish"]["status"] == "UNKNOWN"
    assert "ingest" in (layers["publish"]["reason"] or "")
    assert layers["snapshot"]["status"] == "STALE"
    assert body["data"]["lagging_layer"] == "snapshot"
    assert body["freshness"]["lagging_layer"] == "publish_interval"


# ---------------------------------------------------------------------------
# provenance and generation consistency
# ---------------------------------------------------------------------------


def test_manifest_watermark_never_stands_in_for_the_live_export(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    published = _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=10,
        watermark_ago=10,
    )
    # The live export is four thousand seconds behind, while the manifest
    # still carries a current consumed watermark. Reading the manifest as
    # "export" would make this stopped export look caught up.
    _write_export(tmp_path / "shadow", now=now, ago=4000)
    _write_ingest(performance, now=now, ago=10)

    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    export_watermark = datetime.fromisoformat(layers["export"]["watermark_utc"])
    manifest_watermark = datetime.fromisoformat(published["consumed_at"])
    assert export_watermark < manifest_watermark - timedelta(seconds=3900)
    assert layers["export"]["status"] == "STALE"
    assert layers["export"]["source"].startswith("shadow export")
    assert data["lagging_layer"] == "export"
    assert datetime.fromisoformat(layers["publish"]["watermark_utc"]) == datetime.fromisoformat(
        published["consumed_at"]
    )


def test_publish_and_snapshot_come_from_one_generation(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    published = _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=120,
    )
    _write_export(tmp_path / "shadow", now=now, ago=30)
    _write_ingest(performance, now=now, ago=30)

    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    assert data["generation_id"] == published["generation_id"]
    assert datetime.fromisoformat(layers["publish"]["watermark_utc"]) == datetime.fromisoformat(
        published["consumed_at"]
    )
    assert datetime.fromisoformat(layers["snapshot"]["watermark_utc"]) == datetime.fromisoformat(
        published["published_at"]
    )
    assert "generation" in data["generation_source"]


def test_publish_watermark_falls_back_to_the_manifest_and_says_so(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    published = _publish_generation(
        performance,
        now=now,
        published_ago=60,
        consumed_ago=500,
    )
    generation = resolve_generation(performance)
    assert generation.directory is not None
    summary_path = generation.directory / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.pop("as_of_utc", None)
    write_json(summary_path, summary)
    _write_export(tmp_path / "shadow", now=now, ago=30)
    _write_ingest(performance, now=now, ago=10)

    with _client(tmp_path) as client:
        body = _layers(client)

    layers = _by_name(body["data"])
    assert layers["publish"]["source"] == "generation manifest.json · watermark[0]"
    assert datetime.fromisoformat(layers["publish"]["watermark_utc"]) == datetime.fromisoformat(
        published["consumed_at"]
    )


def test_broken_generation_pointer_does_not_fall_back_to_flat_files(tmp_path: Path):
    now = datetime.now(tz=UTC)
    performance = tmp_path / "performance"
    # A previous flat batch still sits in the directory; a broken pointer must
    # not silently present it as the current generation.
    write_json(
        performance / "summary.json",
        {
            "schema_version": "1.0",
            "kind": "performance_snapshot",
            "generated_at_utc": _iso(now),
            "as_of_utc": _iso(now - timedelta(seconds=120)),
            "stale_after_seconds": 1200,
            "dataset": "fixture",
        },
    )
    write_json(performance / "current.json", {"generation_id": "half-published"})
    _write_export(tmp_path / "shadow", now=now, ago=30)
    _write_ingest(performance, now=now, ago=30)

    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    assert data["generation_id"] is None
    assert "broken" in data["generation_source"]
    assert layers["publish"]["status"] == "UNKNOWN"
    assert layers["snapshot"]["status"] == "UNKNOWN"
    assert body["freshness"]["state"] == "unavailable"


def test_missing_everything_is_unknown_with_reasons(tmp_path: Path):
    with _client(tmp_path) as client:
        body = _layers(client)

    data = body["data"]
    layers = _by_name(data)
    assert set(layers) == {"export", "performance_consume", "publish", "snapshot"}
    assert all(layer["status"] == "UNKNOWN" for layer in layers.values())
    assert all(layer["watermark_utc"] is None for layer in layers.values())
    assert all(layer["reason"] for layer in layers.values())
    assert data["lagging_layer"] is None
    assert body["freshness"]["state"] == "unavailable"


# ---------------------------------------------------------------------------
# collector / shadow / paper
# ---------------------------------------------------------------------------


def test_service_statuses_reuse_heartbeats_and_report_paper_unavailable(tmp_path: Path):
    now = datetime.now(tz=UTC)
    build_tree(
        tmp_path,
        now=now,
        collector=True,
        shadow=True,
        report=False,
        archive=False,
    )

    with _client(tmp_path) as client:
        body = _layers(client)

    services = body["data"]["services"]
    assert services["collector"]["status"] == "OK"
    assert services["shadow"]["status"] == "OK"
    # No Paper engine is reachable in a fixture tree; that is UNKNOWN, not OK.
    assert services["paper"]["status"] == "UNKNOWN"
    assert services["paper"]["reason"]


def test_missing_services_are_unknown_with_reasons(tmp_path: Path):
    with _client(tmp_path) as client:
        body = _layers(client)

    services = body["data"]["services"]
    assert services["collector"]["status"] == "UNKNOWN"
    assert services["shadow"]["status"] == "UNKNOWN"
    assert services["collector"]["reason"]
    assert services["shadow"]["reason"]

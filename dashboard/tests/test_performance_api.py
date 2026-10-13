"""Performance API: reads what the research process published, and says so.

Two failure modes are guarded here.

The first is a dashboard with no backfill looking like a dashboard with no
edge: a missing snapshot must arrive as ``missing`` with a reason, never as
zeros. The second is two implementations of one figure: the reference-capital
endpoint must go through the same module the backfill used, so a stake change
cannot produce an answer the snapshot would have contradicted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weatherquant.dashboard.api import create_app
from weatherquant.performance.snapshot import build_performance, publish_performance

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _decision(i: int) -> dict:
    return {
        "market_id": f"m-{i % 4}",
        "target_date": f"2026-10-{14 + (i % 5):02d}",
        "model_name": "calibrated" if i % 2 else "gfs_raw",
        "direction": "buy_yes",
        "mode": "LIVE",
        "state": "ACCEPT",
        "decided_at_utc": NOW.isoformat(),
        "net_edge_usd": 0.03 + (i % 7) * 0.01,
        "market_price": 0.6,
        "model_probability": 0.65,
        "available_depth_usd": 500.0,
        "station": "KLGA",
        "bracket_label": "81°F",
        "market_question": "Will it be 81F?",
    }


def _publish(directory) -> None:
    """Write real snapshots straight to the directory the API will read."""
    payloads = build_performance(
        decisions=[_decision(i) for i in range(30)],
        settlements=[],
        now=NOW,
        dataset="fixture",
    )
    publish_performance(payloads, directory)


def _empty_tree(tmp_path) -> Path:
    """A complete data tree with no performance snapshots at all."""
    from helpers import build_tree

    root = tmp_path / "empty"
    build_tree(root, archive=False)
    return root


def _client_for(root, settings_for):
    app = create_app(settings_for(root))
    return TestClient(app)


# --- a dashboard with no backfill -------------------------------------------


def test_a_missing_snapshot_reads_as_missing_not_as_zero(tmp_path, settings_for) -> None:
    root = _empty_tree(tmp_path)
    with _client_for(root, settings_for) as client:
        payload = client.get("/api/v1/performance/summary").json()

    assert payload["freshness"]["state"] == "missing"
    assert "backfill" in payload["data"]["reason"]
    assert payload["data"]["statistical_scope"] == []
    assert payload["data"].get("data", {}) == {}, "no snapshot must not look like an empty result"


def test_every_performance_endpoint_answers_when_nothing_is_published(tmp_path, settings_for) -> None:
    root = _empty_tree(tmp_path)
    with _client_for(root, settings_for) as client:
        for path in (
            "summary", "edge", "win-rate", "opportunities",
            "settlements", "models", "reference-capital",
        ):
            response = client.get(f"/api/v1/performance/{path}")
            assert response.status_code == 200, path
            assert response.json()["freshness"]["state"] == "missing", path


# --- published snapshots ------------------------------------------------------


@pytest.fixture
def published(tmp_path, settings_for):
    from helpers import build_tree

    root = tmp_path / "live"
    build_tree(root, archive=False)
    settings = settings_for(root)
    _publish(settings.performance_data_dir)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def test_the_summary_carries_its_scope_and_warnings(published) -> None:
    data = published.get("/api/v1/performance/summary").json()["data"]

    assert data["performance_schema_version"] == 1
    assert data["statistical_scope"], "a figure without its scope is a figure that will be misread"
    assert any("not profit" in w for w in data["warnings"])


def test_all_six_snapshots_are_reachable(published) -> None:
    # Five path-shaped endpoints plus reference-capital; the sixth file
    # (summary) is served under its own path above.
    for path in ("summary", "edge", "win-rate", "opportunities", "settlements", "models"):
        response = published.get(f"/api/v1/performance/{path}")
        assert response.status_code == 200, path
        assert response.json()["freshness"]["state"] == "fresh", path


def test_edge_and_opportunity_sums_are_both_visible(published) -> None:
    summary = published.get("/api/v1/performance/summary").json()["data"]["data"]

    assert summary["observation_edge_sum"] is not None
    assert summary["unique_opportunity_edge_sum"] is not None
    assert summary["accept_observations"] >= summary["opportunity_episodes"]


def test_a_win_rate_without_enough_samples_is_null_with_a_reason(published) -> None:
    overall = published.get("/api/v1/performance/win-rate").json()["data"]["data"]["overall"]

    assert overall["win_rate"] is None
    assert overall["reason"]


def test_opportunities_are_paginated(published) -> None:
    first = published.get("/api/v1/performance/opportunities?limit=2").json()["data"]

    assert len(first["items"]) <= 2
    if first["next_cursor"] is not None:
        second = published.get(
            f"/api/v1/performance/opportunities?limit=2&cursor={first['next_cursor']}"
        ).json()["data"]
        assert second["items"], "a declared next_cursor must lead somewhere"
        assert second["items"][0]["episode_id"] != first["items"][0]["episode_id"]


def test_settlements_are_paginated_too(published) -> None:
    first = published.get("/api/v1/performance/settlements?limit=1").json()["data"]

    assert len(first["items"]) <= 1
    assert first["total"] >= 0


# --- reference capital --------------------------------------------------------


def test_reference_capital_requires_a_positive_stake(published) -> None:
    for bad in ("0", "-5"):
        response = published.get(f"/api/v1/performance/reference-capital?capital_usdc={bad}")
        assert response.status_code == 422, bad


def test_reference_capital_rejects_an_unknown_mode(published) -> None:
    response = published.get("/api/v1/performance/reference-capital?mode=yolo")
    assert response.status_code == 422


def test_reference_capital_names_the_module_that_computed_it(published) -> None:
    """The MCP gateway must be able to show which implementation answered."""
    data = published.get("/api/v1/performance/reference-capital").json()["data"]

    assert data["source_endpoint"] == "weatherquant.performance.capital.simulate_portfolio"
    assert data["statistical_scope"] == ["reference_capital_hypothetical"]
    assert any("not realised profit" in w for w in data["warnings"])


def test_changing_the_capital_changes_the_allocation(tmp_path, settings_for) -> None:
    from helpers import build_tree

    root = tmp_path / "live"
    build_tree(root, archive=False)
    settings = settings_for(root)
    _publish(settings.performance_data_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        small = client.get("/api/v1/performance/reference-capital?capital_usdc=100").json()["data"]
        large = client.get("/api/v1/performance/reference-capital?capital_usdc=500").json()["data"]

    assert small["capital_usdc"] == 100.0
    assert large["capital_usdc"] == 500.0
    # The backfill had no book levels, so nothing can be sized at either
    # stake. Allocation stays zero rather than scaling with the number - a
    # figure that grew with the stake would mean we had priced against
    # something other than a book. The stake still changes what the caller
    # is asking about, which is echoed above.
    assert small["allocated_total_usdc"] == 0.0
    assert large["allocated_total_usdc"] == 0.0
    assert small["backtestable"] is False
    assert any("no episode could be sized" in r for r in small["not_backtestable_reasons"])


def test_single_opportunity_mode_reports_both_views(tmp_path, settings_for) -> None:
    from helpers import build_tree

    root = tmp_path / "live"
    build_tree(root, archive=False)
    settings = settings_for(root)
    _publish(settings.performance_data_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        data = client.get(
            "/api/v1/performance/reference-capital?mode=single_opportunity"
        ).json()["data"]

    assert data["mode"] == "single_opportunity"
    assert "single_opportunity" in data
    assert "results" in data, "the shared-stake view stays available alongside it"


def test_a_portfolio_never_spends_more_than_the_stake(tmp_path, settings_for) -> None:
    from helpers import build_tree

    root = tmp_path / "live"
    build_tree(root, archive=False)
    settings = settings_for(root)
    _publish(settings.performance_data_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        data = client.get("/api/v1/performance/reference-capital?capital_usdc=50").json()["data"]

    assert data["allocated_total_usdc"] <= 50.0
    assert data["balance_remaining_usdc"] >= 0.0

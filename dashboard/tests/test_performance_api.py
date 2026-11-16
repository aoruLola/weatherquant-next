"""Performance API: reads what the research process published, and says so.

Two failure modes are guarded here.

The first is a dashboard with no backfill looking like a dashboard with no
edge: a missing snapshot must arrive as ``missing`` with a reason, never as
zeros. The second is two implementations of one figure: the reference-capital
endpoint must go through the same module the backfill used, so a stake change
cannot produce an answer the snapshot would have contradicted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weatherquant.dashboard.api import create_app
from weatherquant.markets.models import OrderBook
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
        "fee_rate": 0.0,
        "station": "KLGA",
        "bracket_label": "81°F",
        "market_question": "Will it be 81F?",
    }


def _book() -> OrderBook:
    from weatherquant.markets.models import BookLevel

    return OrderBook(
        market_id="m-0",
        token_id="tok-0",
        bids=(BookLevel(price_usd_per_share=0.58, size_shares=500.0),),
        asks=(BookLevel(price_usd_per_share=0.60, size_shares=500.0),),
        venue_ts=None,
        fetched_at_utc=datetime.now(tz=UTC),
    )


def _publish(directory) -> None:
    """Write real snapshots straight to the directory the API will read.

    Published with the wall clock rather than a fixed stamp: freshness is
    judged against now, so a fixture pinned to an hour ago drifts out of
    its own window as the day passes and the assertion starts failing for
    reasons that have nothing to do with the endpoint under test.
    """
    payloads = build_performance(
        decisions=[_decision(i) for i in range(30)],
        settlements=[],
        now=datetime.now(tz=UTC),
        dataset="fixture",
        # Supply levels so the capital endpoints have something to size
        # against. Without them every result is NOT_EXECUTABLE and the
        # difference between the two modes cannot be observed at all.
        book_for=lambda _e: _book(),
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
    assert first["data"]["limit"] == 2
    assert first["data"]["offset"] == 0
    if first["next_cursor"] is not None:
        second = published.get(
            f"/api/v1/performance/opportunities?limit=2&cursor={first['next_cursor']}"
        ).json()["data"]
        assert second["items"], "a declared next_cursor must lead somewhere"
        assert second["items"][0]["episode_id"] != first["items"][0]["episode_id"]
        assert second["data"]["offset"] == 2
        # And back again, from the page's own prev_cursor.
        back = published.get(
            f"/api/v1/performance/opportunities?limit=2&cursor={second['prev_cursor']}"
        ).json()["data"]
        assert [row["episode_id"] for row in back["items"]] == [
            row["episode_id"] for row in first["items"]
        ]


def test_settlements_are_paginated_too(published) -> None:
    first = published.get("/api/v1/performance/settlements?limit=1").json()["data"]

    assert len(first["items"]) <= 1
    assert first["total"] >= 0


# --- reference capital --------------------------------------------------------


@pytest.mark.parametrize("builder", ["materialized", "streaming"])
def test_both_reference_stakes_use_only_the_opening_fee(tmp_path, settings_for, builder) -> None:
    import json

    from weatherquant.performance.streaming import build_performance_streaming

    root = _empty_tree(tmp_path)
    out = settings_for(root).performance_snapshots_dir
    # Two observations of one opportunity, then a new opportunity in the same market.
    rows = [
        dict(_decision(0), decision_id=f"d{i}", decided_at_utc=(NOW + timedelta(minutes=minute)).isoformat(),
             decision_time_utc=(NOW + timedelta(minutes=minute)).isoformat(),
             fee_rate=fee)
        for i, (minute, fee) in enumerate([(0, None), (1, 0.0), (30, 0.0)])
    ]
    options = dict(dataset="fixture", now=NOW + timedelta(hours=1), book_for=lambda _: _book())
    if builder == "materialized":
        publish_performance(build_performance(decisions=rows, settlements=[], **options), out)
    else:
        parts = tmp_path / "export" / "parts"
        parts.mkdir(parents=True)
        for i, row in enumerate(rows, 1):
            (parts / f"part-{i:09d}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        build_performance_streaming(export=parts.parent, out_dir=out, state_db=tmp_path / "state.duckdb", **options)

    with _client_for(root, settings_for) as client:
        summary = client.get("/api/v1/performance/summary").json()["data"]["data"]
        window = client.get("/api/v1/performance/reference-capital").json()["data"]
    assert summary["opportunity_episodes"] == 2
    lifetime = summary["reference_capital"]
    assert (lifetime["scope"], window["scope"]) == ("lifetime", "window")
    for stake in (lifetime, window):
        assert stake["initial_capital_usdc"] == 100
        assert stake["not_executable_reason_counts"] == {"FEE_UNKNOWN": 1}
        assert stake["executable_count"] == 1
        assert 0 < stake["allocated_total_usdc"] <= 10
        assert stake["simulated_net_profit_usdc"] is None
    assert lifetime["allocated_total_usdc"] == window["allocated_total_usdc"]


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
    # A window simulation is labelled as one, and it is not a period of the
    # lifetime account: both facts have to travel with the number.
    assert data["scope"] == "window"
    assert data["statistical_scope"] == [
        "reference_capital_hypothetical",
        "opportunity_episodes",
    ]
    assert data["window_completeness"] in {"complete", "truncated"}
    assert any("not realised profit" in w for w in data["warnings"])
    assert any("not a period of the lifetime account" in w for w in data["warnings"])


def test_changing_the_capital_changes_the_allocation(tmp_path, settings_for) -> None:
    """A cached answer would not respond to the stake the caller asked about."""
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
    # Books are published with the fixture, so both stakes actually fund
    # something - and the larger one must fund more of it.
    assert small["allocated_total_usdc"] > 0.0
    assert large["allocated_total_usdc"] >= small["allocated_total_usdc"]
    assert large["allocated_total_usdc"] <= 500.0 + 1e-9
    assert small["backtestable"] is not False or small["executable_count"] > 0


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
    assert data["source_endpoint"] == "weatherquant.performance.capital.simulate_independent"
    # No combined total: ten "if I staked N here" answers are not one
    # account holding ten stakes, so the sum is deliberately withheld.
    assert data.get("combined_expected_edge") is None
    assert data["per_opportunity_expected_edge_sample"] is not None
    assert data["per_opportunity_expected_edge_sampled"] is True
    assert any("independently" in w for w in data["warnings"])


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


def test_single_opportunity_sizes_every_episode_against_the_full_stake(tmp_path, settings_for) -> None:
    """The two modes must differ, and differ in the way they are defined.

    Portfolio: one stake, shared, so later opportunities get what is left.
    Single opportunity: each episode gets the whole stake on its own, so the
    first one spending capital must not shrink what the second may use.

    Asserted on the *shape* of allocation rather than a specific figure -
    the invariant is "independent", and independence is visible in whether
    an earlier episode constrained a later one.
    """
    from helpers import build_tree

    root = tmp_path / "live"
    build_tree(root, archive=False)
    settings = settings_for(root)
    _publish(settings.performance_data_dir)
    app = create_app(settings)

    with TestClient(app) as client:
        single = client.get(
            "/api/v1/performance/reference-capital?capital_usdc=100&mode=single_opportunity"
        ).json()["data"]
        portfolio = client.get(
            "/api/v1/performance/reference-capital?capital_usdc=100&mode=portfolio"
        ).json()["data"]

    single_used = [r["capital_used_usdc"] for r in single["results"] if r["executable"]]
    assert single_used, "the fixture publishes books, so some episodes must be executable"

    # Every executable episode in single mode is sized against its own stake,
    # not against a shrinking remainder.
    assert all(used > 0 for used in single_used)
    assert single["executable_count"] == len(single_used)
    assert sum(single_used) > 100.0, (
        "single-opportunity results are separate hypotheticals; their sum "
        "is not a balance and may exceed the stake"
    )

    # Portfolio is the opposite: one stake, never overspent.
    assert portfolio["allocated_total_usdc"] <= 100.0 + 1e-9
    assert portfolio["mode"] == "portfolio"
    assert portfolio["executable_count"] <= single["executable_count"]


def test_a_fixture_snapshot_keeps_its_own_label_in_the_envelope(tmp_path, settings_for) -> None:
    """Provenance belongs to the snapshot, not to the dashboard's setting.

    A backfill labelled `fixture` and mounted into a dashboard set to `live`
    would otherwise be stamped `live` on the way out - and the fixture banner
    reads /healthz, which carries the same setting, so sample data could be
    presented as production with nothing on screen to say otherwise.
    """
    from helpers import build_tree

    root = tmp_path / "live-configured"
    build_tree(root, archive=False)
    settings = settings_for(root)  # default dataset is "live"
    assert settings.dataset == "live"

    _publish(settings.performance_data_dir)  # publishes with dataset="fixture"
    app = create_app(settings)

    with TestClient(app) as client:
        payload = client.get("/api/v1/performance/summary").json()

    assert payload["dataset"] == "fixture", (
        "the snapshot's own label must survive; overwriting it with the "
        "dashboard's setting would mislabel sample data as production"
    )
    warnings = " ".join(payload["data"].get("warnings") or [])
    assert "fixture" in warnings and "live" in warnings, (
        "a mismatch must be stated, not silently resolved"
    )

"""The paged detail: bounded reads, real filters, and no silent mixing.

The problem this file exists for: ``opportunities.json`` used to carry every
episode inline, so the endpoint that served fifty rows parsed all of them - 163
MB at five million decisions, to return a page of fifty. These tests hold the
line in both directions:

* a page costs a page (the response is bounded, and the *file* the endpoint
  reads is bounded too);
* the window the detail covers is stated rather than implied, and the
  aggregates are not confused with it;
* a cursor belongs to one published generation, and a cursor from another one
  is refused rather than silently re-pointed at different rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weatherquant.dashboard.api import create_app
from weatherquant.performance.snapshot import (
    build_performance,
    publish_performance,
    resolve_generation,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _decisions(episodes: int = 120, *, start_days_ago: int = 0) -> list[dict]:
    """One ACCEPT per episode, varied so every filter has something to find.

    Each episode is a distinct five-dimensional key, so ``episodes`` rows in
    produce exactly ``episodes`` episodes out - which is what makes "the page
    covers exactly the rows it says" checkable.
    """
    rows: list[dict] = []
    base = NOW - timedelta(days=start_days_ago)
    for index in range(episodes):
        instant = base - timedelta(minutes=index)
        rows.append(
            {
                "market_id": f"m-{index % 7}",
                "target_date": f"2026-10-{14 + (index % 5):02d}",
                "model_name": ["calibrated", "gfs_raw", "fusion"][index % 3],
                "direction": "buy_yes" if index % 2 == 0 else "sell_yes",
                "mode": "LIVE" if index % 3 else "REPLAY",
                "state": "ACCEPT",
                "decided_at_utc": instant.isoformat(),
                "net_edge_usd": round(0.01 + (index % 12) * 0.01, 6),
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
                "station": "KLGA",
                "bracket_label": "81°F",
            }
        )
    # The fold sorts by instant; keeping them descending is a deliberate
    # exercise of "input order does not decide the published order".
    return rows


def _settlements(rows: list[dict]) -> list[dict]:
    pairs = sorted({(row["market_id"], row["target_date"]) for row in rows})
    return [
        {
            "market_id": market_id,
            "target_date": target_date,
            "settlement_status": "RESOLVED",
            "resolved_outcome": 1.0 if index % 3 else 0.0,
            "verification_status": "VERIFIED",
            "fetched_at_utc": NOW.isoformat(),
        }
        for index, (market_id, target_date) in enumerate(pairs)
    ]


def _publish(
    directory: Path,
    rows: list[dict],
    *,
    settlements: list[dict] | None = None,
    now: datetime = NOW,
    **kwargs,
) -> None:
    payloads = build_performance(
        decisions=rows,
        settlements=settlements or [],
        now=now,
        dataset="fixture",
        **kwargs,
    )
    publish_performance(payloads, directory)


@pytest.fixture
def app_for(tmp_path: Path):
    def _make(**overrides) -> TestClient:
        from weatherquant.dashboard.config import DashboardSettings

        settings = DashboardSettings(
            collector_data_dir=tmp_path / "collector",
            shadow_data_dir=tmp_path / "shadow",
            performance_data_dir=tmp_path / "performance",
            rate_limit_per_minute=0,
            **overrides,
        )
        return TestClient(create_app(settings))

    return _make


@pytest.fixture
def published(tmp_path: Path, app_for):
    """A published snapshot of 120 episodes, and a client that reads it."""
    rows = _decisions(120)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    with app_for() as client:
        yield client


def _page(client: TestClient, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
    response = client.get(f"/api/v1/performance/opportunities?{query}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# the detail is bounded
# ---------------------------------------------------------------------------


def test_the_published_detail_is_parts_not_one_array(tmp_path) -> None:
    """The file a page reads is a part, not the whole history."""
    rows = _decisions(1100)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    generation = resolve_generation(tmp_path / "performance")
    assert generation.directory is not None
    payload = json.loads((generation.directory / "opportunities.json").read_text())
    descriptor = payload["data"]["episodes"]

    assert descriptor["partitioned"] is True
    assert descriptor["total"] == 1100
    assert len(descriptor["partitions"]) == 2, "1100 episodes at 1000 a part"
    for part in descriptor["partitions"]:
        assert part["items"] <= 1000
        assert part["first_key"] and part["last_key"]
        assert (generation.directory / "opportunities" / part["file"]).exists()

    # The snapshot itself is a description, not the data: the whole file is
    # smaller than one part.
    assert (generation.directory / "opportunities.json").stat().st_size < 20_000


def test_the_summary_does_not_carry_every_result(tmp_path) -> None:
    """``summary.json`` used to be 131 MB at five million decisions.

    Every byte of it was the per-episode capital results array. The counts and
    a bounded sample answer the same questions, and the file stays the size of
    a page.
    """
    rows = _decisions(300)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    generation = resolve_generation(tmp_path / "performance")
    assert generation.directory is not None
    summary = json.loads((generation.directory / "summary.json").read_text())
    capital = summary["data"]["reference_capital"]

    assert "results" not in capital
    assert len(capital["results_sample"]) <= 50
    assert capital["results_total"] == 300
    assert capital["results_sampled"] is True
    assert set(capital["not_executable_reason_counts"]) <= {"NO_BOOK", "DEPTH_NOT_MEASURED"}
    assert sum(capital["not_executable_reason_counts"].values()) == capital["not_executable_count"]
    assert (generation.directory / "summary.json").stat().st_size < 200_000


def test_the_edge_bands_are_distributions_not_values(tmp_path) -> None:
    """The band arrays were the whole of ``edge.json``: 22 MB at five million."""
    rows = _decisions(300)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    generation = resolve_generation(tmp_path / "performance")
    assert generation.directory is not None
    edge = json.loads((generation.directory / "edge.json").read_text())

    bands = edge["data"]["by_edge_band"]
    assert set(bands) == {"lt_0.02", "0.02_0.05", "0.05_0.10", "gte_0.10"}
    for stats in bands.values():
        assert isinstance(stats, dict)
        assert {"count", "sum", "mean", "median", "p05", "p95", "min", "max"} <= set(stats)
    assert sum(stats["count"] for stats in bands.values()) == 300
    assert (generation.directory / "edge.json").stat().st_size < 100_000


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


def test_walking_every_page_sees_every_row_exactly_once(published) -> None:
    """No gaps and no repeats across the whole window."""
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = _page(published, limit=7, cursor=cursor)
        seen.extend(row["episode_id"] for row in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 100, "pagination did not terminate"
    assert pages == 18, "120 episodes at 7 a page"
    assert len(seen) == 120
    assert len(set(seen)) == 120


def test_a_page_is_stable_and_reproducible(published) -> None:
    """The same cursor twice is the same page, not a new one."""
    first = _page(published, limit=10)
    second = _page(published, limit=10)
    assert [row["episode_id"] for row in first["items"]] == [
        row["episode_id"] for row in second["items"]
    ]

    cursor = first["next_cursor"]
    once = _page(published, limit=10, cursor=cursor)
    twice = _page(published, limit=10, cursor=cursor)
    assert [row["episode_id"] for row in once["items"]] == [
        row["episode_id"] for row in twice["items"]
    ]


def test_previous_returns_the_page_before(published) -> None:
    first = _page(published, limit=10)
    second = _page(published, limit=10, cursor=first["next_cursor"])
    back = _page(published, limit=10, cursor=second["prev_cursor"])

    assert [row["episode_id"] for row in back["items"]] == [
        row["episode_id"] for row in first["items"]
    ]
    assert back["prev_cursor"] is None, "the first page has nothing before it"


def test_the_order_is_newest_first_and_total(published) -> None:
    page = _page(published, limit=120)
    stamps = [row["first_seen_utc"] for row in page["items"]]
    assert stamps == sorted(stamps, reverse=True)
    assert page["data"]["total"] == 120
    assert page["data"]["window"]["complete"] is True


def test_the_page_size_is_capped_whatever_is_asked_for(published) -> None:
    assert published.get("/api/v1/performance/opportunities?limit=5000").status_code == 422
    assert published.get("/api/v1/performance/opportunities?limit=201").status_code == 422
    assert published.get("/api/v1/performance/opportunities?limit=0").status_code == 422
    assert published.get("/api/v1/performance/opportunities?limit=200").status_code == 200


def test_a_page_past_the_end_is_empty_and_says_so(published) -> None:
    page = _page(published, limit=50, cursor=10_000)
    assert page["items"] == []
    assert page["total"] == 120
    assert page["next_cursor"] is None


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------


def test_each_filter_selects_exactly_what_it_says(published) -> None:
    everything = _page(published, limit=1)["total"]

    by_model = _page(published, limit=200, model="fusion")
    assert by_model["data"]["filters"] == {"model": "fusion"}
    assert by_model["total"] == 40
    assert {row["model_name"] for row in by_model["items"]} == {"fusion"}
    assert by_model["total"] < everything

    by_direction = _page(published, limit=200, direction="sell_yes")
    assert {row["direction"] for row in by_direction["items"]} == {"sell_yes"}

    by_mode = _page(published, limit=200, mode="REPLAY")
    assert {row["mode"] for row in by_mode["items"]} == {"REPLAY"}

    by_date = _page(published, limit=200, target_date="2026-10-16")
    assert {row["target_date"] for row in by_date["items"]} == {"2026-10-16"}

    by_outcome = _page(published, limit=200, outcome="WIN")
    assert {row["outcome"] for row in by_outcome["items"]} == {"WIN"}
    assert by_outcome["total"] > 0

    by_edge = _page(published, limit=200, min_edge=0.05, max_edge=0.08)
    assert by_edge["total"] > 0
    assert all(0.05 <= row["first_edge"] <= 0.08 for row in by_edge["items"])


def test_filters_combine(published) -> None:
    combined = _page(published, limit=200, model="fusion", direction="sell_yes", mode="LIVE")
    assert combined["total"] > 0
    for row in combined["items"]:
        assert row["model_name"] == "fusion"
        assert row["direction"] == "sell_yes"
        assert row["mode"] == "LIVE"
    assert combined["data"]["filters"] == {
        "model": "fusion",
        "direction": "sell_yes",
        "mode": "LIVE",
    }


def test_a_filter_with_no_matches_is_empty_not_an_error(published) -> None:
    page = _page(published, limit=50, model="does-not-exist")
    assert page["items"] == []
    assert page["total"] == 0
    assert page["next_cursor"] is None


def test_an_invalid_enumeration_is_refused(published) -> None:
    assert published.get("/api/v1/performance/opportunities?mode=YOLO").status_code == 422
    assert published.get("/api/v1/performance/opportunities?direction=sideways").status_code == 422
    assert published.get("/api/v1/performance/opportunities?outcome=MAYBE").status_code == 422


def test_filters_page_consistently(published) -> None:
    """A filtered walk sees every match exactly once, too."""
    seen: list[str] = []
    cursor = None
    while True:
        page = _page(published, limit=3, cursor=cursor, model="gfs_raw")
        seen.extend(row["episode_id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 40
    assert len(set(seen)) == 40


# ---------------------------------------------------------------------------
# cursors and generations
# ---------------------------------------------------------------------------


def test_a_cursor_from_another_generation_is_refused(tmp_path, app_for) -> None:
    """Refused, not re-pointed: offsets do not survive a republish."""
    rows = _decisions(60)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    with app_for() as client:
        first = _page(client, limit=10)
        cursor = first["next_cursor"]
        assert _page(client, limit=10, cursor=cursor)["data"]["offset"] == 10

        # A second generation: a different snapshot of a different set.
        _publish(tmp_path / "performance", _decisions(90), settlements=_settlements(_decisions(90)))
        response = client.get(f"/api/v1/performance/opportunities?limit=10&cursor={cursor}")
        assert response.status_code == 409
        assert "generation" in response.json()["detail"]

        # And the new generation pages from the start.
        fresh = _page(client, limit=10)
        assert fresh["data"]["offset"] == 0
        assert fresh["items"]


def test_a_malformed_cursor_is_refused(published) -> None:
    response = published.get("/api/v1/performance/opportunities?cursor=not-a-cursor")
    assert response.status_code == 422


def test_a_bare_offset_still_works(published) -> None:
    """The cursor the endpoint accepted before generations were encoded in it."""
    page = _page(published, limit=5, cursor=10)
    assert page["data"]["offset"] == 10
    assert len(page["items"]) == 5


# ---------------------------------------------------------------------------
# the window, and what it is not
# ---------------------------------------------------------------------------


def test_a_truncated_window_says_what_it_is(tmp_path, app_for) -> None:
    """The detail is a window; the aggregates are not, and both say so."""
    rows = _decisions(40)
    _publish(
        tmp_path / "performance",
        rows,
        settlements=_settlements(rows),
        detail_max_episodes=10,
        detail_window_days=3650,
    )
    with app_for() as client:
        page = _page(client, limit=200)
        window = page["data"]["window"]
        assert window["episodes"] == 10
        assert window["episodes_all"] == 40
        assert window["complete"] is False
        assert page["total"] == 10, "the page covers the window, and the window is 10"
        assert page["data"]["total_all"] == 40
        assert any("window" in warning for warning in page["data"]["warnings"])
        assert any(scope.startswith("window:") for scope in page["data"]["statistical_scope"])

        # The aggregates still describe all forty.
        summary = client.get("/api/v1/performance/summary").json()["data"]["data"]
        assert summary["opportunity_episodes"] == 40


def test_an_old_snapshot_is_stale_not_missing(tmp_path, app_for) -> None:
    rows = _decisions(20)
    old = NOW - timedelta(days=2)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows), now=old)
    with app_for() as client:
        envelope = client.get("/api/v1/performance/opportunities").json()
        assert envelope["freshness"]["state"] == "stale"
        assert envelope["freshness"]["reason"]


def test_a_missing_snapshot_is_a_reason_not_zeros(app_for, tmp_path) -> None:
    with app_for() as client:
        envelope = client.get("/api/v1/performance/opportunities").json()
        assert envelope["freshness"]["state"] == "missing"
        assert envelope["data"]["reason"]
        assert envelope["data"]["items"] == []


# ---------------------------------------------------------------------------
# reference capital, over the window
# ---------------------------------------------------------------------------


def test_reference_capital_answers_over_the_window_and_says_so(tmp_path, app_for) -> None:
    rows = _decisions(40)
    _publish(
        tmp_path / "performance",
        rows,
        settlements=_settlements(rows),
        detail_max_episodes=15,
        detail_window_days=3650,
    )
    with app_for() as client:
        body = client.get("/api/v1/performance/reference-capital?capital_usdc=250").json()["data"]

        assert body["capital_usdc"] == 250.0
        assert body["window"]["episodes"] == 15
        assert body["window"]["complete"] is False
        assert body["sample_size"] == 15, "sized over the window, and the window is 15"
        assert any("window" in warning for warning in body["warnings"])
        # Still the same module that produced the published answer.
        assert body["source_endpoint"] == "weatherquant.performance.capital.simulate_portfolio"


def test_reference_capital_does_not_copy_the_history(published) -> None:
    """The response is aggregates plus a bounded sample, not every episode."""
    body = published.get("/api/v1/performance/reference-capital?capital_usdc=100").json()["data"]
    assert "results" not in body
    assert len(body["results_sample"]) <= 50
    assert body["results_total"] == body["executable_count"] + body["not_executable_count"]
    assert sum(body["not_executable_reason_counts"].values()) == body["not_executable_count"]


def test_monitor_recognizes_dashboard_limit_validation_and_previous_cursor(published, monkeypatch):
    import httpx

    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    import performance_monitor as monitor

    def through_dashboard(request):
        response = published.get(str(request.url))
        return httpx.Response(response.status_code, content=response.content)

    route = "/api/v1/performance/opportunities"
    current = published.get(route, params={"limit": 10}).json()["data"]["next_cursor"]
    report = monitor.probe_dashboard(
        "http://testserver", token=None, probes=1, max_p95_ms=1000, max_response_mb=32,
        previous_cursor=current, transport=httpx.MockTransport(through_dashboard),
    )
    assert report["limit_clamp"]["rejected_above"] == 200
    assert report["previous_cursor_status"] == 200
    assert report["failures"] == ["previous generation cursor answered 200; expected 409"]

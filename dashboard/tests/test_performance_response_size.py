"""Bounded reads: a page costs a page, whatever the history has grown to.

The failure this file guards against is subtle, because it looks like success:
the *publisher* stops holding the history, and then the *dashboard* picks it up
again - parsing a 163 MB snapshot to serve fifty rows, and caching it. Moving a
memory wall from one process to another is not a fix, so these tests measure
the things that would move it: the size of what is published, the size of what
is returned, how many files a page opens, and the peak allocation of a request.
"""

from __future__ import annotations

import json
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.performance.snapshot import build_performance, publish_performance, resolve_generation

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _decisions(episodes: int) -> list[dict]:
    rows = []
    for index in range(episodes):
        rows.append(
            {
                "market_id": f"m-{index % 9}",
                "target_date": f"2026-10-{14 + (index % 5):02d}",
                "model_name": ["calibrated", "gfs_raw", "fusion"][index % 3],
                "direction": "buy_yes" if index % 2 else "sell_yes",
                "mode": "LIVE",
                "state": "ACCEPT",
                "decided_at_utc": (NOW - timedelta(minutes=index)).isoformat(),
                "net_edge_usd": round(0.01 + (index % 12) * 0.01, 6),
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
                "station": "KLGA",
                "bracket_label": "81°F",
            }
        )
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


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            DashboardSettings(
                collector_data_dir=tmp_path / "collector",
                shadow_data_dir=tmp_path / "shadow",
                performance_data_dir=tmp_path / "performance",
                rate_limit_per_minute=0,
            )
        )
    )


def _published(tmp_path: Path, episodes: int) -> None:
    rows = _decisions(episodes)
    publish_performance(
        build_performance(decisions=rows, settlements=_settlements(rows), now=NOW, dataset="fixture"),
        tmp_path / "performance",
    )


def _sizes(tmp_path: Path) -> dict[str, int]:
    generation = resolve_generation(tmp_path / "performance")
    assert generation.directory is not None
    return {
        name: (generation.directory / name).stat().st_size
        for name in ("summary.json", "edge.json", "opportunities.json", "win-rate.json")
    }


# ---------------------------------------------------------------------------
# what is published
# ---------------------------------------------------------------------------


def test_the_published_snapshots_stop_growing_with_the_history(tmp_path) -> None:
    """Ten times the episodes must not be ten times the files.

    Before this round every one of these grew linearly: ``summary.json`` by one
    capital result per episode, ``opportunities.json`` by one row, ``edge.json``
    by one float per ACCEPT. At five million decisions that was 131 MB + 163 MB
    + 23 MB, and the dashboard read all of it.
    """
    small_dir = tmp_path / "small"
    large_dir = tmp_path / "large"
    small_dir.mkdir()
    large_dir.mkdir()
    _published(small_dir, 100)
    _published(large_dir, 1000)

    small = _sizes(small_dir)
    large = _sizes(large_dir)

    for name, size in small.items():
        # A generous bound: the point is the *shape*, not the exact bytes.
        assert large[name] < size * 3 + 40_000, (name, size, large[name])
    assert large["summary.json"] < 200_000
    assert large["edge.json"] < 200_000
    # The detail does grow - it is data - but only up to its window.
    assert large["opportunities.json"] < 40_000


def test_the_detail_stops_growing_once_the_window_is_full(tmp_path) -> None:
    """The browsable detail is capped, so the file is capped."""
    small_dir = tmp_path / "small"
    large_dir = tmp_path / "large"
    small_dir.mkdir()
    large_dir.mkdir()
    rows = _decisions(50)
    publish_performance(
        build_performance(
            decisions=rows, settlements=_settlements(rows), now=NOW, dataset="fixture",
            detail_max_episodes=10, detail_window_days=3650,
        ),
        small_dir / "performance",
    )
    rows = _decisions(400)
    publish_performance(
        build_performance(
            decisions=rows, settlements=_settlements(rows), now=NOW, dataset="fixture",
            detail_max_episodes=10, detail_window_days=3650,
        ),
        large_dir / "performance",
    )
    small_detail = _sizes(small_dir)["opportunities.json"]
    large_detail = _sizes(large_dir)["opportunities.json"]
    # The descriptor carries the totals, so the files are not byte-identical -
    # but eight times the episodes must not be eight times the file.
    assert large_detail < small_detail + 500, (small_detail, large_detail)
    assert _sizes(small_dir)["summary.json"] < 200_000
    assert _sizes(large_dir)["summary.json"] < 200_000


# ---------------------------------------------------------------------------
# what is returned
# ---------------------------------------------------------------------------


def test_a_page_is_the_same_size_for_a_hundred_or_a_thousand_episodes(tmp_path) -> None:
    small_dir = tmp_path / "small"
    large_dir = tmp_path / "large"
    small_dir.mkdir()
    large_dir.mkdir()
    _published(small_dir, 100)
    _published(large_dir, 1000)

    with _client(small_dir) as client:
        small = client.get("/api/v1/performance/opportunities?limit=50").content
    with _client(large_dir) as client:
        large = client.get("/api/v1/performance/opportunities?limit=50").content

    assert len(small) > 0
    assert len(large) < len(small) * 1.5, (len(small), len(large))


def test_the_summary_response_does_not_carry_the_history(tmp_path) -> None:
    _published(tmp_path, 1000)
    with _client(tmp_path) as client:
        summary = client.get("/api/v1/performance/summary").content
        capital = client.get("/api/v1/performance/reference-capital?capital_usdc=250").content
    assert len(summary) < 60_000, len(summary)
    assert len(capital) < 60_000, len(capital)


def test_an_unfiltered_page_opens_only_the_parts_it_needs(tmp_path, monkeypatch) -> None:
    """A page in the last part must not read the parts before it.

    This is the difference between "the response is small" and "the read is
    small": a snapshot could publish parts and still be read end to end.
    """
    _published(tmp_path, 2500)  # three parts of 1000
    opened: list[str] = []
    real_open = Path.open

    def counting_open(self, *args, **kwargs):
        if self.suffix == ".json" and "part-" in self.name:
            opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    with _client(tmp_path) as client:
        response = client.get("/api/v1/performance/opportunities?limit=10&cursor=2400")

    assert response.status_code == 200
    assert len(response.json()["data"]["items"]) == 10
    assert opened == ["part-00002.json"], opened


def test_a_filtered_page_reads_the_window_but_returns_a_page(tmp_path) -> None:
    """A filter has no index over an immutable file, so it scans the window -
    and the window is the bound, which is why the response is still a page."""
    _published(tmp_path, 2500)
    with _client(tmp_path) as client:
        response = client.get("/api/v1/performance/opportunities?limit=5&model=fusion")

    assert response.status_code == 200
    body = response.json()["data"]
    assert len(body["items"]) == 5
    assert body["total"] > 5
    assert all(row["model_name"] == "fusion" for row in body["items"])
    assert len(response.content) < 20_000


def test_a_request_does_not_allocate_with_the_history(tmp_path) -> None:
    """The peak allocation of one page is a page, not the archive.

    Measured on the Python side, which is where the old reader died: it parsed
    the whole detail into a dict and cached it.
    """
    small_dir = tmp_path / "small"
    large_dir = tmp_path / "large"
    small_dir.mkdir()
    large_dir.mkdir()
    # A small part size, so both cases read a *full* part and the comparison is
    # about the history rather than about how full the first part happens to be.
    for root, episodes in ((small_dir, 200), (large_dir, 2000)):
        rows = _decisions(episodes)
        publish_performance(
            build_performance(
                decisions=rows, settlements=_settlements(rows), now=NOW, dataset="fixture",
                detail_partition_size=100, detail_window_days=3650,
            ),
            root / "performance",
        )

    def peak(root: Path) -> int:
        with _client(root) as client:
            tracemalloc.start()
            response = client.get("/api/v1/performance/opportunities?limit=50")
            _current, high = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        assert response.status_code == 200
        return high

    small = peak(small_dir)
    large = peak(large_dir)
    assert large < small * 2, f"{large} bytes for 10x the history of {small}"


def test_the_published_detail_descriptor_lists_its_parts(tmp_path) -> None:
    """A reader has to be able to find a page without opening every part."""
    _published(tmp_path, 2500)
    generation = resolve_generation(tmp_path / "performance")
    assert generation.directory is not None
    payload = json.loads((generation.directory / "opportunities.json").read_text())
    descriptor = payload["data"]["episodes"]

    assert descriptor["partition_size"] == 1000
    assert [part["items"] for part in descriptor["partitions"]] == [1000, 1000, 500]
    assert descriptor["total"] == 2500
    for part in descriptor["partitions"]:
        assert part["first_key"][0] >= part["last_key"][0], "newest first, within a part"

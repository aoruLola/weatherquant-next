"""WQN-51: one contract across Dashboard, MCP and the paper ledger API.

These tests exercise the boundaries together: paging cannot mix generations,
invalid arguments have one meaning, MCP relays rather than recomputes, and a
large published detail is read a part at a time.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_performance_detail_api import _decisions, _publish, _settlements

from weatherquant.dashboard import performance
from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.mcp.client import DashboardClient
from weatherquant.mcp.security import RequestGuard
from weatherquant.mcp.server import Gateway, build_server
from weatherquant.paper.web import PaperAPIHandler


@pytest.fixture
def published(tmp_path: Path):
    rows = _decisions(120)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    settings = DashboardSettings(
        collector_data_dir=tmp_path / "collector",
        shadow_data_dir=tmp_path / "shadow",
        performance_data_dir=tmp_path / "performance",
        rate_limit_per_minute=0,
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _get_page(client: TestClient, collection: str, **params) -> dict:
    response = client.get(f"/api/v1/performance/{collection}", params=params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _tool(server, name: str):
    for tool in server._tool_manager.list_tools():  # type: ignore[attr-defined]
        if tool.name == name:
            return tool
    raise AssertionError(f"no tool {name}")


def _mcp_server(handler):
    pytest.importorskip("mcp.server.mcpserver", reason="mcp group not installed")
    client = DashboardClient("https://dashboard.test")
    client.close()
    client._client = httpx.Client(
        base_url="https://dashboard.test",
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
    )
    return build_server(Gateway(client, RequestGuard()))


def _bridge(client: TestClient, methods: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        response = client.get(request.url.path, params=dict(request.url.params))
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    return handler


def test_pagination_is_complete_for_opportunities_and_settlements(published) -> None:
    for collection, key in (
        ("opportunities", lambda row: row["episode_id"]),
        ("settlements", lambda row: (row["market_id"], row["target_date"])),
    ):
        seen = []
        cursor = None
        pages = 0
        while True:
            page = _get_page(published, collection, limit=7, cursor=cursor)
            seen.extend(key(row) for row in page["items"])
            cursor = page["next_cursor"]
            pages += 1
            if cursor is None:
                break
            assert pages < 100, "pagination did not terminate"
        assert len(seen) == page["total"]
        assert len(set(seen)) == len(seen), f"{collection} repeated a row across pages"


def test_filters_and_invalid_arguments_have_one_contract(published) -> None:
    page = _get_page(
        published,
        "opportunities",
        limit=200,
        model="fusion",
        direction="sell_yes",
        min_edge=0.02,
        max_edge=0.10,
    )
    assert page["items"]
    assert page["data"]["filters"] == {
        "model": "fusion",
        "direction": "sell_yes",
        "min_edge": 0.02,
        "max_edge": 0.10,
    }
    for row in page["items"]:
        assert row["model_name"] == "fusion"
        assert row["direction"] == "sell_yes"
        assert 0.02 <= row["first_edge"] <= 0.10

    for collection, invalid in (
        ("opportunities", {"mode": "YOLO"}),
        ("opportunities", {"cursor": "not-a-cursor"}),
        ("settlements", {"status": "MAYBE"}),
        ("settlements", {"cursor": "not-a-cursor"}),
    ):
        response = published.get(f"/api/v1/performance/{collection}", params=invalid)
        assert response.status_code == 422, (collection, invalid, response.text)
    for collection in ("opportunities", "settlements"):
        for limit in (0, 201):
            assert published.get(
                f"/api/v1/performance/{collection}", params={"limit": limit}
            ).status_code == 422


def test_cursors_from_another_generation_are_409_for_both_collections(tmp_path: Path) -> None:
    rows = _decisions(60)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    settings = DashboardSettings(
        collector_data_dir=tmp_path / "collector",
        shadow_data_dir=tmp_path / "shadow",
        performance_data_dir=tmp_path / "performance",
        rate_limit_per_minute=0,
    )
    with TestClient(create_app(settings)) as client:
        cursors = {
            collection: _get_page(client, collection, limit=5)["next_cursor"]
            for collection in ("opportunities", "settlements")
        }
        assert all(isinstance(cursor, str) for cursor in cursors.values())

        replacement = _decisions(90)
        _publish(tmp_path / "performance", replacement, settlements=_settlements(replacement))
        for collection, cursor in cursors.items():
            response = client.get(
                f"/api/v1/performance/{collection}", params={"limit": 5, "cursor": cursor}
            )
            assert response.status_code == 409, (collection, response.text)
            assert "generation" in response.json()["detail"]


def test_an_unfiltered_page_reads_only_the_part_it_serves(tmp_path: Path, monkeypatch) -> None:
    rows = _decisions(1100)
    _publish(tmp_path / "performance", rows, settlements=_settlements(rows))
    settings = DashboardSettings(
        collector_data_dir=tmp_path / "collector",
        shadow_data_dir=tmp_path / "shadow",
        performance_data_dir=tmp_path / "performance",
        rate_limit_per_minute=0,
    )
    reads: list[str] = []
    original = performance.json.load

    def tracked_load(handle):
        reads.append(Path(handle.name).name)
        return original(handle)

    monkeypatch.setattr(performance.json, "load", tracked_load)
    with TestClient(create_app(settings)) as client:
        page = _get_page(client, "opportunities", limit=25)

    assert page["total"] == 1100
    assert len(page["items"]) == 25
    assert len(reads) == 1, reads
    assert reads[0].startswith("part-"), reads
    assert "opportunities.json" not in reads


def test_dashboard_and_mcp_relay_the_same_metrics_and_capital_scope(published) -> None:
    methods: list[str] = []
    server = _mcp_server(_bridge(published, methods))

    dashboard_win_rate = published.get("/api/v1/performance/win-rate").json()["data"]
    mcp_win_rate = _tool(server, "get_win_rate").fn()
    assert mcp_win_rate["data"] == dashboard_win_rate
    assert mcp_win_rate["data"]["sample_size"] == dashboard_win_rate["sample_size"]

    dashboard_capital = published.get(
        "/api/v1/performance/reference-capital", params={"capital_usdc": 250}
    ).json()["data"]
    mcp_capital = _tool(server, "simulate_reference_capital").fn(capital_usdc=250)
    assert dashboard_capital["scope"] == mcp_capital["data"]["scope"] == "window"
    assert mcp_capital["statistical_scope"] == dashboard_capital["statistical_scope"]
    assert "opportunity_episodes" in mcp_capital["statistical_scope"]
    for key in ("sample_size", "capital_usdc", "results_total", "balance_remaining_usdc"):
        assert mcp_capital["data"][key] == dashboard_capital[key], key
    assert mcp_capital["sample_size"] == dashboard_capital["sample_size"]

    summary = published.get("/api/v1/performance/summary").json()["data"]["data"]
    assert summary["reference_capital"]["scope"] == "lifetime"
    assert summary["reference_capital"]["statistical_window"] != dashboard_capital["window"]
    assert methods and set(methods) == {"GET"}


def test_mcp_distinguishes_generation_conflicts_from_bad_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "old":
            return httpx.Response(409, json={"detail": "cursor generation changed"}, request=request)
        return httpx.Response(422, json={"detail": "invalid filter"}, request=request)

    server = _mcp_server(handler)
    conflict = _tool(server, "get_settlements").fn(cursor="old")
    invalid = _tool(server, "get_opportunities").fn(model="bad")

    assert conflict["data"] is None
    assert "different published generation" in conflict["error"]
    assert "restart paging" in conflict["error"]
    assert invalid["data"] is None
    assert "rejected the request parameters" in invalid["error"]
    assert "invalid filter" in invalid["error"]


def test_paper_write_methods_are_refused_without_touching_the_ledger(monkeypatch) -> None:
    handler = PaperAPIHandler.__new__(PaperAPIHandler)
    sent = []
    monkeypatch.setattr(
        handler,
        "_send",
        lambda status, payload, **kwargs: sent.append((status, payload, kwargs)),
    )

    for method in ("do_POST", "do_PUT", "do_PATCH", "do_DELETE"):
        getattr(handler, method)()

    assert [status for status, _payload, _kwargs in sent] == [405] * 4
    for _status, payload, kwargs in sent:
        assert "read-only" in payload["reason"]
        assert kwargs["extra_headers"] == {"Allow": "GET"}

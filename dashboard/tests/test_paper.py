"""Paper dashboard: read-only transport, honest N/A states and account maths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from weatherquant.dashboard import paper
from weatherquant.dashboard.api import create_app

NOW = "2026-11-17T03:00:00+00:00"


def entry(
    seq: int,
    kind: str,
    cash: float,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts_utc": f"2026-11-17T0{seq}:00:00+00:00",
        "kind": kind,
        "amount_usdc": 0.0,
        "balance_after_usdc": cash,
        "ref_type": "fixture",
        "ref_id": str(seq),
        "detail": detail or {},
    }


class FakePaperClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, resource: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((resource, params))
        response = self.responses[resource]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(params)
        return response


def summary_responses(*, with_position: bool = True) -> dict[str, Any]:
    positions = [
        {
            "market_id": "market-1",
            "token_id": "token-yes",
            "shares": 40.0,
            "avg_cost_usd_per_share": 1.0,
            "cost_basis_usdc": 40.0,
        }
    ] if with_position else []
    return {
        "account": {"account_id": "paper-main", "initial_capital_usdc": 100.0},
        "positions": {"positions": positions, "occupied_usdc": 40.0 if with_position else 0.0},
        "metrics": {
            "balances": {
                "available_usdc": 60.0 if with_position else 100.0,
                "occupied_usdc": 40.0 if with_position else 0.0,
                "realized_pnl_usdc": 0.0,
            },
            "refusals": {"NOT_EXECUTABLE": 3, "STALE_SIGNAL": 1},
            "ledger_consistent": True,
            "conservation_ok": True,
            "fill_count": 1 if with_position else 0,
        },
        "rejections": {
            "items": [
                {
                    "decision_id": "decision-1",
                    "market_id": "market-1",
                    "refusal_reason": "NOT_EXECUTABLE",
                    "refusal_detail": "no executable evidence",
                }
            ]
        },
        "ledger": lambda params: {
            "entries": (
                [entry(1, "OPEN", 100.0), entry(2, "BUY_SETTLE", 60.0, {"cost_basis_usdc": 40.0})]
                if with_position
                else [entry(1, "OPEN", 100.0)]
            ),
            "next_cursor": None,
            "has_more": False,
        },
    }


def test_ledger_curve_replays_cash_plus_occupied_cost() -> None:
    entries = [
        entry(1, "OPEN", 100.0),
        entry(2, "BUY_SETTLE", 60.0, {"cost_basis_usdc": 40.0}),
        entry(3, "SELL_SETTLE", 72.0, {"cost_released_usdc": 10.0}),
        entry(4, "SETTLE_PAYOUT", 96.0, {"cost_released_usdc": 30.0}),
    ]

    curve = paper.build_equity_curve(reversed(entries))

    assert [point["v"] for point in curve] == [100.0, 100.0, 102.0, 96.0]
    assert paper.maximum_drawdown(curve) == pytest.approx(6.0 / 102.0)
    assert paper.maximum_drawdown([]) is None


def test_current_mark_values_and_roi_include_unrealised_pnl() -> None:
    client = FakePaperClient(summary_responses())

    data = asyncio.run(paper.build_summary(client, {("market-1", "token-yes"): 0.6}))

    position = data["positions"][0]
    assert position["mark_usd_per_share"] == pytest.approx(0.6)
    assert position["market_value_usdc"] == pytest.approx(24.0)
    assert position["unrealized_pnl_usdc"] == pytest.approx(-16.0)
    assert data["equity_usdc"] == pytest.approx(84.0)
    assert data["unrealized_pnl_usdc"] == pytest.approx(-16.0)
    assert data["roi"] == pytest.approx(-0.16)
    assert data["cost_equity_usdc"] == pytest.approx(100.0)
    assert data["max_drawdown"] == 0.0
    assert data["rejection_summary"][0] == {"reason": "NOT_EXECUTABLE", "count": 3}


def test_any_missing_mark_makes_current_valuation_na_not_zero() -> None:
    client = FakePaperClient(summary_responses())

    data = asyncio.run(paper.build_summary(client, {}, marks_reason="market snapshot missing"))

    position = data["positions"][0]
    assert position["mark_usd_per_share"] is None
    assert position["market_value_usdc"] is None
    assert position["unrealized_pnl_usdc"] is None
    assert position["mark_status"] == "MARK_UNAVAILABLE"
    assert data["equity_usdc"] is None
    assert data["unrealized_pnl_usdc"] is None
    assert data["roi"] is None
    assert data["marks_status"] == "unavailable"
    assert data["marks_reason"] == "market snapshot missing"
    # The quote-independent ledger view remains available and is labelled.
    assert data["cost_equity_usdc"] == pytest.approx(100.0)
    assert data["equity_curve"]["basis"] == "ledger_cost"
    assert data["max_drawdown"] == 0.0


def test_no_open_position_needs_no_mark() -> None:
    client = FakePaperClient(summary_responses(with_position=False))

    data = asyncio.run(paper.build_summary(client, {}))

    assert data["marks_status"] == "not_required"
    assert data["unrealized_pnl_usdc"] == 0.0
    assert data["equity_usdc"] == pytest.approx(100.0)
    assert data["roi"] == 0.0


def test_ledger_keyset_pages_are_all_consumed() -> None:
    responses = summary_responses(with_position=False)
    responses["ledger"] = lambda params: (
        {"entries": [entry(1, "OPEN", 100.0)], "next_cursor": 1, "has_more": True}
        if not params or "cursor" not in params
        else {"entries": [entry(2, "CORRECTION", 101.0)], "next_cursor": None, "has_more": False}
    )
    client = FakePaperClient(responses)

    data = asyncio.run(paper.build_summary(client, {}))

    assert [point["seq"] for point in data["equity_curve"]["points"]] == [1, 2]
    assert ("ledger", {"limit": 500}) in client.calls
    assert ("ledger", {"limit": 500, "cursor": 1}) in client.calls


def test_unavailable_payload_has_nulls_instead_of_zeroes() -> None:
    data = paper.unavailable_summary("offline")

    assert data["status"] == "unavailable"
    assert data["reason"] == "offline"
    for field in (
        "initial_capital_usdc",
        "available_usdc",
        "occupied_usdc",
        "equity_usdc",
        "realized_pnl_usdc",
        "unrealized_pnl_usdc",
        "roi",
        "max_drawdown",
    ):
        assert data[field] is None
    assert data["positions"] == []
    assert data["equity_curve"]["points"] == []


def test_summary_route_is_read_only_and_reports_upstream_failure(
    data_root: Path, settings_for
) -> None:
    app = create_app(settings_for(data_root))
    app.state.paper_client = FakePaperClient(
        {name: paper.PaperUnavailable("Paper is offline") for name in ("account", "positions", "metrics", "rejections")}
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/paper/summary")
        assert response.status_code == 200
        body = response.json()
        assert body["api"] == "paper/summary"
        assert body["freshness"]["state"] == "unavailable"
        assert body["data"]["status"] == "unavailable"
        assert body["data"]["initial_capital_usdc"] is None
        assert body["data"]["equity_usdc"] is None
        for method in (client.post, client.put, client.patch, client.delete):
            assert method("/api/v1/paper/summary").status_code == 405


def test_fills_route_forwards_only_keyset_get(data_root: Path, settings_for) -> None:
    fake = FakePaperClient(
        {
            "fills": {
                "fills": [{"decision_id": "decision-2", "market_id": "market-1"}],
                "limit": 2,
                "next_cursor": "decision-2",
                "has_more": True,
            }
        }
    )
    app = create_app(settings_for(data_root))
    app.state.paper_client = fake
    with TestClient(app) as client:
        response = client.get("/api/v1/paper/fills?limit=2&cursor=decision-1")
        assert response.status_code == 200
        assert response.json()["data"]["mode"] == "PAPER"
        assert response.json()["data"]["next_cursor"] == "decision-2"
        assert fake.calls == [("fills", {"limit": 2, "cursor": "decision-1"})]
        assert client.post("/api/v1/paper/fills").status_code == 405
        assert client.get("/api/v1/paper/ledger").status_code == 404


def test_paper_client_cannot_reach_an_unlisted_resource() -> None:
    client = paper.PaperClient("http://127.0.0.1:8790")
    try:
        with pytest.raises(ValueError, match="unsupported Paper resource"):
            asyncio.run(client.get("trade"))
        assert not hasattr(client, "post")
    finally:
        asyncio.run(client.aclose())

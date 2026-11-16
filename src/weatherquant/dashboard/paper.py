"""Read-only Paper Trading proxy and valuation arithmetic for the dashboard.

The dashboard talks to the Paper engine's HTTP API, never to its database.
Only the small set of GET resources used by the UI is exposed, and all money
figures remain explicitly Paper/simulated figures.

Two valuation views are kept separate:

* the ledger curve is cash plus occupied cost basis, so it needs no market
  quotes and describes realised account movements only;
* current equity is cash plus positions marked at fresh best bids. If any
  open position lacks a mark, current equity, unrealised P&L and ROI are
  ``None`` rather than a partially valued account or a zero.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from typing import Any

import httpx

__all__ = [
    "PaperClient",
    "PaperUnavailable",
    "build_equity_curve",
    "build_summary",
    "current_marks",
    "maximum_drawdown",
]


class PaperUnavailable(RuntimeError):
    """The read-only upstream cannot supply an honest response."""


class PaperClient:
    """A GET-only client for the exact Paper resources the dashboard uses."""

    _RESOURCES = frozenset({"account", "positions", "metrics", "ledger", "rejections", "fills"})

    def __init__(self, base_url: str, *, timeout_seconds: float = 3.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, resource: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if resource not in self._RESOURCES:
            raise ValueError(f"unsupported Paper resource {resource!r}")
        try:
            response = await self._client.get(f"/api/v1/paper/{resource}", params=params)
        except httpx.HTTPError as exc:
            raise PaperUnavailable(f"Paper {resource} is unreachable") from exc
        if response.status_code == 400:
            raise PaperUnavailable(f"Paper {resource} rejected the paging parameters")
        if response.status_code != 200:
            raise PaperUnavailable(f"Paper {resource} is unavailable (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PaperUnavailable(f"Paper {resource} returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise PaperUnavailable(f"Paper {resource} reported unavailable data")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PaperUnavailable(f"Paper {resource} returned no data")
        return data


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def current_marks(markets: Iterable[Any]) -> dict[tuple[str, str], float]:
    """Fresh YES-token best bids, keyed by the position's market and token."""
    marks: dict[tuple[str, str], float] = {}
    for market in markets:
        token_id = getattr(market, "token_yes", None)
        book = getattr(market, "book", None)
        if not token_id or book is None or not book.available or book.stale:
            continue
        bid = _number(getattr(book, "best_bid", None))
        if bid is not None and 0.0 <= bid <= 1.0:
            marks[(str(market.market_id), str(token_id))] = bid
    return marks


def build_equity_curve(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replay cash plus occupied cost basis from the immutable ledger.

    BUY entries add occupied basis; sells and payouts release it. Settlement
    corrections move cash but do not reopen a position. The input is the
    Paper API's ascending keyset sequence, so no historical quote is invented.
    """
    occupied = 0.0
    points: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda item: int(item["seq"])):
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
        kind = entry.get("kind")
        if kind == "BUY_SETTLE":
            basis = _number(detail.get("cost_basis_usdc"))
            if basis is None:
                raise ValueError("BUY_SETTLE has no cost basis")
            occupied += basis
        elif kind in {"SELL_SETTLE", "SETTLE_PAYOUT"}:
            released = _number(detail.get("cost_released_usdc"))
            if released is None:
                raise ValueError(f"{kind} has no released cost basis")
            occupied = max(0.0, occupied - released)
        cash = _number(entry.get("balance_after_usdc"))
        if cash is None:
            raise ValueError("ledger entry has no running cash balance")
        points.append(
            {
                "t": entry.get("ts_utc"),
                "v": cash + occupied,
                "seq": int(entry["seq"]),
            }
        )
    return points


def maximum_drawdown(points: Iterable[dict[str, Any]]) -> float | None:
    """Largest peak-to-trough decline as a fraction of the running peak."""
    values = [_number(point.get("v")) for point in points]
    if not values or any(value is None for value in values):
        return None
    peak = values[0]
    assert peak is not None
    if peak <= 0:
        return None
    drawdown = 0.0
    for value in values[1:]:
        assert value is not None
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def _marked_positions(
    positions: list[dict[str, Any]], marks: dict[tuple[str, str], float]
) -> tuple[list[dict[str, Any]], float | None]:
    rows: list[dict[str, Any]] = []
    complete = True
    unrealised = 0.0
    for position in positions:
        shares = _number(position.get("shares"))
        basis = _number(position.get("cost_basis_usdc"))
        mark = marks.get((str(position.get("market_id")), str(position.get("token_id"))))
        value = shares * mark if shares is not None and mark is not None else None
        pnl = value - basis if value is not None and basis is not None else None
        if pnl is None:
            complete = False
        else:
            unrealised += pnl
        rows.append(
            {
                **position,
                "mark_usd_per_share": mark,
                "market_value_usdc": value,
                "unrealized_pnl_usdc": pnl,
                "mark_status": "OK" if pnl is not None else "MARK_UNAVAILABLE",
            }
        )
    return rows, unrealised if complete else None


async def _all_ledger_entries(client: PaperClient) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cursor: str | int | None = None
    while True:
        params: dict[str, Any] = {"limit": 500}
        if cursor is not None:
            params["cursor"] = cursor
        page = await client.get("ledger", params=params)
        batch = page.get("entries")
        if not isinstance(batch, list):
            raise PaperUnavailable("Paper ledger returned an invalid page")
        entries.extend(batch)
        if not page.get("has_more"):
            return entries
        next_cursor = page.get("next_cursor")
        if next_cursor is None or next_cursor == cursor:
            raise PaperUnavailable("Paper ledger returned an invalid next cursor")
        cursor = next_cursor


async def build_summary(
    client: PaperClient,
    marks: dict[tuple[str, str], float],
    *,
    marks_reason: str | None = None,
) -> dict[str, Any]:
    """Fetch the read-only resources and derive all displayed Paper figures."""
    account, positions_data, metrics, rejection_page = await asyncio.gather(
        client.get("account"),
        client.get("positions"),
        client.get("metrics"),
        client.get("rejections", params={"limit": 20}),
    )
    ledger_entries = await _all_ledger_entries(client)
    try:
        curve = build_equity_curve(ledger_entries)
        positions, unrealised = _marked_positions(positions_data.get("positions") or [], marks)
    except (KeyError, TypeError, ValueError) as exc:
        raise PaperUnavailable("Paper ledger data is inconsistent") from exc

    balances = metrics.get("balances") or {}
    cash = _number(balances.get("available_usdc"))
    occupied = _number(balances.get("occupied_usdc"))
    realised = _number(balances.get("realized_pnl_usdc"))
    initial = _number(account.get("initial_capital_usdc"))
    position_value = (
        sum(row["market_value_usdc"] for row in positions)
        if all(row["market_value_usdc"] is not None for row in positions)
        else None
    )
    equity = cash + position_value if cash is not None and position_value is not None else None
    roi = (equity - initial) / initial if equity is not None and initial is not None and initial > 0 else None
    refusals = metrics.get("refusals") if isinstance(metrics.get("refusals"), dict) else {}
    rejection_summary = sorted(
        ({"reason": str(reason), "count": int(count)} for reason, count in refusals.items()),
        key=lambda row: (-row["count"], row["reason"]),
    )
    has_positions = bool(positions)
    marks_complete = unrealised is not None
    return {
        "status": "ok",
        "mode": "PAPER",
        "initial_capital_usdc": initial,
        "available_usdc": cash,
        "occupied_usdc": occupied,
        "cost_equity_usdc": cash + occupied if cash is not None and occupied is not None else None,
        "equity_usdc": equity,
        "realized_pnl_usdc": realised,
        "unrealized_pnl_usdc": unrealised,
        "roi": roi,
        "max_drawdown": maximum_drawdown(curve),
        "drawdown_basis": "ledger_cost",
        "equity_curve": {"basis": "ledger_cost", "points": curve},
        "positions": positions,
        "marks_status": (
            "not_required" if not has_positions else "fresh" if marks_complete else "unavailable"
        ),
        "marks_reason": marks_reason if has_positions and not marks_complete else None,
        "rejection_summary": rejection_summary,
        "rejections": rejection_page.get("items") or [],
        "ledger_consistent": metrics.get("ledger_consistent"),
        "conservation_ok": metrics.get("conservation_ok"),
        "fill_count": metrics.get("fill_count"),
    }


def unavailable_summary(reason: str) -> dict[str, Any]:
    """An explicit empty state; missing figures are null, never zero."""
    return {
        "status": "unavailable",
        "mode": "PAPER",
        "reason": reason,
        "initial_capital_usdc": None,
        "available_usdc": None,
        "occupied_usdc": None,
        "cost_equity_usdc": None,
        "equity_usdc": None,
        "realized_pnl_usdc": None,
        "unrealized_pnl_usdc": None,
        "roi": None,
        "max_drawdown": None,
        "drawdown_basis": "ledger_cost",
        "equity_curve": {"basis": "ledger_cost", "points": []},
        "positions": [],
        "marks_status": "unavailable",
        "marks_reason": reason,
        "rejection_summary": [],
        "rejections": [],
        "ledger_consistent": None,
        "conservation_ok": None,
        "fill_count": None,
    }

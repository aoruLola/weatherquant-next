"""Read-only API payloads for the paper engine (WQN-45).

The same discipline as ``dashboard/producer.py``: this module never imports
duckdb and never opens a file - the connection is injected by whoever holds
it (the ``serve`` command, or a snapshot publisher). Every builder returns
plain JSON-ready dicts, so the HTTP layer stays a thin router and the
payloads are testable without a socket.

The rules the acceptance criteria name, and how they hold:

**Read-only, by construction.** Every builder only issues SELECTs; there is
no POST route anywhere in the router (``web.py`` answers non-GET with 405
before any payload is built), and nothing here can write a ledger row even
by mistake.

**State and ledger agree.** ``metrics`` and ``health`` embed the ledger's
own reconciliation (:meth:`~weatherquant.paper.ledger.Ledger.verify`) and
the conservation law, so an API consumer sees ``ledger_consistent: false``
at the same moment the books disagree - the API cannot claim a clean state
the ledger contradicts.

**Explicit failure, never a hang.** A builder that cannot read its tables
returns ``{"status": "unavailable", "reason": ...}`` rather than raising
into the router: a dashboard that says "unavailable" is useful, a 500 with
a stack trace is not.

**No credentials, no write capability.** The payloads contain counters,
balances, positions and ledger entries - all derived from the engine's own
database, which the server may only open because it *is* the engine's
process. No connection strings, keys or tokens exist anywhere in this
package to expose.

**Pagination is keyset, not offset.** Ledger entries page on the monotonic
``seq``; signals/fills/refusals page on ``decision_id``. An offset page
would duplicate or skip rows whenever a row lands between two calls - and
this engine writes continuously.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "BUILDERS",
    "ENDPOINTS",
    "account_payload",
    "fills_payload",
    "health_payload",
    "ledger_payload",
    "metrics_payload",
    "orders_payload",
    "positions_payload",
    "rejections_payload",
]

#: The route table: GET /api/v1/paper/<name> -> builder. The router serves
#: exactly these; anything else is 404, anything not GET is 405.
ENDPOINTS = (
    "account",
    "positions",
    "orders",
    "fills",
    "ledger",
    "metrics",
    "rejections",
    "health",
)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500

#: The route table: name -> (builder, takes_query). The router serves
#: exactly these, GET only - every builder is a reader, and there is no
#: write path anywhere in this table to expose.
BUILDERS: dict[str, tuple[Any, bool]] = {
    "account": (lambda con, *, now: account_payload(con, now=now), False),
    "positions": (lambda con, *, now: positions_payload(con, now=now), False),
    "orders": (lambda con, *, now, query=None: orders_payload(con, now=now, query=query), True),
    "fills": (lambda con, *, now, query=None: fills_payload(con, now=now, query=query), True),
    "ledger": (lambda con, *, now, query=None: ledger_payload(con, now=now, query=query), True),
    "metrics": (lambda con, *, now: metrics_payload(con, now=now), False),
    "rejections": (lambda con, *, now, query=None: rejections_payload(con, now=now, query=query), True),
    "health": (lambda con, *, now: health_payload(con, now=now), False),
}

_BUILDERS = BUILDERS  # router alias; the table is the same object


class _StoreAdapter:
    """Duck-type a raw connection into what PaperStore/Ledger expect.

    The builders and the ledger's reconciliation only use ``execute`` and
    ``con``; this adapter lets them run against a connection the server was
    handed, without the adapter ever being able to open a file itself.
    """

    def __init__(self, con: Any) -> None:
        self.con = con

    def execute(self, sql: str, params: list | tuple | None = None):
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)


def _iso(value: Any) -> str | None:
    """UTC ISO-8601, or None. Naive timestamps are refused, not guessed."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"timestamp {value!r} is timezone-naive; the API emits UTC only")
        return value.astimezone(UTC).isoformat()
    return str(value)


def _unavailable(kind: str, reason: str, now: datetime) -> dict[str, Any]:
    return {
        "kind": kind,
        "status": "unavailable",
        "reason": reason,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
    }


def _page(query: dict[str, Any]) -> tuple[int, str | None]:
    """Parse and bound the paging parameters."""
    try:
        limit = int(query.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    limit = max(1, min(limit, _MAX_LIMIT))
    cursor = query.get("cursor")
    return limit, str(cursor) if cursor is not None and cursor != "" else None


def account_payload(con: Any, *, now: datetime) -> dict[str, Any]:
    store = _store(con)
    try:
        account = store.account()
        if account is None:
            return _unavailable(
                "account", "no account has been opened (run weatherquant-paper init)", now
            )
        account["created_at_utc"] = _iso(account.get("created_at_utc"))
        return {
            "kind": "account",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": account,
        }
    except Exception as exc:
        return _unavailable("account", f"{type(exc).__name__}: {exc}", now)


def positions_payload(con: Any, *, now: datetime) -> dict[str, Any]:
    try:
        store = _store(con)
        ledger = _ledger(store)
        balances = ledger.balances() or {}
        rows = [
            {
                **position.as_dict(),
                "value_note": "mark values are computed by the engine's valuation, not stored here",
            }
            for position in ledger.positions()
        ]
        return {
            "kind": "positions",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "positions": rows,
                "occupied_usdc": balances.get("occupied_usdc"),
                "cost_basis_total_usdc": sum(r["cost_basis_usdc"] for r in rows),
            },
        }
    except Exception as exc:
        return _unavailable("positions", f"{type(exc).__name__}: {exc}", now)


def orders_payload(con: Any, *, now: datetime, query: dict[str, Any] | None = None) -> dict[str, Any]:
    """The signals the desk received - the paper engine's order flow, read-only."""
    return _signal_page(con, now=now, kind="orders", only_refused=False, query=query or {})


def rejections_payload(con: Any, *, now: datetime, query: dict[str, Any] | None = None) -> dict[str, Any]:
    return _signal_page(con, now=now, kind="rejections", only_refused=True, query=query or {})


def _signal_page(
    con: Any, *, now: datetime, kind: str, only_refused: bool, query: dict[str, Any]
) -> dict[str, Any]:
    try:
        limit, cursor = _page(query)
        con_ = con
        where = "WHERE outcome = 'REFUSED' " if only_refused else ""
        params: list[Any] = []
        if cursor:
            where += "WHERE decision_id > ? " if not only_refused else "AND decision_id > ? "
            params.append(cursor)
        rows = con_.execute(
            "SELECT decision_id, run_id, cycle_id, market_id, token_id, direction, state, mode, "
            "outcome, refusal_reason, refusal_detail, decision_time_utc, consumed_at_utc, net_edge "
            f"FROM paper_signals {where}"
            "ORDER BY decision_id LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "decision_id": str(r[0]),
                "run_id": str(r[1]),
                "cycle_id": str(r[2]),
                "market_id": str(r[3]),
                "token_id": r[4],
                "direction": str(r[5]),
                "decision_state": str(r[6]),
                "mode": str(r[7]),
                "outcome": str(r[8]),
                "refusal_reason": r[9],
                "refusal_detail": r[10],
                "decision_time_utc": _iso(r[11]),
                "consumed_at_utc": _iso(r[12]),
                "net_edge": r[13],
            }
            for r in rows
        ]
        return {
            "kind": kind,
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "items": items,
                "limit": limit,
                "next_cursor": rows[-1][0] if has_more and rows else None,
                "has_more": has_more,
            },
        }
    except Exception as exc:
        return _unavailable(kind, f"{type(exc).__name__}: {exc}", now)


def fills_payload(con: Any, *, now: datetime, query: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        limit, cursor = _page(query or {})
        where = ""
        params: list[Any] = []
        if cursor:
            where = "WHERE decision_id > ? "
            params.append(cursor)
        rows = con.execute(
            "SELECT decision_id, market_id, token_id, direction, side, book_id, book_source, "
            "book_venue_ts_utc, book_age_ms, requested_shares, filled_shares, "
            "avg_price_usd_per_share, best_price_usd_per_share, slippage_usd_per_share, "
            "fill_cost_usd, fees_usd, execution_buffer_usd, total_cost_usd, net_proceeds_usd, "
            "levels_consumed, complete, decision_time_utc, filled_at_utc, fee_rate, "
            "book_bids_json, book_asks_json, book_source_event_id "
            f"FROM paper_fills {where}ORDER BY decision_id LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for r in rows:
            items.append(
                {
                    "decision_id": str(r[0]),
                    "market_id": str(r[1]),
                    "token_id": str(r[2]),
                    "direction": str(r[3]),
                    "side": str(r[4]),
                    "book_id": str(r[5]),
                    "book_source": str(r[6]),
                    "book_venue_ts_utc": _iso(r[7]),
                    "book_age_ms": r[8],
                    "requested_shares": r[9],
                    "filled_shares": r[10],
                    "avg_price_usd_per_share": r[11],
                    "best_price_usd_per_share": r[12],
                    "slippage_usd_per_share": r[13],
                    "fill_cost_usd": r[14],
                    "fees_usd": r[15],
                    "execution_buffer_usd": r[16],
                    "total_cost_usd": r[17],
                    "net_proceeds_usd": r[18],
                    "levels_consumed": r[19],
                    "complete": bool(r[20]),
                    "decision_time_utc": _iso(r[21]),
                    "filled_at_utc": _iso(r[22]),
                    "fee_rate": r[23],
                    # The full two-sided evidence (WQN-24) rides the payload.
                    "book_bids": json.loads(str(r[24])),
                    "book_asks": json.loads(str(r[25])),
                    "book_source_event_id": r[26],
                }
            )
        return {
            "kind": "fills",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "fills": items,
                "limit": limit,
                "next_cursor": rows[-1][0] if has_more and rows else None,
                "has_more": has_more,
            },
        }
    except Exception as exc:
        return _unavailable("fills", f"{type(exc).__name__}: {exc}", now)


def ledger_payload(con: Any, *, now: datetime, query: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        limit, cursor = _page(query or {})
        params: list[Any] = []
        where = ""
        if cursor:
            try:
                seq_cursor = int(cursor)
            except ValueError:
                seq_cursor = None
            if seq_cursor is None or not -(2**63) <= seq_cursor < 2**63:
                return {"kind": "ledger", "status": "error", "reason": "cursor must be a signed 64-bit ledger seq"}
            where = "WHERE seq > ? "
            params.append(seq_cursor)
        rows = con.execute(
            "SELECT seq, ts_utc, kind, amount_usdc, balance_after_usdc, ref_type, ref_id, detail_json "
            f"FROM paper_ledger {where}ORDER BY seq LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "seq": int(r[0]),
                "ts_utc": _iso(r[1]),
                "kind": str(r[2]),
                "amount_usdc": float(r[3]),
                "balance_after_usdc": float(r[4]),
                "ref_type": str(r[5]),
                "ref_id": str(r[6]),
                "detail": json.loads(str(r[7])),
            }
            for r in rows
        ]
        return {
            "kind": "ledger",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "entries": items,
                "limit": limit,
                "next_cursor": int(rows[-1][0]) if has_more and rows else None,
                "has_more": has_more,
            },
        }
    except Exception as exc:
        return _unavailable("ledger", f"{type(exc).__name__}: {exc}", now)


def metrics_payload(con: Any, *, now: datetime) -> dict[str, Any]:
    try:
        store = _store(con)
        ledger = _ledger(store)
        b = ledger.balances() or {}
        conservation = abs(
            (b.get("available_usdc") or 0.0)
            + (b.get("occupied_usdc") or 0.0)
            - _initial_capital(con)
            - (b.get("realized_pnl_usdc") or 0.0)
        ) <= 1e-6
        verification = ledger.verify()
        return {
            "kind": "metrics",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "signals": store.signal_counts(),
                "refusals": store.refusal_counts(),
                "quality_events": store.quality_counts(),
                "fill_count": con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0],
                "balances": {
                    "available_usdc": b.get("available_usdc"),
                    "occupied_usdc": b.get("occupied_usdc"),
                    "realized_pnl_usdc": b.get("realized_pnl_usdc"),
                },
                "conservation_ok": conservation,
                "ledger_consistent": verification["consistent"],
                "ledger_running_balance_consistent": verification["running_balance_consistent"],
            },
        }
    except Exception as exc:
        return _unavailable("metrics", f"{type(exc).__name__}: {exc}", now)


def health_payload(con: Any, *, now: datetime) -> dict[str, Any]:
    """Liveness the API consumer can act on: explicit, never silently stale."""
    try:
        store = _store(con)
        ledger = _ledger(store)
        reviews = ledger.review_required()
        return {
            "kind": "health",
            "status": "ok",
            "generated_at_utc": now.astimezone(UTC).isoformat(),
            "data": {
                "account_open": store.account() is not None,
                "ledger_consistent": ledger.verify()["consistent"],
                "review_required": reviews,
                "review_required_count": len(reviews),
                "signals": store.signal_counts(),
            },
        }
    except Exception as exc:
        return _unavailable("health", f"{type(exc).__name__}: {exc}", now)


# -- internals ---------------------------------------------------------------


def _store(con: Any):
    from weatherquant.paper.store import PaperStore

    return PaperStore(_StoreAdapter(con))


def _ledger(store):
    from weatherquant.paper.ledger import Ledger

    return Ledger(store)


def _initial_capital(con: Any) -> float:
    row = con.execute(
        "SELECT initial_capital_usdc FROM paper_accounts WHERE account_id = 'paper-main'"
    ).fetchone()
    return float(row[0]) if row else 0.0

"""The double-sided ledger: cash, occupied capital, positions and P&L.

Six buckets, separately accounted, because collapsing any two of them is how
a paper account starts lying:

* **available cash** - free to commit to a new fill;
* **occupied capital** ("frozen") - the cost basis currently locked inside
  open positions. It leaves at settlement (WQN-44) or on a sale, never
  silently;
* **realised P&L** - from *sales* only. An unsettled position has no
  realised profit, by rule;
* **positions** - shares, average cost and cost basis per (market, token);
* **the ledger itself** - append-only, every movement stamped with the
  running cash balance;
* **valuation** - computed on read from marks supplied by the caller; a
  position whose mark cannot be established is ``MARK_UNAVAILABLE``, never
  zero and never skipped.

One conservation law answers for all of them::

    available + occupied == initial_capital + realised_pnl

Enforced three ways: the schema's CHECK constraints refuse a negative
balance at the storage layer; :meth:`Ledger.verify` replays the ledger and
compares it against the stored state (the restart-recovery proof); and the
deterministic ``entry_id`` makes replaying a settle a no-op - a fill
redelivered after a crash debits nothing twice.

Cash amounts are the *venue-real* figures: a buy costs ``fill + fees``, a
sale nets ``fill - fees``. The execution buffer is a pricing margin the
shadow's edge arithmetic carries; it is never cash and never enters this
ledger.

Every mutating method must run inside an open transaction
(:meth:`PaperDatabase.transaction`) together with the fill row and the
signal-outcome transition that caused it - the ledger has no transactions
of its own, deliberately, so a position increment and its fill cannot land
in different commits.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from weatherquant.paper.settlements import settlement_id
from weatherquant.paper.store import ACCOUNT_ID, PaperStore

__all__ = [
    "InsufficientFunds",
    "InsufficientInventory",
    "Ledger",
    "LedgerError",
    "MarkStatus",
    "ledger_entry_id",
]

log = logging.getLogger("weatherquant.paper")

#: Tolerance for float reconciliation; money is compared, not bit-matched.
_EPS = 1e-9


class LedgerError(RuntimeError):
    """The ledger refuses a movement that would break its own invariants."""


class InsufficientFunds(LedgerError):
    """A buy whose cash outflow exceeds the available bucket."""


class InsufficientInventory(LedgerError):
    """A sell of shares the account does not hold."""


class MarkStatus:
    OK = "OK"
    MARK_UNAVAILABLE = "MARK_UNAVAILABLE"


def ledger_entry_id(*, kind: str, ref_id: str) -> str:
    """Deterministic id: the same settle replayed is the same row."""
    return hashlib.sha256(f"{kind}|{ref_id}".encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class LedgerPosition:
    market_id: str
    token_id: str
    shares: float
    avg_cost_usd_per_share: float
    cost_basis_usdc: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "shares": self.shares,
            "avg_cost_usd_per_share": self.avg_cost_usd_per_share,
            "cost_basis_usdc": self.cost_basis_usdc,
        }


class Ledger:
    """Accounting over one open :class:`PaperStore`. Single writer, one account."""

    def __init__(self, store: PaperStore) -> None:
        self.store = store
        self.db = store.db

    # -- opening ------------------------------------------------------------

    def open_account(self, *, initial_capital_usdc: float, now: datetime) -> bool:
        """Write the OPEN entry and the balances row. Idempotent.

        Returns True when this call opened the ledger. The amounts are first
        write wins, exactly like the account row itself.
        """
        if self.balances() is not None:
            return False
        self.db.execute(
            "INSERT INTO paper_balances (account_id, available_usdc, occupied_usdc, "
            "realized_pnl_usdc, updated_at_utc) VALUES (?, ?, 0.0, 0.0, ?) "
            "ON CONFLICT (account_id) DO NOTHING",
            [ACCOUNT_ID, float(initial_capital_usdc), now],
        )
        self._insert_entry(
            kind="OPEN",
            ts_utc=now,
            amount=float(initial_capital_usdc),
            balance_after=float(initial_capital_usdc),
            ref_type="account",
            ref_id=ACCOUNT_ID,
            detail={"initial_capital_usdc": float(initial_capital_usdc)},
        )
        return True

    # -- reads ----------------------------------------------------------------

    def balances(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT available_usdc, occupied_usdc, realized_pnl_usdc, updated_at_utc "
            "FROM paper_balances WHERE account_id = ?",
            [ACCOUNT_ID],
        ).fetchone()
        if row is None:
            return None
        return {
            "available_usdc": float(row[0]),
            "occupied_usdc": float(row[1]),
            "realized_pnl_usdc": float(row[2]),
            "updated_at_utc": row[3],
        }

    def positions(self) -> list[LedgerPosition]:
        rows = self.db.execute(
            "SELECT market_id, token_id, shares, avg_cost_usd_per_share, cost_basis_usdc "
            "FROM paper_positions WHERE shares > ? ORDER BY market_id, token_id",
            [_EPS],
        ).fetchall()
        return [
            LedgerPosition(
                market_id=str(r[0]),
                token_id=str(r[1]),
                shares=float(r[2]),
                avg_cost_usd_per_share=float(r[3]),
                cost_basis_usdc=float(r[4]),
            )
            for r in rows
        ]

    def ledger_entries(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT ts_utc, kind, amount_usdc, balance_after_usdc, ref_type, ref_id, detail_json "
            "FROM paper_ledger ORDER BY seq LIMIT ?",
            [int(limit)],
        ).fetchall()
        return [
            {
                "ts_utc": r[0],
                "kind": str(r[1]),
                "amount_usdc": float(r[2]),
                "balance_after_usdc": float(r[3]),
                "ref_type": str(r[4]),
                "ref_id": str(r[5]),
                "detail": json.loads(str(r[6])),
            }
            for r in rows
        ]

    # -- the two sides --------------------------------------------------------

    def apply_buy(self, fill, *, now: datetime) -> bool:
        """Settle a BUY: cash out ``fill + fees``, position up, occupied up.

        Returns False when this decision has already been settled (idempotent
        replay). Raises :class:`InsufficientFunds` rather than allowing the
        available bucket to go negative.
        """
        cash_out = fill.fill_cost_usd + fill.fees_usd
        entry_id = ledger_entry_id(kind="BUY_SETTLE", ref_id=fill.decision_id)
        if self._entry_exists(entry_id):
            return False

        balances = self.balances()
        if balances is None:
            raise LedgerError("the ledger is not open; run `weatherquant-paper init` first")
        if cash_out > balances["available_usdc"] + _EPS:
            raise InsufficientFunds(
                f"buy needs {cash_out:.4f} USDC (fill {fill.fill_cost_usd:.4f} + fees "
                f"{fill.fees_usd:.4f}) but only {balances['available_usdc']:.4f} is available; "
                "the account does not go negative"
            )

        available = balances["available_usdc"] - cash_out
        occupied = balances["occupied_usdc"] + cash_out
        self.db.execute(
            "UPDATE paper_balances SET available_usdc = ?, occupied_usdc = ?, updated_at_utc = ? WHERE account_id = ?",
            [available, occupied, now, ACCOUNT_ID],
        )

        existing = self.db.execute(
            "SELECT shares, cost_basis_usdc FROM paper_positions WHERE market_id = ? AND token_id = ?",
            [fill.market_id, fill.token_id],
        ).fetchone()
        if existing is None:
            shares, basis = fill.filled_shares, cash_out
            opened_at = now
        else:
            shares = float(existing[0]) + fill.filled_shares
            basis = float(existing[1]) + cash_out
            opened_at = None
        avg = basis / shares if shares > _EPS else 0.0
        if opened_at is None:
            self.db.execute(
                "UPDATE paper_positions SET shares = ?, avg_cost_usd_per_share = ?, "
                "cost_basis_usdc = ?, updated_at_utc = ? WHERE market_id = ? AND token_id = ?",
                [shares, avg, basis, now, fill.market_id, fill.token_id],
            )
        else:
            self.db.execute(
                "INSERT INTO paper_positions (market_id, token_id, shares, "
                "avg_cost_usd_per_share, cost_basis_usdc, opened_at_utc, updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (market_id, token_id) DO NOTHING",
                [fill.market_id, fill.token_id, shares, avg, basis, opened_at, now],
            )

        self._insert_entry(
            kind="BUY_SETTLE",
            ts_utc=now,
            amount=-cash_out,
            balance_after=available,
            ref_type="fill",
            ref_id=fill.decision_id,
            detail={
                "market_id": fill.market_id,
                "token_id": fill.token_id,
                "shares": fill.filled_shares,
                "avg_cost_usd_per_share": avg,
                "cost_basis_usdc": cash_out,
                "fill_cost_usd": fill.fill_cost_usd,
                "fees_usd": fill.fees_usd,
            },
        )
        return True

    def apply_sell(self, fill, *, now: datetime) -> bool:
        """Settle a SELL: net cash in, position down, realised P&L booked.

        Realised P&L is ``(fill - fees) - avg_cost * sold_shares`` - booked at
        the sale, never before, and never from an unsettled position.
        """
        net_in = fill.fill_cost_usd - fill.fees_usd
        entry_id = ledger_entry_id(kind="SELL_SETTLE", ref_id=fill.decision_id)
        if self._entry_exists(entry_id):
            return False

        balances = self.balances()
        if balances is None:
            raise LedgerError("the ledger is not open; run `weatherquant-paper init` first")
        row = self.db.execute(
            "SELECT shares, avg_cost_usd_per_share, cost_basis_usdc FROM paper_positions "
            "WHERE market_id = ? AND token_id = ?",
            [fill.market_id, fill.token_id],
        ).fetchone()
        held = float(row[0]) if row else 0.0
        if fill.filled_shares > held + _EPS:
            raise InsufficientInventory(
                f"sell of {fill.filled_shares:.1f} shares refused: the account holds "
                f"{held:.1f} of this token; no short positions and no naked sells"
            )
        avg = float(row[1]) if row else 0.0
        basis = float(row[2]) if row else 0.0
        sold_basis = min(avg * fill.filled_shares, basis)
        realised = net_in - sold_basis

        remaining_shares = max(held - fill.filled_shares, 0.0)
        remaining_basis = max(basis - sold_basis, 0.0)
        self.db.execute(
            "UPDATE paper_positions SET shares = ?, cost_basis_usdc = ?, updated_at_utc = ? "
            "WHERE market_id = ? AND token_id = ?",
            [remaining_shares, remaining_basis, now, fill.market_id, fill.token_id],
        )
        self.db.execute(
            "UPDATE paper_balances SET available_usdc = ?, occupied_usdc = ?, "
            "realized_pnl_usdc = ?, updated_at_utc = ? WHERE account_id = ?",
            [
                balances["available_usdc"] + net_in,
                max(balances["occupied_usdc"] - sold_basis, 0.0),
                balances["realized_pnl_usdc"] + realised,
                now,
                ACCOUNT_ID,
            ],
        )
        self._insert_entry(
            kind="SELL_SETTLE",
            ts_utc=now,
            amount=net_in,
            balance_after=balances["available_usdc"] + net_in,
            ref_type="fill",
            ref_id=fill.decision_id,
            detail={
                "market_id": fill.market_id,
                "token_id": fill.token_id,
                "shares": fill.filled_shares,
                "avg_cost_usd_per_share": avg,
                "proceeds_usdc": net_in,
                "cost_released_usdc": sold_basis,
                "realized_pnl_usdc": realised,
                "fill_cost_usd": fill.fill_cost_usd,
                "fees_usd": fill.fees_usd,
            },
        )
        return True

    # -- exposure ---------------------------------------------------------------

    def market_exposure(self, market_id: str) -> float:
        """Cost basis occupied across all open positions in one market.

        This is the number the per-market exposure cap climbs: repeated
        fills in the same market are one exposure, not many.
        """
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_basis_usdc), 0.0) FROM paper_positions WHERE market_id = ? AND shares > ?",
            [market_id, _EPS],
        ).fetchone()
        return float(row[0])

    def held_shares(self, market_id: str, token_id: str) -> float:
        """Shares of one token currently held (0 when the position is closed)."""
        row = self.db.execute(
            "SELECT shares FROM paper_positions WHERE market_id = ? AND token_id = ?",
            [market_id, token_id],
        ).fetchone()
        return float(row[0]) if row else 0.0

    # -- valuation --------------------------------------------------------------

    def mark_valuation(self, mark_of: Callable[[str], float | None]) -> dict[str, Any]:
        """Value every open position against a caller-supplied mark.

        ``mark_of(token_id)`` returns the price one share would realise now -
        for a long position the *bid*, conservatively - or ``None`` when no
        mark can be established. A position without a mark is reported
        ``MARK_UNAVAILABLE`` and excluded from the equity figure, which is
        stated alongside: an equity that silently drops or zeroes a position
        would understate exposure exactly when the feed is worst.
        """
        rows: list[dict[str, Any]] = []
        unavailable: list[str] = []
        marked_total = 0.0
        for position in self.positions():
            mark = mark_of(position.token_id)
            if mark is None or not 0.0 < mark < 1.0:
                unavailable.append(position.market_id)
                rows.append(
                    {
                        **position.as_dict(),
                        "mark_status": MarkStatus.MARK_UNAVAILABLE,
                        "mark_usd_per_share": None,
                        "value_usdc": None,
                    }
                )
                continue
            value = mark * position.shares
            marked_total += value
            rows.append(
                {
                    **position.as_dict(),
                    "mark_status": MarkStatus.OK,
                    "mark_usd_per_share": mark,
                    "value_usdc": value,
                }
            )
        balances = self.balances() or {}
        return {
            "positions": rows,
            "available_usdc": balances.get("available_usdc"),
            "occupied_usdc": balances.get("occupied_usdc"),
            "realized_pnl_usdc": balances.get("realized_pnl_usdc"),
            "marked_positions_value_usdc": marked_total,
            "equity_usdc": (balances.get("available_usdc") or 0.0) + marked_total,
            "marks_unavailable": unavailable,
            "all_marked": not unavailable,
        }

    # -- recovery & reconciliation ----------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Replay the ledger and compare against the stored state.

        This is the restart-recovery proof: everything the engine holds is
        derivable from the ledger alone, so a crash that committed anything
        less than a whole transaction shows up here as a mismatch, and a
        clean replay shows up as consistent.
        """
        expected_balances, expected_positions = self._replay()
        stored_balances = self.balances() or {}
        stored_positions = {(p.market_id, p.token_id): p for p in self.positions()}
        # Closed positions (shares == 0) are stored but not replayed as open;
        # compare only what should be open.
        stored_open = {k: v for k, v in stored_positions.items() if v.shares > _EPS}
        expected_open = {k: v for k, v in expected_positions.items() if v.shares > _EPS}

        balance_ok = all(
            abs(stored_balances.get(field, 0.0) - expected_balances[field]) <= 1e-6
            for field in ("available_usdc", "occupied_usdc", "realized_pnl_usdc")
        )
        positions_ok = set(stored_open) == set(expected_open) and all(
            _same_position(stored_open[k], expected_open[k]) for k in expected_open
        )
        running_ok = self._running_balance_consistent()
        return {
            "consistent": balance_ok and positions_ok and running_ok,
            "balances_match": balance_ok,
            "positions_match": positions_ok,
            "running_balance_consistent": running_ok,
            "expected_balances": expected_balances,
            "stored_balances": {k: stored_balances.get(k) for k in expected_balances},
            "open_positions": len(expected_open),
        }

    def _replay(self) -> tuple[dict[str, float], dict[tuple[str, str], LedgerPosition]]:
        available = occupied = realised = 0.0
        positions: dict[tuple[str, str], dict[str, float]] = {}
        for entry in self._all_entries():
            detail = entry["detail"]
            if entry["kind"] == "OPEN":
                available += float(detail["initial_capital_usdc"])
            elif entry["kind"] == "BUY_SETTLE":
                available += entry["amount_usdc"]  # negative
                occupied += float(detail["cost_basis_usdc"])
                key = (detail["market_id"], detail["token_id"])
                pos = positions.setdefault(key, {"shares": 0.0, "basis": 0.0})
                pos["shares"] += float(detail["shares"])
                pos["basis"] += float(detail["cost_basis_usdc"])
            elif entry["kind"] == "SELL_SETTLE":
                available += entry["amount_usdc"]  # positive
                occupied -= float(detail["cost_released_usdc"])
                realised += float(detail["realized_pnl_usdc"])
                key = (detail["market_id"], detail["token_id"])
                pos = positions.setdefault(key, {"shares": 0.0, "basis": 0.0})
                pos["shares"] -= float(detail["shares"])
                pos["basis"] -= float(detail["cost_released_usdc"])
            elif entry["kind"] == "SETTLE_PAYOUT":
                # A payout releases every position it names and books the
                # realised P&L the sale-side entries never carried.
                available += entry["amount_usdc"]
                occupied -= float(detail["cost_released_usdc"])
                realised += float(detail["realized_pnl_usdc"])
                for pos_detail in detail.get("positions", []):
                    key = (detail["market_id"], pos_detail["token_id"])
                    pos = positions.setdefault(key, {"shares": 0.0, "basis": 0.0})
                    pos["shares"] -= float(pos_detail["shares"])
                    pos["basis"] -= float(pos_detail["cost_released_usdc"])
            elif entry["kind"] == "CORRECTION":
                # A compensation moves cash and realised P&L by the same
                # delta; positions are untouched (they closed at payout).
                available += entry["amount_usdc"]
                realised += entry["amount_usdc"]
        rebuilt = {
            "available_usdc": available,
            "occupied_usdc": occupied,
            "realized_pnl_usdc": realised,
        }
        out = {}
        for key, pos in positions.items():
            if pos["shares"] > _EPS:
                out[key] = LedgerPosition(
                    market_id=key[0],
                    token_id=key[1],
                    shares=pos["shares"],
                    avg_cost_usd_per_share=pos["basis"] / pos["shares"],
                    cost_basis_usdc=pos["basis"],
                )
        return rebuilt, out

    def _all_entries(self) -> list[dict[str, Any]]:
        return self.ledger_entries(limit=1_000_000)

    def _running_balance_consistent(self) -> bool:
        expected = 0.0
        for entry in self._all_entries():
            if entry["kind"] == "OPEN":
                expected += entry["amount_usdc"]
            else:
                expected += entry["amount_usdc"]
            if abs(expected - entry["balance_after_usdc"]) > 1e-6:
                return False
        return True

    # -- internals ----------------------------------------------------------------

    def _entry_exists(self, entry_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM paper_ledger WHERE entry_id = ?", [entry_id]).fetchone() is not None

    # -- settlements ---------------------------------------------------------

    def apply_settlement(self, event, *, now: datetime) -> dict[str, Any]:
        """Apply one published settlement record. Idempotent by natural key.

        Returns an outcome dict: ``action`` is one of

        * ``APPLIED``          - a payout (or correction) moved the ledger;
        * ``NOOP``             - already applied, superseded, or a redundant
                                 confirmation; nothing moved, ever;
        * ``RECORDED``         - stored for audit, nothing to apply (not a
                                 payout, or no open position);
        * ``REVIEW_REQUIRED``  - a correction or dispute that must not be
                                 settled automatically; balances untouched.

        Must run inside a transaction together with whatever it moves.
        """
        market_id = event.market_id
        inserted = self._insert_settlement_row(event, now=now)
        if not inserted:
            return {
                "action": "NOOP",
                "market_id": market_id,
                "revision": event.revision,
                "detail": "already recorded; the natural key absorbed the redelivery",
            }

        latest = self.db.execute(
            "SELECT MAX(revision) FROM paper_settlements WHERE market_id = ?", [market_id]
        ).fetchone()[0]
        if event.revision < latest:
            return {
                "action": "NOOP",
                "market_id": market_id,
                "revision": event.revision,
                "detail": f"superseded by recorded revision {latest}",
            }

        applied = self._applied_settlement(market_id)
        if not event.is_payout:
            detail = (
                "resolution lacks VERIFIED evidence" if event.status == "RESOLVED" else f"status {event.status}"
            )
            if applied is not None:
                detail = f"{detail} after payout at revision {applied['revision']}; manual review required"
                self._flag_review(event, reason=detail)
            return {
                "action": "REVIEW_REQUIRED" if applied is not None else "RECORDED",
                "market_id": market_id,
                "revision": event.revision,
                "detail": detail,
            }

        payout = float(event.yes_payout)  # guaranteed by is_payout
        if applied is None:
            return self._apply_first_payout(event, payout=payout, now=now)
        if payout == applied["yes_payout"]:
            self._mark_settlement_applied(event, now=now)
            return {
                "action": "NOOP",
                "market_id": market_id,
                "revision": event.revision,
                "detail": f"confirms the payout {payout} already applied at revision "
                f"{applied['revision']}; no entry written",
            }
        return self._apply_correction(event, payout=payout, applied=applied, now=now)

    def _apply_first_payout(self, event, *, payout: float, now: datetime) -> dict[str, Any]:
        """Release occupied capital and book realised P&L on settlement.

        A payout on shares bought at cost basis C pays ``payout * shares``:
        available rises by that, occupied falls by C, and realised P&L
        finally exists - it did not exist the whole time the position was
        merely open.
        """
        positions = [p for p in self.positions() if p.market_id == event.market_id]
        if not positions:
            return {
                "action": "RECORDED",
                "market_id": event.market_id,
                "revision": event.revision,
                "detail": "RESOLVED but the account holds no open position in this market",
            }
        balances = self.balances() or {}
        total_proceeds = 0.0
        total_released = 0.0
        total_realised = 0.0
        details = []
        for position in positions:
            proceeds = payout * position.shares
            released = position.cost_basis_usdc
            realised = proceeds - released
            total_proceeds += proceeds
            total_released += released
            total_realised += realised
            self.db.execute(
                "UPDATE paper_positions SET shares = 0.0, cost_basis_usdc = 0.0, updated_at_utc = ? "
                "WHERE market_id = ? AND token_id = ?",
                [now, position.market_id, position.token_id],
            )
            details.append(
                {
                    "token_id": position.token_id,
                    "shares": position.shares,
                    "avg_cost_usd_per_share": position.avg_cost_usd_per_share,
                    "cost_released_usdc": released,
                    "proceeds_usdc": proceeds,
                    "realized_pnl_usdc": realised,
                }
            )
        available = balances.get("available_usdc", 0.0) + total_proceeds
        occupied = max(balances.get("occupied_usdc", 0.0) - total_released, 0.0)
        realised_total = balances.get("realized_pnl_usdc", 0.0) + total_realised
        self.db.execute(
            "UPDATE paper_balances SET available_usdc = ?, occupied_usdc = ?, "
            "realized_pnl_usdc = ?, updated_at_utc = ? WHERE account_id = ?",
            [available, occupied, realised_total, now, ACCOUNT_ID],
        )
        ref_id = f"{event.market_id}#{event.revision}"
        self._insert_entry(
            kind="SETTLE_PAYOUT",
            ts_utc=now,
            amount=total_proceeds,
            balance_after=available,
            ref_type="settlement",
            ref_id=ref_id,
            detail={
                "market_id": event.market_id,
                "revision": event.revision,
                "yes_payout": payout,
                "positions": details,
                "cost_released_usdc": total_released,
                "realized_pnl_usdc": total_realised,
            },
        )
        self._mark_settlement_applied(event, now=now)
        return {
            "action": "APPLIED",
            "market_id": event.market_id,
            "revision": event.revision,
            "detail": f"payout {payout} on {len(details)} position(s); released "
            f"{total_released:.4f} USDC, realised {total_realised:.4f} USDC",
        }

    def _apply_correction(self, event, *, payout: float, applied: dict[str, Any], now: datetime) -> dict[str, Any]:
        """A changed payout: compensate, never edit.

        The delta is ``(new - old) * settled_shares``. When it claws back
        more cash than the account holds, the correction is flagged for a
        human - an automatic clawback into a negative balance would trade an
        audit problem for an insolvency.
        """
        balances = self.balances() or {}
        delta = (payout - applied["yes_payout"]) * applied["settled_shares"]
        available = balances.get("available_usdc", 0.0)
        if delta < 0 and available + delta < -_EPS:
            self._flag_review(
                event,
                reason=(
                    f"correction would claw back {-delta:.4f} USDC but only "
                    f"{available:.4f} is available; the money has moved on and the "
                    "fix is not automatic"
                ),
            )
            return {
                "action": "REVIEW_REQUIRED",
                "market_id": event.market_id,
                "revision": event.revision,
                "detail": "correction would drive cash negative; flagged for manual review",
            }
        new_available = available + delta
        realised = balances.get("realized_pnl_usdc", 0.0) + delta
        self.db.execute(
            "UPDATE paper_balances SET available_usdc = ?, realized_pnl_usdc = ?, "
            "updated_at_utc = ? WHERE account_id = ?",
            [new_available, realised, now, ACCOUNT_ID],
        )
        ref_id = f"{event.market_id}#{event.revision}"
        self._insert_entry(
            kind="CORRECTION",
            ts_utc=now,
            amount=delta,
            balance_after=new_available,
            ref_type="settlement",
            ref_id=ref_id,
            detail={
                "market_id": event.market_id,
                "revision": event.revision,
                "old_payout": applied["yes_payout"],
                "old_revision": applied["revision"],
                "new_payout": payout,
                "settled_shares": applied["settled_shares"],
                "delta_usdc": delta,
            },
        )
        self._mark_settlement_applied(event, now=now)
        return {
            "action": "APPLIED",
            "market_id": event.market_id,
            "revision": event.revision,
            "detail": f"correction {applied['yes_payout']} -> {payout} on "
            f"{applied['settled_shares']} shares; compensated {delta:+.4f} USDC",
        }

    def review_required(self) -> list[dict[str, Any]]:
        """Settlement rows a human must look at, newest first."""
        rows = self.db.execute(
            "SELECT market_id, revision, status, review_reason, settled_at_utc "
            "FROM paper_settlements WHERE review_required ORDER BY settled_at_utc DESC"
        ).fetchall()
        return [
            {
                "market_id": str(r[0]),
                "revision": int(r[1]),
                "status": str(r[2]),
                "review_reason": r[3],
                "settled_at_utc": r[4],
            }
            for r in rows
        ]

    def _insert_settlement_row(self, event, *, now: datetime) -> bool:
        cursor = self.db.execute(
            "INSERT INTO paper_settlements (settlement_id, market_id, revision, status, "
            "yes_payout, settled_at_utc, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (market_id, revision) DO NOTHING RETURNING settlement_id",
            [
                settlement_id(market_id=event.market_id, revision=event.revision),
                event.market_id,
                event.revision,
                event.status,
                event.yes_payout,
                event.settled_at_utc,
                event.evidence_json(),
            ],
        )
        return cursor.fetchone() is not None

    def _applied_settlement(self, market_id: str) -> dict[str, Any] | None:
        """The newest APPLIED payout for a market, with the shares it paid on.

        The settled share count lives on the SETTLE_PAYOUT entry - the ledger
        is the authority for what actually happened, and a correction needs
        exactly those shares, not whatever the position holds now.
        """
        row = self.db.execute(
            "SELECT revision, yes_payout FROM paper_settlements "
            "WHERE market_id = ? AND applied ORDER BY revision DESC LIMIT 1",
            [market_id],
        ).fetchone()
        if row is None:
            return None
        entry = self.db.execute(
            "SELECT l.detail_json FROM paper_ledger l "
            "JOIN paper_settlements s ON s.market_id = ? "
            "AND l.ref_id = s.market_id || '#' || CAST(s.revision AS VARCHAR) "
            "WHERE l.kind = 'SETTLE_PAYOUT' AND l.ref_type = 'settlement' "
            "ORDER BY l.seq LIMIT 1",
            [market_id],
        ).fetchone()
        shares = 0.0
        if entry is not None:
            detail = json.loads(str(entry[0]))
            shares = sum(float(p["shares"]) for p in detail.get("positions", []))
        return {
            "revision": int(row[0]),
            "yes_payout": float(row[1]),
            "settled_shares": shares,
        }

    def _mark_settlement_applied(self, event, *, now: datetime) -> None:
        self.db.execute(
            "UPDATE paper_settlements SET applied = TRUE, applied_at_utc = ? WHERE settlement_id = ?",
            [now, settlement_id(market_id=event.market_id, revision=event.revision)],
        )

    def _flag_review(self, event, *, reason: str) -> None:
        self.db.execute(
            "UPDATE paper_settlements SET review_required = TRUE, review_reason = ? WHERE settlement_id = ?",
            [reason, settlement_id(market_id=event.market_id, revision=event.revision)],
        )

    def _insert_entry(
        self,
        *,
        kind: str,
        ts_utc: datetime,
        amount: float,
        balance_after: float,
        ref_type: str,
        ref_id: str,
        detail: dict[str, Any],
    ) -> None:
        # The sequence is the engine's actual order of movements: a batch
        # settles many fills in one instant, so they share one ts_utc, and a
        # content hash cannot reconstruct the order the money moved in -
        # only this counter can. Ordering the ledger by (ts, entry_id) once
        # shuffled such a batch and broke the running-balance audit chain.
        # (A wasted sequence on a conflicting insert is a gap, not a problem.)
        seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM paper_ledger").fetchone()[0]
        self.db.execute(
            "INSERT INTO paper_ledger (entry_id, seq, ts_utc, kind, currency, amount_usdc, "
            "balance_after_usdc, ref_type, ref_id, detail_json) "
            "VALUES (?, ?, ?, ?, 'USDC', ?, ?, ?, ?, ?) ON CONFLICT (entry_id) DO NOTHING",
            [
                ledger_entry_id(kind=kind, ref_id=ref_id),
                int(seq),
                ts_utc,
                kind,
                amount,
                balance_after,
                ref_type,
                ref_id,
                json.dumps(detail, sort_keys=True, default=str),
            ],
        )


def _same_position(a: LedgerPosition, b: LedgerPosition) -> bool:
    return (
        abs(a.shares - b.shares) <= 1e-6
        and abs(a.cost_basis_usdc - b.cost_basis_usdc) <= 1e-6
        and abs(a.avg_cost_usd_per_share - b.avg_cost_usd_per_share) <= 1e-6
    )

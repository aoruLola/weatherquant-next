"""Persistence for the paper engine: every write, in one place.

Two rules, inherited from the shadow's store because they are what make a
restart boring instead of interesting:

**Natural keys, so a re-delivery does nothing.** A signal is identified by
the shadow's own deterministic ``decision_id``; the account is identified by
its singleton id. Replaying a batch after a crash - or re-reading a part the
cursor half-advanced past - inserts nothing new. "The same signal must not
become a second trade" is not a check the consumer performs; it is a
property of the primary key.

**Cursor and effects commit together.** The consumer's read cursor is stored
in ``paper_state`` and advanced inside the same transaction as the signal
rows it describes. A crash between them either replays a batch (which the
natural keys make a no-op) or advances nothing; neither can lose or double a
trade.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from weatherquant.paper.events import SignalEvent
from weatherquant.storage.paper import PaperDatabase

__all__ = ["ACCOUNT_ID", "PaperStore", "quality_event_id"]


def quality_event_id(*, kind: str, path: str, line_no: int | None, detail: str) -> str:
    """Deterministic id for a quality event: replaying a batch inserts nothing."""
    basis = "|".join([kind, path, str(line_no if line_no is not None else ""), detail])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


#: The virtual account's singleton id. One engine, one account; anything
#: more is a portfolio, which is a different project.
ACCOUNT_ID = "paper-main"


class PaperStore:
    """Thin, explicit write layer over one open :class:`PaperDatabase`."""

    def __init__(self, db: PaperDatabase) -> None:
        self.db = db

    # -- account ------------------------------------------------------------

    def ensure_account(self, *, initial_capital_usdc: float, now: datetime) -> bool:
        """Open the account if it does not exist. Returns True if created.

        First write wins. An existing account with a *different* capital is
        refused rather than rebased: silently changing the number every
        historical figure was computed against is how a paper ledger stops
        meaning anything. Re-initialising with the same value is a no-op.
        """
        existing = self.account()
        if existing is not None:
            held = float(existing["initial_capital_usdc"])
            if abs(held - initial_capital_usdc) > 1e-9:
                raise ValueError(
                    f"account {ACCOUNT_ID} already exists with initial capital "
                    f"{held} USDC (refusing to rebase to {initial_capital_usdc}); "
                    "an account's opening capital is an immutable fact"
                )
            return False
        self.db.execute(
            "INSERT INTO paper_accounts (account_id, initial_capital_usdc, currency, created_at_utc) "
            "VALUES (?, ?, 'USDC', ?) ON CONFLICT (account_id) DO NOTHING",
            [ACCOUNT_ID, float(initial_capital_usdc), now],
        )
        return True

    def account(self) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT account_id, initial_capital_usdc, currency, created_at_utc "
            "FROM paper_accounts WHERE account_id = ?",
            [ACCOUNT_ID],
        ).fetchone()
        if row is None:
            return None
        return {
            "account_id": str(row[0]),
            "initial_capital_usdc": float(row[1]),
            "currency": str(row[2]),
            "created_at_utc": row[3],
        }

    # -- signals ------------------------------------------------------------

    def record_signal(
        self,
        event: SignalEvent,
        *,
        tradeable: bool,
        outcome: str,
        refusal_reason: str | None = None,
        refusal_detail: str | None = None,
        consumed_at_utc: datetime,
    ) -> bool:
        """Insert one consumed signal. Returns False if it was already here.

        The primary key is the dedup, and ``RETURNING`` is how the caller
        learns which happened: the same ``decision_id`` redelivered after a
        restart or a cursor rewind inserts nothing and yields False, so the
        consumer can freely re-run a batch it is unsure about.
        """
        cursor = self.db.execute(
            "INSERT INTO paper_signals ("
            "decision_id, run_id, cycle_id, market_id, event_id, token_id, bracket_label, "
            "direction, state, mode, decision_time_utc, net_edge, best_bid, best_ask, "
            "book_id, snapshot_id, venue_ts_utc, collector_received_at_utc, "
            "thresholds_version, fee_rate, fee_exponent, tradeable, outcome, "
            "refusal_reason, refusal_detail, consumed_at_utc, event_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (decision_id) DO NOTHING "
            "RETURNING decision_id",
            [
                event.decision_id,
                event.run_id,
                event.cycle_id,
                event.market_id,
                event.event_id,
                event.token_id,
                event.bracket_label,
                event.direction.value,
                event.state.value,
                event.mode.value,
                event.decision_time_utc,
                event.net_edge,
                event.best_bid,
                event.best_ask,
                event.book_id,
                event.snapshot_id,
                event.venue_ts_utc,
                event.collector_received_at_utc,
                event.thresholds_version,
                event.fee_rate,
                event.fee_exponent,
                bool(tradeable),
                str(outcome),
                refusal_reason,
                refusal_detail,
                consumed_at_utc,
                event.to_json(),
            ],
        )
        return cursor.fetchone() is not None

    def signal(self, decision_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT decision_id, outcome, refusal_reason, refusal_detail, consumed_at_utc, tradeable "
            "FROM paper_signals WHERE decision_id = ?",
            [decision_id],
        ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": str(row[0]),
            "outcome": str(row[1]),
            "refusal_reason": row[2],
            "refusal_detail": row[3],
            "consumed_at_utc": row[4],
            "tradeable": bool(row[5]),
        }

    def signal_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT outcome, COUNT(*) FROM paper_signals GROUP BY outcome ORDER BY outcome"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def refusal_counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT refusal_reason, COUNT(*) FROM paper_signals "
            "WHERE refusal_reason IS NOT NULL GROUP BY refusal_reason ORDER BY refusal_reason"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    # -- quality events -----------------------------------------------------

    def record_quality_event(
        self,
        *,
        kind: str,
        severity: str,
        detail: str,
        ts_utc: datetime,
        path: str | None = None,
        line_no: int | None = None,
    ) -> bool:
        """Insert one quality event. Returns False if it was already recorded.

        The id is a deterministic hash of the content, so replaying a batch
        after a crash re-inserts nothing - the same natural-key rule as the
        signals.
        """
        cursor = self.db.execute(
            "INSERT INTO paper_quality_events (event_id, ts_utc, kind, severity, detail, source, path, line_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING "
            "RETURNING event_id",
            [
                quality_event_id(kind=kind, path=path or "", line_no=line_no, detail=detail),
                ts_utc,
                kind,
                severity,
                detail,
                "paper-consumer",
                path,
                line_no,
            ],
        )
        return cursor.fetchone() is not None

    def quality_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT kind, COUNT(*) FROM paper_quality_events GROUP BY kind ORDER BY kind").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    # -- fills ---------------------------------------------------------------

    def signals_with_outcome(self, outcome: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT decision_id, market_id, token_id, direction, outcome, decision_time_utc "
            "FROM paper_signals WHERE outcome = ? ORDER BY consumed_at_utc, decision_time_utc, decision_id LIMIT ?",
            [outcome, int(limit)],
        ).fetchall()
        return [
            {
                "decision_id": str(row[0]),
                "market_id": str(row[1]),
                "token_id": row[2],
                "direction": str(row[3]),
                "outcome": str(row[4]),
                "decision_time_utc": row[5],
            }
            for row in rows
        ]

    def set_signal_outcome(
        self,
        decision_id: str,
        *,
        outcome: str,
        refusal_reason: str | None = None,
        refusal_detail: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE paper_signals SET outcome = ?, refusal_reason = ?, refusal_detail = ? WHERE decision_id = ?",
            [outcome, refusal_reason, refusal_detail, decision_id],
        )

    def event_of(self, decision_id: str):
        """Re-decode the signal's stored published row, verbatim."""
        row = self.db.execute("SELECT event_json FROM paper_signals WHERE decision_id = ?", [decision_id]).fetchone()
        if row is None:
            return None
        from weatherquant.paper.events import parse_signal_event

        try:
            return parse_signal_event(json.loads(str(row[0])))
        except (json.JSONDecodeError, ValueError):
            return None

    def record_fill(self, fill) -> bool:
        """Insert one fill. Returns False if this decision already has one."""
        cursor = self.db.execute(
            "INSERT INTO paper_fills ("
            "decision_id, market_id, token_id, direction, side, book_id, book_source, "
            "book_venue_ts_utc, book_collector_received_at_utc, book_age_ms, "
            "requested_shares, filled_shares, avg_price_usd_per_share, "
            "best_price_usd_per_share, slippage_usd_per_share, fill_cost_usd, fees_usd, "
            "execution_buffer_usd, total_cost_usd, net_proceeds_usd, levels_consumed, "
            "complete, consumed_levels_json, decision_time_utc, filled_at_utc, fee_rate, "
            "thresholds_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (decision_id) DO NOTHING RETURNING decision_id",
            [
                fill.decision_id,
                fill.market_id,
                fill.token_id,
                fill.direction,
                fill.side,
                fill.book_id,
                fill.book_source,
                fill.book_venue_ts_utc,
                fill.book_collector_received_at_utc,
                fill.book_age_ms,
                fill.requested_shares,
                fill.filled_shares,
                fill.avg_price_usd_per_share,
                fill.best_price_usd_per_share,
                fill.slippage_usd_per_share,
                fill.fill_cost_usd,
                fill.fees_usd,
                fill.execution_buffer_usd,
                fill.total_cost_usd,
                fill.net_proceeds_usd,
                fill.levels_consumed,
                bool(fill.complete),
                fill.consumed_levels_json(),
                fill.decision_time_utc,
                fill.filled_at_utc,
                fill.fee_rate,
                fill.thresholds_version,
            ],
        )
        return cursor.fetchone() is not None

    def fill_of(self, decision_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT decision_id, filled_shares, avg_price_usd_per_share, fees_usd, "
            "total_cost_usd, net_proceeds_usd, levels_consumed, complete, consumed_levels_json, "
            "slippage_usd_per_share, best_price_usd_per_share, execution_buffer_usd "
            "FROM paper_fills WHERE decision_id = ?",
            [decision_id],
        ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": str(row[0]),
            "filled_shares": float(row[1]),
            "avg_price_usd_per_share": float(row[2]),
            "fees_usd": float(row[3]),
            "total_cost_usd": row[4],
            "net_proceeds_usd": row[5],
            "levels_consumed": int(row[6]),
            "complete": bool(row[7]),
            "consumed_levels": json.loads(str(row[8])),
            "slippage_usd_per_share": float(row[9]),
            "best_price_usd_per_share": float(row[10]),
            "execution_buffer_usd": float(row[11]),
        }

    # -- cursor state -------------------------------------------------------

    def save_state(self, key: str, value: dict[str, Any], *, now: datetime) -> None:
        self.db.execute(
            "INSERT INTO paper_state (key, value_json, updated_at_utc) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at_utc = excluded.updated_at_utc",
            [key, json.dumps(value, sort_keys=True, default=str), now],
        )

    def load_state(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value_json FROM paper_state WHERE key = ?", [key]).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

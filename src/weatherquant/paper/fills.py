"""The simulated taker: fill an accepted signal against its own book.

The engine turns an ACCEPTED signal into either a recorded fill or a
recorded refusal, and the arithmetic is *entirely* delegated to
:mod:`weatherquant.execution.pricing` - the one place in the codebase that
may compute a fill, a fee or an edge (quantitative principle 8). Nothing
here walks a book twice or re-derives a fee.

What a fill may never do, each enforced before any arithmetic:

* **Fill against a book the decision did not use.** The resolved evidence's
  ``book_id`` must equal the decision's reference (same content hash over
  levels + venue timestamp), else ``BOOK_EVIDENCE_MISMATCH``.
* **Use future data.** The resolver only ever returns frames received at or
  before the decision instant; the engine additionally refuses a book whose
  venue timestamp is ahead of it.
* **Fill a stale book.** Age is judged on the venue's own clock; a book with
  no venue timestamp cannot be shown to be fresh and is refused.
* **Fake capacity.** A partial fill under ``require_complete_fill`` is a
  refusal, not a smaller trade (the shadow's own gate, for the same reason:
  the unfilled remainder would have to be repriced against a book we just
  drove through).

Evidence over convenience: ``consumed_levels`` records the price and taken
size of every level the plan consumed, so ``fill_cost_usd`` is recomputable
from the row alone. For a buy the cash figure is ``total_cost_usd`` (fill +
fees + buffer); for a sell it is ``net_proceeds_usd`` (fill - fees - buffer)
- two columns, never one whose sign flips by direction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from weatherquant.execution.pricing import (
    ExecutionRefused,
    FeeStructureUnknown,
    build_fill_plan,
    execution_cost,
)
from weatherquant.markets.models import Side
from weatherquant.paper.books import ArchiveBookSource, BookEvidence
from weatherquant.paper.events import RefusalReason
from weatherquant.paper.ledger import (
    InsufficientFunds,
    InsufficientInventory,
    Ledger,
    LedgerError,
)
from weatherquant.paper.risk import RiskGate
from weatherquant.paper.store import PaperStore

__all__ = ["FillEngine", "FillOutcome", "FillResult", "PaperFill"]

log = logging.getLogger("weatherquant.paper")

# Money comparisons carry a tolerance; the ledger's own checks are exact.
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class PaperFill:
    """One simulated taker fill, with every input that produced it."""

    decision_id: str
    market_id: str
    token_id: str
    direction: str  # buy_yes | sell_yes
    side: str  # buy | sell - the side of the book that was walked
    book_id: str
    book_source: str
    book_venue_ts_utc: datetime | None
    book_collector_received_at_utc: datetime
    book_age_ms: int
    requested_shares: float
    filled_shares: float
    avg_price_usd_per_share: float
    best_price_usd_per_share: float
    slippage_usd_per_share: float
    fill_cost_usd: float
    fees_usd: float
    execution_buffer_usd: float
    total_cost_usd: float | None
    net_proceeds_usd: float | None
    levels_consumed: int
    complete: bool
    consumed_levels: list[dict[str, float]]
    decision_time_utc: datetime
    filled_at_utc: datetime
    fee_rate: float
    thresholds_version: str | None

    def consumed_levels_json(self) -> str:
        return json.dumps(self.consumed_levels, separators=(",", ":"), sort_keys=True)


@dataclass
class FillOutcome:
    """What became of one signal's fill attempt."""

    decision_id: str
    filled: bool = False
    reason: str | None = None
    detail: str | None = None
    fill: PaperFill | None = None


@dataclass
class FillResult:
    """One bounded fill pass."""

    attempted: int = 0
    filled: int = 0
    refused: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    caught_up: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "filled": self.filled,
            "refused": self.refused,
            "by_reason": dict(self.by_reason),
            "caught_up": self.caught_up,
            "notes": list(self.notes),
        }


class FillEngine:
    """Price and record fills for accepted signals, one bounded batch at a time."""

    def __init__(
        self,
        store: PaperStore,
        *,
        book_source: ArchiveBookSource,
        buffer_fraction: float,
        require_complete_fill: bool = True,
        max_book_age_seconds: float = 90.0,
        clock: Any = None,
        batch_signals: int = 100,
        ledger: Ledger | None = None,
        risk: RiskGate | None = None,
    ) -> None:
        if buffer_fraction < 0.0:
            raise ValueError("buffer_fraction must be >= 0")
        if max_book_age_seconds <= 0:
            raise ValueError("max_book_age_seconds must be > 0")
        self.store = store
        self.books = book_source
        self.buffer_fraction = float(buffer_fraction)
        self.require_complete_fill = bool(require_complete_fill)
        self.max_book_age_seconds = float(max_book_age_seconds)
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.batch_signals = int(batch_signals)
        self.ledger = ledger if ledger is not None else Ledger(store)
        # Deliberately not optional-with-a-permissive-default: a fill engine
        # constructed without a gate must be told so explicitly, by a caller
        # that passes an explicit unrestricted gate.
        if risk is None:
            raise ValueError(
                "FillEngine needs a RiskGate; construct one from the versioned "
                "rules file - a fill engine without risk rules is a missing "
                "gate, not a permissive one"
            )
        self.risk = risk

    def fill_pending(self) -> FillResult:
        """Fill every ACCEPTED signal currently in the account, bounded.

        Signals are priced-and-settled **in order, one at a time**, inside one
        transaction per batch: the second signal's pricing sees the cash and
        inventory the first signal's settlement just moved, which is what
        makes a batch behave like the same sequence of trades would have in
        reality. The ledger movement, the fill row, the position and the
        outcome transition commit together; a ledger refusal (no funds, no
        inventory) converts the signal to a recorded refusal instead of
        writing half a trade.
        """
        result = FillResult()
        try:
            pending = self.store.signals_with_outcome("ACCEPTED", limit=self.batch_signals)
            if not pending:
                result.caught_up = True
                return result
            now = self.clock()
            with self.store.db.transaction():
                for row in pending:
                    result.attempted += 1
                    outcome = self._fill_one_isolated(row, now)
            with self.store.db.transaction():
                for row in pending:
                    result.attempted += 1
                    outcome = self._fill_one(row, now)
                    if outcome.filled and outcome.fill is not None:
                        settled, refusal = self._settle(outcome.fill, now)
                        if settled:
                            self.store.record_fill(outcome.fill)
                            self.store.set_signal_outcome(outcome.decision_id, outcome="FILLED")
                            result.filled += 1
                            continue
                        outcome.filled = False
                        outcome.reason, outcome.detail = refusal
                    self.store.set_signal_outcome(
                        outcome.decision_id,
                        outcome="REFUSED",
                        refusal_reason=outcome.reason,
                        refusal_detail=outcome.detail,
                    )
                    result.refused += 1
                    key = outcome.reason or "UNKNOWN"
                    result.by_reason[key] = result.by_reason.get(key, 0) + 1
        except Exception as exc:  # a side channel must not raise
            result.notes.append(f"fill pass failed: {type(exc).__name__}: {exc}")
        return result

    def _settle(self, fill: PaperFill, now: datetime) -> tuple[bool, tuple[str, str] | None]:
        """Move the cash and the position for a priced fill.

        Returns ``(True, None)`` when settled, or ``(False, (reason, detail))``
        when the ledger refused. The ledger's own methods raise rather than
        write half a movement, and the caller's transaction makes the
        refusal leave nothing behind.
        """
        is_buy = fill.side == "buy"
        try:
            if is_buy:
                applied = self.ledger.apply_buy(fill, now=now)
            else:
                applied = self.ledger.apply_sell(fill, now=now)
        except InsufficientFunds as exc:
            return False, (RefusalReason.INSUFFICIENT_FUNDS.value, str(exc))
        except InsufficientInventory as exc:
            return False, (RefusalReason.NO_INVENTORY.value, str(exc))
        except LedgerError as exc:
            return False, (RefusalReason.UNPRICABLE_EVENT.value, f"ledger refused: {exc}")
        if not applied:
            # Already settled by an earlier pass: nothing to do, the fill row
            # exists (or the decision was refused) and must not change.
            return False, (RefusalReason.UNPRICABLE_EVENT.value, "settle replayed; no state change")
        return True, None

    # -- one signal ---------------------------------------------------------

    def _fill_one_isolated(self, row: dict[str, Any], now: datetime) -> FillOutcome:
        """Price one signal with its own failure boundary.

        A bug or an exotic payload in one signal's pricing must cost that
        signal - a recorded refusal with the exception's name on it - not
        the whole batch. One crashing frame once stalled an entire fill pass
        with every signal stranded ACCEPTED; this boundary is the regression
        that cannot ship twice. Settlement keeps no such boundary: a failure
        while *moving money* aborts the batch loudly and retries whole.
        """
        outcome = FillOutcome(decision_id=str(row["decision_id"]))
        try:
            return self._fill_one(row, now)
        except Exception as exc:
            outcome.reason = RefusalReason.UNPRICABLE_EVENT.value
            outcome.detail = (
                f"pricing failed: {type(exc).__name__}: {exc}; the signal is "
                "recorded as refused rather than stalling the pass"
            )
            return outcome

    def _fill_one(self, row: dict[str, Any], now: datetime) -> FillOutcome:
        outcome = FillOutcome(decision_id=str(row["decision_id"]))
        event = self.store.event_of(str(row["decision_id"]))
        if event is None:
            outcome.reason = RefusalReason.UNPRICABLE_EVENT.value
            outcome.detail = "the signal's published event row is missing; nothing to price"
            return outcome

        if event.fee_rate is None:
            # An unknown schedule is refused, never priced as free - the same
            # rule the shadow's decision layer applies before us.
            outcome.reason = RefusalReason.UNPRICABLE_EVENT.value
            outcome.detail = "the market did not report a fee schedule; refusing to assume zero"
            return outcome
        if not event.token_id:
            outcome.reason = RefusalReason.NO_BOOK_EVIDENCE.value
            outcome.detail = "signal names no token; its book cannot be resolved"
            return outcome

        # The inventory gate needs no book: you cannot sell what you do not
        # hold, whatever the book says. It runs before pricing, which is the
        # earliest broken precondition.
        verdict = self.risk.before_fill(event)
        if not verdict.allowed:
            outcome.reason, outcome.detail = verdict.reason, verdict.detail
            return outcome

        evidence = self.books.resolve(token_id=event.token_id, decision_time=event.decision_time_utc)
        gate = self._gate_book(event, evidence)
        if gate is not None:
            outcome.reason, outcome.detail = gate
            return outcome
        assert evidence is not None  # narrowed by _gate_book

        # Size the opportunity to the account's own rules before pricing.
        shares, verdict = self.risk.size(event, evidence)
        if not verdict.allowed:
            outcome.reason, outcome.detail = verdict.reason, verdict.detail
            return outcome
        if shares <= _EPS:
            outcome.reason = RefusalReason.INSUFFICIENT_FUNDS.value
            outcome.detail = (
                f"the risk rules sized this opportunity to {shares} shares; nothing to trade"
            )
            return outcome

        side = Side.BUY if event.direction.value == "buy_yes" else Side.SELL
        try:
            plan = build_fill_plan(evidence.book, side=side, shares=shares)
            cost = execution_cost(plan, fee_rate=event.fee_rate, buffer_fraction=self.buffer_fraction)
        except ExecutionRefused as exc:
            if exc.code == "NO_LIQUIDITY" or exc.code == "INVALID_ORDER_SIZE":
                outcome.reason = RefusalReason.INSUFFICIENT_CAPACITY.value
            else:
                # A crossed or one-sided book is evidence failure, not a
                # thin market - the decision's own gates should have caught
                # it, so it is recorded as such here.
                outcome.reason = RefusalReason.NO_BOOK_EVIDENCE.value
            outcome.detail = f"{exc.code}: {exc.detail}"
            return outcome
        except FeeStructureUnknown as exc:
            outcome.reason = RefusalReason.UNPRICABLE_EVENT.value
            outcome.detail = str(exc)
            return outcome

        if self.require_complete_fill and not plan.complete:
            outcome.reason = RefusalReason.INSUFFICIENT_CAPACITY.value
            outcome.detail = (
                f"only {plan.filled_shares:.1f} of {plan.requested_shares:.1f} shares "
                f"fillable across {plan.levels_consumed} level(s); a partial fill is a "
                "different trade, so none is recorded"
            )
            return outcome

        best = (
            evidence.book.best_ask_usd_per_share
            if side is Side.BUY
            else evidence.book.best_bid_usd_per_share
        )
        avg = plan.avg_price_usd_per_share
        assert avg is not None and best is not None  # guaranteed for a priced plan
        is_buy = side is Side.BUY
        fill = PaperFill(
            decision_id=event.decision_id,
            market_id=event.market_id,
            token_id=event.token_id,
            direction=event.direction.value,
            side=side.value,
            book_id=evidence.book_id,
            book_source=evidence.book_source,
            book_venue_ts_utc=evidence.venue_ts_utc,
            book_collector_received_at_utc=evidence.collector_received_at_utc,
            book_age_ms=_book_age_ms(evidence, event.decision_time_utc),
            requested_shares=plan.requested_shares,
            filled_shares=plan.filled_shares,
            avg_price_usd_per_share=avg,
            best_price_usd_per_share=best,
            slippage_usd_per_share=max(avg - best, 0.0),
            fill_cost_usd=plan.fill_cost_usd,
            fees_usd=cost.fees_usd,
            execution_buffer_usd=cost.execution_buffer_usd,
            total_cost_usd=cost.total_cost_usd if is_buy else None,
            net_proceeds_usd=None if is_buy else cost.net_proceeds_usd,
            levels_consumed=plan.levels_consumed,
            complete=plan.complete,
            consumed_levels=_consumed_levels(evidence.book, side, plan),
            decision_time_utc=event.decision_time_utc,
            filled_at_utc=now,
            fee_rate=event.fee_rate,
            thresholds_version=event.thresholds_version,
        )
        # The caps apply to the *actual* outflow a real walk produced - a
        # deeper walk costs more than the best level suggested, and this is
        # the last place an over-budget trade can be stopped.
        verdict = self.risk.check_priced(event, fill)
        if not verdict.allowed:
            outcome.reason, outcome.detail = verdict.reason, verdict.detail
            return outcome
        outcome.filled = True
        outcome.fill = fill
        return outcome

    def _gate_book(
        self, event, evidence: BookEvidence | None
    ) -> tuple[str, str] | None:
        """Evidence gates, in causal order. ``None`` means the book may be used."""
        if evidence is None:
            return (
                RefusalReason.NO_BOOK_EVIDENCE.value,
                f"no archived L2 frame for token {event.token_id} at or before "
                f"{event.decision_time_utc.isoformat()}; nothing honest to fill against",
            )
        if event.book_id and evidence.book_id != event.book_id:
            return (
                RefusalReason.BOOK_EVIDENCE_MISMATCH.value,
                f"resolved book {evidence.book_id[:12]} is not the one the decision "
                f"references ({(event.book_id or '')[:12]}); the evidence does not "
                "describe this trade",
            )
        if evidence.venue_ts_utc is None:
            return (
                RefusalReason.BOOK_STALE.value,
                "the resolved book carries no venue timestamp, so its age cannot "
                "be established and it cannot be shown to be fresh",
            )
        if evidence.venue_ts_utc > event.decision_time_utc:
            return (
                RefusalReason.NO_BOOK_EVIDENCE.value,
                f"the resolved book's venue timestamp {evidence.venue_ts_utc.isoformat()} "
                "is after the decision instant; it is not evidence for this trade",
            )
        age_ms = _book_age_ms(evidence, event.decision_time_utc)
        if age_ms > self.max_book_age_seconds * 1000.0:
            return (
                RefusalReason.BOOK_STALE.value,
                f"book is {age_ms}ms old at decision time (limit "
                f"{self.max_book_age_seconds:.0f}s)",
            )
        return None


def _book_age_ms(evidence: BookEvidence, decision_time: datetime) -> int:
    assert evidence.venue_ts_utc is not None
    return int((decision_time - evidence.venue_ts_utc).total_seconds() * 1000)


def _consumed_levels(book, side: Side, plan) -> list[dict[str, float]]:
    """The price and taken size of each level the plan consumed.

    Reconstructed from the same book the plan walked: every level before the
    last is taken whole, the last one takes the remainder. The test suite
    asserts the reconstruction reproduces the plan's own cost exactly, so a
    drift between the two would fail loudly rather than audit wrongly.
    """
    levels = book.asks if side is Side.BUY else book.bids
    remaining = plan.filled_shares
    consumed: list[dict[str, float]] = []
    for level in levels[: plan.levels_consumed]:
        take = min(remaining, level.size_shares)
        consumed.append(
            {
                "price_usd_per_share": level.price_usd_per_share,
                "size_shares_taken": take,
            }
        )
        remaining -= take
    return consumed

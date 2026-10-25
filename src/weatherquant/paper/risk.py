"""The risk gates: what the account is willing to commit, and to where.

MVP rules, set a priori and versioned in ``configs/paper-risk.json`` - the
same discipline as the shadow's thresholds file, for the same reason: a
gate that can be widened by an environment variable on the box producing
the results cannot be audited afterwards, and a gate tuned on the outcomes
it will be judged against is a strategy being invented, not a limit being
set. **Nothing here is claimed optimal.**

The four rules:

* **Per-opportunity budget** (≤ 10% of initial capital): one fill's cash
  outflow never exceeds the cap. The shadow proposes a study size
  (``proposed_shares`` = 100, "shadow only, no capital exists"); the paper
  account is real money by the standard of its own ledger, so it *sizes the
  opportunity down* to what the budget affords - filling ``min(proposed,
  budget)``, never more than the proposal, never more than the cap.
* **Per-market exposure** (≤ 20% of initial capital, cumulative): all open
  positions in one market together. Cumulative is what makes "many small
  fills" the same object as "one big fill" - multiple episodes in a market
  climb one cap, not several.
* **No leverage**: a buy's outflow never exceeds available cash. Pre-checked
  here and re-enforced by the ledger's CHECK constraints - two locks on one
  door, because the ledger's is the one that cannot be forgotten.
* **No naked shorts**: a SELL YES without inventory is refused
  (``NO_INVENTORY``) before anything is priced. A sell is sized down to what
  is actually held; only an empty position is a refusal.

BUY NO is deliberately absent: pricing NO off the YES book (or a midpoint)
would fabricate a price nobody quoted. The shadow's vocabulary cannot yet
express a NO-side signal, so none can reach this engine; when it can, the
NO token's own book must be resolved and verified first, and a rule added
here. Refusing by omission is the conservative side of that gap.

Ordering: inventory is checked *before* pricing (you cannot sell what you
do not hold, whatever the book says); budget, exposure and cash are checked
*after* pricing, on the actual outflow a real book walk produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weatherquant.paper.books import BookEvidence
from weatherquant.paper.events import RefusalReason, SignalEvent
from weatherquant.paper.ledger import Ledger

__all__ = ["RiskGate", "RiskRules", "load_risk_rules"]

# Money comparisons carry a tolerance; the ledger's own checks are exact.
_EPS = 1e-9

#: Polymarket's taker fee per share is ``rate * p * (1 - p)``, bounded above
#: by ``rate * 0.25`` per share (the concave maximum at p=0.5) - in absolute
#: per-share terms, not as a fraction of price. Reserving that margin before
#: sizing keeps the first estimate on the safe side of the cap; the priced
#: fill is checked against the real number afterwards.
_FEE_MARGIN_PER_SHARE = 0.25

_REQUIRED_THRESHOLDS = (
    "max_opportunity_fraction",
    "max_market_exposure_fraction",
)


@dataclass(frozen=True, slots=True)
class RiskRules:
    """The caps in force, with the version they were agreed under."""

    version: str
    basis: str
    max_opportunity_fraction: float
    max_market_exposure_fraction: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "basis": self.basis,
            "max_opportunity_fraction": self.max_opportunity_fraction,
            "max_market_exposure_fraction": self.max_market_exposure_fraction,
        }


def load_risk_rules(path: str | Path) -> RiskRules:
    """Load the rules, refusing a file that is missing or silently incomplete.

    A missing key is an error rather than a default - the failure mode of a
    permissive default here is a cap that is off without anyone turning it
    off, indistinguishable in the results from a cap that never existed.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"paper risk rules file not found: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not payload.get("version"):
        raise ValueError(f"{target} has no version; a rule set without a version cannot be audited")
    entries = payload.get("thresholds") or {}
    missing = [key for key in _REQUIRED_THRESHOLDS if key not in entries]
    if missing:
        raise ValueError(f"{target} is missing risk rule(s): {missing}")
    rules = RiskRules(
        version=str(payload["version"]),
        basis=str(payload.get("basis", "")),
        max_opportunity_fraction=float(entries["max_opportunity_fraction"]["value"]),
        max_market_exposure_fraction=float(entries["max_market_exposure_fraction"]["value"]),
    )
    for name in ("max_opportunity_fraction", "max_market_exposure_fraction"):
        value = getattr(rules, name)
        if not 0.0 < value <= 1.0:
            raise ValueError(
                f"{target}: {name} must be in (0, 1], got {value}; a cap outside "
                "the unit interval is either a no-op or a total lock, and "
                "neither is what the file says"
            )
    return rules


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """A refusal, or the absence of one. ``reason`` is None when allowed."""

    reason: str | None = None
    detail: str | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None

    @classmethod
    def refuse(cls, reason: RefusalReason, detail: str) -> GateVerdict:
        return cls(reason=reason.value, detail=detail)


class RiskGate:
    """Size and vet one opportunity against the account's rules."""

    def __init__(
        self,
        ledger: Ledger,
        rules: RiskRules,
        *,
        initial_capital_usdc: float,
        min_order_shares: float = 5.0,
    ) -> None:
        if initial_capital_usdc <= 0:
            raise ValueError("initial_capital_usdc must be > 0")
        if min_order_shares < 0:
            raise ValueError("min_order_shares must be >= 0")
        self.ledger = ledger
        self.rules = rules
        self.initial_capital = float(initial_capital_usdc)
        # The venue's own minimum order size: a budget that sizes an
        # opportunity below this would simulate a fill the venue could never
        # have accepted, so it is refused rather than printed.
        self.min_order_shares = float(min_order_shares)

    # -- properties ----------------------------------------------------------

    @property
    def budget_usdc(self) -> float:
        return self.rules.max_opportunity_fraction * self.initial_capital

    @property
    def market_cap_usdc(self) -> float:
        return self.rules.max_market_exposure_fraction * self.initial_capital

    def market_exposure_usdc(self, market_id: str) -> float:
        """Cost basis currently occupied across ALL open positions in a market.

        Cumulative by construction: this is what makes repeated episodes in
        one market climb a single cap instead of a fresh one per fill.
        """
        return self.ledger.market_exposure(market_id)

    # -- before pricing --------------------------------------------------------

    def before_fill(self, event: SignalEvent) -> GateVerdict:
        """Gates that need no book: the sell side's inventory.

        A SELL YES with nothing held is refused here, before any pricing -
        the earliest broken precondition, and the one that makes "no naked
        shorts" true by construction rather than by ledger catch.
        """
        if event.direction.value == "sell_yes":
            held = self.ledger.held_shares(event.market_id, event.token_id)
            if held <= _EPS:
                return GateVerdict.refuse(
                    RefusalReason.NO_INVENTORY,
                    f"SELL YES refused: the account holds no {event.token_id} shares; "
                    "no naked shorts",
                )
        return GateVerdict()

    # -- sizing ----------------------------------------------------------------

    def size(self, event: SignalEvent, evidence: BookEvidence | None) -> tuple[float, GateVerdict]:
        """How many shares this account may trade of the proposal.

        A buy is sized to the *tightest* of: the proposal itself, the
        per-opportunity budget, the market's remaining exposure headroom, and
        available cash - each converted at the best price the book quotes,
        with the fee margin reserved up front. A sell is sized to what is
        actually held. The priced fill is checked against the caps again in
        :meth:`check_priced`, because a multi-level walk costs more than the
        best level suggests.
        """
        if not event.proposed_shares or event.proposed_shares <= 0.0:
            return 0.0, GateVerdict.refuse(
                RefusalReason.UNPRICABLE_EVENT,
                f"signal carries no order size to fill (proposed_shares={event.proposed_shares!r})",
            )
        if event.direction.value == "sell_yes":
            held = self.ledger.held_shares(event.market_id, event.token_id)
            shares = min(event.proposed_shares, held)
            if shares < self.min_order_shares:
                return 0.0, GateVerdict.refuse(
                    RefusalReason.NO_INVENTORY,
                    f"holding {held:g} shares sizes this sell to {shares:.2f}, below the "
                    f"venue's minimum order of {self.min_order_shares:g} shares",
                )
            return shares, GateVerdict()

        if evidence is None or evidence.book.best_ask_usd_per_share is None:
            return 0.0, GateVerdict()  # the book gate reports the real reason
        price = evidence.book.best_ask_usd_per_share
        fee_rate = event.fee_rate or 0.0
        cost_per_share = price + fee_rate * _FEE_MARGIN_PER_SHARE

        available = (self.ledger.balances() or {}).get("available_usdc") or 0.0
        headroom = self.market_cap_usdc - self.market_exposure_usdc(event.market_id)
        cap_usdc = min(self.budget_usdc, headroom, available)
        if cap_usdc <= _EPS:
            return 0.0, GateVerdict.refuse(
                RefusalReason.RISK_BUDGET_EXCEEDED if headroom <= _EPS else RefusalReason.INSUFFICIENT_FUNDS,
                (
                    f"no budget for {event.market_id}: market exposure "
                    f"{self.market_exposure_usdc(event.market_id):.4f} USDC against a "
                    f"{self.market_cap_usdc:.2f} USDC cap (rules {self.rules.version})"
                    if headroom <= _EPS
                    else f"only {available:.4f} USDC available, no budget left "
                    f"(rules {self.rules.version})"
                ),
            )
        affordable = cap_usdc / cost_per_share
        shares = min(event.proposed_shares, affordable)
        if shares < self.min_order_shares:
            return 0.0, GateVerdict.refuse(
                RefusalReason.RISK_BUDGET_EXCEEDED,
                f"the budget sizes this opportunity to {shares:.2f} shares, below the "
                f"venue's minimum order of {self.min_order_shares:g} shares; a "
                "sub-minimum fill is one the venue could never have accepted "
                f"(rules {self.rules.version})",
            )
        return shares, GateVerdict()

    # -- after pricing -----------------------------------------------------------

    def check_priced(self, event: SignalEvent, fill) -> GateVerdict:
        """Caps on the *actual* outflow a real book walk produced.

        Runs before settlement; a fill that slipped past sizing (deeper
        levels cost more than the best quote) is refused here rather than
        charged over budget.
        """
        if fill.side != "buy":
            return GateVerdict()
        cash_out = fill.fill_cost_usd + fill.fees_usd
        if cash_out > self.budget_usdc + _EPS:
            return GateVerdict.refuse(
                RefusalReason.RISK_BUDGET_EXCEEDED,
                f"opportunity would commit {cash_out:.4f} USDC against a "
                f"{self.budget_usdc:.2f} USDC cap "
                f"({self.rules.max_opportunity_fraction:.0%} of {self.initial_capital:.0f} "
                f"USDC, rules {self.rules.version})",
            )
        exposure = self.market_exposure_usdc(event.market_id)
        if exposure + cash_out > self.market_cap_usdc + _EPS:
            return GateVerdict.refuse(
                RefusalReason.RISK_EXPOSURE_EXCEEDED,
                f"{event.market_id} exposure would reach {exposure + cash_out:.4f} USDC "
                f"against a {self.market_cap_usdc:.2f} USDC cap "
                f"({self.rules.max_market_exposure_fraction:.0%} of "
                f"{self.initial_capital:.0f} USDC, rules {self.rules.version})",
            )
        available = (self.ledger.balances() or {}).get("available_usdc") or 0.0
        if cash_out > available + _EPS:
            return GateVerdict.refuse(
                RefusalReason.INSUFFICIENT_FUNDS,
                f"buy needs {cash_out:.4f} USDC but only {available:.4f} is available; "
                "no leverage",
            )
        return GateVerdict()

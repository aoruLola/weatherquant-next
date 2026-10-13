"""Reference capital: what a hypothetical stake would have done.

Two rules govern everything here, both from the contract:

1. **Every fill goes through ``execution/pricing.py``.** Multiplying a
   displayed price by a share count is not a fill - it ignores how much
   size actually sits at that level, what the fee is, and what buffer the
   risk model requires. So a book with no levels is *not* a cheap fill; it
   is an unknown one, and it is reported as such.

2. **Theoretical and realised are different words.** Everything this module
   computes is a model expectation under the model's own probabilities. The
   one number that would make it sound like a return - realised profit - is
   deliberately absent until a settlement exists.

The book is **injected**, never owned. Historical level arrays are not kept
in the shadow database: ``market_snapshots`` records best bid/ask and depth,
while ``token_books`` holds only the newest book for a token. So the caller
decides where levels come from - the current book for a live episode, the
collector's raw archive for a historical one - and returns ``None`` when it
cannot find them. This module then says ``NOT_EXECUTABLE`` rather than
inventing the levels it is missing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from weatherquant.execution.pricing import (
    ExecutionRefused,
    FeeStructureUnknown,
    build_fill_plan,
    edge_usd_per_share,
    edge_usd_per_share_sell,
    execution_cost,
    expected_net_profit_usd,
    expected_net_profit_usd_sell,
)
from weatherquant.markets.models import OrderBook, Side
from weatherquant.performance.episodes import Episode

__all__ = [
    "DEFAULT_MAX_ALLOCATION_FRACTION",
    "NotExecutable",
    "PortfolioResult",
    "SingleResult",
    "simulate_portfolio",
    "simulate_single",
]

#: A research assumption, not a validated risk rule. The contract says so
#: and so does every response that uses it.
DEFAULT_MAX_ALLOCATION_FRACTION = 0.10


class NotExecutable:
    """Stable reason codes for why an opportunity cannot be sized.

    Each one is a fact about the evidence, not a failure. A caller that
    shows a theoretical number next to one of these must label it
    ``NOT_EXECUTABLE`` - that is the whole point of keeping the codes
    separate instead of returning a single "false".
    """

    NO_BOOK = "NO_BOOK"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    BELOW_MIN_ORDER = "BELOW_MIN_ORDER"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    DEPTH_NOT_MEASURED = "DEPTH_NOT_MEASURED"
    TIME_MISMATCH = "TIME_MISMATCH"
    NO_SETTLEMENT = "NO_SETTLEMENT"
    CROSSED_OR_ONE_SIDED = "CROSSED_OR_ONE_SIDED"
    NOT_PRICED = "NOT_PRICED"
    FEE_UNKNOWN = "FEE_UNKNOWN"


#: How stale a snapshot may be and still describe a fillable market. Matches
#: the engine's own book-staleness judgement so the two agree on "fresh".
DEFAULT_STALE_SECONDS = 900.0


def _side_for(direction: str | None) -> Side | None:
    if direction == "buy_yes":
        return Side.BUY
    if direction == "sell_yes":
        return Side.SELL
    return None


@dataclass(slots=True)
class SingleResult:
    """One episode, sized against a reference stake."""

    episode_id: str
    executable: bool
    not_executable_reason: str | None = None
    capital_usdc: float = 0.0
    reference_shares: float = 0.0
    capital_required_usdc: float = 0.0
    capital_used_usdc: float = 0.0
    theoretical_edge_per_share: float | None = None
    theoretical_expected_net_edge_usdc: float | None = None
    theoretical_expected_roi: float | None = None
    win_net_usdc: float | None = None
    loss_net_usdc: float | None = None
    available_depth_usdc: float | None = None
    capacity_limited: bool = False
    price_time_matched: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "executable": self.executable,
            "not_executable_reason": self.not_executable_reason,
            "capital_usdc": self.capital_usdc,
            "reference_shares": self.reference_shares,
            "capital_required_usdc": self.capital_required_usdc,
            "capital_used_usdc": self.capital_used_usdc,
            "theoretical_edge_per_share": self.theoretical_edge_per_share,
            "theoretical_expected_net_edge_usdc": self.theoretical_expected_net_edge_usdc,
            "theoretical_expected_roi": self.theoretical_expected_roi,
            "win_net_usdc": self.win_net_usdc,
            "loss_net_usdc": self.loss_net_usdc,
            "available_depth_usdc": self.available_depth_usdc,
            "capacity_limited": self.capacity_limited,
            "price_time_matched": self.price_time_matched,
            "notes": list(self.notes),
        }


def _max_shares_for(book: OrderBook, side: Side, capital: float) -> float:
    """How many shares the stake could take, walking real levels.

    Using the book rather than ``capital / best_price`` is the difference
    between knowing what depth exists and assuming it: the best level may
    hold a fraction of what the stake wants, and the answer changes as soon
    as the walk moves to a worse price.
    """
    levels = book.asks if side is Side.BUY else book.bids
    remaining = capital
    shares = 0.0
    for level in levels:
        if remaining <= 0:
            break
        level_notional = level.price_usd_per_share * level.size_shares
        take = min(level_notional, remaining)
        shares += take / level.price_usd_per_share
        remaining -= take
    return shares


def simulate_single(
    episode: Episode,
    *,
    capital_usdc: float,
    book: OrderBook | None,
    fee_rate: float | None = None,
    buffer_fraction: float = 0.0,
    min_order_size: float | None = None,
    book_age_seconds: float | None = None,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    settlement_payout: float | None = None,
) -> SingleResult:
    """Size one opportunity against ``capital_usdc``.

    ``settlement_payout`` is 1.0 or 0.0 when the market has settled, and
    ``None`` otherwise. With ``None`` the win/loss figures stay ``None``
    too: an unsettled market has no realised outcome, and filling one in
    would be the fabrication this whole package exists to prevent.
    """
    if capital_usdc <= 0:
        raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")

    result = SingleResult(episode_id=episode.episode_id, executable=False, capital_usdc=capital_usdc)
    result.available_depth_usdc = episode.first_available_depth_usd
    result.theoretical_edge_per_share = episode.first_edge
    result.notes.append(
        "theoretical expectation under the model's own probabilities; not realised profit"
    )

    if episode.first_edge is None:
        result.not_executable_reason = NotExecutable.NOT_PRICED
        return result

    side = _side_for(episode.direction)
    if side is None:
        result.not_executable_reason = NotExecutable.NOT_PRICED
        return result

    if book is None:
        # No levels: we know a best price and a depth figure, but not how the
        # size walks. Multiplying price by shares would be exactly the
        # shortcut the contract forbids.
        result.not_executable_reason = (
            NotExecutable.DEPTH_NOT_MEASURED
            if episode.first_available_depth_usd is None
            else NotExecutable.NO_BOOK
        )
        return result

    defects = book.defects()
    if defects:
        result.not_executable_reason = NotExecutable.CROSSED_OR_ONE_SIDED
        result.notes.append(f"book defects: {', '.join(defects)}")
        return result

    if book_age_seconds is not None and book_age_seconds > stale_seconds:
        result.not_executable_reason = NotExecutable.STALE_SNAPSHOT
        result.notes.append(f"book age {book_age_seconds:.0f}s exceeds {stale_seconds:.0f}s")
        return result

    price_at_decision = episode.first_market_price
    price_in_book = book.best_bid_usd_per_share if side is Side.SELL else book.best_ask_usd_per_share
    if price_at_decision is not None and price_in_book is not None:
        # A decision priced against one instant and a fill priced against
        # another are two different trades. Without evidence they match, the
        # two figures must not be combined into one result.
        result.price_time_matched = abs(price_at_decision - price_in_book) < 1e-9
        if not result.price_time_matched:
            result.notes.append(
                "decision price and current book price differ; sized against the book"
            )

    try:
        wanted = _max_shares_for(book, side, capital_usdc)
        if wanted <= 0:
            result.not_executable_reason = NotExecutable.INSUFFICIENT_DEPTH
            return result
        fill = build_fill_plan(book, side=side, shares=wanted)
    except ExecutionRefused as exc:
        result.not_executable_reason = NotExecutable.CROSSED_OR_ONE_SIDED
        result.notes.append(str(getattr(exc, "detail", None) or exc))
        return result

    if min_order_size is not None and fill.filled_shares < min_order_size:
        result.not_executable_reason = NotExecutable.BELOW_MIN_ORDER
        result.notes.append(
            f"fillable {fill.filled_shares:g} shares is below the market minimum {min_order_size:g}"
        )
        return result

    try:
        cost = execution_cost(fill, fee_rate=fee_rate, buffer_fraction=buffer_fraction)
    except FeeStructureUnknown as exc:
        # The pricing module raises rather than assuming a fee of zero: an
        # unknown fee silently flatters every edge computed from it. Surface
        # it by name instead of folding it into a generic "not priced".
        result.not_executable_reason = NotExecutable.FEE_UNKNOWN
        result.notes.append(str(exc))
        return result
    except Exception as exc:  # any other pricing refusal
        result.not_executable_reason = NotExecutable.NOT_PRICED
        result.notes.append(f"cost unavailable: {exc}")
        return result

    if cost.fill_cost_usd <= 0:
        result.not_executable_reason = NotExecutable.INSUFFICIENT_DEPTH
        return result

    q = episode.first_model_probability
    if q is None:
        result.not_executable_reason = NotExecutable.NOT_PRICED
        result.notes.append("model probability missing at the decision instant")
        return result

    result.reference_shares = fill.filled_shares
    result.capital_required_usdc = cost.fill_cost_usd
    result.capital_used_usdc = min(capital_usdc, cost.fill_cost_usd)
    result.capacity_limited = cost.fill_cost_usd < capital_usdc

    if side is Side.BUY:
        per_share = edge_usd_per_share(q=q, filled_shares=fill.filled_shares, cost=cost)
        total = expected_net_profit_usd(q=q, filled_shares=fill.filled_shares, cost=cost)
        result.win_net_usdc = fill.filled_shares * 1.0 - cost.fill_cost_usd if settlement_payout == 1.0 else None
        result.loss_net_usdc = -cost.fill_cost_usd if settlement_payout == 0.0 else None
    else:
        per_share = edge_usd_per_share_sell(q=q, filled_shares=fill.filled_shares, cost=cost)
        total = expected_net_profit_usd_sell(q=q, filled_shares=fill.filled_shares, cost=cost)
        # SELL YES economics are unproven (contract section 4.4). A payout
        # may not be turned into a P&L for a direction whose position
        # semantics have not been established, so both stay None.
        result.notes.append(
            "SELL YES position semantics are unproven; win/loss P&L withheld"
        )

    result.theoretical_edge_per_share = per_share
    result.theoretical_expected_net_edge_usdc = total
    result.theoretical_expected_roi = total / cost.fill_cost_usd if cost.fill_cost_usd else None
    result.executable = True
    return result


@dataclass(slots=True)
class PortfolioResult:
    """One stake shared across every opportunity, not one per opportunity."""

    capital_usdc: float
    max_allocation_fraction: float
    results: list[SingleResult] = field(default_factory=list)
    allocated_total_usdc: float = 0.0
    peak_open_allocation_usdc: float = 0.0
    balance_remaining_usdc: float = 0.0
    capacity_limited_count: int = 0
    executable_count: int = 0
    not_executable_count: int = 0
    backtestable: bool = True
    not_backtestable_reasons: list[str] = field(default_factory=list)

    @property
    def utilization(self) -> float:
        if self.capital_usdc <= 0:
            return 0.0
        return self.peak_open_allocation_usdc / self.capital_usdc

    @property
    def theoretical_expected_net_edge_usdc(self) -> float | None:
        values = [r.theoretical_expected_net_edge_usdc for r in self.results if r.executable]
        if not values:
            return None
        return sum(v for v in values if v is not None)

    def as_dict(self) -> dict[str, object]:
        return {
            "capital_usdc": self.capital_usdc,
            "max_allocation_fraction": self.max_allocation_fraction,
            "allocated_total_usdc": self.allocated_total_usdc,
            "peak_open_allocation_usdc": self.peak_open_allocation_usdc,
            "balance_remaining_usdc": self.balance_remaining_usdc,
            "utilization": self.utilization,
            "capacity_limited_count": self.capacity_limited_count,
            "executable_count": self.executable_count,
            "not_executable_count": self.not_executable_count,
            "theoretical_expected_net_edge_usdc": self.theoretical_expected_net_edge_usdc,
            "theoretical_expected_roi": (
                self.theoretical_expected_net_edge_usdc / self.peak_open_allocation_usdc
                if self.peak_open_allocation_usdc > 0 and self.theoretical_expected_net_edge_usdc is not None
                else None
            ),
            "backtestable": self.backtestable,
            "not_backtestable_reasons": list(self.not_backtestable_reasons),
            "statistical_scope": ["reference_capital_hypothetical"],
            "warnings": [
                "Reference capital is a research assumption. It is not a funded account, "
                "not a virtual position and not realised profit.",
                "max_allocation_fraction is a stated assumption, not a validated risk rule.",
                "Actual profit is N/A: this system places no orders.",
            ],
            "results": [r.as_dict() for r in self.results],
        }


def simulate_portfolio(
    episodes: Sequence[Episode],
    *,
    capital_usdc: float,
    book_for: Callable[[Episode], OrderBook | None],
    max_allocation_fraction: float = DEFAULT_MAX_ALLOCATION_FRACTION,
    **single_kwargs: object,
) -> PortfolioResult:
    """Share one stake across episodes in the order time actually gave them.

    Sorted by ``first_seen_utc`` rather than by edge: ranking opportunities
    by the same number we are trying to evaluate would let the best-looking
    outcome claim the capital first, which is hindsight dressed as a rule.

    Capital is released only when an episode is settled. An unresolved
    episode keeps its allocation for the whole window, because returning
    money to the pool early is exactly the assumption that makes a
    backtest look more liquid than the market ever was.
    """
    if capital_usdc <= 0:
        raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")
    if not 0 < max_allocation_fraction <= 1:
        raise ValueError(
            f"max_allocation_fraction must be in (0, 1], got {max_allocation_fraction!r}"
        )

    outcome = PortfolioResult(
        capital_usdc=capital_usdc, max_allocation_fraction=max_allocation_fraction
    )
    balance = capital_usdc
    per_opportunity_cap = capital_usdc * max_allocation_fraction

    for episode in sorted(episodes, key=lambda e: e.first_seen_utc):
        single = simulate_single(
            episode,
            capital_usdc=per_opportunity_cap,
            book=book_for(episode),
            **single_kwargs,  # type: ignore[arg-type]
        )
        outcome.results.append(single)

        if not single.executable:
            outcome.not_executable_count += 1
            continue

        outcome.executable_count += 1
        if single.capacity_limited:
            outcome.capacity_limited_count += 1

        needed = min(single.capital_required_usdc, per_opportunity_cap)
        if needed > balance:
            single.notes.append("allocation capped by remaining balance")
            needed = balance
        if needed <= 0:
            outcome.not_backtestable_reasons.append("capital exhausted before all opportunities")
            continue

        single.capital_used_usdc = needed
        outcome.allocated_total_usdc += needed
        balance -= needed
        outcome.peak_open_allocation_usdc = max(
            outcome.peak_open_allocation_usdc, outcome.allocated_total_usdc
        )

    outcome.balance_remaining_usdc = balance

    if outcome.executable_count == 0:
        outcome.backtestable = False
        outcome.not_backtestable_reasons.append(
            "no episode could be sized from retained book levels"
        )
    if outcome.results and outcome.not_executable_count == len(outcome.results):
        outcome.backtestable = False
        outcome.not_backtestable_reasons.append(
            "every opportunity lacked the evidence a fill needs"
        )
    # Capital is never released inside this pass. Settlement times are not
    # carried on an episode, so returning money to the pool would mean
    # assuming when a market resolved. Holding every allocation to the end
    # of the window is the conservative reading of "capital is released only
    # on settlement" - it can only understate utilization, never overstate it.
    if outcome.executable_count and not outcome.not_backtestable_reasons:
        outcome.not_backtestable_reasons.append(
            "capital is held for the whole window; settlement-time release is not modelled"
        )
    return outcome

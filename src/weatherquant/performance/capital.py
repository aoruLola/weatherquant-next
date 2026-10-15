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
from datetime import UTC, datetime

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
from weatherquant.markets.models import BookLevel, OrderBook, Side
from weatherquant.performance.episodes import Episode

__all__ = [
    "DEFAULT_MAX_ALLOCATION_FRACTION",
    "IndependentResult",
    "NotExecutable",
    "PortfolioResult",
    "PortfolioWalk",
    "SingleResult",
    "book_from_mapping",
    "simulate_independent",
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
    #: The portfolio had nothing left to allocate. Distinct from a book that
    #: is too thin: there, the market refused; here, we never got to ask.
    NO_FUNDS = "NO_FUNDS"


#: How stale a snapshot may be and still describe a fillable market. Matches
#: the engine's own book-staleness judgement so the two agree on "fresh".
DEFAULT_STALE_SECONDS = 900.0


def _side_for(direction: str | None) -> Side | None:
    if direction == "buy_yes":
        return Side.BUY
    if direction == "sell_yes":
        return Side.SELL
    return None


def book_from_mapping(
    payload: object,
    *,
    market_id: str,
    token_id: str,
    venue_ts=None,
    fetched_at_utc=None,
) -> OrderBook | None:
    """Rebuild a book from the levels a snapshot stored, or None.

    The snapshot carries ``[[price, size], …]`` pairs only when the research
    process actually had the levels. A missing or malformed ``book`` key
    returns ``None``, which the simulator turns into ``NOT_EXECUTABLE`` -
    the whole point of not inventing depth applies to reconstructing it too.
    """
    if not isinstance(payload, dict):
        return None
    try:
        bids = tuple(
            BookLevel(price_usd_per_share=float(p), size_shares=float(s))
            for p, s in (payload.get("bids") or [])
        )
        asks = tuple(
            BookLevel(price_usd_per_share=float(p), size_shares=float(s))
            for p, s in (payload.get("asks") or [])
        )
    except (TypeError, ValueError):
        return None
    if not bids and not asks:
        return None
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        bids=bids,
        asks=asks,
        venue_ts=venue_ts,
        fetched_at_utc=fetched_at_utc or datetime.now(tz=UTC),
    )


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


def _max_shares_for(book: OrderBook, side: Side, capital: float) -> tuple[float, bool]:
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
    # Decided after the walk, not inside it: a single-level book never gets
    # a second iteration in which to notice that the stake ran out first, so
    # asking the question mid-loop reports the shallowest book as exhausted.
    exhausted = bool(levels) and remaining > 0
    return shares, exhausted


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

    # The market's own fee travels with the episode. An explicit argument
    # still wins, but defaulting to None when the episode carries a rate
    # would refuse every sizing for a reason the data had already answered.
    if fee_rate is None:
        fee_rate = episode.fee_rate

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
        # another are two different trades. Continuing anyway would pair the
        # episode's model probability with an execution price it never saw,
        # which is precisely how a historical episode acquires a return it
        # could not have had. The contract names this TIME_MISMATCH.
        result.price_time_matched = abs(price_at_decision - price_in_book) < 1e-9
        if not result.price_time_matched:
            result.not_executable_reason = NotExecutable.TIME_MISMATCH
            result.notes.append(
                f"decision price {price_at_decision:g} and book price "
                f"{price_in_book:g} are from different instants; refusing to "
                "combine a probability from one with a fill from the other"
            )
            return result

    try:
        wanted, book_exhausted = _max_shares_for(book, side, capital_usdc)
        if wanted <= 0:
            result.not_executable_reason = NotExecutable.INSUFFICIENT_DEPTH
            return result
        # Size within the *all-in* budget. `_max_shares_for` walks notional,
        # which is the fill only - fees and the buffer sit on top of it - so
        # the first pass can ask for more than the stake can pay for. Shrink
        # and re-walk until the total fits. Converges in one or two rounds
        # because each pass scales by the shortfall it just measured.
        fill = build_fill_plan(book, side=side, shares=wanted)
        cost = execution_cost(fill, fee_rate=fee_rate, buffer_fraction=buffer_fraction)
        for _ in range(3):
            if cost.total_cost_usd <= capital_usdc or fill.filled_shares <= 0:
                break
            scale = (capital_usdc / cost.total_cost_usd) * 0.999
            shrunk = fill.filled_shares * scale
            if shrunk <= 0:
                break
            fill = build_fill_plan(book, side=side, shares=shrunk)
            cost = execution_cost(fill, fee_rate=fee_rate, buffer_fraction=buffer_fraction)
    except FeeStructureUnknown as exc:
        # Raised by the very first costing pass inside the loop above, so it
        # has to be caught here too - not only at the second call, or an
        # unknown fee escapes as a traceback instead of a refusal.
        result.not_executable_reason = NotExecutable.FEE_UNKNOWN
        result.notes.append(str(exc))
        return result
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

    if cost.total_cost_usd > capital_usdc:
        # Still over budget after shrinking: the position cannot be paid for
        # with this stake. Reporting it as executable would book a trade the
        # money does not cover.
        result.not_executable_reason = NotExecutable.INSUFFICIENT_DEPTH
        result.notes.append(
            f"all-in cost {cost.total_cost_usd:.4f} (fill {cost.fill_cost_usd:.4f} "
            f"+ fees {cost.fees_usd:.4f} + buffer {cost.execution_buffer_usd:.4f}) "
            f"exceeds the {capital_usdc:.4f} stake"
        )
        return result

    if cost.total_cost_usd <= 0:
        result.not_executable_reason = NotExecutable.INSUFFICIENT_DEPTH
        return result

    q = episode.first_model_probability
    if q is None:
        result.not_executable_reason = NotExecutable.NOT_PRICED
        result.notes.append("model probability missing at the decision instant")
        return result

    result.reference_shares = fill.filled_shares
    # All-in, not the fill notional: fees and the buffer are cash the stake
    # has to produce too. Recording only the fill would book a trade that
    # costs more than the capital it was sized against.
    result.capital_required_usdc = cost.total_cost_usd
    result.capital_used_usdc = min(capital_usdc, cost.total_cost_usd)
    # Limited by the *book*, not by our own shrink-to-fit. "The market would
    # not give me this much" and "I asked for less than I could afford" are
    # different facts, and only the first is a capacity limit.
    result.capacity_limited = book_exhausted

    if side is Side.BUY:
        per_share = edge_usd_per_share(q=q, filled_shares=fill.filled_shares, cost=cost)
        total = expected_net_profit_usd(q=q, filled_shares=fill.filled_shares, cost=cost)
        # Against the all-in cost: winning pays a share and recovering the
        # outlay, losing loses everything that was laid out - fees included.
        result.win_net_usdc = (
            fill.filled_shares * 1.0 - cost.total_cost_usd if settlement_payout == 1.0 else None
        )
        result.loss_net_usdc = -cost.total_cost_usd if settlement_payout == 0.0 else None
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
    result.theoretical_expected_roi = total / cost.total_cost_usd
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
    #: Running total of the executable results' expected net edges, folded as
    #: each result is recorded. Kept here rather than recomputed from
    #: ``results`` so a walk that never materialises them still reports the
    #: same number - and the same rounding, since the fold order is the
    #: episode order either way.
    expected_net_edge_total: float = 0

    @property
    def utilization(self) -> float:
        if self.capital_usdc <= 0:
            return 0.0
        return self.peak_open_allocation_usdc / self.capital_usdc

    @property
    def theoretical_expected_net_edge_usdc(self) -> float | None:
        if self.results:
            values = [r.theoretical_expected_net_edge_usdc for r in self.results if r.executable]
            if not values:
                return None
            return sum(v for v in values if v is not None)
        if self.executable_count == 0:
            return None
        return self.expected_net_edge_total

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


def simulate_independent(
    episodes: Sequence[Episode],
    *,
    capital_usdc: float,
    book_for: Callable[[Episode], OrderBook | None],
    **single_kwargs: object,
) -> IndependentResult:
    """Size every episode against the full stake, as if it stood alone.

    This is the answer to "what if I put 100 USDC on *this* opportunity" -
    asked once per episode. Nothing is shared: the first episode's fill
    does not reduce what the second one may use, because they are separate
    hypotheticals rather than positions in one account.

    Which is exactly why their totals must not be presented as one account.
    The sum of ten independent "if I had staked 100 here" answers is not
    "I would have had 1000" - it is ten separate questions, each assumed to
    have its own stake. ``IndependentResult.as_dict()`` says so on every
    payload instead of letting a total imply a balance that never existed.
    """
    if capital_usdc <= 0:
        raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")

    results: list[SingleResult] = []
    for episode in episodes:
        results.append(
            simulate_single(
                episode,
                capital_usdc=capital_usdc,
                book=book_for(episode),
                **single_kwargs,  # type: ignore[arg-type]
            )
        )
    return IndependentResult(capital_usdc=capital_usdc, results=results)


@dataclass(slots=True)
class IndependentResult:
    """A set of single-opportunity hypotheticals, deliberately unsummed."""

    capital_usdc: float
    results: list[SingleResult] = field(default_factory=list)

    @property
    def executable_count(self) -> int:
        return sum(1 for r in self.results if r.executable)

    @property
    def not_executable_count(self) -> int:
        return sum(1 for r in self.results if not r.executable)

    @property
    def capacity_limited_count(self) -> int:
        return sum(1 for r in self.results if r.capacity_limited)

    @property
    def per_opportunity_expected_edge(self) -> list[float]:
        """Edges of the executable results, kept separate on purpose.

        Not summed: ten of these sharing one row would invite the reader to
        add them, and the sum has no referent - there is no account that
        held ten stakes at once.
        """
        return [
            r.theoretical_expected_net_edge_usdc
            for r in self.results
            if r.executable and r.theoretical_expected_net_edge_usdc is not None
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "single_opportunity",
            "capital_usdc": self.capital_usdc,
            "evaluated_opportunities": len(self.results),
            "executable_count": self.executable_count,
            "not_executable_count": self.not_executable_count,
            "capacity_limited_count": self.capacity_limited_count,
            # A list, never a total: these are independent hypotheticals.
            "per_opportunity_expected_edge": self.per_opportunity_expected_edge,
            "combined_expected_edge": None,
            "statistical_scope": ["reference_capital_hypothetical"],
            "warnings": [
                "Each result assumes its own reference stake, held "
                "independently of every other result.",
                "There is deliberately no combined total: ten separate "
                "'if I staked N here' answers are not one account holding "
                "ten stakes, and summing them would describe money never held.",
                "Reference capital is a research assumption, not a funded "
                "account, not a virtual position and not realised profit.",
                "Actual profit is N/A: this system places no orders.",
            ],
            "results": [r.as_dict() for r in self.results],
        }


class PortfolioWalk:
    """Share one stake across episodes in the order time actually gave them.

    One implementation, two drivers. :func:`simulate_portfolio` collects every
    per-opportunity result and returns the whole record; the streaming engine
    hands each result straight to the snapshot writer and keeps only the
    running totals, so the reference-capital payload is produced without the
    results ever being held.

    The rules are the contract's and are unchanged: order by ``first_seen_utc``
    (never by edge - ranking by the number under evaluation would let the best
    outcome claim the capital first), a per-market cap shared by every episode
    of that market, and capital released only when an episode settles, which
    this module cannot observe, so nothing is released at all.
    """

    def __init__(
        self,
        *,
        capital_usdc: float,
        max_allocation_fraction: float = DEFAULT_MAX_ALLOCATION_FRACTION,
        collect_results: bool = True,
        **single_kwargs: object,
    ) -> None:
        if capital_usdc <= 0:
            raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")
        if not 0 < max_allocation_fraction <= 1:
            raise ValueError(
                f"max_allocation_fraction must be in (0, 1], got {max_allocation_fraction!r}"
            )
        self.outcome = PortfolioResult(
            capital_usdc=capital_usdc, max_allocation_fraction=max_allocation_fraction
        )
        self.collect_results = collect_results
        self.single_kwargs = single_kwargs
        self.balance = capital_usdc
        self.per_opportunity_cap = capital_usdc * max_allocation_fraction
        # Contract section 3.3: episodes from the same market are the same
        # correlated exposure. Giving each a fresh cap would let five signals on
        # one bracket consume five times the share the risk assumption allowed.
        self.allocated_by_market: dict[str, float] = {}
        self.total_count = 0
        self._funds_exhausted = False

    def add(self, episode: Episode, book: OrderBook | None) -> SingleResult:
        """Price one opportunity against what is left, and record the outcome."""
        outcome = self.outcome
        self.total_count += 1

        # Decide what this episode may spend *before* pricing it. Pricing at
        # the full cap and trimming the result afterwards leaves shares,
        # fees and expected edge describing a position that was never
        # funded - which is how a portfolio reports a profit on money it
        # did not have.
        market_id = episode.key.market_id
        market_used = self.allocated_by_market.get(market_id, 0.0)
        available = min(self.per_opportunity_cap - market_used, self.balance)
        if available <= 0:
            if not self._funds_exhausted:
                outcome.not_backtestable_reasons.append(
                    "capital exhausted before all opportunities were funded"
                )
                self._funds_exhausted = True
            return self._record(
                SingleResult(
                    episode_id=episode.episode_id,
                    executable=False,
                    not_executable_reason=NotExecutable.NO_FUNDS,
                    capital_usdc=self.per_opportunity_cap,
                    theoretical_edge_per_share=episode.first_edge,
                    notes=["no reference capital remained for this opportunity"],
                )
            )

        single = simulate_single(
            episode,
            capital_usdc=available,
            book=book,
            **self.single_kwargs,  # type: ignore[arg-type]
        )

        if not single.executable:
            return self._record(single)

        used = single.capital_used_usdc
        if used <= 0:
            # Sized but not spendable: treat it as unfunded rather than
            # counting a position that contributes nothing.
            single.executable = False
            single.not_executable_reason = NotExecutable.NO_FUNDS
            return self._record(single)

        if used > self.balance:  # pragma: no cover - guards the invariant
            raise AssertionError(
                f"allocated {used} exceeds the remaining balance {self.balance}: "
                "pricing must never exceed the funds it was given"
            )

        outcome.executable_count += 1
        if single.capacity_limited:
            outcome.capacity_limited_count += 1
        outcome.allocated_total_usdc += used
        self.allocated_by_market[market_id] = market_used + used
        self.balance -= used
        outcome.peak_open_allocation_usdc = max(
            outcome.peak_open_allocation_usdc, outcome.allocated_total_usdc
        )
        if single.theoretical_expected_net_edge_usdc is not None:
            outcome.expected_net_edge_total += single.theoretical_expected_net_edge_usdc
        return self._record(single)

    def _record(self, single: SingleResult) -> SingleResult:
        outcome = self.outcome
        if not single.executable:
            outcome.not_executable_count += 1
        if self.collect_results:
            outcome.results.append(single)
        return single

    def finish(self) -> PortfolioResult:
        """Close the walk: invariants, honesty flags, and the running totals."""
        outcome = self.outcome
        outcome.balance_remaining_usdc = self.balance

        # The core invariant, checked rather than asserted in prose.
        if outcome.allocated_total_usdc > outcome.capital_usdc + 1e-9:
            raise AssertionError(
                f"allocated {outcome.allocated_total_usdc} exceeds the reference "
                f"capital {outcome.capital_usdc}"
            )

        if outcome.executable_count == 0:
            outcome.backtestable = False
            outcome.not_backtestable_reasons.append(
                "no episode could be sized from retained book levels"
            )
        if self.total_count and outcome.not_executable_count == self.total_count:
            outcome.backtestable = False
            outcome.not_backtestable_reasons.append(
                "every opportunity lacked the evidence a fill needs"
            )
        # Capital is never released inside this pass. Settlement times are not
        # carried on an episode, and neither the snapshot builder nor the
        # reference-capital endpoint supplies them, so the capital timeline
        # cannot be reconstructed. Holding every allocation to the end of the
        # window is the conservative reading of that - it can only understate
        # utilization - but "conservative" is not "validated". Anything with an
        # executable book therefore cannot be presented as historically tested.
        if outcome.executable_count:
            outcome.backtestable = False
            outcome.not_backtestable_reasons.append(
                "settlement timing is not available, so the capital timeline "
                "cannot be reconstructed and these results are theoretical only"
            )
        return outcome


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
    walk = PortfolioWalk(
        capital_usdc=capital_usdc,
        max_allocation_fraction=max_allocation_fraction,
        **single_kwargs,
    )
    for episode in sorted(episodes, key=lambda e: e.first_seen_utc):
        walk.add(episode, book_for(episode))
    return walk.finish()

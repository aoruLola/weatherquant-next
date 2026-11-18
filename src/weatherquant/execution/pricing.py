"""Cost, fee and edge arithmetic shared by signals and simulated fills.

The maths here is transcribed from the P0 specification (§六) and from
Polymarket's published fee documentation. It is the *only* place in the codebase
that computes what a trade costs. Both the research engine and the paper
trading engine call these functions; neither is permitted to re-derive cost.

Definitions (units in brackets)
-------------------------------
``q``                       model probability of the outcome [dimensionless]
``filled_shares``           shares actually obtainable at the book [shares]
``fill_cost_usd``           ``sum(price_i * shares_i)`` walked over real book
                            levels [USD] - never a midpoint
``fees_usd``                Polymarket taker fee [USD]
``execution_buffer_usd``    headroom reserved for price movement between signal
                            and fill [USD]

.. code-block:: text

    cost            = fill_cost_usd + fees_usd + execution_buffer_usd   [USD]
    expected_net_profit       = q * filled_shares - cost                [USD]
    expected_net_profit_per_share = expected_net_profit / filled_shares  [USD/share]

``expected_net_profit`` is the expectation of ``shares * 1{win} - cost`` in
USD: a winning position redeems at $1 per share, so ``E[payout] = q * shares``.
It is an *expectation under the model's own probability*, not a forecast of
realised profit - see ``docs/architecture.md``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from weatherquant.markets.models import BookLevel, OrderBook, Side

__all__ = [
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "DepthFill",
    "ExecutionCost",
    "ExecutionIntent",
    "ExecutionRefused",
    "ExecutionToken",
    "FeeRole",
    "FeeSchedule",
    "FeeStructureUnknown",
    "FillPlan",
    "PricingResult",
    "PricingStatus",
    "adverse_slippage_usd_per_share",
    "build_fill_plan",
    "edge_usd_per_share",
    "edge_usd_per_share_sell",
    "execution_buffer_usd",
    "execution_charges_usd",
    "execution_cost",
    "expected_net_profit_usd",
    "expected_net_profit_usd_per_share",
    "expected_net_profit_usd_sell",
    "maker_fee_usd",
    "max_fee_usd_per_share",
    "max_shares_for_notional",
    "price_execution",
    "quote_age_seconds",
    "sum_executable_expected_net_profit_usd",
    "taker_fee_usd",
]

# Polymarket rounds charged fees to 5 decimal places (docs.polymarket.com/trading/fees).
FEE_QUANTUM = Decimal("0.00001")

#: How old a quote may be at the decision instant and still describe a
#: fillable market. 900s matches the research capital simulation and the
#: engine's collector-silence window; a live fill gate may pass a tighter
#: value (the paper engine uses 90s). The threshold is a documented research
#: assumption, not a venue rule.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 900.0


class ExecutionRefused(Exception):
    """The book cannot be used to build a fill plan.

    ``code`` is a stable identifier suitable for storing on a signal row.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class FeeStructureUnknown(Exception):
    """The venue did not report a fee rate for this market.

    Raised rather than returning 0.0: an unknown fee schedule must never be
    silently treated as free, because that inflates every edge computed from it.
    """


class FillPlan(BaseModel):
    """A hypothetical fill built by walking real order-book depth.

    When the book cannot cover the requested size the plan is *partial*
    (``complete`` is false and ``unfilled_shares`` is positive) rather than
    rejected - the caller needs the partial figure to decide, and discarding it
    would hide how thin the market actually is.

    Invariants (enforced):
        * ``filled_shares + unfilled_shares == requested_shares``
        * ``fill_cost_usd == filled_shares * avg_price_usd_per_share``
        * ``fill_cost_usd <= requested_shares * worst_level_price``
        * ``levels_consumed`` equals the number of book levels actually taken
    """

    model_config = ConfigDict(frozen=True)

    side: Side
    token_id: str
    requested_shares: Annotated[float, Field(gt=0.0)]
    filled_shares: Annotated[float, Field(ge=0.0)]
    fill_cost_usd: Annotated[float, Field(ge=0.0)]
    levels_consumed: Annotated[int, Field(ge=0)]
    worst_price_usd_per_share: Annotated[float, Field(gt=0.0, lt=1.0)] | None

    @property
    def unfilled_shares(self) -> float:
        return self.requested_shares - self.filled_shares

    @property
    def complete(self) -> bool:
        return self.unfilled_shares <= 1e-12

    @property
    def avg_price_usd_per_share(self) -> float | None:
        """Volume-weighted average price actually paid, or ``None`` if nothing filled."""
        if self.filled_shares <= 0.0:
            return None
        return self.fill_cost_usd / self.filled_shares

    @model_validator(mode="after")
    def _check(self) -> FillPlan:
        if self.filled_shares > self.requested_shares + 1e-9:
            raise ValueError(
                f"filled ({self.filled_shares}) cannot exceed requested ({self.requested_shares})"
            )
        if self.filled_shares <= 0.0 and self.fill_cost_usd != 0.0:
            raise ValueError("a plan with no filled shares must have zero fill cost")
        if self.filled_shares > 0.0:
            avg = self.fill_cost_usd / self.filled_shares
            if not 0.0 < avg < 1.0:
                raise ValueError(f"average fill price {avg} is outside (0, 1) USD per share")
            if self.filled_shares + 1e-9 < self.requested_shares and self.worst_price_usd_per_share is None:
                raise ValueError("a partial fill must record the worst price reached")
        if self.levels_consumed == 0 and self.filled_shares > 0.0:
            raise ValueError("filled shares must consume at least one book level")
        return self


class ExecutionCost(BaseModel):
    """Total cash laid out for a fill: exactly three additive terms.

    The decomposition is explicit and additive **by construction** so that a fee
    can never be folded into the fill price (and then charged again), and so
    that ``fees_usd`` remains auditable on its own.

    Invariants (enforced):
        * ``total_cost_usd == fill_cost_usd + fees_usd + execution_buffer_usd``
        * all three components are non-negative
        * ``filled_shares > 0``
        * ``cost_usd_per_share == total_cost_usd / filled_shares``
    """

    model_config = ConfigDict(frozen=True)

    filled_shares: Annotated[float, Field(gt=0.0)]
    fill_cost_usd: Annotated[float, Field(ge=0.0)]
    fees_usd: Annotated[float, Field(ge=0.0)]
    execution_buffer_usd: Annotated[float, Field(ge=0.0)]

    @property
    def total_cost_usd(self) -> float:
        """Sum of exactly three terms. No fourth term may be added here."""
        return self.fill_cost_usd + self.fees_usd + self.execution_buffer_usd

    @property
    def cost_usd_per_share(self) -> float:
        return self.total_cost_usd / self.filled_shares

    @property
    def fill_cost_usd_per_share(self) -> float:
        return self.fill_cost_usd / self.filled_shares

    @property
    def net_proceeds_usd(self) -> float:
        """``fill - fees - buffer``: what a *sale* of these shares nets.

        The mirror image of :attr:`total_cost_usd` over the same three terms.
        Deriving both from one decomposition is what makes it impossible for a
        fee to be charged on a buy and forgotten on a sale, or counted twice
        on either.
        """
        return self.fill_cost_usd - self.fees_usd - self.execution_buffer_usd

    @model_validator(mode="after")
    def _check(self) -> ExecutionCost:
        if self.fill_cost_usd <= 0.0:
            raise ValueError("fill_cost_usd must be > 0 for a cost to exist")
        return self


class FeeRole(StrEnum):
    """Who pays the fee. Makers and takers are separate schedules.

    Polymarket charges takers and gives makers a zero rate on the weather
    markets this system studies. The role is explicit rather than implied by
    a direction because a maker and a taker can trade the same side of the
    same book at the same price and owe different amounts.
    """

    MAKER = "maker"
    TAKER = "taker"


class FeeSchedule(BaseModel):
    """The venue-reported fee schedule for one role.

    ``rate is None`` means **the venue did not report a rate** - not that the
    rate is zero. :meth:`fee_usd` refuses to price an unknown schedule rather
    than imputing ``0``; that refusal is the ``FEE_UNKNOWN`` status in
    :func:`price_execution`.

    ``exponent`` is carried for audit. The documented Polymarket base formula
    (``C * rate * p * (1 - p)``) does not use it, and this contract does not
    invent a use for it; a market that reports a different schedule must be
    modelled explicitly before its fees are trusted.
    """

    model_config = ConfigDict(frozen=True)

    role: FeeRole
    rate: Annotated[float, Field(ge=0.0)] | None = None
    exponent: Annotated[float, Field(ge=0.0)] | None = None

    @classmethod
    def unknown(cls, role: FeeRole) -> FeeSchedule:
        """A schedule the venue has not reported."""
        return cls(role=role, rate=None)

    @property
    def known(self) -> bool:
        return self.rate is not None

    def fee_usd(self, *, filled_shares: float, price_usd_per_share: float) -> float:
        """Fee for this role at the plan's volume-weighted price."""
        return _fee_usd(
            filled_shares=filled_shares,
            price_usd_per_share=price_usd_per_share,
            fee_rate=self.rate,
        )


class ExecutionToken(StrEnum):
    """Which outcome token an execution is in."""

    YES = "yes"
    NO = "no"


class ExecutionIntent(StrEnum):
    """A trade expressed on the outcome token it actually trades.

    ``BUY_NO`` is deliberately **not** modelled as ``1 - SELL_YES``. The two
    hit different books (YES bids vs NO asks), have different depth, round
    differently at the venue's tick, and can pay different fees even though
    the fee formula is symmetric in ``p``. See
    ``docs/pricing-execution-contract.md``.
    """

    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"
    BUY_NO = "buy_no"
    SELL_NO = "sell_no"

    @property
    def token(self) -> ExecutionToken:
        return ExecutionToken.NO if self.value.endswith("_no") else ExecutionToken.YES

    @property
    def side(self) -> Side:
        return Side.BUY if self.value.startswith("buy_") else Side.SELL


class PricingStatus(StrEnum):
    """The typed verdict of :func:`price_execution`.

    Only :attr:`EXECUTABLE` carries a cost and a USD profit. ``PARTIAL``
    describes how far the book would have got but is *not* an executable
    trade for the requested size, so it carries no fee or profit either.
    """

    EXECUTABLE = "EXECUTABLE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    FEE_UNKNOWN = "FEE_UNKNOWN"
    STALE_QUOTE = "STALE_QUOTE"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"

    @property
    def is_executable(self) -> bool:
        return self is PricingStatus.EXECUTABLE


def quote_age_seconds(venue_ts: datetime | None, decision_time: datetime) -> float | None:
    """How old the quote is at the decision instant, in seconds.

    ``None`` when the quote carries no venue timestamp: an age that cannot be
    established is not a fresh age. A negative result means the quote is from
    the future relative to the decision, i.e. look-ahead.
    """
    if venue_ts is None:
        return None
    return (decision_time - venue_ts).total_seconds()


def adverse_slippage_usd_per_share(
    *, side: Side, vwap_usd_per_share: float, top_of_book_usd_per_share: float
) -> float:
    """Price impact against the top of the book, always adverse or zero.

    A buy slipping means paying more than the best ask; a sell slipping means
    receiving less than the best bid. Using one signed subtraction for both
    would report a sell's slippage as negative (or clamp it to zero), hiding
    the cost of walking a thin bid side.
    """
    if side is Side.BUY:
        return max(vwap_usd_per_share - top_of_book_usd_per_share, 0.0)
    return max(top_of_book_usd_per_share - vwap_usd_per_share, 0.0)


class DepthFill(BaseModel):
    """A depth walk that may be partial or empty, unlike :class:`FillPlan`.

    ``FillPlan`` is the legacy all-or-error shape used by the paper engine.
    This record exists because a research result has to be able to say
    "nothing filled" and "half filled" as values, not as exceptions.
    """

    model_config = ConfigDict(frozen=True)

    token: ExecutionToken
    side: Side
    requested_shares: Annotated[float, Field(gt=0.0)]
    filled_shares: Annotated[float, Field(ge=0.0)]
    fill_cost_usd: Annotated[float, Field(ge=0.0)]
    levels_consumed: Annotated[int, Field(ge=0)]
    top_of_book_price_usd_per_share: Annotated[float, Field(gt=0.0, lt=1.0)] | None
    worst_price_usd_per_share: Annotated[float, Field(gt=0.0, lt=1.0)] | None

    @property
    def unfilled_shares(self) -> float:
        return self.requested_shares - self.filled_shares

    @property
    def complete(self) -> bool:
        return self.unfilled_shares <= 1e-12

    @property
    def vwap_usd_per_share(self) -> float | None:
        if self.filled_shares <= 0.0:
            return None
        return self.fill_cost_usd / self.filled_shares

    @property
    def slippage_usd_per_share(self) -> float | None:
        """Adverse price impact of the walk versus the top of the book."""
        vwap = self.vwap_usd_per_share
        if vwap is None or self.top_of_book_price_usd_per_share is None:
            return None
        return adverse_slippage_usd_per_share(
            side=self.side,
            vwap_usd_per_share=vwap,
            top_of_book_usd_per_share=self.top_of_book_price_usd_per_share,
        )

    @property
    def slippage_usd(self) -> float | None:
        per_share = self.slippage_usd_per_share
        if per_share is None:
            return None
        return per_share * self.filled_shares

    @model_validator(mode="after")
    def _check(self) -> DepthFill:
        if self.filled_shares > self.requested_shares + 1e-9:
            raise ValueError(
                f"filled ({self.filled_shares}) cannot exceed requested ({self.requested_shares})"
            )
        if self.filled_shares <= 0.0 and self.fill_cost_usd != 0.0:
            raise ValueError("a walk with no filled shares must have zero fill cost")
        if self.filled_shares > 0.0:
            avg = self.fill_cost_usd / self.filled_shares
            if not 0.0 < avg < 1.0:
                raise ValueError(f"average fill price {avg} is outside (0, 1) USD per share")
            if self.levels_consumed == 0:
                raise ValueError("filled shares must consume at least one book level")
            if self.top_of_book_price_usd_per_share is None:
                raise ValueError("a fill must record the top of book it walked from")
        if not self.complete and self.worst_price_usd_per_share is None and self.filled_shares > 0.0:
            raise ValueError("a partial fill must record the worst price reached")
        return self


class PricingResult(BaseModel):
    """Typed research result for one requested execution.

    The status is the answer; ``reasons`` carries every stable code that
    contributed to it. Monetary cost and profit fields are ``None`` unless
    the status is :attr:`PricingStatus.EXECUTABLE` - a refusal never carries a
    number that could be mistaken for a fill.
    """

    model_config = ConfigDict(frozen=True)

    status: PricingStatus
    reasons: tuple[str, ...] = ()
    intent: ExecutionIntent
    token_id: str | None
    fee_role: FeeRole
    fee_rate: float | None
    decision_time_utc: datetime
    quote_ts_utc: datetime | None
    quote_age_seconds: float | None
    requested_shares: Annotated[float, Field(gt=0.0)]
    fill: DepthFill | None = None

    fees_usd: Annotated[float, Field(ge=0.0)] | None = None
    execution_buffer_usd: Annotated[float, Field(ge=0.0)] | None = None
    total_cost_usd: Annotated[float, Field(ge=0.0)] | None = None
    net_proceeds_usd: float | None = None
    expected_net_profit_usd: float | None = None
    expected_net_profit_usd_per_share: float | None = None
    model_probability: Annotated[float, Field(ge=0.0, le=1.0)] | None = None

    @property
    def executable(self) -> bool:
        return self.status.is_executable

    @property
    def filled_shares(self) -> float | None:
        return None if self.fill is None else self.fill.filled_shares

    @property
    def vwap_usd_per_share(self) -> float | None:
        return None if self.fill is None else self.fill.vwap_usd_per_share

    @property
    def slippage_usd_per_share(self) -> float | None:
        return None if self.fill is None else self.fill.slippage_usd_per_share

    @model_validator(mode="after")
    def _check(self) -> PricingResult:
        monetary = (
            self.fees_usd,
            self.execution_buffer_usd,
            self.total_cost_usd,
            self.net_proceeds_usd,
            self.expected_net_profit_usd,
            self.expected_net_profit_usd_per_share,
        )
        if not self.status.is_executable:
            if any(value is not None for value in monetary):
                raise ValueError(f"{self.status.value} must not carry a USD cost or profit")
            if self.status is PricingStatus.PARTIAL:
                if self.fill is None or self.fill.complete:
                    raise ValueError("PARTIAL must carry an incomplete depth walk")
            elif self.fill is not None:
                raise ValueError(f"{self.status.value} must not carry a fill")
            return self

        if self.fill is None or not self.fill.complete:
            raise ValueError("EXECUTABLE requires a complete depth walk")
        if self.fees_usd is None or self.execution_buffer_usd is None:
            raise ValueError("EXECUTABLE requires fees and an execution buffer")
        if self.intent.side is Side.BUY:
            if self.total_cost_usd is None or self.net_proceeds_usd is not None:
                raise ValueError("a BUY execution carries total_cost_usd, never net_proceeds_usd")
        elif self.net_proceeds_usd is None or self.total_cost_usd is not None:
            raise ValueError("a SELL execution carries net_proceeds_usd, never total_cost_usd")
        if (self.expected_net_profit_usd is None) != (
            self.expected_net_profit_usd_per_share is None
        ):
            raise ValueError("total and per-share expected profit must be present together")
        return self


def taker_fee_usd(
    *,
    filled_shares: float,
    price_usd_per_share: float,
    fee_rate: float | None,
) -> float:
    """Polymarket taker fee, in USD.

    Official formula (docs.polymarket.com/trading/fees):

    .. code-block:: text

        fee = C * feeRate * p * (1 - p)

    where ``C`` is shares traded, ``p`` the execution price, and ``feeRate`` the
    category base rate (weather markets: 0.05 taker, 0 maker). Charged at match
    time to **takers only**; makers never pay. Rounded to 5 decimal places.

    ``price_usd_per_share`` is the plan's volume-weighted fill price, i.e. what
    the shares cost as a whole. Since ``p * (1 - p)`` is concave, this is never
    cheaper than adding a fee per swept level (Jensen), so the convention is the
    conservative reading of the formula for a multi-level fill - and it leaves
    exactly one fee function in the codebase, which is what stops a fee from
    being counted twice.

    Raises :class:`FeeStructureUnknown` when ``fee_rate`` is ``None`` - an
    unspecified schedule is not the same as a zero one.
    """
    return _fee_usd(
        filled_shares=filled_shares,
        price_usd_per_share=price_usd_per_share,
        fee_rate=fee_rate,
    )


def maker_fee_usd(
    *,
    filled_shares: float,
    price_usd_per_share: float,
    fee_rate: float | None,
) -> float:
    """Maker fee, in USD, from the same documented base formula.

    Polymarket's weather markets quote a maker rate of zero, so a known
    ``fee_rate=0.0`` costs nothing. A non-zero maker rate is priced with
    ``C * rate * p * (1 - p)`` rather than assumed away; ``None`` raises, for
    the same reason it does on the taker side.
    """
    return _fee_usd(
        filled_shares=filled_shares,
        price_usd_per_share=price_usd_per_share,
        fee_rate=fee_rate,
    )


def _fee_usd(*, filled_shares: float, price_usd_per_share: float, fee_rate: float | None) -> float:
    if fee_rate is None:
        raise FeeStructureUnknown(
            "market did not report a fee rate; refusing to assume zero fee. "
            "Fetch fee_rate/fee_exponent from the venue before pricing this trade."
        )
    if fee_rate < 0.0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")
    if filled_shares < 0.0:
        raise ValueError(f"filled_shares must be >= 0, got {filled_shares}")
    if not 0.0 < price_usd_per_share < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price_usd_per_share}")
    if filled_shares == 0.0 or fee_rate == 0.0:
        return 0.0

    raw = Decimal(filled_shares) * Decimal(str(fee_rate)) * Decimal(str(price_usd_per_share)) * (
        Decimal("1") - Decimal(str(price_usd_per_share))
    )
    return float(raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP))


def max_fee_usd_per_share(*, fee_rate: float | None) -> float:
    """Upper bound of the per-share fee at any price in ``(0, 1)``.

    ``rate * p * (1 - p)`` is maximised at ``p = 0.5``, so the bound is
    ``rate / 4``. Used to reserve fee headroom before a fill is priced; the
    priced fill still checks the real number afterwards.
    """
    if fee_rate is None:
        raise FeeStructureUnknown(
            "market did not report a fee rate; refusing to assume zero fee."
        )
    if fee_rate < 0.0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")
    return fee_rate * 0.25


def execution_buffer_usd(*, fill_cost_usd: float, buffer_fraction: float) -> float:
    """Headroom reserved against price movement, as a fraction of fill notional.

    A *cost* term, not a fee: it is not charged by the venue, it is the budget
    the plan allows itself before the edge goes negative.
    """
    if buffer_fraction < 0.0:
        raise ValueError(f"buffer_fraction must be >= 0, got {buffer_fraction}")
    return fill_cost_usd * buffer_fraction


def build_fill_plan(
    book: OrderBook,
    *,
    side: Side,
    shares: float,
) -> FillPlan:
    """Walk real book depth to produce a hypothetical fill for ``shares``.

    Never uses the midpoint. Never assumes the whole size fills at the best
    level. A book with any :meth:`OrderBook.defects` is refused with a stable
    error code rather than silently priced.

    Raises
    ------
    ExecutionRefused
        The side being walked is unusable (missing, non-monotonic) or the book
        is crossed, or ``shares`` is not a positive finite number.

    Notes
    -----
    Only defects that would corrupt *this* fill block it. A buy consumes the ask
    side, so an absent or scrambled bid side is recorded on the book but does
    not prevent pricing the buy; a crossed book blocks both sides, because in
    that state neither side's prices are meaningful. The full defect list stays
    available on :meth:`OrderBook.defects` for storage and audit.
    """
    if not (shares > 0.0) or shares != shares or shares == float("inf"):
        raise ExecutionRefused("INVALID_ORDER_SIZE", f"shares must be finite and > 0, got {shares}")

    defects = _blocking_defects(book, side)
    if defects:
        raise ExecutionRefused("+".join(defects), f"order book is not executable: {defects}")

    levels = book.asks if side is Side.BUY else book.bids
    filled, cost, consumed, worst_price = _walk_levels(levels, shares)

    if filled <= 0.0:
        raise ExecutionRefused("NO_LIQUIDITY", "order book contained no usable size on that side")

    return FillPlan(
        side=side,
        token_id=book.token_id,
        requested_shares=shares,
        filled_shares=filled,
        fill_cost_usd=cost,
        levels_consumed=consumed,
        worst_price_usd_per_share=worst_price,
    )


def _blocking_defects(book: OrderBook, side: Side) -> list[str]:
    """Defects that make *this* side unexecutable, in the book's own codes."""
    blocking_side = "ASK" if side is Side.BUY else "BID"
    blocking = {
        f"NON_MONOTONIC_{blocking_side}",
        f"NO_{blocking_side}_SIDE",
        "CROSSED_OR_LOCKED",
    }
    return [d for d in book.defects() if d in blocking]


def _walk_levels(
    levels: Sequence[BookLevel], shares: float
) -> tuple[float, float, int, float | None]:
    """Walk ``levels`` for ``shares``; return filled, cost, levels taken, worst price."""
    remaining = shares
    filled = 0.0
    cost = 0.0
    consumed = 0
    worst_price: float | None = None

    for level in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, level.size_shares)
        filled += take
        cost += take * level.price_usd_per_share
        worst_price = level.price_usd_per_share
        consumed += 1
        remaining -= take

    # Reconcile float drift so the plan's own invariants hold exactly.
    filled = min(filled, shares)
    if abs(filled + remaining - shares) > 1e-9:
        filled = shares - remaining

    return filled, cost, consumed, worst_price


def price_execution(
    book: OrderBook | None,
    *,
    intent: ExecutionIntent,
    shares: float,
    fee_schedule: FeeSchedule,
    decision_time: datetime,
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    buffer_fraction: float = 0.0,
    q: float | None = None,
) -> PricingResult:
    """Price one requested execution against its **own** token's book.

    ``book`` must be the book of ``intent.token`` - the YES book for
    ``BUY_YES``/``SELL_YES`` and the NO book for ``BUY_NO``/``SELL_NO``. The
    function never derives one token's price from the other; a NO ask is not
    ``1 - YES bid`` and using it that way would invent a quote nobody made.

    ``q`` is the probability that ``intent.token`` wins: P(YES) for the YES
    intents, P(NO) for the NO intents.

    Status precedence is causal: an unusable book first, then a quote that
    cannot be trusted at the decision instant, then an unreported fee, then
    depth. Every applicable code is collected in ``reasons``.

    Monetary cost and USD profit are populated only for ``EXECUTABLE``. A
    ``PARTIAL`` result reports the depth walk (filled size, VWAP, slippage)
    but no fee or profit, because a partial fill is not the requested trade.
    """
    if max_quote_age_seconds <= 0:
        raise ValueError(f"max_quote_age_seconds must be > 0, got {max_quote_age_seconds}")
    if buffer_fraction < 0.0:
        raise ValueError(f"buffer_fraction must be >= 0, got {buffer_fraction}")
    if q is not None and not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be a probability in [0, 1], got {q}")
    if not (shares > 0.0) or shares != shares or shares == float("inf"):
        raise ValueError(f"shares must be finite and > 0, got {shares}")

    base = {
        "intent": intent,
        "token_id": None if book is None else book.token_id,
        "fee_role": fee_schedule.role,
        "fee_rate": fee_schedule.rate,
        "decision_time_utc": decision_time,
        "quote_ts_utc": None if book is None else book.venue_ts,
        "quote_age_seconds": (
            None if book is None else quote_age_seconds(book.venue_ts, decision_time)
        ),
        "requested_shares": shares,
        "model_probability": q,
    }

    if book is None:
        return PricingResult(
            status=PricingStatus.NOT_EXECUTABLE, reasons=("NO_ORDER_BOOK",), **base
        )

    defects = _blocking_defects(book, intent.side)
    if defects:
        return PricingResult(
            status=PricingStatus.NOT_EXECUTABLE, reasons=tuple(defects), **base
        )

    age = base["quote_age_seconds"]
    if age is None:
        return PricingResult(
            status=PricingStatus.STALE_QUOTE, reasons=("QUOTE_AGE_UNDEFINED",), **base
        )
    if age < 0.0:
        return PricingResult(
            status=PricingStatus.STALE_QUOTE, reasons=("LOOK_AHEAD_QUOTE",), **base
        )
    if age > max_quote_age_seconds:
        return PricingResult(
            status=PricingStatus.STALE_QUOTE, reasons=("QUOTE_TOO_OLD",), **base
        )

    if not fee_schedule.known:
        return PricingResult(
            status=PricingStatus.FEE_UNKNOWN, reasons=("FEE_RATE_UNKNOWN",), **base
        )

    levels = book.asks if intent.side is Side.BUY else book.bids
    filled, fill_cost, consumed, worst = _walk_levels(levels, shares)
    top = levels[0].price_usd_per_share if levels else None
    fill = DepthFill(
        token=intent.token,
        side=intent.side,
        requested_shares=shares,
        filled_shares=filled,
        fill_cost_usd=fill_cost,
        levels_consumed=consumed,
        top_of_book_price_usd_per_share=top,
        worst_price_usd_per_share=worst if consumed else None,
    )

    if filled <= 0.0:
        # No fill facts to report: a zero-share walk must not look like a plan.
        return PricingResult(
            status=PricingStatus.INSUFFICIENT_DEPTH, reasons=("NO_LIQUIDITY",), **base
        )
    if not fill.complete:
        return PricingResult(
            status=PricingStatus.PARTIAL, reasons=("UNFILLED_REMAINDER",), fill=fill, **base
        )

    vwap = fill.vwap_usd_per_share
    assert vwap is not None  # guaranteed: filled > 0
    fees = fee_schedule.fee_usd(filled_shares=filled, price_usd_per_share=vwap)
    buffer = execution_buffer_usd(fill_cost_usd=fill_cost, buffer_fraction=buffer_fraction)
    cost = ExecutionCost(
        filled_shares=filled,
        fill_cost_usd=fill_cost,
        fees_usd=fees,
        execution_buffer_usd=buffer,
    )
    if intent.side is Side.BUY:
        total_cost = cost.total_cost_usd
        net_proceeds = None
        profit = (
            None if q is None else expected_net_profit_usd(q=q, filled_shares=filled, cost=cost)
        )
        per_share = (
            None
            if q is None
            else expected_net_profit_usd_per_share(q=q, filled_shares=filled, cost=cost)
        )
    else:
        total_cost = None
        net_proceeds = cost.net_proceeds_usd
        profit = (
            None
            if q is None
            else expected_net_profit_usd_sell(q=q, filled_shares=filled, cost=cost)
        )
        per_share = (
            None if q is None else edge_usd_per_share_sell(q=q, filled_shares=filled, cost=cost)
        )

    return PricingResult(
        status=PricingStatus.EXECUTABLE,
        reasons=(),
        fill=fill,
        fees_usd=fees,
        execution_buffer_usd=buffer,
        total_cost_usd=total_cost,
        net_proceeds_usd=net_proceeds,
        expected_net_profit_usd=profit,
        expected_net_profit_usd_per_share=per_share,
        **base,
    )


def sum_executable_expected_net_profit_usd(results: list[PricingResult]) -> float | None:
    """Sum USD profit **only** across executable pricing results.

    This is the guard for the contract's hardest rule: an Observation Edge is
    a dimensionless per-share expectation, and summing it (or a refused
    result's absent profit) into a USD total fabricates money. The function
    raises rather than skipping anything, so a caller cannot quietly turn a
    mixed list into a plausible-looking number.

    Returns ``None`` when there is nothing to sum - an empty list is not a
    total of zero.
    """
    if not results:
        return None
    for result in results:
        if not result.executable:
            raise ValueError(
                f"USD totals may only be summed from EXECUTABLE results; got "
                f"{result.status.value} ({', '.join(result.reasons) or 'no reason'})"
            )
        if result.expected_net_profit_usd is None:
            raise ValueError(
                "an EXECUTABLE result has no USD profit: it was priced without a "
                "model probability, so it cannot enter a USD total"
            )
    return math.fsum(
        result.expected_net_profit_usd  # type: ignore[arg-type]
        for result in results
    )


def max_shares_for_notional(
    book: OrderBook, *, side: Side, notional_usd: float
) -> tuple[float, bool]:
    """How many shares a cash budget could take by walking real levels.

    Returns ``(shares, exhausted)``. ``exhausted`` means the budget was still
    unspent when the book ran out, i.e. the book - not the budget - is the
    binding constraint. This is the sizing walk the research capital simulator
    uses; the actual fill still goes through :func:`build_fill_plan` and
    :func:`execution_cost`.
    """
    if notional_usd < 0.0:
        raise ValueError(f"notional_usd must be >= 0, got {notional_usd}")
    levels = book.asks if side is Side.BUY else book.bids
    remaining = notional_usd
    shares = 0.0
    for level in levels:
        if remaining <= 0.0:
            break
        level_notional = level.price_usd_per_share * level.size_shares
        take = min(level_notional, remaining)
        shares += take / level.price_usd_per_share
        remaining -= take
    exhausted = bool(levels) and remaining > 0.0
    return shares, exhausted


def execution_charges_usd(cost: ExecutionCost) -> float:
    """Fees plus buffer: what the fill costs beyond its own notional.

    Deliberately excludes ``fill_cost_usd``, whose sign differs between a buy
    (money spent) and a sell (money received). Keeping one column that always
    means "charges on top of the fill" avoids a field whose meaning flips with
    direction.
    """
    return cost.fees_usd + cost.execution_buffer_usd


def expected_net_profit_usd(*, q: float, filled_shares: float, cost: ExecutionCost) -> float:
    """``q * filled_shares - cost.total_cost_usd`` [USD].

    ``q`` must be a probability in [0, 1]; a value outside that range is a
    modelling bug, not a large edge, so it raises.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be a probability in [0, 1], got {q}")
    if filled_shares < 0.0:
        raise ValueError(f"filled_shares must be >= 0, got {filled_shares}")
    return q * filled_shares - cost.total_cost_usd


def expected_net_profit_usd_per_share(*, q: float, filled_shares: float, cost: ExecutionCost) -> float:
    """``expected_net_profit / filled_shares`` [USD per share].

    Algebraically equal to ``q - cost.cost_usd_per_share``; asserted as an
    invariant in the test suite rather than assumed.
    """
    if filled_shares <= 0.0:
        raise ValueError(f"filled_shares must be > 0 to express a per-share figure, got {filled_shares}")
    return expected_net_profit_usd(q=q, filled_shares=filled_shares, cost=cost) / filled_shares


def edge_usd_per_share(*, q: float, filled_shares: float, cost: ExecutionCost) -> float:
    """The signal's edge: expected net profit per share, in USD per share.

    Same quantity as :func:`expected_net_profit_usd_per_share`, under the name
    the strategy layer uses. Positive means the *model's own* expectation is
    positive after costs - it is not a claim that the trade is profitable.
    """
    return expected_net_profit_usd_per_share(q=q, filled_shares=filled_shares, cost=cost)


def execution_cost(fill: FillPlan, *, fee_rate: float | None, buffer_fraction: float) -> ExecutionCost:
    """Compose the three cost terms for a plan, in one place.

    Kept here rather than left to each caller because "add the fee, then add
    the buffer, then forget the fee" is a failure mode that looks exactly like
    a strategy with a positive edge.
    """
    if fill.filled_shares <= 0.0:
        raise ExecutionRefused("NO_LIQUIDITY", "cannot cost a fill that filled nothing")
    price = fill.avg_price_usd_per_share
    assert price is not None  # guaranteed by FillPlan when filled_shares > 0
    return ExecutionCost(
        filled_shares=fill.filled_shares,
        fill_cost_usd=fill.fill_cost_usd,
        fees_usd=taker_fee_usd(
            filled_shares=fill.filled_shares,
            price_usd_per_share=price,
            fee_rate=fee_rate,
        ),
        execution_buffer_usd=execution_buffer_usd(
            fill_cost_usd=fill.fill_cost_usd,
            buffer_fraction=buffer_fraction,
        ),
    )


def expected_net_profit_usd_sell(
    *, q: float, filled_shares: float, cost: ExecutionCost
) -> float:
    """``E[profit]`` from *selling* ``filled_shares`` YES at this book.

    Selling YES means taking the proceeds now and owing a $1 payout on every
    share if YES wins::

        net = proceeds - fees - buffer        (certain, today)
        if YES wins: net -= shares            (the payout)
        E[net] = (proceeds - fees - buffer) - q * shares
               = cost.net_proceeds_usd - q * filled_shares

    The payoff has the same algebraic shape as holding a NO position, but this
    function values **closing a long YES position** - there is no order mapping
    to BUY NO and no naked short. The paper engine only sells shares it already
    holds (``paper/risk.py``). The price is taken from the **bid** side, which
    is the side a seller actually hits. Using the ask here - or the midpoint -
    would value the trade against prices nobody would pay.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be a probability in [0, 1], got {q}")
    if filled_shares < 0.0:
        raise ValueError(f"filled_shares must be >= 0, got {filled_shares}")
    return cost.net_proceeds_usd - q * filled_shares


def edge_usd_per_share_sell(*, q: float, filled_shares: float, cost: ExecutionCost) -> float:
    """Sell-side edge, USD per share. Positive means the model thinks the bid
    is higher than YES is worth - not that the trade would have profited."""
    if filled_shares <= 0.0:
        raise ValueError(f"filled_shares must be > 0, got {filled_shares}")
    return expected_net_profit_usd_sell(q=q, filled_shares=filled_shares, cost=cost) / filled_shares

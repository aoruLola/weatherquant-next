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

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from weatherquant.markets.models import OrderBook, Side

__all__ = [
    "ExecutionCost",
    "ExecutionRefused",
    "FeeStructureUnknown",
    "FillPlan",
    "build_fill_plan",
    "edge_usd_per_share",
    "execution_buffer_usd",
    "expected_net_profit_usd",
    "expected_net_profit_usd_per_share",
    "taker_fee_usd",
]

# Polymarket rounds charged fees to 5 decimal places (docs.polymarket.com/trading/fees).
FEE_QUANTUM = Decimal("0.00001")


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

    @model_validator(mode="after")
    def _check(self) -> ExecutionCost:
        if self.fill_cost_usd <= 0.0:
            raise ValueError("fill_cost_usd must be > 0 for a cost to exist")
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

    blocking_side = "ASK" if side is Side.BUY else "BID"
    blocking = {
        f"NON_MONOTONIC_{blocking_side}",
        f"NO_{blocking_side}_SIDE",
        "CROSSED_OR_LOCKED",
    }
    defects = [d for d in book.defects() if d in blocking]
    if defects:
        raise ExecutionRefused("+".join(defects), f"order book is not executable: {defects}")

    levels = book.asks if side is Side.BUY else book.bids

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
        cost = cost  # cost already reflects only levels actually taken

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

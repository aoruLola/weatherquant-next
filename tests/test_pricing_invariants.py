"""Mathematical invariants for cost, fee and edge (spec §六).

Expected values are computed by hand from the documented Polymarket fee
formula, not by re-calling the code under test.
"""

from __future__ import annotations

import pytest

from weatherquant.execution.pricing import (
    ExecutionCost,
    ExecutionRefused,
    FeeStructureUnknown,
    build_fill_plan,
    edge_usd_per_share,
    execution_buffer_usd,
    expected_net_profit_usd,
    expected_net_profit_usd_per_share,
    taker_fee_usd,
)
from weatherquant.markets.models import Side

# ---------------------------------------------------------------------------
# Fee
# ---------------------------------------------------------------------------


def test_fee_matches_polymarket_documented_example_sports() -> None:
    # docs.polymarket.com/trading/fees: 100 shares @ $0.50, sports rate 0.05 -> $1.25
    assert taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.50, fee_rate=0.05) == pytest.approx(1.25)


def test_fee_matches_polymarket_documented_symmetry() -> None:
    # "A trade at 30c incurs the same dollar fee as a trade at 70c."
    at_30 = taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.30, fee_rate=0.05)
    at_70 = taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.70, fee_rate=0.05)
    assert at_30 == at_70
    assert at_30 == pytest.approx(100 * 0.05 * 0.30 * 0.70)  # 1.05


def test_fee_is_linear_in_shares() -> None:
    one = taker_fee_usd(filled_shares=1.0, price_usd_per_share=0.42, fee_rate=0.05)
    many = taker_fee_usd(filled_shares=37.0, price_usd_per_share=0.42, fee_rate=0.05)
    assert many == pytest.approx(one * 37.0, rel=1e-9)


def test_fee_is_zero_when_rate_is_zero() -> None:
    assert taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.5, fee_rate=0.0) == 0.0


def test_unknown_fee_schedule_raises_instead_of_defaulting_to_free() -> None:
    """An unspecified rate must never be read as 'no fee' - that inflates every edge."""
    with pytest.raises(FeeStructureUnknown, match="refusing to assume zero fee"):
        taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.5, fee_rate=None)


def test_fee_is_rounded_to_five_decimals() -> None:
    fee = taker_fee_usd(filled_shares=1.0, price_usd_per_share=0.3333333, fee_rate=0.05)
    assert round(fee, 5) == fee


def test_fee_rejects_out_of_range_price() -> None:
    with pytest.raises(ValueError, match=r"\(0, 1\)"):
        taker_fee_usd(filled_shares=1.0, price_usd_per_share=1.0, fee_rate=0.05)


def test_fee_is_charged_on_the_plan_vwap_not_per_level() -> None:
    """Documented convention: one fee, computed on the volume-weighted fill price.

    ``p * (1 - p)`` is concave, so by Jensen's inequality the VWAP-based fee is
    never *smaller* than summing a fee per swept level. Charging on the VWAP is
    therefore the conservative of the two readings of Polymarket's
    ``C * feeRate * p * (1 - p)`` for a multi-level fill, and it keeps exactly
    one fee function in the codebase (spec: 费用不能重复计算).
    """
    # 100 @ 0.50 + 50 @ 0.56, VWAP = 78/150
    shares, cost_usd = 150.0, 78.0
    vwap = cost_usd / shares
    on_vwap = taker_fee_usd(filled_shares=shares, price_usd_per_share=vwap, fee_rate=0.05)
    per_level = (
        taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.50, fee_rate=0.05)
        + taker_fee_usd(filled_shares=50.0, price_usd_per_share=0.56, fee_rate=0.05)
    )
    assert on_vwap == pytest.approx(1.872)
    assert on_vwap >= per_level - 1e-12


def test_execution_buffer_is_a_fraction_of_fill_notional() -> None:
    assert execution_buffer_usd(fill_cost_usd=100.0, buffer_fraction=0.005) == pytest.approx(0.5)
    assert execution_buffer_usd(fill_cost_usd=100.0, buffer_fraction=0.0) == 0.0
    with pytest.raises(ValueError, match="buffer_fraction"):
        execution_buffer_usd(fill_cost_usd=100.0, buffer_fraction=-0.1)


# ---------------------------------------------------------------------------
# Fill plans walk real depth
# ---------------------------------------------------------------------------


def test_fill_plan_walks_levels_and_costs_exactly(make_book) -> None:
    book = make_book(bids=[(0.48, 100.0)], asks=[(0.50, 100.0), (0.55, 400.0)])
    plan = build_fill_plan(book, side=Side.BUY, shares=150.0)

    assert plan.filled_shares == pytest.approx(150.0)
    assert plan.fill_cost_usd == pytest.approx(100.0 * 0.50 + 50.0 * 0.55)  # 50 + 27.5
    assert plan.fill_cost_usd == pytest.approx(77.5)
    assert plan.avg_price_usd_per_share == pytest.approx(77.5 / 150.0)
    assert plan.levels_consumed == 2
    assert plan.complete is True
    assert plan.unfilled_shares == pytest.approx(0.0)


def test_fill_price_is_never_the_midpoint(make_book) -> None:
    """Spec: 不允许使用 midpoint 冒充成交价格."""
    book = make_book(bids=[(0.40, 1000.0)], asks=[(0.60, 1000.0)])
    plan = build_fill_plan(book, side=Side.BUY, shares=50.0)

    midpoint = book.midpoint_usd_per_share
    assert midpoint == pytest.approx(0.50)
    assert plan.avg_price_usd_per_share == pytest.approx(0.60)
    assert plan.avg_price_usd_per_share != pytest.approx(midpoint)


def test_partial_fill_reports_unfilled_shares(make_book) -> None:
    book = make_book(bids=[], asks=[(0.50, 100.0), (0.55, 200.0)])
    plan = build_fill_plan(book, side=Side.BUY, shares=500.0)

    assert plan.complete is False
    assert plan.filled_shares == pytest.approx(300.0)
    assert plan.unfilled_shares == pytest.approx(200.0)
    assert plan.fill_cost_usd == pytest.approx(100 * 0.50 + 200 * 0.55)
    assert plan.worst_price_usd_per_share == pytest.approx(0.55)
    assert plan.filled_shares + plan.unfilled_shares == pytest.approx(plan.requested_shares)


def test_sell_side_walks_bids_from_the_top_down(make_book) -> None:
    book = make_book(bids=[(0.48, 100.0), (0.40, 100.0)], asks=[(0.55, 100.0)])
    plan = build_fill_plan(book, side=Side.SELL, shares=150.0)

    assert plan.fill_cost_usd == pytest.approx(100 * 0.48 + 50 * 0.40)
    assert plan.avg_price_usd_per_share == pytest.approx((48 + 20) / 150.0)


def test_book_with_no_ask_side_is_refused(make_book) -> None:
    book = make_book(bids=[(0.48, 100.0)], asks=[])
    with pytest.raises(ExecutionRefused) as exc:
        build_fill_plan(book, side=Side.BUY, shares=10.0)
    assert "NO_ASK_SIDE" in exc.value.code


def test_crossed_book_is_refused_rather_than_mispriced(make_book) -> None:
    book = make_book(bids=[(0.60, 100.0)], asks=[(0.50, 100.0)])
    with pytest.raises(ExecutionRefused) as exc:
        build_fill_plan(book, side=Side.BUY, shares=10.0)
    assert "CROSSED_OR_LOCKED" in exc.value.code


def test_non_monotonic_ask_side_is_refused(make_book) -> None:
    book = make_book(bids=[(0.40, 100.0)], asks=[(0.50, 100.0), (0.45, 100.0)])
    with pytest.raises(ExecutionRefused) as exc:
        build_fill_plan(book, side=Side.BUY, shares=10.0)
    assert "NON_MONOTONIC_ASK" in exc.value.code


def test_non_positive_order_size_is_refused(make_book) -> None:
    book = make_book(bids=[(0.40, 100.0)], asks=[(0.50, 100.0)])
    for bad in (0.0, -5.0):
        with pytest.raises(ExecutionRefused) as exc:
            build_fill_plan(book, side=Side.BUY, shares=bad)
        assert exc.value.code == "INVALID_ORDER_SIZE"


def test_a_buy_needs_only_the_ask_side(make_book) -> None:
    """A one-sided book is thin but still buyable: the bid side is irrelevant to a buy."""
    book = make_book(bids=[], asks=[(0.50, 100.0), (0.55, 400.0)])
    assert "NO_BID_SIDE" in book.defects()  # recorded on the snapshot...
    plan = build_fill_plan(book, side=Side.BUY, shares=50.0)
    assert plan.filled_shares == pytest.approx(50.0)  # ...but not blocking


def test_a_sell_needs_the_bid_side(make_book) -> None:
    book = make_book(bids=[], asks=[(0.50, 100.0)])
    with pytest.raises(ExecutionRefused) as exc:
        build_fill_plan(book, side=Side.SELL, shares=50.0)
    assert "NO_BID_SIDE" in exc.value.code


def test_a_buy_is_blocked_by_a_bad_ask_side_even_if_bids_are_healthy(make_book) -> None:
    book = make_book(bids=[(0.40, 100.0)], asks=[(0.50, 100.0), (0.45, 100.0)])
    with pytest.raises(ExecutionRefused) as exc:
        build_fill_plan(book, side=Side.BUY, shares=10.0)
    assert "NON_MONOTONIC_ASK" in exc.value.code


def test_a_crossed_book_blocks_both_sides(make_book) -> None:
    """In a crossed state neither side's prices mean anything, so neither is usable."""
    book = make_book(bids=[(0.60, 100.0)], asks=[(0.50, 100.0)])
    for side in (Side.BUY, Side.SELL):
        with pytest.raises(ExecutionRefused) as exc:
            build_fill_plan(book, side=side, shares=10.0)
        assert "CROSSED_OR_LOCKED" in exc.value.code


# ---------------------------------------------------------------------------
# ExecutionCost decomposition - no double counting
# ---------------------------------------------------------------------------


def _cost(filled: float = 100.0, fill: float = 50.0, fees: float = 1.25, buffer: float = 0.25) -> ExecutionCost:
    return ExecutionCost(
        filled_shares=filled, fill_cost_usd=fill, fees_usd=fees, execution_buffer_usd=buffer
    )


def test_total_is_the_sum_of_exactly_three_terms() -> None:
    c = _cost()
    assert c.total_cost_usd == pytest.approx(50.0 + 1.25 + 0.25)
    assert c.total_cost_usd == pytest.approx(51.5)


def test_fee_is_counted_once_and_only_once() -> None:
    """Shifting fee by d must shift the total by exactly d - never 2d or 0d."""
    base = _cost(fees=1.25)
    higher = _cost(fees=2.50)
    delta = higher.total_cost_usd - base.total_cost_usd
    assert delta == pytest.approx(1.25)
    assert delta == pytest.approx(higher.fees_usd - base.fees_usd)


def test_removing_the_fee_from_components_removes_exactly_the_fee_from_total() -> None:
    c = _cost()
    without_fee = c.total_cost_usd - c.fees_usd
    assert without_fee == pytest.approx(c.fill_cost_usd + c.execution_buffer_usd)


def test_cost_per_share_is_total_over_shares() -> None:
    c = _cost(filled=100.0, fill=50.0, fees=1.25, buffer=0.25)
    assert c.cost_usd_per_share == pytest.approx(51.5 / 100.0)
    assert c.cost_usd_per_share == pytest.approx(c.total_cost_usd / c.filled_shares)


def test_execution_cost_rejects_a_zero_fill() -> None:
    with pytest.raises(ValueError, match="fill_cost_usd"):
        ExecutionCost(filled_shares=10.0, fill_cost_usd=0.0, fees_usd=0.0, execution_buffer_usd=0.0)


def test_execution_cost_rejects_negative_component() -> None:
    with pytest.raises(ValueError):
        ExecutionCost(
            filled_shares=10.0, fill_cost_usd=5.0, fees_usd=-0.1, execution_buffer_usd=0.0
        )


# ---------------------------------------------------------------------------
# Expected net profit  (q * shares - cost)
# ---------------------------------------------------------------------------


def test_expected_net_profit_matches_the_spec_formula() -> None:
    q, shares = 0.60, 100.0
    cost = _cost(filled=shares, fill=50.0, fees=1.25, buffer=0.25)
    # q * shares = 60 USD of expected payout; cost = 51.5 USD
    assert expected_net_profit_usd(q=q, filled_shares=shares, cost=cost) == pytest.approx(60.0 - 51.5)
    assert expected_net_profit_usd(q=q, filled_shares=shares, cost=cost) == pytest.approx(8.5)


def test_per_share_profit_equals_total_divided_by_shares() -> None:
    q, shares = 0.73, 40.0
    cost = ExecutionCost(
        filled_shares=shares, fill_cost_usd=20.0, fees_usd=0.9, execution_buffer_usd=0.1
    )
    total = expected_net_profit_usd(q=q, filled_shares=shares, cost=cost)
    per_share = expected_net_profit_usd_per_share(q=q, filled_shares=shares, cost=cost)
    assert per_share == pytest.approx(total / shares)


def test_per_share_profit_equals_q_minus_cost_per_share() -> None:
    """The algebraic identity the spec's two lines imply - asserted, not assumed."""
    q, shares = 0.42, 250.0
    cost = ExecutionCost(
        filled_shares=shares, fill_cost_usd=80.0, fees_usd=2.5, execution_buffer_usd=1.5
    )
    per_share = expected_net_profit_usd_per_share(q=q, filled_shares=shares, cost=cost)
    assert per_share == pytest.approx(q - cost.cost_usd_per_share)


def test_edge_is_positive_exactly_when_cost_per_share_beats_q() -> None:
    shares = 100.0
    cheap = ExecutionCost(
        filled_shares=shares, fill_cost_usd=30.0, fees_usd=1.0, execution_buffer_usd=0.5
    )
    dear = ExecutionCost(
        filled_shares=shares, fill_cost_usd=70.0, fees_usd=3.0, execution_buffer_usd=1.0
    )
    q = 0.40
    assert edge_usd_per_share(q=q, filled_shares=shares, cost=cheap) > 0
    assert edge_usd_per_share(q=q, filled_shares=shares, cost=dear) < 0


def test_expected_profit_is_monotone_in_q() -> None:
    shares = 100.0
    cost = _cost(filled=shares, fill=50.0, fees=1.25, buffer=0.25)
    profits = [
        expected_net_profit_usd(q=q, filled_shares=shares, cost=cost)
        for q in (0.0, 0.1, 0.4, 0.7, 1.0)
    ]
    assert profits == sorted(profits)


def test_q_of_one_pays_out_every_share() -> None:
    """A certain win redeems at $1/share, so profit = shares - cost."""
    shares = 200.0
    cost = _cost(filled=shares, fill=100.0, fees=2.5, buffer=0.5)
    assert expected_net_profit_usd(q=1.0, filled_shares=shares, cost=cost) == pytest.approx(
        200.0 - 103.0
    )


def test_q_of_zero_loses_the_entire_cost() -> None:
    shares = 200.0
    cost = _cost(filled=shares, fill=100.0, fees=2.5, buffer=0.5)
    assert expected_net_profit_usd(q=0.0, filled_shares=shares, cost=cost) == pytest.approx(-103.0)


def test_probability_outside_unit_interval_is_a_bug_not_an_edge() -> None:
    cost = _cost()
    for bad in (-0.1, 1.2, float("nan")):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            expected_net_profit_usd(q=bad, filled_shares=100.0, cost=cost)


def test_per_share_requires_positive_shares() -> None:
    cost = _cost(filled=0.01, fill=0.005, fees=0.0, buffer=0.0)
    with pytest.raises(ValueError, match="> 0"):
        expected_net_profit_usd_per_share(q=0.5, filled_shares=0.0, cost=cost)


# ---------------------------------------------------------------------------
# End-to-end: book -> plan -> fee -> cost -> edge
# ---------------------------------------------------------------------------


def test_end_to_end_pricing_chain_is_consistent(make_book) -> None:
    book = make_book(bids=[(0.44, 1000.0)], asks=[(0.50, 100.0), (0.56, 900.0)])
    plan = build_fill_plan(book, side=Side.BUY, shares=150.0)

    fee = taker_fee_usd(
        filled_shares=plan.filled_shares,
        price_usd_per_share=plan.avg_price_usd_per_share,  # type: ignore[arg-type]
        fee_rate=0.05,
    )
    buffer = execution_buffer_usd(fill_cost_usd=plan.fill_cost_usd, buffer_fraction=0.005)
    cost = ExecutionCost(
        filled_shares=plan.filled_shares,
        fill_cost_usd=plan.fill_cost_usd,
        fees_usd=fee,
        execution_buffer_usd=buffer,
    )

    # Independent arithmetic: 100 @ 0.50 + 50 @ 0.56 = 50 + 28 = 78
    assert plan.fill_cost_usd == pytest.approx(78.0)

    # Fee is charged on the plan's volume-weighted average price (78/150 = 0.52),
    # i.e. on what the shares actually cost as a whole:
    #   150 * 0.05 * 0.52 * 0.48 = 1.872
    expected_fee = 150.0 * 0.05 * 0.52 * 0.48
    assert plan.avg_price_usd_per_share == pytest.approx(0.52)
    assert fee == pytest.approx(expected_fee, abs=1e-9)
    assert cost.total_cost_usd == pytest.approx(78.0 + expected_fee + 78.0 * 0.005)

    q = 0.55
    edge = edge_usd_per_share(q=q, filled_shares=plan.filled_shares, cost=cost)
    assert edge == pytest.approx(q - cost.cost_usd_per_share)
    assert expected_net_profit_usd(q=q, filled_shares=plan.filled_shares, cost=cost) == pytest.approx(
        edge * plan.filled_shares
    )

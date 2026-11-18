"""The typed research pricing contract: statuses, depth, staleness, NO vs SELL.

These fixtures are synthetic on purpose. Each test isolates one rule of
``docs/pricing-execution-contract.md`` so a failure names the rule, not a
market.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.execution.pricing import (
    ExecutionIntent,
    FeeRole,
    FeeSchedule,
    PricingStatus,
    adverse_slippage_usd_per_share,
    price_execution,
    sum_executable_expected_net_profit_usd,
)
from weatherquant.markets.models import BookLevel, OrderBook, Side
from weatherquant.performance.edge import daily_entry, observation_edge

DECISION = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _levels(*pairs: tuple[float, float]) -> tuple[BookLevel, ...]:
    return tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in pairs)


def _book(
    *,
    token_id: str = "yes-token",
    bids: tuple[tuple[float, float], ...] = ((0.40, 100.0),),
    asks: tuple[tuple[float, float], ...] = ((0.50, 100.0),),
    venue_ts: datetime | None = DECISION - timedelta(seconds=5),
) -> OrderBook:
    return OrderBook(
        market_id="market-1",
        token_id=token_id,
        bids=_levels(*bids),
        asks=_levels(*asks),
        venue_ts=venue_ts,
        fetched_at_utc=DECISION,
    )


def _taker(rate: float | None = 0.05) -> FeeSchedule:
    return FeeSchedule(role=FeeRole.TAKER, rate=rate)


def _price(book: OrderBook | None, *, intent: ExecutionIntent, shares: float, **kwargs):
    return price_execution(
        book,
        intent=intent,
        shares=shares,
        fee_schedule=kwargs.pop("fee_schedule", _taker()),
        decision_time=kwargs.pop("decision_time", DECISION),
        **kwargs,
    )


def _assert_no_usd(result) -> None:
    assert result.fees_usd is None
    assert result.execution_buffer_usd is None
    assert result.total_cost_usd is None
    assert result.net_proceeds_usd is None
    assert result.expected_net_profit_usd is None
    assert result.expected_net_profit_usd_per_share is None


# --- maker vs taker --------------------------------------------------------


def test_taker_pays_the_documented_fee_and_reports_a_cost() -> None:
    result = _price(_book(asks=((0.50, 100.0),)), intent=ExecutionIntent.BUY_YES, shares=100, q=0.6)

    assert result.status is PricingStatus.EXECUTABLE
    assert result.fees_usd == pytest.approx(1.25)  # 100 * 0.05 * 0.5 * 0.5
    assert result.fill is not None
    assert result.fill.complete
    assert result.total_cost_usd == pytest.approx(51.25)
    assert result.net_proceeds_usd is None
    assert result.expected_net_profit_usd == pytest.approx(60.0 - 51.25)
    assert result.expected_net_profit_usd_per_share == pytest.approx((60.0 - 51.25) / 100)


def test_a_reported_zero_maker_rate_is_known_and_costs_nothing() -> None:
    maker = FeeSchedule(role=FeeRole.MAKER, rate=0.0)
    result = _price(
        _book(asks=((0.50, 100.0),)),
        intent=ExecutionIntent.BUY_YES,
        shares=100,
        fee_schedule=maker,
    )

    assert result.status is PricingStatus.EXECUTABLE
    assert result.fees_usd == 0.0
    assert result.total_cost_usd == pytest.approx(50.0)


def test_a_nonzero_maker_rate_is_priced_not_assumed_away() -> None:
    maker = FeeSchedule(role=FeeRole.MAKER, rate=0.05)
    result = _price(
        _book(asks=((0.50, 100.0),)),
        intent=ExecutionIntent.BUY_YES,
        shares=100,
        fee_schedule=maker,
    )

    assert result.status is PricingStatus.EXECUTABLE
    assert result.fees_usd == pytest.approx(1.25)


@pytest.mark.parametrize("role", [FeeRole.MAKER, FeeRole.TAKER])
def test_an_unreported_fee_rate_is_fee_unknown_not_zero(role: FeeRole) -> None:
    result = _price(
        _book(),
        intent=ExecutionIntent.BUY_YES,
        shares=10,
        fee_schedule=FeeSchedule.unknown(role),
        q=0.9,
    )

    assert result.status is PricingStatus.FEE_UNKNOWN
    assert result.reasons == ("FEE_RATE_UNKNOWN",)
    assert result.fee_rate is None
    assert result.fill is None
    _assert_no_usd(result)


# --- missing book / empty side --------------------------------------------


def test_no_order_book_is_not_executable() -> None:
    result = _price(None, intent=ExecutionIntent.BUY_YES, shares=10, q=0.9)

    assert result.status is PricingStatus.NOT_EXECUTABLE
    assert result.reasons == ("NO_ORDER_BOOK",)
    assert result.fill is None
    _assert_no_usd(result)


@pytest.mark.parametrize(
    ("intent", "bids", "asks", "reason"),
    [
        (ExecutionIntent.BUY_YES, ((0.40, 100.0),), (), "NO_ASK_SIDE"),
        (ExecutionIntent.SELL_YES, (), ((0.50, 100.0),), "NO_BID_SIDE"),
        (ExecutionIntent.BUY_NO, ((0.60, 100.0),), (), "NO_ASK_SIDE"),
    ],
)
def test_an_empty_side_is_not_executable(
    intent: ExecutionIntent,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
    reason: str,
) -> None:
    result = _price(_book(bids=bids, asks=asks), intent=intent, shares=10)

    assert result.status is PricingStatus.NOT_EXECUTABLE
    assert result.reasons == (reason,)
    assert result.fill is None
    _assert_no_usd(result)


# --- depth walking --------------------------------------------------------


def test_depth_walk_reports_vwap_slippage_and_filled_size() -> None:
    book = _book(bids=((0.30, 100.0),), asks=((0.40, 100.0), (0.44, 100.0)))
    result = _price(book, intent=ExecutionIntent.BUY_YES, shares=150)

    assert result.status is PricingStatus.EXECUTABLE
    assert result.fill is not None
    assert result.fill.filled_shares == pytest.approx(150.0)
    assert result.fill.unfilled_shares == pytest.approx(0.0)
    assert result.fill.levels_consumed == 2
    assert result.fill.vwap_usd_per_share == pytest.approx(62.0 / 150.0)
    assert result.fill.top_of_book_price_usd_per_share == pytest.approx(0.40)
    assert result.fill.slippage_usd_per_share == pytest.approx(62.0 / 150.0 - 0.40)
    assert result.fill.slippage_usd == pytest.approx((62.0 / 150.0 - 0.40) * 150.0)


def test_insufficient_depth_is_partial_and_carries_no_cost_or_profit() -> None:
    book = _book(bids=((0.30, 100.0),), asks=((0.40, 100.0),))
    result = _price(book, intent=ExecutionIntent.BUY_YES, shares=250, q=0.9)

    assert result.status is PricingStatus.PARTIAL
    assert result.reasons == ("UNFILLED_REMAINDER",)
    assert result.fill is not None
    assert not result.fill.complete
    assert result.fill.filled_shares == pytest.approx(100.0)
    assert result.fill.unfilled_shares == pytest.approx(150.0)
    assert result.fill.vwap_usd_per_share == pytest.approx(0.40)
    _assert_no_usd(result)


def test_a_sell_walks_the_bid_side_and_reports_adverse_slippage() -> None:
    book = _book(bids=((0.40, 100.0), (0.36, 100.0)))
    result = _price(book, intent=ExecutionIntent.SELL_YES, shares=150)

    assert result.status is PricingStatus.EXECUTABLE
    assert result.fill is not None
    assert result.fill.vwap_usd_per_share == pytest.approx((40.0 + 18.0) / 150.0)
    assert result.fill.slippage_usd_per_share == pytest.approx(0.40 - 58.0 / 150.0)
    assert result.total_cost_usd is None
    assert result.net_proceeds_usd is not None


def test_adverse_slippage_has_a_direction() -> None:
    assert adverse_slippage_usd_per_share(
        side=Side.BUY, vwap_usd_per_share=0.43, top_of_book_usd_per_share=0.40
    ) == pytest.approx(0.03)
    assert adverse_slippage_usd_per_share(
        side=Side.SELL, vwap_usd_per_share=0.37, top_of_book_usd_per_share=0.40
    ) == pytest.approx(0.03)
    assert adverse_slippage_usd_per_share(
        side=Side.BUY, vwap_usd_per_share=0.40, top_of_book_usd_per_share=0.40
    ) == 0.0


# --- staleness ------------------------------------------------------------


def test_an_old_quote_is_stale_and_not_executable() -> None:
    book = _book(venue_ts=DECISION - timedelta(seconds=10))
    result = _price(book, intent=ExecutionIntent.BUY_YES, shares=10, max_quote_age_seconds=5.0)

    assert result.status is PricingStatus.STALE_QUOTE
    assert result.reasons == ("QUOTE_TOO_OLD",)
    assert result.quote_age_seconds == pytest.approx(10.0)
    assert result.fill is None
    _assert_no_usd(result)


def test_a_look_ahead_quote_is_stale() -> None:
    book = _book(venue_ts=DECISION + timedelta(seconds=1))
    result = _price(book, intent=ExecutionIntent.BUY_YES, shares=10)

    assert result.status is PricingStatus.STALE_QUOTE
    assert result.reasons == ("LOOK_AHEAD_QUOTE",)
    assert result.quote_age_seconds == pytest.approx(-1.0)
    _assert_no_usd(result)


def test_an_undated_quote_cannot_prove_freshness() -> None:
    book = _book(venue_ts=None)
    result = _price(book, intent=ExecutionIntent.BUY_YES, shares=10)

    assert result.status is PricingStatus.STALE_QUOTE
    assert result.reasons == ("QUOTE_AGE_UNDEFINED",)
    assert result.quote_age_seconds is None
    _assert_no_usd(result)


def test_staleness_is_checked_before_an_unknown_fee() -> None:
    book = _book(venue_ts=DECISION - timedelta(hours=2))
    result = _price(
        book,
        intent=ExecutionIntent.BUY_YES,
        shares=10,
        fee_schedule=FeeSchedule.unknown(FeeRole.TAKER),
    )

    assert result.status is PricingStatus.STALE_QUOTE
    assert result.reasons == ("QUOTE_TOO_OLD",)


# --- SELL YES vs BUY NO ---------------------------------------------------


def test_sell_yes_and_buy_no_are_priced_against_their_own_books() -> None:
    yes_book = _book(token_id="yes-token", bids=((0.40, 100.0),), asks=((0.42, 100.0),))
    no_book = _book(
        token_id="no-token",
        bids=((0.34, 100.0),),
        asks=((0.62, 30.0), (0.66, 100.0)),
    )

    sell_yes = _price(yes_book, intent=ExecutionIntent.SELL_YES, shares=50, q=0.30)
    buy_no = _price(no_book, intent=ExecutionIntent.BUY_NO, shares=50, q=0.70)

    assert sell_yes.status is PricingStatus.EXECUTABLE
    assert buy_no.status is PricingStatus.EXECUTABLE
    assert sell_yes.intent.token.value == "yes"
    assert buy_no.intent.token.value == "no"
    assert sell_yes.token_id != buy_no.token_id

    # SELL YES hits the YES bid at 0.40; BUY NO must pay the NO ask, which is
    # 0.62 - not the 0.60 that "1 - best YES bid" would have invented.
    assert sell_yes.fill is not None and buy_no.fill is not None
    assert sell_yes.vwap_usd_per_share == pytest.approx(0.40)
    assert buy_no.vwap_usd_per_share == pytest.approx(31.8 / 50.0)
    assert buy_no.vwap_usd_per_share != pytest.approx(1.0 - 0.40)
    assert buy_no.fill.levels_consumed == 2

    # Symmetric fee formula, asymmetric inputs: the two executions owe
    # different fees because their prices and depth differ.
    assert sell_yes.fees_usd == pytest.approx(0.6)
    assert buy_no.fees_usd != pytest.approx(sell_yes.fees_usd)
    assert sell_yes.net_proceeds_usd == pytest.approx(20.0 - 0.6)
    assert buy_no.total_cost_usd == pytest.approx(31.8 + buy_no.fees_usd)

    # q is the probability that *this* token wins.
    assert sell_yes.expected_net_profit_usd == pytest.approx(19.4 - 0.30 * 50.0)
    assert buy_no.expected_net_profit_usd == pytest.approx(0.70 * 50.0 - buy_no.total_cost_usd)


# --- Observation Edge is never a USD total --------------------------------


def test_usd_totals_only_sum_executable_results() -> None:
    executable = _price(_book(), intent=ExecutionIntent.BUY_YES, shares=100, q=0.9)
    partial = _price(_book(asks=((0.50, 10.0),)), intent=ExecutionIntent.BUY_YES, shares=100, q=0.9)

    assert executable.status is PricingStatus.EXECUTABLE
    assert partial.status is PricingStatus.PARTIAL
    assert sum_executable_expected_net_profit_usd([]) is None
    assert sum_executable_expected_net_profit_usd([executable]) == pytest.approx(
        executable.expected_net_profit_usd
    )

    with pytest.raises(ValueError, match="EXECUTABLE"):
        sum_executable_expected_net_profit_usd([executable, partial])


def test_an_executable_result_without_a_probability_cannot_enter_a_usd_total() -> None:
    priced_without_q = _price(_book(), intent=ExecutionIntent.BUY_YES, shares=10)

    assert priced_without_q.status is PricingStatus.EXECUTABLE
    with pytest.raises(ValueError, match="without a model probability"):
        sum_executable_expected_net_profit_usd([priced_without_q])


def test_observation_edge_is_a_per_share_sum_not_a_usd_total() -> None:
    rows = [
        {"state": "ACCEPT", "net_edge_usd": 0.01, "decided_at_utc": DECISION},
        {"state": "ACCEPT", "net_edge_usd": 0.03, "decided_at_utc": DECISION},
        {"state": "REJECT", "net_edge_usd": 9.99, "decided_at_utc": DECISION},
    ]
    payload = observation_edge(rows)

    assert payload["overall"]["count"] == 2
    assert payload["overall"]["sum"] == pytest.approx(math.fsum([0.01, 0.03]))
    assert payload["overall"]["mean"] == pytest.approx(math.fsum([0.01, 0.03]) / 2)

    def keys(value: object) -> list[str]:
        if isinstance(value, dict):
            return [str(k) for k in value] + [k for v in value.values() for k in keys(v)]
        if isinstance(value, list):
            return [k for v in value for k in keys(v)]
        return []

    # No field in the observation payload claims to be a cash total...
    assert not [k for k in keys(payload) if k.endswith("_usd")]
    # ...and the daily total is the same per-share sum, under its own name.
    day = daily_entry("2026-01-01", 2, math.fsum([0.01, 0.03]))
    assert day["observation_edge_sum"] == pytest.approx(0.04)
    assert "observation_edge_usd" not in day

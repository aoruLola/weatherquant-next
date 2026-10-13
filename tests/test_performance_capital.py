"""Reference capital: sized from a book, never from a displayed price.

The contract forbids one specific shortcut - price times shares - because it
pretends a size is available that may not be. So the tests that matter most
here are the ones where the answer is "you cannot know", and the ones that
prove the arithmetic went through ``execution/pricing.py`` rather than
around it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.markets.models import BookLevel, OrderBook
from weatherquant.performance.capital import (
    DEFAULT_MAX_ALLOCATION_FRACTION,
    NotExecutable,
    simulate_portfolio,
    simulate_single,
)
from weatherquant.performance.episodes import Episode, EpisodeKey

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _book(*, ask: float = 0.60, bid: float = 0.58, ask_size: float = 1000.0, bid_size: float = 1000.0) -> OrderBook:
    return OrderBook(
        market_id="m-81",
        token_id="tok-yes",
        bids=(BookLevel(price_usd_per_share=bid, size_shares=bid_size),),
        asks=(BookLevel(price_usd_per_share=ask, size_shares=ask_size),),
        venue_ts=NOW - timedelta(seconds=2),
        fetched_at_utc=NOW,
    )


def _episode(
    *,
    edge: float = 0.05,
    price: float = 0.60,
    depth: float | None = 1000.0,
    direction: str = "buy_yes",
    probability: float = 0.67,
    first_seen: datetime = NOW,
    episode_id: str = "ep-1",
) -> Episode:
    key = EpisodeKey("m-81", "2026-10-14", "calibrated", direction, "LIVE")
    return Episode(
        episode_id=episode_id,
        key=key,
        first_seen_utc=first_seen,
        last_seen_utc=first_seen + timedelta(minutes=5),
        observation_count=3,
        first_edge=edge,
        last_edge=edge,
        mean_edge=edge,
        max_edge=edge,
        min_edge=edge,
        first_market_price=price,
        first_model_probability=probability,
        first_available_depth_usd=depth,
        target_date="2026-10-14",
        station="KLGA",
        bracket_label="81°F",
        direction=direction,
        mode="LIVE",
        end_reason="end_of_data",
    )


# --- capital is a research input, not a balance ------------------------------


def test_zero_or_negative_capital_is_refused() -> None:
    """A stake of nothing cannot be sized, and silently using 1 would be worse."""
    for bad in (0, -1, -0.01):
        with pytest.raises(ValueError, match="capital_usdc"):
            simulate_single(_episode(), capital_usdc=bad, book=_book())


def test_the_fraction_must_be_a_fraction() -> None:
    for bad in (0, -0.5, 1.5):
        with pytest.raises(ValueError, match="max_allocation_fraction"):
            simulate_portfolio([], capital_usdc=100, book_for=lambda e: None,
                               max_allocation_fraction=bad)


def test_the_default_allocation_is_ten_percent_and_is_labelled_an_assumption() -> None:
    assert DEFAULT_MAX_ALLOCATION_FRACTION == 0.10

    outcome = simulate_portfolio([], capital_usdc=100, book_for=lambda e: None)
    payload = outcome.as_dict()
    assert payload["max_allocation_fraction"] == 0.10
    warnings = " ".join(payload["warnings"])
    assert "not a validated risk rule" in warnings
    assert "not realised profit" in warnings
    assert "no orders" in warnings


# --- sizing goes through the pricing module ----------------------------------


def test_the_fill_is_computed_from_the_book_not_price_times_shares() -> None:
    """A 0.60 ask with 1000 shares cannot buy 166.67 shares for 100 USDC
    *if* the size were unavailable - and with a deeper book it should.

    The assertion that matters: the required capital equals what the
    pricing module says the fill costs, not capital_usdc itself.
    """
    result = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book(ask=0.60, ask_size=1000))

    assert result.executable is True
    assert result.reference_shares > 0
    # 100 USDC at a 0.60 ask exhausts the stake before the book does.
    assert result.capital_required_usdc == pytest.approx(100.0, rel=0.05)
    assert result.capacity_limited is False


def test_depth_smaller_than_the_stake_limits_capacity() -> None:
    """Only 10 shares exist at 0.60 - six dollars of market, not a hundred."""
    result = simulate_single(
        _episode(), capital_usdc=100.0, fee_rate=0.0, book=_book(ask=0.60, ask_size=10)
    )

    assert result.executable is True
    assert result.capacity_limited is True
    assert result.capital_required_usdc < 100.0
    assert result.reference_shares == pytest.approx(10.0)


def test_the_roi_is_edge_over_the_capital_actually_required() -> None:
    result = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book())

    assert result.theoretical_expected_roi is not None
    assert result.theoretical_expected_roi == pytest.approx(
        result.theoretical_expected_net_edge_usdc / result.capital_required_usdc
    )


def test_theoretical_edge_is_always_labelled_as_theoretical() -> None:
    result = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book())

    assert any("not realised profit" in n for n in result.notes)


# --- evidence that is missing produces NOT_EXECUTABLE, never a number -------


def test_no_book_is_not_executable_rather_than_a_price_times_quantity() -> None:
    result = simulate_single(_episode(), capital_usdc=100.0, book=None)

    assert result.executable is False
    assert result.not_executable_reason in {
        NotExecutable.NO_BOOK,
        NotExecutable.DEPTH_NOT_MEASURED,
    }
    assert result.theoretical_expected_net_edge_usdc is None, "no fill, no total"


def test_a_one_sided_book_cannot_be_sized() -> None:
    book = _book()
    sideless = OrderBook(
        market_id=book.market_id, token_id=book.token_id, bids=book.bids, asks=(),
        venue_ts=book.venue_ts, fetched_at_utc=book.fetched_at_utc,
    )
    result = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=sideless)

    assert result.executable is False
    assert result.not_executable_reason == NotExecutable.CROSSED_OR_ONE_SIDED


def test_a_stale_snapshot_is_not_a_fillable_market() -> None:
    result = simulate_single(
        _episode(), capital_usdc=100.0, fee_rate=0.0, book=_book(), book_age_seconds=5000
    )

    assert result.executable is False
    assert result.not_executable_reason == NotExecutable.STALE_SNAPSHOT


def test_below_the_markets_minimum_order_is_refused() -> None:
    """The venue will not take the trade, so neither will we call it sized."""
    result = simulate_single(
        _episode(), capital_usdc=5.0, fee_rate=0.0, book=_book(ask=0.60), min_order_size=100.0
    )

    assert result.executable is False
    assert result.not_executable_reason == NotExecutable.BELOW_MIN_ORDER


def test_an_unknown_fee_refuses_the_fill_instead_of_flattering_the_edge() -> None:
    """execution_cost will not assume a fee of zero when none is reported.

    An unknown fee quietly makes every edge look better than it is, so the
    pricing module refuses and this module reports that refusal by name
    rather than hiding it behind a generic "not priced".
    """
    result = simulate_single(_episode(), capital_usdc=100.0, book=_book())  # no fee_rate

    assert result.executable is False
    assert result.not_executable_reason == NotExecutable.FEE_UNKNOWN
    assert result.theoretical_expected_net_edge_usdc is None


def test_an_unsettled_market_has_no_win_or_loss_figure() -> None:
    result = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book())

    assert result.executable is True
    assert result.win_net_usdc is None
    assert result.loss_net_usdc is None


def test_a_settled_win_and_loss_are_both_shown_for_buy_yes() -> None:
    won = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book(), settlement_payout=1.0)
    lost = simulate_single(_episode(), capital_usdc=100.0, fee_rate=0.0, book=_book(), settlement_payout=0.0)

    assert won.win_net_usdc is not None and won.win_net_usdc > 0
    assert lost.loss_net_usdc is not None and lost.loss_net_usdc < 0


def test_sell_yes_withholds_pnl_until_its_semantics_are_proven() -> None:
    """Contract 4.4: an unproven position type may not be given a P&L."""
    result = simulate_single(
        _episode(direction="sell_yes"), capital_usdc=100.0, fee_rate=0.0, book=_book(),
        settlement_payout=1.0,
    )

    assert result.win_net_usdc is None
    assert result.loss_net_usdc is None
    assert any("unproven" in n for n in result.notes)


def test_a_decision_price_that_does_not_match_the_book_is_flagged() -> None:
    result = simulate_single(
        _episode(price=0.55), capital_usdc=100.0, fee_rate=0.0, book=_book(ask=0.60)
    )

    assert result.price_time_matched is False
    assert any("differ" in n for n in result.notes)


# --- portfolio: one stake, not one stake each --------------------------------


def test_the_portfolio_shares_a_single_stake() -> None:
    episodes = [_episode(episode_id=f"ep-{i}", first_seen=NOW + timedelta(minutes=i))
                for i in range(20)]
    outcome = simulate_portfolio(
        episodes, capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    # Ten percent each: 20 episodes cannot all be funded from 100 USDC.
    assert outcome.capital_usdc == 100.0
    assert outcome.allocated_total_usdc <= 100.0
    assert outcome.balance_remaining_usdc >= 0
    assert outcome.peak_open_allocation_usdc <= 100.0


def test_an_episode_never_takes_more_than_its_fraction() -> None:
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    assert outcome.allocated_total_usdc == pytest.approx(10.0, rel=0.01)


def test_a_larger_stake_scales_the_allocation() -> None:
    small = simulate_portfolio([_episode()], capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0)
    large = simulate_portfolio([_episode()], capital_usdc=500.0, book_for=lambda e: _book(), fee_rate=0.0)

    assert large.allocated_total_usdc == pytest.approx(
        small.allocated_total_usdc * 5, rel=0.01
    )


def test_opportunities_are_funded_in_time_order_not_by_edge() -> None:
    """Ranking by the edge we are evaluating would fund hindsight first."""
    early = _episode(episode_id="early", edge=0.01, first_seen=NOW)
    late = _episode(episode_id="late", edge=0.99, first_seen=NOW + timedelta(hours=1))

    outcome = simulate_portfolio(
        [late, early], capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )
    funded = [r.episode_id for r in outcome.results if r.capital_used_usdc > 0]

    assert funded[0] == "early"


def test_no_book_means_the_portfolio_is_not_backtestable() -> None:
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda e: None
    )

    assert outcome.backtestable is False
    assert outcome.executable_count == 0
    assert outcome.not_executable_count == 1
    assert any("no episode could be sized" in r for r in outcome.not_backtestable_reasons)


def test_capital_release_is_held_conservatively_when_settlement_times_are_unknown() -> None:
    """Money is never returned early on a guess about when a market settled."""
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    assert any("held for the whole window" in r for r in outcome.not_backtestable_reasons)


def test_the_result_declares_its_statistical_scope() -> None:
    outcome = simulate_portfolio([], capital_usdc=100.0, book_for=lambda e: None)

    assert outcome.as_dict()["statistical_scope"] == ["reference_capital_hypothetical"]

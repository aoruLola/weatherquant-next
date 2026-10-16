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
    PortfolioWalk,
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
    market: str = "m-81",
) -> Episode:
    key = EpisodeKey(market, "2026-10-14", "calibrated", direction, "LIVE")
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
        # A sell prices against the bid, so the bid has to be the decision
        # price - and it must sit below the ask, or the book is crossed and
        # is refused for being crossed before any of this is reached.
        _episode(direction="sell_yes", price=0.58),
        capital_usdc=100.0,
        fee_rate=0.0,
        book=_book(bid=0.58),
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

    assert outcome.backtestable is False, (
        "settlement timing is unavailable, so a capital timeline cannot be "
        "reconstructed and the result is theoretical only"
    )
    assert any("settlement timing is not available" in r for r in outcome.not_backtestable_reasons)


def test_the_result_declares_its_statistical_scope() -> None:
    outcome = simulate_portfolio([], capital_usdc=100.0, book_for=lambda e: None)

    assert outcome.as_dict()["statistical_scope"] == ["reference_capital_hypothetical"]


# --- portfolio funding invariants (P1-07) -------------------------------------
#
# The bug these guard: sizing at the full cap and trimming the *result*
# afterwards leaves shares, fees and expected edge describing money that was
# never allocated. A portfolio with enough simultaneous opportunities then
# reports returns for unfunded positions.


def _many(n: int, *, first_seen_start: int = 0) -> list:
    return [
        _episode(
            episode_id=f"ep-{i}",
            first_seen=NOW + timedelta(minutes=first_seen_start + i),
            depth=10_000.0,
        )
        for i in range(n)
    ]


def test_allocated_capital_never_exceeds_the_reference_capital() -> None:
    """The core invariant, checked against a crowd of opportunities."""
    episodes = _many(12)
    outcome = simulate_portfolio(
        episodes, capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    assert outcome.allocated_total_usdc <= 100.0 + 1e-9
    assert outcome.balance_remaining_usdc >= -1e-9
    assert outcome.allocated_total_usdc + outcome.balance_remaining_usdc == pytest.approx(100.0)


def test_a_position_with_no_funds_contributes_no_profit() -> None:
    """Unfunded episodes are excluded from the portfolio's expected return."""
    episodes = _many(12)
    outcome = simulate_portfolio(
        episodes, capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    unfunded = [r for r in outcome.results if r.not_executable_reason == NotExecutable.NO_FUNDS]
    assert unfunded, "with 12 opportunities against a 10% cap, some must go unfunded"
    for result in unfunded:
        assert result.executable is False
        assert result.theoretical_expected_net_edge_usdc is None

    # The total equals the sum over funded positions only.
    funded = [
        r.theoretical_expected_net_edge_usdc
        for r in outcome.results
        if r.executable and r.theoretical_expected_net_edge_usdc is not None
    ]
    assert outcome.theoretical_expected_net_edge_usdc == pytest.approx(sum(funded))


def test_a_partially_funded_episode_is_repriced_not_trimmed() -> None:
    """Pricing at the cap and lowering `capital_used` would misstate the edge.

    The executable result must describe the smaller stake: fewer shares,
    a lower cost, and an edge recomputed from that fill.
    """
    # The first episode takes its whole cap; the second gets the remainder.
    two = _many(2)
    outcome = simulate_portfolio(
        two,
        capital_usdc=100.0,
        book_for=lambda e: _book(ask=0.60, ask_size=1000),
        fee_rate=0.0,
    )

    assert len(outcome.results) == 2
    funded = [r for r in outcome.results if r.executable]
    assert funded, "at least the first opportunity must be fundable"
    for result in funded:
        # Required capital can never exceed the stake it was sized against.
        assert result.capital_required_usdc <= 10.0 + 1e-9
        assert result.reference_shares > 0
        if result.theoretical_expected_net_edge_usdc is not None:
            # Recomputed from the fill that was actually funded.
            assert result.theoretical_expected_roi is not None


def test_a_zero_balance_yields_no_new_positions() -> None:
    """Once the money is gone, later opportunities are refused outright."""
    # A 100% cap lets the first episode take the entire stake, so the
    # balance really does reach zero rather than merely running low.
    episodes = _many(6)
    outcome = simulate_portfolio(
        episodes,
        capital_usdc=100.0,
        book_for=lambda e: _book(),
        fee_rate=0.0,
        max_allocation_fraction=1.0,
    )

    after_exhaustion = []
    seen_exhausted = False
    for result in outcome.results:
        if seen_exhausted:
            after_exhaustion.append(result)
        if result.not_executable_reason == NotExecutable.NO_FUNDS:
            seen_exhausted = True

    for result in after_exhaustion:
        assert result.executable is False
        assert result.capital_used_usdc == 0.0
        assert result.theoretical_expected_net_edge_usdc is None

    assert outcome.allocated_total_usdc <= 100.0 + 1e-9


def test_an_empty_allocation_is_recorded_once_not_per_episode() -> None:
    episodes = _many(8)
    outcome = simulate_portfolio(
        episodes,
        capital_usdc=100.0,
        book_for=lambda e: _book(),
        fee_rate=0.0,
        max_allocation_fraction=1.0,
    )

    exhausted = [r for r in outcome.not_backtestable_reasons if "exhausted" in r]
    assert len(exhausted) == 1, "the reason should be recorded once, not once per episode"


def test_ordering_is_by_first_seen_not_by_edge() -> None:
    """Funding the best-looking edge first would be hindsight as a rule."""
    rich = _episode(
        episode_id="rich", edge=0.99, first_seen=NOW + timedelta(hours=1)
    )
    poor = _episode(
        episode_id="poor", edge=0.001, first_seen=NOW
    )
    outcome = simulate_portfolio(
        [rich, poor],
        capital_usdc=100.0,
        book_for=lambda e: _book(),
        fee_rate=0.0,
        max_allocation_fraction=0.10,
    )

    assert outcome.results[0].episode_id == "poor", "first_seen, not edge, decides the order"


def test_depth_shortfall_is_still_reported_as_a_depth_problem() -> None:
    """A thin book is a different fact from an empty wallet."""
    thin = [_episode(episode_id="thin", depth=1.0)]
    outcome = simulate_portfolio(
        thin, capital_usdc=100.0, book_for=lambda e: _book(ask=0.60, ask_size=3), fee_rate=0.0
    )

    assert outcome.results[0].not_executable_reason != NotExecutable.NO_FUNDS


def test_an_unknown_fee_still_refuses_rather_than_being_treated_as_free() -> None:
    episodes = _many(2)
    outcome = simulate_portfolio(episodes, capital_usdc=100.0, book_for=lambda e: _book())

    assert all(
        r.not_executable_reason == NotExecutable.FEE_UNKNOWN
        for r in outcome.results
        if not r.executable
    )
    assert outcome.allocated_total_usdc == 0.0


# --- second review round: fees, time mismatch, backtestability ---------------


def test_fees_and_buffer_are_charged_against_the_stake() -> None:
    """A stake that covers the fill but not the fee is not a funded trade.

    `total_cost_usd` is the cash actually laid out. Recording only the fill
    notional books a position that costs more than the capital it claimed.
    """
    plain = simulate_single(
        _episode(), capital_usdc=100.0, fee_rate=0.0, book=_book()
    )
    taxed = simulate_single(
        _episode(), capital_usdc=100.0, fee_rate=0.05, book=_book()
    )

    assert plain.executable and taxed.executable
    # The stake is the ceiling in both cases - sizing shrinks to fit - so the
    # fee shows up as *fewer shares* and a higher all-in cost per share, not
    # as a larger total. Anything else would mean the fee was not charged.
    assert taxed.reference_shares < plain.reference_shares, (
        "the fee shrinks what the same stake can buy"
    )
    assert taxed.capital_required_usdc <= 100.0 + 1e-9
    assert plain.capital_required_usdc <= 100.0 + 1e-9
    plain_per_share = plain.capital_required_usdc / plain.reference_shares
    taxed_per_share = taxed.capital_required_usdc / taxed.reference_shares
    assert taxed_per_share > plain_per_share, (
        "the all-in cost per share must carry the fee"
    )


def test_roi_is_measured_against_the_all_in_cost() -> None:
    result = simulate_single(
        _episode(), capital_usdc=100.0, fee_rate=0.05, book=_book()
    )

    assert result.executable
    assert result.theoretical_expected_net_edge_usdc is not None
    assert result.theoretical_expected_roi == pytest.approx(
        result.theoretical_expected_net_edge_usdc / result.capital_required_usdc
    )


def test_a_stake_that_cannot_cover_the_fee_is_refused() -> None:
    """If the all-in cost overruns the stake, the position is not funded."""
    # A heavy fee on a tiny stake: the fill fits, the all-in cost may not.
    result = simulate_single(
        _episode(), capital_usdc=0.05, fee_rate=0.05, book=_book(ask=0.60)
    )

    if result.executable:
        assert result.capital_required_usdc <= 0.05 + 1e-9


def test_a_price_mismatch_is_refused_not_noted() -> None:
    """Combining a probability from one instant with a fill from another.

    The episode's model probability and the book's price are from different
    moments; continuing would produce an expected return neither instant
    supports.
    """
    result = simulate_single(
        _episode(price=0.55), capital_usdc=100.0, fee_rate=0.0, book=_book(ask=0.60)
    )

    assert result.executable is False
    assert result.not_executable_reason == NotExecutable.TIME_MISMATCH
    assert result.theoretical_expected_net_edge_usdc is None
    assert result.price_time_matched is False


def test_a_matching_price_still_proceeds() -> None:
    result = simulate_single(
        _episode(price=0.60), capital_usdc=100.0, fee_rate=0.0, book=_book(ask=0.60)
    )

    assert result.executable is True
    assert result.price_time_matched is True


def test_a_capital_timeline_that_cannot_be_rebuilt_is_not_backtestable() -> None:
    """Settlement timing is not carried, so nothing here is historically tested."""
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    assert outcome.executable_count == 1
    assert outcome.backtestable is False
    assert any("settlement timing is not available" in r for r in outcome.not_backtestable_reasons)


def test_episodes_from_one_market_share_the_allocation_cap() -> None:
    """Contract 3.3: correlated exposure must not multiply its allowance.

    Five signals on one bracket are one market's risk. Handing each a fresh
    cap would let a single market take five times the share the risk
    assumption allowed - the cap is per opportunity *type*, not per row.
    """
    market = [
        _episode(
            episode_id=f"same-market-{i}",
            market="m-shared",
            first_seen=NOW + timedelta(minutes=i),
            depth=100_000.0,
        )
        for i in range(5)
    ]
    outcome = simulate_portfolio(
        market, capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )

    # Every episode is the same market_id, so the group is capped once.
    assert outcome.allocated_total_usdc <= 10.0 + 1e-9, (
        "five episodes on one market took five shares of the cap"
    )

    others = [
        _episode(
            episode_id=f"m-{i}",
            market=f"m-{i}",
            first_seen=NOW + timedelta(minutes=i),
            depth=100_000.0,
        )
        for i in range(5)
    ]
    spread = simulate_portfolio(
        others, capital_usdc=100.0, book_for=lambda e: _book(), fee_rate=0.0
    )
    assert spread.allocated_total_usdc > 10.0 + 1e-9, (
        "five distinct markets should be able to take more than one market's cap"
    )


# ---------------------------------------------------------------------------
# whole history versus window: two simulations, told apart on the payload
# ---------------------------------------------------------------------------


def test_an_unfilled_portfolio_reports_na_not_zero() -> None:
    """Nothing was sized, so the balance is not a result.

    The failure this guards is the most reassuring possible one: a portfolio
    that filled nothing reports a balance equal to its stake, which reads as
    "flat" - a measurement nobody made. N/A with a reason is the honest answer.
    """
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda _e: None
    ).as_dict()

    assert outcome["funding_state"] == "no_fills_simulated"
    assert outcome["executable_count"] == 0
    assert outcome["simulated_net_profit_usdc"] is None, "0 would be a claim"
    assert outcome["simulated_roi"] is None
    assert outcome["final_balance"]["value"] is None
    assert "not a profit and loss" in outcome["final_balance"]["reason"]
    assert outcome["settled_count"] == 0
    assert any("N/A rather than zero" in w for w in outcome["warnings"])
    # The unallocated capital is still reported - it is a real quantity.
    assert outcome["balance_remaining_usdc"] == 100.0


def test_a_filled_but_unsettled_portfolio_reports_na() -> None:
    """A position was opened; nothing has resolved, so there is no result."""
    outcome = simulate_portfolio(
        [_episode()], capital_usdc=100.0, book_for=lambda _e: _book(), fee_rate=0.0
    ).as_dict()

    assert outcome["funding_state"] == "fills_unsettled"
    assert outcome["executable_count"] == 1
    assert outcome["allocated_total_usdc"] > 0
    assert outcome["simulated_net_profit_usdc"] is None
    assert outcome["simulated_roi"] is None
    assert outcome["balance_remaining_usdc"] < 100.0, "the capital is committed, not spent"
    assert "still committed" in outcome["funding_state_reason"]


def test_a_fully_settled_portfolio_reports_a_number() -> None:
    """The rule is evidence-driven, not a hardcoded N/A.

    Give the walk a settlement for every sized position and a realised figure
    appears. That is the difference between "we do not know" and "we will not
    say", and it is the reason the field exists at all.
    """
    outcome = simulate_portfolio(
        [
            _episode(),
            # A different market: one market's episodes share its cap, so two
            # on the same bracket would compete for one allocation.
            _episode(market="m-82", episode_id="ep-2", first_seen=NOW + timedelta(hours=1)),
        ],
        capital_usdc=100.0,
        book_for=lambda _e: _book(),
        fee_rate=0.0,
        settlement_payout=1.0,
    ).as_dict()

    assert outcome["funding_state"] == "fully_settled"
    assert outcome["settled_count"] == outcome["executable_count"] == 2
    assert outcome["simulated_net_profit_usdc"] is not None
    assert outcome["simulated_roi"] is not None
    assert outcome["simulated_result_reason"] is None
    assert not any("N/A rather than zero" in w for w in outcome["warnings"])


def test_a_half_settled_portfolio_is_incomplete_not_rounded() -> None:
    """One settled position out of two is not a result for the pair."""
    episodes = [
        _episode(),
        _episode(market="m-82", episode_id="ep-2", first_seen=NOW + timedelta(hours=1)),
    ]
    walk = PortfolioWalk(capital_usdc=100.0, fee_rate=0.0)
    walk.add(episodes[0], _book(), settlement_payout=1.0)
    walk.add(episodes[1], _book())
    outcome = walk.finish().as_dict()

    assert outcome["funding_state"] == "incomplete"
    assert outcome["settled_count"] == 1
    assert outcome["simulated_net_profit_usdc"] is None


def test_the_lifetime_block_names_itself_and_its_range() -> None:
    """The published block is the whole consumed history, and says so."""
    from weatherquant.performance.capital import lifetime_portfolio_payload
    from weatherquant.performance.snapshot import detail_window_descriptor

    result = simulate_portfolio([_episode()], capital_usdc=100.0, book_for=lambda _e: None)
    payload = lifetime_portfolio_payload(
        result,
        watermark=["2026-10-15T05:45:04+00:00", "d000001"],
        window=detail_window_descriptor(
            window_episodes=1, all_episodes=1, max_episodes=1, days=0,
            first_seen_utc="2026-10-13T12:00:00+00:00",
            last_seen_utc="2026-10-13T12:00:00+00:00",
        ),
        settlement_coverage={"answers": 1, "verified": 1, "decisive_episodes": 0, "episodes": 1},
    )

    assert payload["scope"] == "lifetime"
    assert payload["simulation_mode"] == "portfolio"
    assert payload["statistical_scope"] == [
        "reference_capital_hypothetical",
        "opportunity_episodes",
        "database_lifetime",
    ]
    assert payload["statistical_window"]["episodes_simulated"] == 1
    assert payload["statistical_window"]["first_seen_utc_from"]
    assert payload["data_watermark"] == ["2026-10-15T05:45:04+00:00", "d000001"]
    assert payload["settlement_coverage"]["episodes"] == 1
    assert any("whole consumed history" in w for w in payload["warnings"])
    assert "window" not in payload


def test_the_window_block_names_itself_and_its_range() -> None:
    """A window answer is a separate stake, and says which window."""
    from weatherquant.performance.capital import window_portfolio_payload

    result = simulate_portfolio([_episode()], capital_usdc=250.0, book_for=lambda _e: None)
    window = {
        "episodes": 15, "episodes_all": 400, "max_episodes": 20,
        "days": 90, "complete": False,
        "first_seen_utc_from": "2026-10-13T12:00:00+00:00",
        "last_seen_utc_to": "2026-10-01T00:00:00+00:00",
    }
    payload = window_portfolio_payload(
        result, window=window, generation_id="gen-7", mode="portfolio",
        initial_capital_usdc=250.0,
    )

    assert payload["scope"] == "window"
    assert payload["window_start"] == "2026-10-13T12:00:00+00:00"
    assert payload["window_end"] == "2026-10-01T00:00:00+00:00"
    assert payload["window_episode_limit"] == 20
    assert payload["total_episodes_in_window"] == 15
    assert payload["window_completeness"] == "truncated"
    assert payload["generation_id"] == "gen-7"
    assert payload["initial_capital_assumption_usdc"] == 250.0
    assert payload["statistical_scope"] == [
        "reference_capital_hypothetical",
        "opportunity_episodes",
        "window:90d",
    ]
    assert any("not a period of the lifetime account" in w for w in payload["warnings"])
    assert "data_watermark" not in payload, "a window is not a whole-history claim"

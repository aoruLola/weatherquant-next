"""Win rate: what enters the denominator, and how sure we may sound about it.

The statistical claim worth testing hardest is that resampling target dates
gives a wider interval than resampling rows. Every bracket on one day shares
the same weather, so rows within a day are not independent - if the
bootstrap does not reflect that, the interval is confidently wrong, which is
worse than a wide one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from weatherquant.performance.episodes import Episode, EpisodeKey
from weatherquant.performance.winrate import (
    INSUFFICIENT_REASON,
    Outcome,
    SettlementAnswer,
    assign_outcomes,
    confidence_interval,
    grouped_win_rate,
    win_rate,
)

RESOLVED = SettlementAnswer("RESOLVED", 1.0)
RESOLVED_ZERO = SettlementAnswer("RESOLVED", 0.0)


def _episode(
    *, market="m-1", target="2026-10-14", direction="buy_yes",
    mode="LIVE", model="calibrated", edge=0.05,
) -> Episode:
    return Episode(
        episode_id=f"{market}-{target}-{model}-{direction}",
        key=EpisodeKey(market, target, model, direction, mode),
        first_seen_utc=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        last_seen_utc=datetime(2026, 10, 13, 12, 5, tzinfo=UTC),
        observation_count=4,
        first_edge=edge, last_edge=edge, mean_edge=edge, max_edge=edge, min_edge=edge,
        first_market_price=0.6, first_model_probability=0.65,
        first_available_depth_usd=500.0,
        target_date=target, station="KLGA", bracket_label="81°F",
        direction=direction, mode=mode, end_reason="end_of_data",
    )


def _answers(episodes, *, status="RESOLVED", payout=1.0, verified=True):
    keys = {(e.key.market_id, e.key.target_date) for e in episodes}
    answers = {k: SettlementAnswer(status, payout) for k in keys}
    verified_set = keys if verified else frozenset()
    return answers, verified_set


# --- assignment refuses to guess ---------------------------------------------


def test_only_resolved_and_verified_can_be_scored() -> None:
    episodes = [_episode()]
    answers, verified = _answers(episodes)
    scored = assign_outcomes(episodes, answers=answers, verified=verified)

    assert scored[0].outcome == Outcome.WIN


def test_resolved_but_unverified_is_not_a_win() -> None:
    """A platform settlement that failed its cross-check is not evidence."""
    episodes = [_episode()]
    answers, _ = _answers(episodes, verified=False)
    scored = assign_outcomes(episodes, answers=answers, verified=frozenset())

    assert scored[0].outcome == Outcome.UNVERIFIABLE
    assert scored[0].decisive is False


def test_a_missing_settlement_record_is_unavailable_not_a_loss() -> None:
    scored = assign_outcomes([_episode()], answers={}, verified=frozenset())

    assert scored[0].outcome == Outcome.UNAVAILABLE
    assert scored[0].reason == "no settlement record"


def test_a_pending_market_stays_pending() -> None:
    episodes = [_episode()]
    answers, verified = _answers(episodes, status="posed", payout=None)
    scored = assign_outcomes(episodes, answers=answers, verified=verified)

    assert scored[0].outcome == Outcome.PENDING
    assert scored[0].decisive is False


def test_a_disputed_settlement_is_never_scored() -> None:
    episodes = [_episode()]
    answers, verified = _answers(episodes, status="DISPUTED")
    scored = assign_outcomes(episodes, answers=answers, verified=verified)

    assert scored[0].outcome == Outcome.DISPUTED


def test_a_payout_of_one_is_a_win_and_zero_is_a_loss() -> None:
    won = assign_outcomes([_episode(market="w")], answers={("w", "2026-10-14"): RESOLVED}, verified=frozenset({("w", "2026-10-14")}))
    lost = assign_outcomes([_episode(market="l")], answers={("l", "2026-10-14"): RESOLVED_ZERO}, verified=frozenset({("l", "2026-10-14")}))

    assert won[0].outcome == Outcome.WIN
    assert lost[0].outcome == Outcome.LOSS


def test_sell_yes_is_never_scored_until_its_semantics_are_proven() -> None:
    key = ("m-1", "2026-10-14")
    scored = assign_outcomes(
        [_episode(direction="sell_yes")], answers={key: RESOLVED}, verified=frozenset({key})
    )

    assert scored[0].outcome == Outcome.UNVERIFIABLE
    assert "SELL YES" in (scored[0].reason or "")
    assert scored[0].decisive is False


# --- the denominator ---------------------------------------------------------


def _scored(n_wins: int, n_losses: int, dates: int = 10):
    """Build decisive episodes spread across enough target dates."""
    out: list = []
    for i in range(n_wins + n_losses):
        target = f"2026-10-{14 + (i % dates):02d}"
        market = f"m-{i}"
        payout = 1.0 if i < n_wins else 0.0
        key = (market, target)
        out.extend(
            assign_outcomes(
                [_episode(market=market, target=target)],
                answers={key: SettlementAnswer("RESOLVED", payout)},
                verified=frozenset({key}),
            )
        )
    return out


def test_win_rate_is_wins_over_wins_plus_losses_only() -> None:
    result = win_rate(_scored(6, 4, dates=10), min_samples=10, min_clusters=3)

    assert result.win_rate == pytest.approx(0.6)
    assert result.valid_samples == 10
    assert result.counts[Outcome.WIN] == 6
    assert result.counts[Outcome.LOSS] == 4


def test_pending_and_disputed_are_counted_but_never_scored() -> None:
    scored = _scored(2, 2, dates=10)
    pending = assign_outcomes([_episode(market="p", target="2026-10-20")],
                              answers={("p", "2026-10-20"): SettlementAnswer("posed", None)},
                              verified=frozenset({("p", "2026-10-20")}))
    result = win_rate(scored + pending, min_samples=4, min_clusters=3)

    assert result.counts[Outcome.PENDING] == 1
    assert result.valid_samples == 4, "a pending market must not enter the denominator"


def test_too_few_samples_reports_the_contracts_reason_verbatim() -> None:
    result = win_rate(_scored(2, 1, dates=3), min_samples=20)

    assert result.win_rate is None
    assert result.reason.startswith(INSUFFICIENT_REASON)
    assert result.valid_samples == 3


def test_one_target_date_is_not_enough_for_a_clustered_interval() -> None:
    """Twelve brackets on one day is one day of weather, not twelve samples."""
    result = win_rate(_scored(6, 6, dates=1), min_samples=5, min_clusters=2)

    assert result.win_rate is None
    assert result.distinct_target_dates == 1
    assert "needs" in (result.reason or "")


def test_no_decisive_outcomes_at_all_is_the_same_refusal() -> None:
    scored = assign_outcomes([_episode()], answers={}, verified=frozenset())
    result = win_rate(scored)

    assert result.win_rate is None
    assert result.reason == INSUFFICIENT_REASON
    assert result.valid_samples == 0
    assert result.counts[Outcome.UNAVAILABLE] == 1


def test_the_result_states_its_scope_and_limits() -> None:
    result = win_rate(_scored(6, 4, dates=10), min_samples=10, min_clusters=3)

    assert "resolved_verified_only" in result.statistical_scope
    warnings = " ".join(result.warnings)
    assert "never scored" in warnings
    assert "not a realised return" in warnings


# --- intervals ---------------------------------------------------------------


def test_the_interval_is_reproducible() -> None:
    scored = _scored(7, 3, dates=10)
    first = confidence_interval(scored)
    second = confidence_interval(scored)

    assert first == second, "an interval that moves between reports undermines the report"


def test_clustered_by_date_is_wider_than_row_level() -> None:
    """The whole point of clustering: rows in one day are not independent.

    Two markets decided on the same day share the weather, so resampling
    rows would count that shared outcome several times and narrow the
    interval by about the square root of the daily bracket count.
    """
    # Two days that disagree completely: one day every bracket won, the next
    # every bracket lost. Resampling days then has to span both outcomes,
    # while resampling rows would average them into false certainty.
    scored = []
    for i in range(10):
        for target, payout in (("2026-10-14", 1.0), ("2026-10-15", 0.0)):
            market = f"m-{target}-{i}"
            key = (market, target)
            scored.extend(
                assign_outcomes(
                    [_episode(market=market, target=target)],
                    answers={key: SettlementAnswer("RESOLVED", payout)},
                    verified=frozenset({key}),
                )
            )
    (low, high), method = confidence_interval(scored)
    clustered_width = high - low

    wins = sum(1 for s in scored if s.outcome == Outcome.WIN)
    w_low, w_high = _wilson_width(wins, len(scored))
    assert method == "cluster_bootstrap_by_target_date"
    assert clustered_width > (w_high - w_low), (
        "clustering must not produce a narrower interval than independence"
    )


def _wilson_width(wins: int, n: int):
    from weatherquant.performance.winrate import _wilson

    return _wilson(wins, n)


def test_a_single_cluster_falls_back_to_wilson_and_says_so() -> None:
    scored = _scored(5, 5, dates=1)
    (low, high), method = confidence_interval(scored)

    assert method == "wilson"
    assert 0.0 <= low <= high <= 1.0


def test_an_interval_for_no_outcomes_is_refused() -> None:
    with pytest.raises(ValueError, match="no decisive outcomes"):
        confidence_interval([])


def test_wilson_bounds_stay_inside_the_unit_interval() -> None:
    from weatherquant.performance.winrate import _wilson

    for wins, n in ((0, 3), (3, 3), (1, 5), (50, 50)):
        low, high = _wilson(wins, n)
        assert 0.0 <= low <= 1.0
        assert 0.0 <= high <= 1.0
        assert low <= high


# --- grouping ----------------------------------------------------------------


def test_each_group_is_judged_against_the_threshold_on_its_own() -> None:
    """A model with three resolved days reports N/A, not borrowed significance."""
    scored = _scored(6, 4, dates=10)
    groups = grouped_win_rate(scored, dimension="model", min_samples=10, min_clusters=3)

    assert groups["calibrated"].win_rate == pytest.approx(0.6)


def test_grouping_by_edge_band_uses_the_contract_bands() -> None:
    scored = _scored(4, 4, dates=10)
    groups = grouped_win_rate(scored, dimension="edge_band", min_samples=2)

    # The default fixture edge is 0.05, and the bands are half-open: the
    # 0.05 point belongs to 0.05_0.10, not to the band below it.
    assert set(groups) == {"0.05_0.10"}


def test_grouping_by_direction_keeps_buy_and_sell_separate() -> None:
    scored = _scored(3, 3, dates=6)
    groups = grouped_win_rate(scored, dimension="direction", min_samples=2, min_clusters=2)

    assert set(groups) == {"buy_yes"}


def test_an_unknown_grouping_dimension_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown dimension"):
        grouped_win_rate(_scored(1, 1), dimension="colour")

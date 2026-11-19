from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.markets.models import BookLevel, OrderBook
from weatherquant.performance.episodes import Episode, EpisodeKey
from weatherquant.performance.research_drift import build_research_drift
from weatherquant.performance.winrate import SettlementAnswer

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _book(price: float = 0.6) -> OrderBook:
    return OrderBook(
        market_id="m-1",
        token_id="yes-1",
        bids=(BookLevel(price_usd_per_share=price - 0.02, size_shares=1000.0),),
        asks=(BookLevel(price_usd_per_share=price, size_shares=1000.0),),
        venue_ts=NOW,
        fetched_at_utc=NOW,
    )


def _episode(
    episode_id: str,
    *,
    first_seen: datetime = NOW,
    target_date: str = "2026-01-06",
    observations: int = 1,
    edge: float = 0.1,
    model_p: float | None = 0.8,
    market_p: float | None = 0.6,
    fee_rate: float | None = 0.0,
    direction: str = "buy_yes",
) -> Episode:
    return Episode(
        episode_id=episode_id,
        key=EpisodeKey(f"market-{episode_id}", target_date, "calibrated", direction, "LIVE"),
        first_seen_utc=first_seen,
        last_seen_utc=first_seen + timedelta(minutes=5),
        observation_count=observations,
        first_edge=edge,
        last_edge=edge,
        mean_edge=edge,
        max_edge=edge,
        min_edge=edge,
        first_market_price=market_p,
        first_model_probability=model_p,
        first_available_depth_usd=1000.0,
        target_date=target_date,
        station="KLGA",
        bracket_label="40°F",
        direction=direction,
        mode="LIVE",
        end_reason="end_of_data",
        fee_rate=fee_rate,
    )


def _answers(*episodes: Episode, payout: float = 1.0) -> tuple[
    dict[tuple[str, str | None], SettlementAnswer], frozenset[tuple[str, str | None]]
]:
    keys = {(episode.key.market_id, episode.key.target_date) for episode in episodes}
    return (
        {key: SettlementAnswer(status="RESOLVED", payout=payout) for key in keys},
        frozenset(keys),
    )


def test_counts_calibration_and_iso_week_month_buckets_are_separate() -> None:
    first = _episode("first", observations=3, edge=0.1, model_p=0.8, market_p=0.6)
    second = _episode(
        "second",
        first_seen=NOW + timedelta(days=1),
        target_date="2026-01-07",
        observations=2,
        edge=-0.05,
        model_p=0.4,
        market_p=0.5,
    )
    answers, verified = _answers(first, payout=1.0)
    second_answers, second_verified = _answers(second, payout=0.0)
    answers.update(second_answers)
    verified |= second_verified

    data = build_research_drift(
        [first, second],
        answers=answers,
        verified=verified,
        book_for=lambda episode: _book(float(episode.first_market_price or 0.6)),
    )
    bucket = data["buckets"]["iso_week"][0]

    assert bucket["period"] == "2026-W02"
    assert bucket["start_date"] == "2026-01-05"
    assert bucket["observation_count"] == 5
    assert bucket["episode_count"] == 2
    assert bucket["executable_count"] == 2
    assert bucket["calibration"]["n"] == 2
    assert bucket["calibration"]["brier"] == pytest.approx(0.1)
    assert bucket["calibration"]["ece"] == pytest.approx(0.3)
    assert sum(item["count"] for item in bucket["calibration"]["bins"]) == 2
    assert bucket["market_bias"] == {
        "n": 2,
        "mean_market_minus_outcome": pytest.approx(0.05),
        "mean_market_probability": pytest.approx(0.55),
        "mean_outcome": pytest.approx(0.5),
    }
    assert bucket["baseline_comparison"]["market"]["brier"] == pytest.approx(0.205)
    assert bucket["baseline_comparison"]["climatology"]["available"] is False
    tiers = bucket["edge_tiers"]
    assert tiers["theoretical_observation_edge_per_share"] == {
        "n": 5,
        "sum": pytest.approx(0.2),
        "mean": pytest.approx(0.04),
        "unit": "probability_per_share",
    }
    assert tiers["executable_expected_net_usd"]["n"] == 2
    assert tiers["verified_realised_net_usd"]["n"] == 2
    assert data["buckets"]["month"][0]["period"] == "2026-01"
    assert {row["period"] for row in data["buckets"]["target_date"]} == {"2026-01-06", "2026-01-07"}


def test_unknown_fee_and_missing_book_are_counted_but_never_enter_usd_tiers() -> None:
    executable = _episode("executable")
    unknown_fee = _episode("unknown-fee", target_date="2026-01-07", fee_rate=None)
    both_missing = _episode("both-missing", target_date="2026-01-08", fee_rate=None)
    episodes = [executable, unknown_fee, both_missing]
    answers, verified = _answers(executable)
    books = {"executable": _book(), "unknown-fee": _book(), "both-missing": None}

    data = build_research_drift(
        episodes,
        answers=answers,
        verified=verified,
        book_for=lambda episode: books[episode.episode_id],
    )
    bucket = data["buckets"]["iso_week"][0]

    assert bucket["episode_count"] == 3
    assert bucket["executable_count"] == 1
    assert bucket["not_executable_count"] == 2
    assert bucket["fee_unknown_count"] == 2
    assert bucket["no_book_count"] == 1
    assert bucket["not_executable_reason_counts"] == {"FEE_UNKNOWN": 1, "NO_BOOK": 1}
    assert bucket["edge_tiers"]["theoretical_observation_edge_per_share"]["n"] == 3
    assert bucket["edge_tiers"]["executable_expected_net_usd"]["n"] == 1
    assert bucket["edge_tiers"]["verified_realised_net_usd"]["n"] == 1
    assert bucket["calibration"]["n"] == 1, "unverified episodes do not enter calibration"


def test_realised_tier_requires_a_verified_settlement_and_supported_position() -> None:
    unsettled = _episode("unsettled")
    unverified = _episode("unverified", target_date="2026-01-07")
    sell = _episode("sell", target_date="2026-01-08", direction="sell_yes")
    episodes = [unsettled, unverified, sell]
    answers, verified = _answers(unverified, sell)
    verified = frozenset(key for key in verified if key[0] == "market-sell")

    data = build_research_drift(
        episodes,
        answers=answers,
        verified=verified,
        book_for=lambda episode: _book() if episode.direction == "buy_yes" else _book(0.62),
    )
    tiers = data["buckets"]["iso_week"][0]["edge_tiers"]

    assert tiers["executable_expected_net_usd"]["n"] == 3
    assert tiers["verified_realised_net_usd"] == {
        "n": 0,
        "sum": None,
        "mean": None,
        "unit": "USD",
    }


def test_trailing_mad_anomaly_carries_current_source_episode_ids() -> None:
    episodes = [
        _episode(
            f"ep-{index}",
            first_seen=NOW + timedelta(days=7 * index),
            target_date=f"2026-02-{index + 1:02d}",
            edge=0.8 if index == 4 else 0.1,
        )
        for index in range(5)
    ]

    data = build_research_drift(episodes, answers={}, verified=frozenset())
    anomalies = [
        row
        for row in data["anomalies"]
        if row["granularity"] == "iso_week"
        and row["metric"] == "edge_tiers.theoretical_observation_edge_per_share.mean"
    ]

    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly["period"] == "2026-W06"
    assert anomaly["value"] == pytest.approx(0.8)
    assert anomaly["trailing_median"] == pytest.approx(0.1)
    assert anomaly["trailing_mad"] == 0.0
    assert anomaly["trailing_bucket_count"] == 4
    assert anomaly["source_episode_ids"] == ["ep-4"]
    assert data["buckets"]["iso_week"][-1]["anomaly_metrics"] == [
        "edge_tiers.theoretical_observation_edge_per_share.mean"
    ]


def test_empty_input_is_an_explicit_empty_research_payload() -> None:
    data = build_research_drift([], answers={}, verified=frozenset())

    assert data["totals"]["observation_count"] == 0
    assert data["buckets"] == {"iso_week": [], "month": [], "target_date": []}
    assert data["anomalies"] == []

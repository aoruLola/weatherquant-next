"""Episodes: what counts as one opportunity, and what counts as many.

The engine writes a row every cycle for every market it can price, so a raw
ACCEPT total counts evaluations. These tests exist to hold the line between
that and the number of opportunities, and to pin the two ways an episode can
stop that look identical until you ask why.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.performance.episodes import (
    EndReason,
    EpisodeKey,
    build_episodes,
    summarize,
)

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def _row(
    market: str = "m-81",
    *,
    minutes: float = 0,
    state: str = "ACCEPT",
    edge: float | None = 0.05,
    model: str = "calibrated",
    direction: str = "buy_yes",
    mode: str = "LIVE",
    target: str = "2026-10-13",
    price: float | None = 0.62,
    depth: float | None = 50.0,
) -> dict:
    return {
        "market_id": market,
        "target_date": target,
        "model_name": model,
        "direction": direction,
        "mode": mode,
        "state": state,
        "decided_at_utc": T0 + timedelta(minutes=minutes),
        "net_edge_usd": edge,
        "market_price": price,
        "model_probability": 0.67,
        "available_depth_usd": depth,
        "station": "KLGA",
        "bracket_label": "81°F",
        "market_question": "Will it be 81F?",
    }


# --- one opportunity is one episode, however many times it was seen --------


def test_a_repeated_signal_is_one_episode_not_many() -> None:
    """Three ACCEPTs on the same bracket an hour apart are one opportunity."""
    rows = [_row(minutes=i) for i in range(0, 60, 10)]
    episodes, counters = build_episodes(rows)

    assert len(episodes) == 1
    assert counters["episodes_built"] == 1
    assert episodes[0].observation_count == 6
    assert episodes[0].first_seen_utc == T0
    assert episodes[0].duration_seconds == pytest.approx(3000.0)


def test_the_representative_edge_is_the_first_accept() -> None:
    """The contract picks first_edge; later edges must not drift it."""
    rows = [_row(minutes=0, edge=0.04), _row(minutes=10, edge=0.09), _row(minutes=20, edge=0.06)]
    (episode,) = build_episodes(rows)[0]

    assert episode.first_edge == pytest.approx(0.04)
    assert episode.last_edge == pytest.approx(0.06)
    assert episode.mean_edge == pytest.approx((0.04 + 0.09 + 0.06) / 3)
    assert episode.max_edge == pytest.approx(0.09)
    assert episode.min_edge == pytest.approx(0.04)


def test_the_first_price_and_probability_are_the_first_ones_seen() -> None:
    rows = [_row(minutes=0, price=0.60, depth=10.0), _row(minutes=5, price=0.70, depth=900.0)]
    (episode,) = build_episodes(rows)[0]

    assert episode.first_market_price == pytest.approx(0.60)
    assert episode.first_available_depth_usd == pytest.approx(10.0)
    assert episode.first_model_probability == pytest.approx(0.67)


# --- what ends an episode ---------------------------------------------------


def test_a_non_accept_state_ends_the_episode() -> None:
    rows = [_row(minutes=0), _row(minutes=10, state="REJECT", edge=None)]
    episodes, _ = build_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].end_reason == EndReason.STATE_CHANGE


#: One hour, so the gap cases below can speak in minutes without the window
#: and the fixture disagreeing about units by a factor of sixty.
GAP_WINDOW = 3600.0


def test_a_gap_with_other_markets_moving_is_a_signal_break() -> None:
    """The engine kept working; this bracket simply stopped passing."""
    rows = [
        _row("m-81", minutes=0),
        _row("m-other", minutes=100),  # anything else, still inside the gap
        _row("m-81", minutes=200),
    ]
    episodes, _ = build_episodes(rows, gap_window_seconds=GAP_WINDOW)

    # Three episodes: m-81 twice (split by the gap) and m-other once, whose
    # own signal ran to the end of the stream rather than ending.
    mine = [e for e in episodes if e.market_id == "m-81"]
    assert len(mine) == 2
    assert mine[0].end_reason == EndReason.GAP_SIGNAL_BREAK
    assert mine[1].end_reason == EndReason.END_OF_DATA


def test_a_gap_with_nothing_at_all_is_data_missing() -> None:
    """Nobody decided anything, so nothing about this signal was learned."""
    rows = [
        _row("m-81", minutes=0),
        _row("m-81", minutes=200),  # no other row anywhere in between
    ]
    episodes, _ = build_episodes(rows, gap_window_seconds=GAP_WINDOW)

    assert len(episodes) == 2
    assert episodes[0].end_reason == EndReason.GAP_DATA_MISSING


def test_a_gap_inside_the_window_is_not_a_gap() -> None:
    rows = [_row("m-81", minutes=0), _row("m-81", minutes=30)]
    episodes, _ = build_episodes(rows, gap_window_seconds=GAP_WINDOW)

    assert len(episodes) == 1
    assert episodes[0].observation_count == 2


def test_a_new_signal_after_a_gap_is_a_new_episode() -> None:
    rows = [_row(minutes=0), _row(minutes=500)]
    episodes, _ = build_episodes(rows, gap_window_seconds=GAP_WINDOW)

    assert len(episodes) == 2
    assert all(e.observation_count == 1 for e in episodes)


def test_a_stream_that_simply_stops_does_not_claim_an_ending() -> None:
    rows = [_row(minutes=0)]
    (episode,) = build_episodes(rows)[0]

    assert episode.end_reason == EndReason.END_OF_DATA


# --- the identity key is five-dimensional -----------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "gfs_raw"},
        {"direction": "sell_yes"},
        {"mode": "REPLAY"},
        {"target": "2026-10-14"},
        {"market": "m-82"},
    ],
)
def test_every_dimension_of_the_key_isolates(overrides: dict) -> None:
    """Change any one field and you have a different opportunity.

    Merging on market_id alone would fold a REPLAY replay into a LIVE
    signal, or a buy into a sell on the same bracket - both of which
    produce a plausible-looking number that means nothing.
    """
    rows = [_row("m-81", minutes=0), _row(minutes=30, **overrides)]
    episodes, _ = build_episodes(rows)

    assert len(episodes) == 2
    assert episodes[0].key != episodes[1].key


def test_a_replay_and_a_live_signal_are_never_the_same_episode() -> None:
    rows = [_row(mode="LIVE", minutes=0), _row(mode="REPLAY", minutes=0)]
    episodes, _ = build_episodes(rows)

    assert {e.mode for e in episodes} == {"LIVE", "REPLAY"}


def test_the_key_is_hashable_so_episodes_can_be_grouped() -> None:
    assert EpisodeKey("m", "2026-10-13", "calibrated", "buy_yes", "LIVE") == EpisodeKey(
        "m", "2026-10-13", "calibrated", "buy_yes", "LIVE"
    )


# --- what is refused rather than counted ------------------------------------


def test_an_accept_with_no_usable_edge_is_not_an_opportunity() -> None:
    """A decision that never got priced cannot carry a representative edge."""
    rows = [_row(minutes=0, edge=None), _row(minutes=10, edge=None)]
    episodes, counters = build_episodes(rows)

    assert episodes == []
    assert counters["accepts_without_edge"] == 2
    assert counters["episodes_built"] == 0


def test_a_non_finite_edge_is_refused_not_averaged_away() -> None:
    """NaN and inf arrive from a book priced against itself."""
    rows = [_row(minutes=0, edge=float("nan")), _row(minutes=10, edge=float("inf"))]
    episodes, counters = build_episodes(rows)

    assert episodes == []
    assert counters["accepts_without_edge"] == 2


def test_rows_without_a_market_id_are_dropped() -> None:
    rows = [_row(""), _row(minutes=5)]
    episodes, counters = build_episodes(rows)

    assert len(episodes) == 1
    assert counters["decisions_seen"] == 1


def test_an_exported_timestamp_string_is_accepted() -> None:
    """JSON exports carry ISO-8601 strings; a database cursor carries datetimes.

    The research process reads an export, so refusing strings here would
    fail on the only path production actually uses.
    """
    rows = [
        {**_row(minutes=0), "decided_at_utc": "2026-10-12T12:00:00+00:00"},
        {**_row(minutes=10), "decided_at_utc": "2026-10-12T12:10:00+00:00"},
    ]
    episodes, _ = build_episodes(rows)

    assert len(episodes) == 1
    assert episodes[0].duration_seconds == pytest.approx(600.0)


def test_a_timestamp_without_an_offset_is_read_as_utc() -> None:
    """The engine's clock is UTC, so a naive stamp is not ambiguous - just undecorated."""
    rows = [{**_row(minutes=0), "decided_at_utc": "2026-10-12T12:00:00"}]
    episodes, _ = build_episodes(rows)

    assert episodes[0].first_seen_utc.tzinfo is not None
    assert episodes[0].first_seen_utc.isoformat().endswith("+00:00")


def test_a_zulu_suffix_is_read_as_utc() -> None:
    rows = [{**_row(minutes=0), "decided_at_utc": "2026-10-12T12:00:00Z"}]
    episodes, _ = build_episodes(rows)

    assert episodes[0].first_seen_utc.tzinfo is not None


def test_a_timestamp_that_is_not_a_time_is_refused() -> None:
    with pytest.raises(TypeError, match="decided_at_utc"):
        build_episodes([{**_row(minutes=0), "decided_at_utc": 12345}])


def test_unsorted_input_produces_the_same_episodes() -> None:
    """The state machine is defined over time; order in must not matter."""
    ordered = [_row(minutes=i) for i in (0, 10, 20, 30)]
    shuffled = [ordered[2], ordered[0], ordered[3], ordered[1]]

    a, _ = build_episodes(ordered)
    b, _ = build_episodes(shuffled)

    assert [e.as_dict() for e in a] == [e.as_dict() for e in b]


def test_the_episode_id_is_stable_across_runs() -> None:
    """Same opportunity, same id - or a restart would look like two chances."""
    rows = [_row(minutes=0), _row(minutes=10)]
    first, _ = build_episodes(rows)
    second, _ = build_episodes(rows)

    assert first[0].episode_id == second[0].episode_id


def test_a_restart_does_not_cut_an_opportunity_in_half() -> None:
    """Episodes are built from decided_at_utc, not from any process counter."""
    before = [_row(minutes=i) for i in (0, 5, 10)]
    after = [_row(minutes=i) for i in (15, 20)]
    episodes, _ = build_episodes(before + after)

    assert len(episodes) == 1
    assert episodes[0].observation_count == 5


# --- the five counts are different questions --------------------------------


def test_the_summary_keeps_observations_and_opportunities_apart() -> None:
    rows = [
        _row("m-81", minutes=0),
        _row("m-81", minutes=5),
        _row("m-81", minutes=10),
        _row("m-82", minutes=0),
        _row("m-82", minutes=5),
    ]
    episodes, _ = build_episodes(rows)
    summary = summarize(episodes)

    assert summary["accept_observations"] == 5
    assert summary["opportunity_episodes"] == 2
    assert summary["distinct_markets"] == 2
    assert summary["distinct_market_direction_pairs"] == 2
    assert summary["distinct_target_dates"] == 1


def test_the_summary_separates_the_two_edge_sums() -> None:
    rows = [
        _row("m-81", minutes=0, edge=0.04),
        _row("m-81", minutes=5, edge=0.04),
        _row("m-82", minutes=0, edge=0.06),
    ]
    episodes, _ = build_episodes(rows)
    summary = summarize(episodes)

    # 0.04 once, 0.06 once - the deduplicated view.
    assert summary["unique_opportunity_edge_sum"] == pytest.approx(0.10)
    # 0.04 twice, 0.06 once - the repeated-evaluation view.
    assert summary["observation_edge_sum"] == pytest.approx(0.14)


def test_an_empty_summary_is_zero_not_a_fabricated_number() -> None:
    summary = summarize([])

    assert summary["opportunity_episodes"] == 0
    assert summary["unique_opportunity_edge_sum"] == 0.0
    assert summary["mean_episode_edge"] is None


def test_the_summary_says_what_the_numbers_are_not() -> None:
    summary = summarize([])

    warnings = " ".join(summary["warnings"])
    assert "not profit" in warnings
    assert "not realised profit" in warnings

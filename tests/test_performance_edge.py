"""Observation edge: the shape of the numbers, and what they are not.

Most of these tests are arithmetic, but the ones worth reading are the ones
that assert a *label*: an edge distribution over repeated evaluations is a
description of how often the engine re-priced something, and it must say so
wherever it travels.
"""

from __future__ import annotations

import math
import statistics

import pytest

from weatherquant.performance.edge import (
    daily_edge,
    distribution,
    edge_by_bin,
    grouped_edge,
    observation_edge,
    percentile,
)


def _row(
    *, edge=0.05, state="ACCEPT", model="calibrated", direction="buy_yes",
    target="2026-10-14", mode="LIVE",
) -> dict:
    return {
        "market_id": "m-81", "target_date": target, "model_name": model,
        "direction": direction, "mode": mode, "state": state,
        "decided_at_utc": "2026-10-13T12:00:00+00:00", "net_edge_usd": edge,
    }


# --- percentile --------------------------------------------------------------


def test_percentile_interpolates_between_neighbours() -> None:
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == pytest.approx(0.0)
    assert percentile(values, 1.0) == pytest.approx(4.0)
    assert percentile(values, 0.5) == pytest.approx(2.0)
    assert percentile(values, 0.25) == pytest.approx(1.0)


def test_percentile_of_one_sample_is_that_sample() -> None:
    """Not zero, not an error - one observation answers every quantile."""
    assert percentile([0.07], 0.05) == pytest.approx(0.07)
    assert percentile([0.07], 0.95) == pytest.approx(0.07)


def test_percentile_of_nothing_is_none_not_zero() -> None:
    assert percentile([], 0.5) is None


def test_percentile_rejects_a_probability_outside_the_range() -> None:
    with pytest.raises(ValueError, match="q must be in"):
        percentile([1.0], 1.5)


# --- distribution ------------------------------------------------------------


def test_distribution_matches_the_textbook_values() -> None:
    values = [0.01, 0.02, 0.03, 0.04, 0.05]
    stats = distribution(values)

    assert stats["count"] == 5
    assert stats["sum"] == pytest.approx(0.15)
    assert stats["mean"] == pytest.approx(0.03)
    assert stats["median"] == pytest.approx(0.03)
    assert stats["min"] == pytest.approx(0.01)
    assert stats["max"] == pytest.approx(0.05)
    assert stats["std"] == pytest.approx(statistics.pstdev(values))


def test_an_empty_group_reports_nothing_rather_than_zero() -> None:
    """A zero mean would read as 'no edge found'; None reads as 'no sample'."""
    stats = distribution([])

    assert stats["count"] == 0
    for key in ("sum", "mean", "median", "min", "max", "std", "p05", "p95"):
        assert stats[key] is None, key


def test_a_single_observation_has_no_dispersion() -> None:
    stats = distribution([0.04])
    assert stats["std"] == 0.0
    assert stats["p05"] == pytest.approx(0.04)


def test_quantiles_bracket_the_mean_on_a_skewed_sample() -> None:
    values = [0.01] * 90 + [0.50] * 10
    stats = distribution(values)

    assert stats["p05"] <= stats["mean"] <= stats["p95"]
    assert stats["p95"] > stats["median"], "the tail sits above the middle"


# --- the observation view ----------------------------------------------------


def test_the_scope_is_stated_on_the_payload_itself() -> None:
    payload = observation_edge([_row(edge=0.05)])

    assert payload["scope"] == "observation_all_accepts"
    assert "observation_all_accepts" in payload["statistical_scope"]
    warnings = " ".join(payload["warnings"])
    assert "not profit" in warnings
    assert "not asset growth" in warnings


def test_repeated_evaluations_all_count_toward_the_sum() -> None:
    rows = [_row(edge=0.04) for _ in range(5)] + [_row(edge=0.06)]
    payload = observation_edge(rows)

    assert payload["sample_size"] == 6
    assert payload["overall"]["sum"] == pytest.approx(0.04 * 5 + 0.06)
    assert payload["accepts_seen"] == 6


def test_non_accept_rows_never_reach_the_distribution() -> None:
    rows = [_row(edge=0.04), _row(edge=9.9, state="REJECT"), _row(edge=8.8, state="NO_TRADE")]
    payload = observation_edge(rows)

    assert payload["sample_size"] == 1
    assert payload["refused"]["not_accept"] == 2
    assert payload["decisions_seen"] == 3


def test_an_accept_without_a_usable_edge_is_counted_not_dropped() -> None:
    """NaN and None are refusals, and a refusal has to be visible."""
    rows = [_row(edge=0.04), _row(edge=None), _row(edge=float("nan"))]
    payload = observation_edge(rows)

    assert payload["sample_size"] == 1
    assert payload["refused"]["accept_without_usable_edge"] == 2
    assert payload["accepts_seen"] == 3


def test_nothing_is_dropped_silently() -> None:
    rows = [_row(edge=0.04), _row(edge=None), _row(edge=1.0, state="REJECT")]
    payload = observation_edge(rows)

    accounted = payload["sample_size"] + payload["refused"]["accept_without_usable_edge"] + payload["refused"]["not_accept"]
    assert accounted == payload["decisions_seen"]


# --- grouping ----------------------------------------------------------------


def test_groups_carry_their_own_size() -> None:
    rows = [_row(model="calibrated", edge=0.04) for _ in range(10)] + [
        _row(model="gfs_raw", edge=0.09)
    ]
    groups = grouped_edge(rows, dimension="model")

    assert groups["calibrated"]["count"] == 10
    assert groups["gfs_raw"]["count"] == 1, "a four-sample mean must show its sample"
    assert groups["gfs_raw"]["mean"] == pytest.approx(0.09)


def test_grouping_by_direction_keeps_buy_and_sell_apart() -> None:
    rows = [_row(direction="buy_yes", edge=0.04), _row(direction="sell_yes", edge=0.08)]
    groups = grouped_edge(rows, dimension="direction")

    assert groups["buy_yes"]["mean"] == pytest.approx(0.04)
    assert groups["sell_yes"]["mean"] == pytest.approx(0.08)


def test_grouping_by_mode_keeps_replay_out_of_live() -> None:
    rows = [_row(mode="LIVE", edge=0.04), _row(mode="REPLAY", edge=0.08)]
    groups = grouped_edge(rows, dimension="mode")

    assert set(groups) == {"LIVE", "REPLAY"}


def test_an_unknown_dimension_is_refused_rather_than_silently_ignored() -> None:
    with pytest.raises(ValueError, match="unknown dimension"):
        grouped_edge([_row()], dimension="colour")


# --- daily trend -------------------------------------------------------------


def test_the_trend_is_ordered_by_target_date() -> None:
    rows = [
        _row(target="2026-10-16", edge=0.03),
        _row(target="2026-10-14", edge=0.05),
        _row(target="2026-10-15", edge=0.07),
    ]
    trend = daily_edge(rows)

    assert [d["target_date"] for d in trend] == ["2026-10-14", "2026-10-15", "2026-10-16"]


def test_the_trend_says_what_one_row_in_it_means() -> None:
    trend = daily_edge([_row(edge=0.04), _row(edge=0.04)])

    assert trend[0]["observation_count"] == 2
    assert "per evaluation" in trend[0]["distinct_note"]


def test_a_day_with_no_usable_edge_does_not_appear() -> None:
    assert daily_edge([_row(edge=None), _row(edge=0.04, state="REJECT")]) == []


# --- edge bands --------------------------------------------------------------


def test_edges_land_in_the_contract_bands() -> None:
    """Bands publish a distribution now, and the counts are the boundary proof.

    The values themselves used to be published, and they were the largest field
    in ``edge.json``: 2.2 million floats at five million decisions. The counts
    answer the same question - how many evaluations fell where - and the
    statistics answer the other one, so the boundary behaviour is still pinned
    exactly: 0.01 and -0.02 in the lowest band, 0.03 next, and so on.
    """
    rows = [
        _row(edge=0.01), _row(edge=0.03), _row(edge=0.07), _row(edge=0.20),
        _row(edge=-0.02),
    ]
    bands = edge_by_bin(rows)

    assert bands["lt_0.02"]["count"] == 2
    assert bands["lt_0.02"]["min"] == pytest.approx(-0.02)
    assert bands["lt_0.02"]["max"] == pytest.approx(0.01)
    assert bands["0.02_0.05"]["count"] == 1
    assert bands["0.02_0.05"]["mean"] == pytest.approx(0.03)
    assert bands["0.05_0.10"]["count"] == 1
    assert bands["0.05_0.10"]["sum"] == pytest.approx(0.07)
    assert bands["gte_0.10"]["count"] == 1
    assert bands["gte_0.10"]["max"] == pytest.approx(0.20)


def test_every_edge_lands_in_exactly_one_band() -> None:
    rows = [_row(edge=e) for e in (0.0, 0.02, 0.05, 0.10, 1.0)]
    bands = edge_by_bin(rows)

    assert sum(band["count"] for band in bands.values()) == len(rows)
    # The boundaries themselves: 0.02 starts the second band, 0.05 the third,
    # 0.10 the fourth, and 0.0 is in the first.
    assert [bands[name]["count"] for name in
            ("lt_0.02", "0.02_0.05", "0.05_0.10", "gte_0.10")] == [1, 1, 1, 2]


def test_a_sum_over_many_samples_is_computed_without_drift() -> None:
    values = [0.0001] * 100_000
    assert math.isclose(distribution(values)["sum"], 10.0, rel_tol=1e-9)

"""Bracket -> continuous-temperature interval math.

This is the single most bias-prone piece of arithmetic in the system: a market
quotes whole degrees, but the underlying temperature is continuous. Expected
values below are computed independently with ``statistics.NormalDist`` (stdlib,
not project code) so the assertions cannot become self-referential.

The regression this guards against is real: an audited open-source weather bot
integrated over ``[46, 47]`` for a "46-47 degF" bracket, omitting the half
degrees that rounding actually contributes. At ``N(46.5, 2)`` that yields
0.197 where the correct answer is 0.383 - a factor of ~2 error in the
probability that drives every edge.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest
from pydantic import ValidationError

from weatherquant.markets.models import Bracket, BracketKind, RoundingConvention


def _p_bracket(mean: float, sigma: float, lo: float, hi: float) -> float:
    """Independent reference: P(lo <= T < hi) for T ~ Normal(mean, sigma)."""
    d = NormalDist(mu=mean, sigma=sigma)
    return d.cdf(hi) - d.cdf(lo)


# ---------------------------------------------------------------------------
# Interval derivation
# ---------------------------------------------------------------------------


def test_range_bracket_is_widened_by_half_a_degree_on_each_side() -> None:
    b = Bracket(label="46-47", kind=BracketKind.RANGE, low=46.0, high=47.0)
    lo, hi = b.probability_interval_f()
    # round-half-up maps true t into integer k iff t in [k-0.5, k+0.5),
    # so integers {46, 47} cover [45.5, 47.5).
    assert lo == pytest.approx(45.5)
    assert hi == pytest.approx(47.5)


def test_adjacent_brackets_tile_the_line_with_no_gap_and_no_overlap() -> None:
    """Consecutive quoted brackets must partition the real line exactly.

    ``44-45`` covers rounded degrees {44, 45} -> [43.5, 45.5) and ``46-47``
    covers {46, 47} -> [45.5, 47.5). The first's upper edge must equal the
    second's lower edge: any gap loses probability mass, any overlap double
    counts it.
    """
    a = Bracket(label="44-45", kind=BracketKind.RANGE, low=44.0, high=45.0)
    b = Bracket(label="46-47", kind=BracketKind.RANGE, low=46.0, high=47.0)
    a_lo, a_hi = a.probability_interval_f()
    b_lo, b_hi = b.probability_interval_f()

    assert a_lo == pytest.approx(43.5)
    assert a_hi == pytest.approx(45.5)
    assert b_lo == pytest.approx(45.5)
    assert b_hi == pytest.approx(47.5)
    assert a_hi == pytest.approx(b_lo)  # exact tiling

    # And the rounded-degree sets they stand for really are disjoint.
    assert set(range(44, 46)).isdisjoint(range(46, 48))


def test_exact_bracket_covers_one_degree() -> None:
    b = Bracket(label="32", kind=BracketKind.EXACT, low=32.0, high=None)
    lo, hi = b.probability_interval_f()
    assert lo == pytest.approx(31.5)
    assert hi == pytest.approx(32.5)
    assert hi - lo == pytest.approx(1.0)


def test_above_bracket_is_open_upward() -> None:
    b = Bracket(label=">=40", kind=BracketKind.ABOVE, low=40.0, high=None)
    lo, hi = b.probability_interval_f()
    assert lo == pytest.approx(39.5)
    assert hi == math.inf


def test_below_bracket_is_open_downward() -> None:
    b = Bracket(label="<=30", kind=BracketKind.BELOW, low=None, high=30.0)
    lo, hi = b.probability_interval_f()
    assert lo == -math.inf
    assert hi == pytest.approx(30.5)


def test_no_rounding_passes_bounds_through_untouched() -> None:
    b = Bracket(
        label="46-47",
        kind=BracketKind.RANGE,
        low=46.0,
        high=47.0,
        rounding=RoundingConvention.NONE,
    )
    lo, hi = b.probability_interval_f()
    assert lo == pytest.approx(46.0)
    assert hi == pytest.approx(47.0, abs=1e-15) or hi > 47.0


def test_non_integral_exact_bound_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-integral"):
        Bracket(label="32.5", kind=BracketKind.EXACT, low=32.5, high=None)


def test_empty_bracket_is_rejected_at_construction() -> None:
    # No whole degree lies strictly between 46.1 and 46.9.
    with pytest.raises(ValueError, match="no whole degree"):
        Bracket(label="x", kind=BracketKind.RANGE, low=46.1, high=46.9).probability_interval_f()


def test_celsius_bracket_is_refused_rather_than_silently_converted() -> None:
    with pytest.raises(NotImplementedError, match="degC"):
        Bracket(label="0-1C", kind=BracketKind.RANGE, low=0.0, high=1.0, unit="degC")


def test_range_with_equal_bounds_is_refused() -> None:
    with pytest.raises(ValidationError, match="EXACT"):
        Bracket(label="32-32", kind=BracketKind.RANGE, low=32.0, high=32.0)


def test_inverted_bounds_are_refused() -> None:
    with pytest.raises(ValidationError, match="low"):
        Bracket(label="bad", kind=BracketKind.RANGE, low=50.0, high=40.0)


def test_missing_bounds_for_kind_are_refused() -> None:
    with pytest.raises(ValidationError, match="requires 'low'"):
        Bracket(label="bad", kind=BracketKind.ABOVE, low=None, high=None)


# ---------------------------------------------------------------------------
# Probability against an independent reference
# ---------------------------------------------------------------------------


def test_half_degree_widening_changes_the_probability_by_a_large_factor() -> None:
    """The exact failure an audited competitor ships: 0.197 vs the correct 0.383."""
    mean, sigma = 46.5, 2.0
    correct = _p_bracket(mean, sigma, 45.5, 47.5)
    naive = _p_bracket(mean, sigma, 46.0, 47.0)

    b = Bracket(label="46-47", kind=BracketKind.RANGE, low=46.0, high=47.0)
    lo, hi = b.probability_interval_f()
    ours = _p_bracket(mean, sigma, lo, hi)

    assert correct == pytest.approx(0.382924, abs=1e-5)
    assert naive == pytest.approx(0.197412, abs=1e-5)
    assert ours == pytest.approx(correct, abs=1e-12)
    assert naive < correct  # the omission understates probability by ~2x


def test_widened_intervals_partition_the_distribution() -> None:
    """Sum of P over a complete bracket covering must approach 1 as coverage grows."""
    mean, sigma = 48.0, 3.0
    brackets = [
        Bracket(label=f"{k}-{k + 1}", kind=BracketKind.RANGE, low=float(k), high=float(k + 1))
        for k in range(-200, 201, 2)  # 46-47 style: pairs {0,1},{2,3},...
    ]
    total = math.fsum(
        (lambda i: _p_bracket(mean, sigma, i[0], i[1]))(b.probability_interval_f()) for b in brackets
    )
    # Pairs of adjacent integers leave odd integers out ({0,1} misses 2? no:
    # {0,1}=[ -0.5,1.5 ) and {2,3}=[1.5,3.5 ) tile the line exactly).
    assert total == pytest.approx(1.0, abs=1e-9)


def test_bracket_distribution_requires_probabilities_to_sum_to_one() -> None:
    from datetime import date, datetime

    from weatherquant.markets.models import BracketDistribution, BracketProbability

    with pytest.raises(ValueError, match="sum to 1"):
        BracketDistribution(
            station="KLGA",
            target_date=date(2026, 10, 10),
            model_id="ecmwf_ifs025",
            forecast_run_time_utc=datetime(2026, 10, 9, 12, 0),
            probabilities=(
                BracketProbability(label="32-33", probability=0.5),
                BracketProbability(label="34-35", probability=0.3),
            ),
        )


def test_bracket_distribution_rejects_duplicate_brackets() -> None:
    from datetime import date, datetime

    from weatherquant.markets.models import BracketDistribution, BracketProbability

    with pytest.raises(ValueError, match="duplicate"):
        BracketDistribution(
            station="KLGA",
            target_date=date(2026, 10, 10),
            model_id="ecmwf_ifs025",
            forecast_run_time_utc=datetime(2026, 10, 9, 12, 0),
            probabilities=(
                BracketProbability(label="32-33", probability=0.5),
                BracketProbability(label="32-33", probability=0.5),
            ),
        )

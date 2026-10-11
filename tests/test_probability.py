"""Residual model and probability generation.

The expected values here are textbook constants and hand-derived arithmetic,
not outputs of the code under test - a probability pipeline whose tests echo its
own numbers cannot detect that both are wrong.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest

from weatherquant.markets.models import Bracket, BracketKind
from weatherquant.models.probability import (
    bracket_probability,
    distribution_for_event,
    is_complete_ladder,
    normal_cdf,
)
from weatherquant.models.residual import (
    LEAD_BUCKETS,
    ResidualModel,
    fit_residual_model,
    lead_bucket,
    sigma_from_mae,
)

# Textbook values of the standard normal CDF.
PHI_0 = 0.5
PHI_1 = 0.8413447460685429
PHI_196 = 0.9750021048517795


# ---------------------------------------------------------------------------
# sigma from MAE - the audited competitor's bug
# ---------------------------------------------------------------------------


def test_sigma_from_mae_applies_the_sqrt_pi_over_2_factor() -> None:
    """E|X| = sigma*sqrt(2/pi)  =>  sigma = MAE*sqrt(pi/2) ~= 1.2533 * MAE."""
    expected_factor = math.sqrt(math.pi / 2.0)
    assert expected_factor == pytest.approx(1.2533141373155003)

    assert sigma_from_mae(1.0) == pytest.approx(expected_factor)
    assert sigma_from_mae(2.0) == pytest.approx(2.0 * expected_factor)

    # The bug: returning MAE unchanged understates the spread by 20%.
    assert sigma_from_mae(1.0) != pytest.approx(1.0)
    assert sigma_from_mae(1.0) > 1.0


def test_sigma_from_mae_rejects_a_negative_error() -> None:
    with pytest.raises(ValueError, match="mae must be >= 0"):
        sigma_from_mae(-0.5)


def test_mae_round_trips_through_sigma() -> None:
    """sigma -> MAE -> sigma must be the identity: MAE = sigma*sqrt(2/pi)."""
    sigma = 3.7
    mae = sigma * math.sqrt(2.0 / math.pi)
    assert sigma_from_mae(mae) == pytest.approx(sigma, rel=1e-12)


# ---------------------------------------------------------------------------
# Lead-time buckets
# ---------------------------------------------------------------------------


def test_lead_buckets_are_contiguous_and_cover_everything() -> None:
    seen = []
    for hours in (0, 1, 23.9, 24, 25, 47.9, 48, 71.9, 72, 119.9, 120, 500, 5000):
        seen.append(lead_bucket(hours))
    assert seen[0] == "0-24h"
    assert seen[3] == "24-48h"
    assert seen[6] == "48-72h"
    assert seen[9] == "72-120h"
    assert seen[-1] == "120h+"
    assert set(seen) <= {name for _, _, name in LEAD_BUCKETS}


def test_a_negative_lead_time_is_a_data_error() -> None:
    """A forecast initialised after the day it predicts is not an early forecast."""
    with pytest.raises(ValueError, match="lead_hours must be >= 0"):
        lead_bucket(-1.0)


# ---------------------------------------------------------------------------
# Normal CDF
# ---------------------------------------------------------------------------


def test_normal_cdf_matches_textbook_values() -> None:
    assert normal_cdf(0.0) == pytest.approx(PHI_0, abs=1e-15)
    assert normal_cdf(1.0) == pytest.approx(PHI_1, abs=1e-12)
    assert normal_cdf(1.96) == pytest.approx(PHI_196, abs=1e-6)


def test_normal_cdf_is_symmetric() -> None:
    for x in (0.5, 1.0, 2.5):
        assert normal_cdf(x) + normal_cdf(-x) == pytest.approx(1.0, abs=1e-12)


def test_normal_cdf_extremes() -> None:
    assert normal_cdf(-40.0) == pytest.approx(0.0, abs=1e-300)
    assert normal_cdf(40.0) == pytest.approx(1.0, abs=1e-300)


# ---------------------------------------------------------------------------
# Residual fitting
# ---------------------------------------------------------------------------


def _pairs(residuals_by_date: dict[date, list[float]], leads: Sequence[float]) -> list[dict]:
    out = []
    for target_date, residuals in residuals_by_date.items():
        for i, residual in enumerate(residuals):
            lead = leads[i % len(leads)]
            out.append(
                {
                    "target_date": target_date,
                    "lead_hours": lead,
                    "predicted_max_f": 70.0,
                    "observed_max_f": 70.0 + residual,
                }
            )
    return out


def test_bias_is_the_mean_residual() -> None:
    # observed - predicted: +2 and -2 -> bias 0; +1,+1 -> bias 1
    pairs = _pairs({date(2026, 7, 1): [2.0, -2.0], date(2026, 7, 2): [1.0, 1.0]}, [12.0])
    model = fit_residual_model(pairs, model_id="ecmwf_ifs025")
    assert model.bias_f == pytest.approx((2 - 2 + 1 + 1) / 4)
    assert model.n_pairs == 4
    assert model.n_dates == 2
    assert model.is_fitted


def test_positive_bias_means_the_model_runs_cold() -> None:
    pairs = _pairs({date(2026, 7, 1): [3.0], date(2026, 7, 2): [3.0]}, [12.0])
    model = fit_residual_model(pairs, model_id="gfs")
    assert model.bias_f == pytest.approx(3.0)


def test_spread_is_measured_after_collapsing_by_date() -> None:
    """More runs for one day must not shrink the estimated spread.

    This is the independence bug: three runs of the same day share that day's
    weather, so treating them as three samples makes the error look smaller
    than it is and the probabilities look better than they are.
    """
    # Day A has 10 runs, all off by +1; day B has 1 run, off by -1.
    # Pooled std (treating 11 runs as independent) is much smaller than the
    # across-date std of [+1, -1] = sqrt(2).
    big = _pairs({date(2026, 7, 1): [1.0] * 10, date(2026, 7, 2): [-1.0]}, [12.0])
    model = fit_residual_model(big, model_id="m", min_samples_per_bucket=2)

    # Collapsed: two date-level means [+1, -1] -> sample std = sqrt(2).
    per_date = [1.0, -1.0]
    mean = sum(per_date) / 2
    across_date_sigma = math.sqrt(sum((x - mean) ** 2 for x in per_date) / (len(per_date) - 1))
    assert model.sigma(12.0) == pytest.approx(across_date_sigma, rel=1e-9)

    # Pooled naively over all 11 residuals: mean = 9/11, sample std ~0.603.
    pooled_residuals = [1.0] * 10 + [-1.0]
    pooled_mean = sum(pooled_residuals) / len(pooled_residuals)
    pooled = math.sqrt(
        sum((x - pooled_mean) ** 2 for x in pooled_residuals) / (len(pooled_residuals) - 1)
    )
    assert pooled == pytest.approx(0.6030226891555273, rel=1e-6)
    # Collapsing by date more than doubles the spread - which is the point.
    assert model.sigma(12.0) > pooled * 2


def test_an_empty_bucket_yields_no_sigma_rather_than_a_fallback() -> None:
    pairs = _pairs({date(2026, 7, 1): [1.0], date(2026, 7, 2): [2.0]}, [12.0])
    model = fit_residual_model(pairs, model_id="m", min_samples_per_bucket=5)
    assert model.sigma(12.0) is None, "two dates cannot estimate a spread"
    assert model.is_usable(12.0) is False


def test_a_thin_bucket_does_not_borrow_from_a_thick_one() -> None:
    """Each lead bucket stands alone; no global sigma is silently substituted."""
    pairs = _pairs(
        {date(2026, 7, 1): [1.0], date(2026, 7, 2): [2.0], date(2026, 7, 3): [1.5]},
        [12.0],  # only the 0-24h bucket is populated
    )
    model = fit_residual_model(pairs, model_id="m", min_samples_per_bucket=2)
    assert model.sigma(12.0) is not None
    assert model.sigma(100.0) is None, "72-120h has no samples at all"


def test_incomplete_pairs_are_skipped_and_counted() -> None:
    pairs = _pairs({date(2026, 7, 1): [1.0]}, [12.0])
    pairs.append({"target_date": date(2026, 7, 2), "lead_hours": 12.0, "predicted_max_f": None})
    pairs.append({"target_date": date(2026, 7, 3), "lead_hours": 12.0, "predicted_max_f": 70.0})
    model = fit_residual_model(pairs, model_id="m")
    assert model.n_pairs == 1, "pairs without an observation must not be counted"
    assert any("skipped" in note for note in model.fit_notes)


def test_no_pairs_at_all_is_an_empty_model_not_a_crash() -> None:
    model = fit_residual_model([], model_id="m")
    assert model.is_fitted is False
    assert model.sigma_by_lead == {}
    assert model.fit_notes


# ---------------------------------------------------------------------------
# Probability generation
# ---------------------------------------------------------------------------


def _ladder(bounds: Sequence[tuple[float | None, float | None, str]]) -> list[Bracket]:
    out = []
    for low, high, label in bounds:
        if low is None:
            out.append(Bracket(label=label, kind=BracketKind.BELOW, low=None, high=high))
        elif high is None:
            out.append(Bracket(label=label, kind=BracketKind.ABOVE, low=low, high=None))
        else:
            out.append(Bracket(label=label, kind=BracketKind.RANGE, low=low, high=high))
    return out


def _model(bias: float, sigma: float) -> ResidualModel:
    pairs = [
        {"target_date": date(2026, 7, d), "lead_hours": 12.0, "predicted_max_f": 0.0,
         "observed_max_f": bias + (sigma if d % 2 else -sigma)}
        for d in range(1, 31)
    ]
    fitted = fit_residual_model(pairs, model_id="m", bias=bias, min_samples_per_bucket=5)
    # Override the sigma per bucket so tests can assert against hand-derived
    # values rather than against whatever the fit happened to produce.
    from dataclasses import replace

    return replace(
        fitted,
        sigma_by_lead={"0-24h": sigma, "24-48h": sigma, "48-72h": sigma,
                       "72-120h": sigma, "120h+": sigma},
        counts_by_lead={k: 30 for k in ("0-24h", "24-48h", "48-72h", "72-120h", "120h+")},
    )


def test_probability_is_hand_computable() -> None:
    """One bracket, mean 0, sigma 1: P in [lo, hi) = Phi(hi) - Phi(lo)."""
    model = _model(bias=0.0, sigma=1.0)
    b = Bracket(label="0-1", kind=BracketKind.RANGE, low=0.0, high=1.0)
    # The bracket widens to [-0.5, 1.5) for whole-degree quoting.
    expected = normal_cdf(1.5) - normal_cdf(-0.5)

    got = bracket_probability(0.0, b, model=model, lead_hours=12.0)
    assert got == pytest.approx(expected, abs=1e-12)
    # And the value itself, so editing `expected` alone cannot make it pass.
    assert got == pytest.approx(0.6246552600051549, abs=1e-9)  # Phi(1.5) - Phi(-0.5)


def test_the_half_degree_widening_reaches_the_probability() -> None:
    """P over [lo,hi] must differ from P over [lo+0.5, hi-0.5]."""
    model = _model(bias=0.0, sigma=2.0)
    b = Bracket(label="46-47", kind=BracketKind.RANGE, low=46.0, high=47.0)

    widened = bracket_probability(46.5, b, model=model, lead_hours=12.0)
    narrowed = normal_cdf((47.0 - 46.5) / 2.0) - normal_cdf((46.0 - 46.5) / 2.0)
    expected_widened = normal_cdf((47.5 - 46.5) / 2.0) - normal_cdf((45.5 - 46.5) / 2.0)

    assert widened == pytest.approx(expected_widened, abs=1e-12)
    assert widened != pytest.approx(narrowed)
    # The classic error understates by ~2x here.
    assert widened / narrowed > 1.5


def test_bias_shifts_probability_in_the_expected_direction() -> None:
    b = Bracket(label="70-71", kind=BracketKind.RANGE, low=70.0, high=71.0)
    cold = bracket_probability(70.5, b, model=_model(bias=-3.0, sigma=2.0), lead_hours=12.0)
    hot = bracket_probability(70.5, b, model=_model(bias=3.0, sigma=2.0), lead_hours=12.0)
    # A model that runs cold needs the observation shifted down, so a bracket
    # at the forecast becomes less likely after correction.
    assert cold > hot


def test_no_sigma_means_no_probability() -> None:
    model = _model(bias=0.0, sigma=1.0)
    from dataclasses import replace

    starved = replace(model, counts_by_lead={"0-24h": 1}, sigma_by_lead={"0-24h": 1.0})
    b = Bracket(label="70-71", kind=BracketKind.RANGE, low=70.0, high=71.0)
    assert bracket_probability(70.5, b, model=starved, lead_hours=12.0) is None


def test_zero_sigma_is_refused_as_degenerate() -> None:
    model = _model(bias=0.0, sigma=1.0)
    from dataclasses import replace

    degenerate = replace(model, sigma_by_lead={"0-24h": 0.0}, counts_by_lead={"0-24h": 30})
    b = Bracket(label="70-71", kind=BracketKind.RANGE, low=70.0, high=71.0)
    assert bracket_probability(70.5, b, model=degenerate, lead_hours=12.0) is None


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------


def _full_ladder() -> list[Bracket]:
    """A ladder shaped like the venue's real one: step 2, disjoint, open ends.

    Adjacent brackets must NOT share a degree ("40-41" next to "41-42") - that
    would double-count the shared integer and the probabilities would sum to
    more than 1. The real markets quote 56-57, 58-59, 60-61 ... with open ends
    at the extremes, and BracketDistribution rejects an overlapping ladder.
    """
    return _ladder(
        [(None, 39.0, "39 or below"), (40.0, 41.0, "40-41"),
         (42.0, 43.0, "42-43"), (44.0, None, "44 or above")]
    )


def test_a_complete_ladder_produces_probabilities_summing_to_one() -> None:
    model = _model(bias=0.0, sigma=1.5)
    dist = distribution_for_event(
        predicted_max_f=41.5,
        brackets=_full_ladder(),
        model=model,
        lead_hours=12.0,
        station="KLGA",
        target_date=date(2026, 7, 1),
        model_id="m",
    )
    total = sum(p.probability for p in dist.probabilities)
    assert total == pytest.approx(1.0, abs=1e-6)
    assert len(dist.probabilities) == 4


def test_a_complete_ladder_is_detected() -> None:
    assert is_complete_ladder(_full_ladder())
    assert not is_complete_ladder(_ladder([(40.0, 41.0, "40-41")]))
    assert not is_complete_ladder(_ladder([(None, 41.0, "41 or below")]))
    # Overlapping brackets still have open ends, so "complete" here means only
    # that the range is covered - the sum check is what catches the overlap.
    overlapping = _ladder(
        [(None, 40.0, "40 or below"), (40.0, 41.0, "40-41"),
         (41.0, 42.0, "41-42"), (42.0, None, "42 or above")]
    )
    assert is_complete_ladder(overlapping)


def test_an_incomplete_ladder_is_refused_rather_than_renormalised() -> None:
    """Renormalising a partial ladder would invent probability mass."""
    model = _model(bias=0.0, sigma=1.5)
    with pytest.raises(ValueError, match="complete ladder"):
        distribution_for_event(
            predicted_max_f=41.5,
            brackets=_ladder([(40.0, 41.0, "40-41"), (41.0, 42.0, "41-42")]),
            model=model,
            lead_hours=12.0,
            station="KLGA",
            target_date=date(2026, 7, 1),
            model_id="m",
        )


def test_a_distribution_without_a_usable_spread_is_refused() -> None:
    model = _model(bias=0.0, sigma=1.5)
    from dataclasses import replace

    starved = replace(model, counts_by_lead={}, sigma_by_lead={})
    with pytest.raises(ValueError, match="no usable spread"):
        distribution_for_event(
            predicted_max_f=41.5,
            brackets=_full_ladder(),
            model=starved,
            lead_hours=12.0,
            station="KLGA",
            target_date=date(2026, 7, 1),
            model_id="m",
        )


def test_the_distribution_carries_its_provenance() -> None:
    model = _model(bias=0.0, sigma=1.5)
    run_time = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    dist = distribution_for_event(
        predicted_max_f=41.5,
        brackets=_full_ladder(),
        model=model,
        lead_hours=12.0,
        station="KLGA",
        target_date=date(2026, 7, 1),
        model_id="ecmwf_ifs025",
        forecast_run_time_utc=run_time,
    )
    assert dist.station == "KLGA"
    assert dist.model_id == "ecmwf_ifs025"
    assert dist.forecast_run_time_utc == run_time
    assert dist.target_date == date(2026, 7, 1)


def test_probabilities_are_individually_valid() -> None:
    model = _model(bias=0.0, sigma=1.5)
    dist = distribution_for_event(
        predicted_max_f=41.5,
        brackets=_full_ladder(),
        model=model,
        lead_hours=12.0,
        station="KLGA",
        target_date=date(2026, 7, 1),
        model_id="m",
    )
    for p in dist.probabilities:
        assert 0.0 <= p.probability <= 1.0


# ---------------------------------------------------------------------------
# Unit safety - the bug that made the first backfill unusable
# ---------------------------------------------------------------------------


def test_a_celsius_payload_is_refused_rather_than_stored_as_fahrenheit() -> None:
    """Storing degC in a `*_f` column makes every residual ~50 degrees wrong.

    The failure is invisible in the data: the series looks plausible, and the
    damage only surfaces as an absurd score. So the unit is asserted from the
    response's hourly_units, and a mismatch fails the fetch.
    """
    from weatherquant.weather.single_runs import parse_run_response

    base = {
        "latitude": 40.75,
        "longitude": -74.0,
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-10-10T00:00", "2026-10-10T01:00"],
            "temperature_2m": [16.9, 16.4],
        },
        "hourly_units": {"time": "iso8601", "temperature_2m": "°C"},
    }
    result = parse_run_response(base, model="ecmwf_ifs025")
    assert result.status == "failed"
    assert "degF" in (result.error_text or "")
    assert result.n_points == 0 or result.hourly == []


def test_a_fahrenheit_payload_is_accepted() -> None:
    from weatherquant.weather.single_runs import parse_run_response

    payload = {
        "latitude": 40.75,
        "longitude": -74.0,
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-10-10T00:00", "2026-10-10T01:00"],
            "temperature_2m": [62.5, 61.6],
        },
        "hourly_units": {"time": "iso8601", "temperature_2m": "°F"},
    }
    result = parse_run_response(payload, model="ecmwf_ifs025")
    assert result.status == "ok"
    assert result.temperature_unit is not None and "F" in result.temperature_unit
    assert [p.temperature_2m for p in result.hourly] == [62.5, 61.6]


def test_the_run_time_is_taken_from_the_first_stamp() -> None:
    from weatherquant.weather.single_runs import parse_run_response

    payload = {
        "utc_offset_seconds": 0,
        "hourly": {
            "time": ["2026-10-10T06:00", "2026-10-10T07:00"],
            "temperature_2m": [60.0, 61.0],
        },
        "hourly_units": {"temperature_2m": "°F"},
    }
    result = parse_run_response(payload, model="gfs")
    assert result.run_time_utc is not None
    assert result.run_time_utc.isoformat().startswith("2026-10-10T06:00")


def test_a_local_timezone_offset_is_converted_back_to_utc() -> None:
    """With timezone=America/New_York the stamps are local; offset recovers UTC."""
    from weatherquant.weather.single_runs import parse_run_response

    payload = {
        "utc_offset_seconds": -14400,  # EDT
        "hourly": {"time": ["2026-10-10T02:00"], "temperature_2m": [60.0]},
        "hourly_units": {"temperature_2m": "°F"},
    }
    result = parse_run_response(payload, model="gfs")
    assert result.run_time_utc is not None
    assert result.run_time_utc.hour == 6, "02:00 EDT is 06:00 UTC"

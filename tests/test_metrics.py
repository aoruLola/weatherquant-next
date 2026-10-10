"""Scoring metrics.

Expected values are hand-computed. The date-level test is the one that matters
most: it asserts that correlated runs for the same target date are *not* counted
as independent observations.
"""

from __future__ import annotations

import math

import pytest

from weatherquant.models.metrics import (
    brier_multiclass,
    calibration_bins,
    date_level_summary,
    expected_calibration_error,
    log_loss,
    mae,
    rmse,
)

# ---------------------------------------------------------------------------
# Point-forecast metrics
# ---------------------------------------------------------------------------


def test_mae_is_the_mean_absolute_deviation() -> None:
    assert mae([1.0, 2.0, 3.0], [1.5, 2.0, 2.5]) == pytest.approx((0.5 + 0.0 + 0.5) / 3)


def test_rmse_penalises_a_single_large_miss_more_than_mae() -> None:
    true = [0.0, 0.0, 0.0]
    pred = [0.0, 0.0, 4.0]
    assert mae(true, pred) == pytest.approx(4.0 / 3.0)
    assert rmse(true, pred) == pytest.approx(math.sqrt(16.0 / 3.0))
    assert rmse(true, pred) > mae(true, pred)


def test_perfect_forecast_scores_zero() -> None:
    y = [10.0, 20.0, 30.0]
    assert mae(y, y) == 0.0
    assert rmse(y, y) == 0.0


def test_length_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        mae([1.0, 2.0], [1.0])


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        rmse([], [])


# ---------------------------------------------------------------------------
# Probability metrics
# ---------------------------------------------------------------------------


def test_brier_reduces_to_the_binary_score() -> None:
    # (0.7 - 1)^2 + (0.3 - 0)^2 = 0.09 + 0.09
    assert brier_multiclass([[0.7, 0.3]], [[1.0, 0.0]]) == pytest.approx(0.18)


def test_perfect_and_worst_case_brier() -> None:
    assert brier_multiclass([[1.0, 0.0]], [[1.0, 0.0]]) == pytest.approx(0.0)
    # Ranged over [0, 2]: confidently wrong on a binary problem scores 2.
    assert brier_multiclass([[0.0, 1.0]], [[1.0, 0.0]]) == pytest.approx(2.0)


def test_brier_is_the_mean_over_samples_not_the_sum() -> None:
    rows = [[0.7, 0.3], [0.7, 0.3]]
    outcomes = [[1.0, 0.0], [1.0, 0.0]]
    assert brier_multiclass(rows, outcomes) == pytest.approx(0.18)


def test_brier_rejects_probabilities_that_do_not_sum_to_one() -> None:
    with pytest.raises(ValueError, match="not 1"):
        brier_multiclass([[0.7, 0.2]], [[1.0, 0.0]])


def test_log_loss_of_a_ninety_percent_correct_call() -> None:
    assert log_loss([[0.9, 0.1]], [[1.0, 0.0]]) == pytest.approx(-math.log(0.9))


def test_log_loss_of_an_uninformative_call() -> None:
    assert log_loss([[0.5, 0.5]], [[1.0, 0.0]]) == pytest.approx(-math.log(0.5))


def test_log_loss_stays_finite_for_a_confident_wrong_call() -> None:
    """Clipping prevents -inf from poisoning every aggregate of the run."""
    score = log_loss([[0.0, 1.0]], [[1.0, 0.0]])
    assert math.isfinite(score)
    assert score > 30.0  # -ln(1e-15) ~= 34.5


def test_log_loss_ignores_zero_probability_on_unrealised_classes() -> None:
    assert log_loss([[0.9, 0.1]], [[1.0, 0.0]]) == pytest.approx(-math.log(0.9))


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_perfectly_calibrated_bins_have_zero_gap() -> None:
    probs = [0.25] * 8 + [0.75] * 8
    outcomes = [0.0, 0.0, 0.0, 1.0] * 2 + [0.0, 1.0, 1.0, 1.0] * 2
    bins = calibration_bins(probs, outcomes, n_bins=4)

    assert bins, "expected populated bins"
    for b in bins:
        assert b.gap == pytest.approx(0.0, abs=1e-12)
    ece = expected_calibration_error(bins, total_count=len(probs))
    assert ece == pytest.approx(0.0, abs=1e-12)


def test_overconfident_predictions_produce_a_positive_gap() -> None:
    # Model always says 0.9, outcome happens 0% of the time.
    bins = calibration_bins([0.9] * 10, [0.0] * 10, n_bins=10)
    assert len(bins) == 1
    assert bins[0].gap == pytest.approx(0.9)


def test_bin_counts_partition_the_sample() -> None:
    probs = [0.0, 0.5, 0.999, 1.0, 0.33]
    outcomes = [0.0, 1.0, 1.0, 0.0, 1.0]
    bins = calibration_bins(probs, outcomes, n_bins=10)
    assert sum(b.count for b in bins) == len(probs)


def test_probability_of_one_lands_in_the_final_closed_bin() -> None:
    bins = calibration_bins([1.0], [1.0], n_bins=10)
    assert len(bins) == 1
    assert bins[0].lower == pytest.approx(0.9)
    assert bins[0].upper == pytest.approx(1.0)


def test_ece_requires_a_consistent_sample_count() -> None:
    bins = calibration_bins([0.5, 0.5], [1.0, 0.0], n_bins=2)
    with pytest.raises(ValueError, match="bin counts sum"):
        expected_calibration_error(bins, total_count=99)


def test_calibation_rejects_non_binary_outcome() -> None:
    with pytest.raises(ValueError, match="binary outcome"):
        calibration_bins([0.5], [0.4], n_bins=2)


# ---------------------------------------------------------------------------
# Date-level statistical uncertainty
# ---------------------------------------------------------------------------


def test_date_level_summary_collapses_correlated_runs_per_date() -> None:
    # 6 runs but only 3 target dates. Runs within a date share the same weather.
    values = [1.0, 3.0, 2.0, 4.0, 3.0, 5.0]
    dates = ["d1", "d1", "d2", "d2", "d3", "d3"]

    s = date_level_summary(values, dates)

    assert s.n_dates == 3  # NOT 6
    # cluster means: d1=2, d2=3, d3=4 -> mean 3, sample std 1.0
    assert s.mean == pytest.approx(3.0)
    assert s.std_across_dates == pytest.approx(1.0)
    assert s.standard_error == pytest.approx(1.0 / math.sqrt(3))
    assert s.ci95_low == pytest.approx(3.0 - 1.959963984540054 / math.sqrt(3))
    assert s.ci95_high == pytest.approx(3.0 + 1.959963984540054 / math.sqrt(3))


def test_treating_runs_as_independent_would_understate_uncertainty() -> None:
    """The spec's constraint, quantified: naive SE over runs is far too small."""
    values = [1.0, 3.0, 2.0, 4.0, 3.0, 5.0]
    dates = ["d1", "d1", "d2", "d2", "d3", "d3"]

    clustered = date_level_summary(values, dates)
    naive_mean = sum(values) / len(values)
    naive_var = sum((v - naive_mean) ** 2 for v in values) / (len(values) - 1)
    naive_se = math.sqrt(naive_var / len(values))

    assert clustered.standard_error > naive_se
    # The point estimate is unchanged; only the uncertainty differs.
    assert clustered.mean == pytest.approx(naive_mean)


def test_one_date_yields_no_interval_rather_than_a_fake_one() -> None:
    with pytest.raises(ValueError, match=">= 2 distinct target dates"):
        date_level_summary([1.0, 2.0, 3.0], ["d1", "d1", "d1"])


def test_two_dates_are_accepted_but_produce_a_wide_interval() -> None:
    s = date_level_summary([1.0, 1.0, 5.0, 5.0], ["a", "a", "b", "b"])
    assert s.n_dates == 2
    assert s.mean == pytest.approx(3.0)
    assert s.ci95_low < 1.0
    assert s.ci95_high > 5.0


def test_date_level_summary_rejects_mismatched_inputs() -> None:
    with pytest.raises(ValueError, match="same length"):
        date_level_summary([1.0, 2.0], ["a"])

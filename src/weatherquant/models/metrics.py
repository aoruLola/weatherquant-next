"""Out-of-sample scoring metrics for probability and point forecasts.

Every function here is a *pure* scoring rule. Two conventions are fixed by this
module so that results from different baselines and different runs are directly
comparable - the alternative is that two papers quote "Brier" numbers that were
never the same quantity:

``brier_multiclass``
    ``mean_i sum_k (p_ik - y_ik)^2`` - the multi-probability Brier, range
    ``[0, 2]``, reducing to the ordinary ``(p - y)^2`` for a binary problem.
    There is **no** division by the number of classes.

``log_loss``
    ``-mean_i sum_k y_ik * ln(p_ik)`` with probabilities clipped to
    ``[eps, 1]`' before taking the log, because a model that assigns exact zero
    to the realised outcome otherwise scores ``-inf`` and every aggregate of it
    becomes unusable.

Statistical uncertainty
-----------------------
:func:`date_level_summary` exists because of a hard constraint in the spec: two
forecast *runs* for the same target date are correlated samples of the same
weather, not two independent events. Pooling them as if independent shrinks the
standard error by roughly ``sqrt(runs_per_day)`` and turns noise into
"significance". All confidence intervals in this project are therefore computed
at the **date** level.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "CalibrationBin",
    "brier_multiclass",
    "calibration_bins",
    "date_level_summary",
    "expected_calibration_error",
    "log_loss",
    "mae",
    "rmse",
]

PROBABILITY_CLIP = 1e-15


def _check_lengths(a: Sequence[float], b: Sequence[float], name_a: str, name_b: str) -> None:
    if len(a) != len(b):
        raise ValueError(f"{name_a} and {name_b} must have the same length ({len(a)} != {len(b)})")
    if not a:
        raise ValueError(f"{name_a} is empty; cannot score no samples")


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Mean absolute error [same unit as the inputs, e.g. degF]."""
    _check_lengths(y_true, y_pred, "y_true", "y_pred")
    return math.fsum(abs(t - p) for t, p in zip(y_true, y_pred, strict=True)) / len(y_true)


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    """Root mean squared error [same unit as the inputs, e.g. degF]."""
    _check_lengths(y_true, y_pred, "y_true", "y_pred")
    return math.sqrt(math.fsum((t - p) ** 2 for t, p in zip(y_true, y_pred, strict=True)) / len(y_true))


def brier_multiclass(probabilities: Sequence[Sequence[float]], outcomes: Sequence[Sequence[float]]) -> float:
    """Multi-probability Brier score, ``mean_i sum_k (p_ik - y_ik)^2`` in ``[0, 2]``.

    ``outcomes`` must be one-hot (or, more generally, a distribution that sums
    to 1) for the score to be interpretable. Validation is strict: a row that is
    not a proper distribution indicates a bug upstream, not a bad forecast.
    """
    _check_lengths(probabilities, outcomes, "probabilities", "outcomes")
    total = 0.0
    for i, (p_row, y_row) in enumerate(zip(probabilities, outcomes, strict=True)):
        if len(p_row) != len(y_row):
            raise ValueError(f"row {i}: {len(p_row)} probabilities vs {len(y_row)} outcomes")
        if not p_row:
            raise ValueError(f"row {i} is empty")
        p_sum = math.fsum(p_row)
        y_sum = math.fsum(y_row)
        if abs(p_sum - 1.0) > 1e-6:
            raise ValueError(f"row {i}: probabilities sum to {p_sum!r}, not 1")
        if abs(y_sum - 1.0) > 1e-6:
            raise ValueError(f"row {i}: outcomes sum to {y_sum!r}, not 1")
        if any(p < 0.0 or p > 1.0 for p in p_row):
            raise ValueError(f"row {i}: probability outside [0, 1]")
        total += math.fsum((p - y) ** 2 for p, y in zip(p_row, y_row, strict=True))
    return total / len(probabilities)


def log_loss(probabilities: Sequence[Sequence[float]], outcomes: Sequence[Sequence[float]]) -> float:
    """Multi-class log loss / cross-entropy in nats: ``-mean_i sum_k y_ik ln p_ik``.

    Probabilities are clipped to ``PROBABILITY_CLIP`` before the log so a
    confident-but-wrong prediction scores a large finite number instead of
    poisoning the mean with ``inf``.
    """
    _check_lengths(probabilities, outcomes, "probabilities", "outcomes")
    total = 0.0
    for i, (p_row, y_row) in enumerate(zip(probabilities, outcomes, strict=True)):
        if len(p_row) != len(y_row):
            raise ValueError(f"row {i}: {len(p_row)} probabilities vs {len(y_row)} outcomes")
        if abs(math.fsum(p_row) - 1.0) > 1e-6:
            raise ValueError(f"row {i}: probabilities sum to {math.fsum(p_row)!r}, not 1")
        for p, y in zip(p_row, y_row, strict=True):
            if y == 0.0:
                continue
            clipped = min(max(p, PROBABILITY_CLIP), 1.0)
            total -= y * math.log(clipped)
    return total / len(probabilities)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One reliability-diagram bin."""

    predicted_mean: float  # mean forecast probability within the bin
    observed_frequency: float  # empirical frequency of the outcome
    count: int  # number of samples in the bin
    lower: float
    upper: float

    @property
    def gap(self) -> float:
        """signed miscalibration: positive means over-confident."""
        return self.predicted_mean - self.observed_frequency


def calibration_bins(
    probabilities: Sequence[float],
    outcomes: Sequence[float],
    *,
    n_bins: int = 10,
) -> list[CalibrationBin]:
    """Reliability data: bin forecasts by predicted probability, compare to observed rate.

    Bins are ``[k/n, (k+1)/n)`` with the final bin closed on the right so that
    ``p = 1.0`` is binned rather than dropped. Empty bins are omitted (they
    carry no information), which is why ``sum(b.count)`` equals the sample size.
    """
    _check_lengths(probabilities, outcomes, "probabilities", "outcomes")
    if n_bins < 2:
        raise ValueError(f"n_bins must be >= 2, got {n_bins}")

    # Accumulate sums rather than keeping member lists: the bin's representative
    # forecast is its own mean prediction, which is where its mass actually sits
    # (the bin centre is not).
    pred_sum = [0.0] * n_bins
    obs_sum = [0.0] * n_bins
    counts = [0] * n_bins
    for p, y in zip(probabilities, outcomes, strict=True):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"probability {p} outside [0, 1]")
        if y not in (0.0, 1.0):
            raise ValueError(f"binary outcome must be 0 or 1, got {y}")
        idx = min(int(p * n_bins), n_bins - 1)  # final bin closed on the right
        pred_sum[idx] += p
        obs_sum[idx] += y
        counts[idx] += 1

    bins: list[CalibrationBin] = []
    for k in range(n_bins):
        if counts[k] == 0:
            continue
        bins.append(
            CalibrationBin(
                predicted_mean=pred_sum[k] / counts[k],
                observed_frequency=obs_sum[k] / counts[k],
                count=counts[k],
                lower=k / n_bins,
                upper=(k + 1) / n_bins,
            )
        )
    return bins


def expected_calibration_error(bins: Sequence[CalibrationBin], *, total_count: int) -> float:
    """ECE: ``sum_g (n_g / N) * |predicted_g - observed_g|`` in ``[0, 1]``."""
    if total_count <= 0:
        raise ValueError(f"total_count must be positive, got {total_count}")
    if sum(b.count for b in bins) != total_count:
        raise ValueError(
            f"bin counts sum to {sum(b.count for b in bins)} but total_count is {total_count}; "
            "pass the number of samples the bins were built from"
        )
    return math.fsum((b.count / total_count) * abs(b.gap) for b in bins)


@dataclass(frozen=True, slots=True)
class DateLevelSummary:
    """Mean and confidence interval computed **across dates**, not across runs."""

    n_dates: int
    mean: float
    std_across_dates: float
    standard_error: float
    ci95_low: float
    ci95_high: float


def date_level_summary(
    values: Sequence[float],
    cluster_ids: Sequence[object],
    *,
    z: float = 1.959963984540054,
) -> DateLevelSummary:
    """Aggregate per-sample values into a date-level mean with a 95% CI.

    ``values`` are per-sample scores (one per forecast run). ``cluster_ids`` are
    the target dates those runs predict. Runs sharing a ``cluster_id`` describe
    the same day's weather and are **not** independent, so each cluster is first
    collapsed to its own mean and the standard error is taken over the ``G``
    cluster means: ``SE = s / sqrt(G)``.

    Treating runs as independent instead would divide by ``sqrt(N)`` where
    ``N >> G``, understating uncertainty by roughly ``sqrt(N/G)``.

    Requires at least two clusters: with one date there is no between-date
    variance to estimate, and reporting an interval from it would be fabrication.
    """
    _check_lengths(values, cluster_ids, "values", "cluster_ids")
    grouped: dict[object, list[float]] = {}
    for v, cid in zip(values, cluster_ids, strict=True):
        grouped.setdefault(cid, []).append(float(v))

    g = len(grouped)
    if g < 2:
        raise ValueError(
            f"date-level uncertainty needs >= 2 distinct target dates, got {g}. "
            "Collapsing correlated runs into an interval would invent precision."
        )

    cluster_means = sorted(math.fsum(xs) / len(xs) for xs in grouped.values())
    mean = math.fsum(cluster_means) / g
    variance = math.fsum((m - mean) ** 2 for m in cluster_means) / (g - 1)
    std = math.sqrt(variance)
    se = std / math.sqrt(g)
    return DateLevelSummary(
        n_dates=g,
        mean=mean,
        std_across_dates=std,
        standard_error=se,
        ci95_low=mean - z * se,
        ci95_high=mean + z * se,
    )

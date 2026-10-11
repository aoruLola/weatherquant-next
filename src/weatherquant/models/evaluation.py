"""Out-of-sample evaluation: chronological splitting and date-level scoring.

Three constraints from the spec are enforced structurally here rather than by
convention, because each one is easy to violate without noticing:

**Split by date, never by row.** Several runs predict the same target day.
Splitting rows would put some runs for a day in training and others in test,
and the model would be scored on days it was fitted to. :func:`chronological_split`
partitions on ``target_date`` and asserts the two sides do not overlap.

**Score at the date level.** Runs for one day are correlated samples of that
day's weather. :func:`score` reports a naive per-row metric *and* a date-level
one built with :func:`weatherquant.models.metrics.date_level_summary`, so the
difference between "many rows" and "many independent days" is visible instead
of hidden.

**Compare models only on identical date sets.** A baseline that is scored on
different days than another is not comparable. :func:`evaluate_pairwise`
compares on the intersection and reports how many dates each side had to give
up to get there.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from weatherquant.models.metrics import (
    brier_multiclass,
    calibration_bins,
    date_level_summary,
    expected_calibration_error,
    log_loss,
)

__all__ = [
    "BaselineScore",
    "chronological_split",
    "evaluate_pairwise",
    "one_hot",
    "score",
]


@dataclass(frozen=True, slots=True)
class BaselineScore:
    """Scores for one baseline over one set of test dates."""

    name: str
    n_rows: int
    n_dates: int
    mae: float | None
    rmse: float | None
    brier: float
    log_loss: float
    ece: float | None
    date_level: dict  # from date_level_summary: mean, se, ci95_low/high
    test_dates: tuple[date, ...]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "n_rows": self.n_rows,
            "n_dates": self.n_dates,
            "mae": self.mae,
            "rmse": self.rmse,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "ece": self.ece,
            **{f"date_{k}": v for k, v in self.date_level.items()},
        }


def chronological_split(
    pairs: Sequence[dict],
    *,
    train_fraction: float = 0.7,
) -> tuple[list[dict], list[dict], date, date]:
    """Split into (train, test) by target date, oldest first.

    Returns ``(train, test, last_train_date, first_test_date)``. Raises if the
    same date appears on both sides - which is exactly what a row-level split
    would do.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must be in (0, 1), got {train_fraction}")

    by_date: dict[date, list[dict]] = {}
    for pair in pairs:
        d = pair.get("target_date")
        if d is None:
            continue
        by_date.setdefault(d, []).append(pair)

    dates = sorted(by_date)
    if len(dates) < 2:
        raise ValueError(
            f"need at least 2 distinct target dates to split chronologically, got {len(dates)}"
        )

    boundary_index = max(1, min(len(dates) - 1, int(len(dates) * train_fraction)))
    train_dates = set(dates[:boundary_index])
    test_dates = set(dates[boundary_index:])

    if train_dates & test_dates:
        raise AssertionError("chronological split leaked a date across the boundary")

    train = [p for d in sorted(train_dates) for p in by_date[d]]
    test = [p for d in sorted(test_dates) for p in by_date[d]]
    return train, test, max(train_dates), min(test_dates)


def one_hot(labels: Sequence[str], classes: Sequence[str]) -> list[list[float]]:
    """One-hot encode outcomes for a multi-class score."""
    index = {c: i for i, c in enumerate(classes)}
    out = []
    for label in labels:
        if label not in index:
            raise ValueError(f"unknown class {label!r}; known: {sorted(index)}")
        row = [0.0] * len(classes)
        row[index[label]] = 1.0
        out.append(row)
    return out


def score(
    *,
    name: str,
    predicted_brackets: Sequence[str],
    actual_brackets: Sequence[str],
    classes: Sequence[str],
    dates: Sequence[date],
    predicted_max_f: Sequence[float] | None = None,
    observed_max_f: Sequence[float] | None = None,
) -> BaselineScore:
    """Score one baseline.

    ``brier``/``log_loss`` are the multi-class metrics from
    :mod:`weatherquant.models.metrics`. ``date_level`` carries a confidence
    interval built over *dates*, not rows - see the module docstring.

    Point-forecast metrics (``mae``/``rmse``) are returned only when the
    temperature values were supplied; a market-probability baseline has no
    point forecast, and inventing one would make it look like it did.
    """
    if len(predicted_brackets) != len(actual_brackets) or len(predicted_brackets) != len(dates):
        raise ValueError("predicted_brackets, actual_brackets and dates must align")
    if not predicted_brackets:
        raise ValueError("nothing to score")

    probs = _probability_rows(predicted_brackets, classes)
    outcomes = one_hot(list(actual_brackets), classes)

    brier = brier_multiclass(probs, outcomes)
    nll = log_loss(probs, outcomes)

    # Calibration needs a binary event; use "predicted the class that happened".
    hits = [
        1.0 if row.index(max(row)) == truth.index(1.0) else 0.0
        for row, truth in zip(probs, outcomes, strict=True)
    ]
    confidence = [max(row) for row in probs]
    bins = calibration_bins(confidence, hits, n_bins=min(10, max(2, len(hits) // 5)))
    ece = expected_calibration_error(bins, total_count=len(hits))

    mae = rmse = None
    if predicted_max_f is not None and observed_max_f is not None:
        from weatherquant.models.metrics import mae as mae_fn
        from weatherquant.models.metrics import rmse as rmse_fn

        mae = mae_fn(list(observed_max_f), list(predicted_max_f))
        rmse = rmse_fn(list(observed_max_f), list(predicted_max_f))

    # Per-date Brier, so the interval is over independent days. Values and
    # cluster ids are built from the SAME ordered key list - pairing a value
    # list with a differently sorted cluster list would silently mislabel
    # every date.
    per_date: dict[date, list[float]] = {}
    for d, row, truth in zip(dates, probs, outcomes, strict=True):
        per_date.setdefault(d, []).append(
            math.fsum((p - t) ** 2 for p, t in zip(row, truth, strict=True))
        )
    ordered_dates = sorted(per_date)
    date_values = [math.fsum(per_date[d]) / len(per_date[d]) for d in ordered_dates]

    if len(ordered_dates) >= 2:
        summary = date_level_summary(date_values, ordered_dates)
        date_level = {
            "mean": summary.mean,
            "standard_error": summary.standard_error,
            "ci95_low": summary.ci95_low,
            "ci95_high": summary.ci95_high,
        }
    else:
        # One date carries no between-date variance; reporting an interval from
        # it would invent precision. The metric is reported without one.
        date_level = {
            "mean": date_values[0],
            "standard_error": None,
            "ci95_low": None,
            "ci95_high": None,
        }

    return BaselineScore(
        name=name,
        n_rows=len(probs),
        n_dates=len(per_date),
        mae=mae,
        rmse=rmse,
        brier=brier,
        log_loss=nll,
        ece=ece,
        date_level=date_level,
        test_dates=tuple(ordered_dates),
    )


def _probability_rows(
    predicted_brackets: Sequence[str], classes: Sequence[str]
) -> list[list[float]]:
    """Convert hard predictions to rows that sum to 1.

    A baseline that only produces a most-likely bracket still has to be scored
    against a proper probability metric, so its mass is concentrated on the
    class it named. That is a deliberate, documented encoding - it makes the
    baseline's *certainty* visible in the log loss rather than hiding it.
    """
    index = {c: i for i, c in enumerate(classes)}
    rows = []
    for label in predicted_brackets:
        if label not in index:
            raise ValueError(f"predicted class {label!r} is not in classes {sorted(index)}")
        row = [0.0] * len(classes)
        row[index[label]] = 1.0
        rows.append(row)
    return rows


def evaluate_pairwise(
    scores: Sequence[BaselineScore],
    *,
    other: BaselineScore,
) -> dict:
    """Compare each score to ``other`` on the intersection of their date sets.

    Reporting a comparison only over shared dates makes clear when a "better"
    model is actually just being scored on easier days.
    """
    other_days = set(other.test_dates)
    out: dict[str, dict] = {}
    for s in scores:
        shared = sorted(set(s.test_dates) & other_days)
        out[s.name] = {
            "shared_dates": len(shared),
            "only_in_this": len(set(s.test_dates) - other_days),
            "only_in_other": len(other_days - set(s.test_dates)),
            "brier_this": s.brier,
            "brier_other": other.brier,
            "better_on_shared": s.brier < other.brier,
        }
    return out

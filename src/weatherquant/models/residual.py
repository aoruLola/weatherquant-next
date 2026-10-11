"""Residual model: turn a deterministic forecast's historical errors into spread.

Why this exists
---------------
The only forecast source available is a *single deterministic run* per issue
time - there are no ensemble members to spread over. So the uncertainty in a
forecast has to come from how wrong that kind of forecast has been before, and
that has to be measured on data the forecast could not have seen.

Three rules are load-bearing, each of them a mistake an audited open-source bot
actually made (see docs/open-source-review.md):

1. **Never take the spread from a single scalar like MAE.** Under a normal
   distribution ``MAE = sigma * sqrt(2/pi)``, so ``sigma = MAE * sqrt(pi/2)``
   (about 1.253x). Setting ``sigma = MAE`` understates the spread by 20%,
   makes every tail look impossible, and inflates every probability computed
   from it. If only MAE is available, :func:`sigma_from_mae` applies the
   factor; when residuals are available, :func:`fit_residual_model` uses their
   standard deviation directly and the conversion never enters.

2. **Collapse by date before measuring spread.** Several runs predict the same
   day, and their errors share that day's weather - they are not independent
   samples. Pooling them directly shrinks the standard error by roughly
   ``sqrt(runs_per_day)`` and turns one lucky day into apparent skill. Within a
   lead-time bucket, residuals are averaged per target date first, and the
   spread is taken across dates.

3. **Fit only on data that precedes what it is scored on.** The caller supplies
   the split; this module records the window it saw so a later reader can check
   that it did.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

__all__ = [
    "LEAD_BUCKETS",
    "ResidualModel",
    "fit_residual_model",
    "lead_bucket",
    "sigma_from_mae",
]

# Lead-time buckets in hours from run initialisation to the end of the target
# local day. Coarse on purpose: 63 target dates cannot support fine buckets
# without each one resting on two samples.
LEAD_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 24.0, "0-24h"),
    (24.0, 48.0, "24-48h"),
    (48.0, 72.0, "48-72h"),
    (72.0, 120.0, "72-120h"),
    (120.0, math.inf, "120h+"),
)


def lead_bucket(lead_hours: float) -> str:
    """Bucket a lead time. Negative leads are a data error, not an early forecast."""
    if lead_hours < 0:
        raise ValueError(f"lead_hours must be >= 0, got {lead_hours}")
    for low, high, name in LEAD_BUCKETS:
        if low <= lead_hours < high:
            return name
    return LEAD_BUCKETS[-1][2]


def sigma_from_mae(mae: float) -> float:
    """Convert a mean absolute error to a normal-distribution sigma.

    ``E|X| = sigma * sqrt(2/pi)`` for zero-mean ``X ~ N(0, sigma^2)``, hence
    ``sigma = MAE * sqrt(pi/2)``. Returning ``mae`` unchanged - the bug found in
    an audited competitor - understates the spread by 20%.
    """
    if mae < 0:
        raise ValueError(f"mae must be >= 0, got {mae}")
    return mae * math.sqrt(math.pi / 2.0)


@dataclass(frozen=True, slots=True)
class ResidualModel:
    """How wrong a model's daily-max forecast has been, by lead time."""

    model_id: str
    bias_f: float
    sigma_by_lead: dict[str, float]
    counts_by_lead: dict[str, int]
    n_pairs: int
    n_dates: int
    train_start: date | None
    train_end: date | None
    min_samples_per_bucket: int = 5
    fit_notes: tuple[str, ...] = field(default_factory=tuple)

    def sigma(self, lead_hours: float) -> float | None:
        """Spread for a lead time, or ``None`` when the bucket is too thin.

        Returning ``None`` rather than falling back to a global sigma is
        deliberate: a number derived from two samples is not a spread, and a
        caller that refuses to trade on it is behaving correctly.
        """
        name = lead_bucket(lead_hours)
        if self.counts_by_lead.get(name, 0) < self.min_samples_per_bucket:
            return None
        return self.sigma_by_lead.get(name)

    def is_usable(self, lead_hours: float) -> bool:
        return self.sigma(lead_hours) is not None

    @property
    def is_fitted(self) -> bool:
        return self.n_pairs > 0 and bool(self.sigma_by_lead)

    def summary(self) -> dict:
        return {
            "model_id": self.model_id,
            "bias_f": round(self.bias_f, 4),
            "n_pairs": self.n_pairs,
            "n_dates": self.n_dates,
            "train_start": str(self.train_start),
            "train_end": str(self.train_end),
            "sigma_by_lead": {k: round(v, 4) for k, v in sorted(self.sigma_by_lead.items())},
            "counts_by_lead": dict(sorted(self.counts_by_lead.items())),
            "notes": list(self.fit_notes),
        }


def _collapse_by_date(
    rows: Sequence[tuple[date, str, float]],
) -> tuple[list[float], int]:
    """Average residuals within (target_date, lead_bucket), then split by date.

    Returns the per-date means and the number of distinct dates. Averaging
    first means a day with four runs contributes one observation to the spread,
    not four - which is what makes the spread an estimate of *day-to-day*
    error rather than of run-to-run jitter within a day.
    """
    grouped: dict[tuple[date, str], list[float]] = {}
    for target_date, bucket, residual in rows:
        grouped.setdefault((target_date, bucket), []).append(residual)

    per_date: dict[date, list[float]] = {}
    for (target_date, _bucket), values in grouped.items():
        per_date.setdefault(target_date, []).append(sum(values) / len(values))

    flattened: list[float] = []
    for values in per_date.values():
        flattened.append(sum(values) / len(values))
    return flattened, len(per_date)


def fit_residual_model(
    pairs: Iterable[dict],
    *,
    model_id: str,
    min_samples_per_bucket: int = 5,
    bias: float | None = None,
) -> ResidualModel:
    """Fit bias and per-lead-time spread from ``(forecast, observation)`` pairs.

    Each pair needs ``target_date``, ``lead_hours``, ``predicted_max_f`` and
    ``observed_max_f``. ``residual = observed - predicted``, so a positive bias
    means the model has been running cold.

    ``bias`` may be supplied when it was fitted elsewhere; otherwise it is the
    mean residual over all pairs. Note that the spread is measured *within*
    lead buckets after collapsing by date, while the bias is a plain mean -
    bias is a location, and one value per day and one per run give the same
    mean when dates are equally represented, which they are here.
    """
    rows: list[tuple[date, str, float]] = []
    residuals_all: list[float] = []
    dates: set[date] = set()
    skipped = 0

    for pair in pairs:
        target_date = pair.get("target_date")
        predicted = pair.get("predicted_max_f")
        observed = pair.get("observed_max_f")
        lead = pair.get("lead_hours")
        if target_date is None or predicted is None or observed is None or lead is None:
            skipped += 1
            continue
        if predicted != predicted or observed != observed:  # NaN
            skipped += 1
            continue
        bucket = lead_bucket(float(lead))
        residual = float(observed) - float(predicted)
        rows.append((target_date, bucket, residual))
        residuals_all.append(residual)
        dates.add(target_date)

    notes: list[str] = []
    if not rows:
        return ResidualModel(
            model_id=model_id,
            bias_f=0.0,
            sigma_by_lead={},
            counts_by_lead={},
            n_pairs=0,
            n_dates=0,
            train_start=None,
            train_end=None,
            min_samples_per_bucket=min_samples_per_bucket,
            fit_notes=("no usable (forecast, observation) pairs",),
        )
    if skipped:
        notes.append(f"{skipped} pair(s) skipped: missing or non-numeric values")

    fitted_bias = float(bias) if bias is not None else math.fsum(residuals_all) / len(residuals_all)

    sigma: dict[str, float] = {}
    counts: dict[str, int] = {}
    for _low, _high, name in LEAD_BUCKETS:
        bucket_rows = [(d, b, r) for d, b, r in rows if b == name]
        if not bucket_rows:
            continue
        collapsed, n_dates = _collapse_by_date(bucket_rows)
        counts[name] = n_dates
        if n_dates >= 2:
            mean = math.fsum(collapsed) / n_dates
            variance = math.fsum((x - mean) ** 2 for x in collapsed) / (n_dates - 1)
            sigma[name] = math.sqrt(max(variance, 0.0))
        else:
            notes.append(f"bucket {name}: {n_dates} date(s), spread not estimable")

    return ResidualModel(
        model_id=model_id,
        bias_f=fitted_bias,
        sigma_by_lead=sigma,
        counts_by_lead=counts,
        n_pairs=len(rows),
        n_dates=len(dates),
        train_start=min(dates) if dates else None,
        train_end=max(dates) if dates else None,
        min_samples_per_bucket=min_samples_per_bucket,
        fit_notes=tuple(notes),
    )

"""Turn a point forecast into a probability distribution over a market's brackets.

The chain
---------
``residual model`` gives the distribution of (observation - forecast) by lead
time; :class:`~weatherquant.markets.models.Bracket` gives the continuous
interval a quoted bucket covers; this module joins them:

.. code-block:: text

    corrected = predicted_max_f + bias
    P(bracket) = Phi((hi - corrected) / sigma) - Phi((lo - corrected) / sigma)

``[lo, hi)`` comes from ``Bracket.probability_interval_f()``, which applies the
half-degree widening that whole-degree quoting requires. Re-deriving the
interval here would be exactly the inconsistency that produced a factor-of-two
error in an audited competitor, so there is exactly one implementation of it.

Three refusals, all deliberate
------------------------------
* **No sigma, no probability.** If the lead-time bucket has too few dates to
  estimate a spread, this returns ``None`` rather than borrowing a global
  sigma. A spread from two samples is not a spread.
* **No complete ladder, no distribution.** A set of brackets that does not
  cover the real line cannot sum to 1, and forcing it to by renormalising
  would invent probability mass.
* **No clamping to make things sum.** Rounding is applied only where a
  distribution is already valid to within tolerance.

``Phi`` is computed with :func:`math.erf`, which is exact and stdlib - no
scipy call sits between the model and the number that decides a trade.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from weatherquant.markets.models import Bracket, BracketDistribution, BracketProbability
from weatherquant.models.residual import ResidualModel

__all__ = [
    "bracket_probability",
    "distribution_for_event",
    "is_complete_ladder",
    "normal_cdf",
]


def normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function: ``0.5 * (1 + erf(x/sqrt(2)))``."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def is_complete_ladder(brackets: Sequence[Bracket]) -> bool:
    """True if the brackets have both an unbounded-low and an unbounded-high end.

    Only such a ladder can cover every possible observation, which is what makes
    its probabilities sum to 1 without renormalisation.
    """
    has_open_low = any(b.low is None for b in brackets)
    has_open_high = any(b.high is None for b in brackets)
    return has_open_low and has_open_high


def bracket_probability(
    predicted_max_f: float,
    bracket: Bracket,
    *,
    model: ResidualModel,
    lead_hours: float,
) -> float | None:
    """Probability that this run's outcome lands in ``bracket``.

    Returns ``None`` when the model has no usable spread for ``lead_hours``.
    """
    sigma = model.sigma(lead_hours)
    if sigma is None:
        return None
    if sigma <= 0.0:
        # A zero spread means every historical residual was identical; that is
        # a degenerate fit, not a certain outcome.
        return None

    corrected = predicted_max_f + model.bias_f
    lo, hi = bracket.probability_interval_f()
    return normal_cdf((hi - corrected) / sigma) - normal_cdf((lo - corrected) / sigma)


def distribution_for_event(
    predicted_max_f: float,
    brackets: Sequence[Bracket],
    *,
    model: ResidualModel,
    lead_hours: float,
    station: str,
    target_date,
    model_id: str,
    forecast_run_time_utc=None,
) -> BracketDistribution:
    """A full distribution over one event's brackets.

    Raises ``ValueError`` if the ladder is incomplete or if any bracket's
    probability could not be computed - a partially populated distribution is
    not a distribution, and shipping one would make every downstream score
    meaningless.
    """
    if not brackets:
        raise ValueError("no brackets supplied")
    if not is_complete_ladder(brackets):
        open_labels = [b.label for b in brackets if b.low is None or b.high is None]
        raise ValueError(
            "brackets do not form a complete ladder (need one open at each end); "
            f"unbounded ends present: {open_labels}. Without both, the "
            "probabilities cannot sum to 1 without inventing mass."
        )

    sigma = model.sigma(lead_hours)
    if sigma is None:
        raise ValueError(
            f"no usable spread for lead_hours={lead_hours:.1f} "
            f"(model={model.model_id}); refusing to emit a distribution"
        )

    probabilities: list[BracketProbability] = []
    for bracket in brackets:
        value = bracket_probability(
            predicted_max_f, bracket, model=model, lead_hours=lead_hours
        )
        if value is None:
            raise ValueError(f"probability not computable for bracket {bracket.label!r}")
        probabilities.append(BracketProbability(label=bracket.label, probability=value))

    return BracketDistribution(
        station=station,
        target_date=target_date,
        model_id=model_id,
        forecast_run_time_utc=forecast_run_time_utc,
        probabilities=tuple(probabilities),
    )

"""The market baseline: what the venue itself believed, at the same instant.

This is the number every model baseline is measured against. It needs no
forecast model at all - it reads the venue's own mid prices - which is exactly
why it must obey the *same* point-in-time rule as the forecasts. A model scored
against a market price taken later than the model's own availability would be
comparing a guess to an answer.

Decision instant
----------------
The latest mid at or before the **start of the target local day**, matching
:func:`weatherquant.weather.daily_max.point_in_time`. Anything later could
already contain information about the day being predicted.

Two things this deliberately does not do:

* **It does not use the settlement outcome.** The winning bracket is what
  happened, not what was believed; using it here would make the baseline
  omniscient.
* **It does not treat a mid as executable.** Mids record belief, and belief is
  what is being scored. Nothing downstream may use them as fill prices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from weatherquant.models.baselines import EventLadder
from weatherquant.models.evaluation import BaselineScore, score
from weatherquant.weather.daily_max import local_day_bounds_utc

__all__ = ["MarketImplied", "market_implied_probabilities", "run_market_baseline"]


@dataclass(frozen=True, slots=True)
class MarketImplied:
    target_date: date
    decision_instant_utc: datetime
    probabilities: dict[str, float]   # bracket label -> mid of its YES token
    n_tokens_priced: int
    missing: tuple[str, ...]          # brackets with no usable price


def _decision_instant(target_date: date) -> datetime:
    """Start of the target local day - the same deadline forecasts are held to."""
    return local_day_bounds_utc(target_date)[0]


def market_implied_probabilities(
    con,
    *,
    target_date: date,
    event_id: str,
    ladder: EventLadder,
) -> MarketImplied:
    """Latest YES-token mid per bracket, at or before the decision instant."""
    deadline = _decision_instant(target_date)
    tokens = con.execute(
        "SELECT market_id, bracket_label, token_id FROM market_tokens "
        "WHERE event_id = ? AND target_date = ? AND is_yes_token",
        [event_id, target_date],
    ).fetchall()

    probabilities: dict[str, float] = {}
    missing: list[str] = []
    seen_labels: set[str] = set()

    for market_id, label, token_id in tokens:
        if label is None:
            continue
        row = con.execute(
            "SELECT mid_usd_per_share FROM market_price_history "
            "WHERE market_id = ? AND token_id = ? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            [market_id, token_id, deadline],
        ).fetchone()
        if row is None:
            missing.append(label)
            continue
        probabilities[label] = float(row[0])
        seen_labels.add(label)

    for bracket in ladder.brackets:
        if bracket.label not in seen_labels:
            missing.append(bracket.label)

    return MarketImplied(
        target_date=target_date,
        decision_instant_utc=deadline,
        probabilities=probabilities,
        n_tokens_priced=len(probabilities),
        missing=tuple(sorted(set(missing))),
    )


def run_market_baseline(
    con,
    *,
    ladders: dict[date, EventLadder],
    dates: list[date] | None = None,
) -> tuple[BaselineScore | None, dict]:
    """Score the market's own beliefs on every date it priced.

    Returns ``(score, diagnostics)``. Dates where the market could not be priced
    before the deadline are excluded and counted in ``diagnostics`` rather than
    dropped silently - a market baseline with holes in it must say so.
    """
    target_dates = sorted(ladders) if dates is None else sorted(dates)

    predicted: list[str] = []
    actual: list[str] = []
    scored_dates: list[date] = []
    unpriced: list[str] = []
    no_winner: list[str] = []

    for target_date in target_dates:
        ladder = ladders[target_date]
        if ladder.winning_label is None:
            no_winner.append(str(target_date))
            continue
        implied = market_implied_probabilities(
            con, target_date=target_date, event_id=ladder.event_id, ladder=ladder
        )
        if not implied.probabilities:
            unpriced.append(str(target_date))
            continue

        best_label = max(implied.probabilities, key=lambda k: implied.probabilities[k])
        if implied.missing and best_label not in {b.label for b in ladder.brackets}:
            unpriced.append(str(target_date))
            continue

        predicted.append(best_label)
        actual.append(ladder.winning_label)
        scored_dates.append(target_date)

    diagnostics = {
        "dates_considered": len(target_dates),
        "dates_scored": len(predicted),
        "dates_unpriced_before_deadline": len(unpriced),
        "dates_without_a_unique_winner": len(no_winner),
        "examples_unpriced": unpriced[:5],
    }
    if not predicted:
        return None, diagnostics

    classes = sorted({b.label for d in ladders.values() for b in d.brackets})
    return (
        score(
            name="market",
            predicted_brackets=predicted,
            actual_brackets=actual,
            classes=classes,
            dates=scored_dates,
            # A market baseline has no point forecast; leaving these out is
            # what keeps the comparison honest.
            predicted_max_f=None,
            observed_max_f=None,
        ),
        diagnostics,
    )

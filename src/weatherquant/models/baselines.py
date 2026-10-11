"""Baselines: what a model must beat before any strategy is worth building.

Four model baselines and one market baseline, all evaluated the same way:

======================  ==========================================================
name                    point forecast
======================  ==========================================================
``ecmwf_raw``           ECMWF run as issued (no bias correction)
``gfs_raw``             GFS run as issued
``fusion``              mean of the two
``calibrated``          best raw point + fitted bias correction
``market``              the venue's own implied probability at decision time
======================  ==========================================================

The raw/calibrated pair is an ablation, not two random variants: both use the
same fitted spread, so any difference in score is attributable to the bias
correction alone. That is the only honest way to answer "does calibration help?"

Every model baseline shares one probability path
(:func:`weatherquant.models.probability.distribution_for_event`) so a difference
in results cannot come from a difference in plumbing.

The market baseline is deliberately separate: it never touches a forecast model,
and it is scored only on dates where the venue's price is available at a
decision instant that satisfies the same point-in-time rule as the forecasts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from weatherquant.markets.models import Bracket
from weatherquant.models.evaluation import BaselineScore, chronological_split, score
from weatherquant.models.residual import ResidualModel, fit_residual_model

__all__ = [
    "BaselineRun",
    "EventLadder",
    "load_event_ladders",
    "load_forecast_observation_pairs",
    "run_model_baselines",
]


@dataclass(frozen=True, slots=True)
class EventLadder:
    """One event's full bracket set plus the bracket it resolved to."""

    event_id: str
    target_date: date
    brackets: tuple[Bracket, ...]
    winning_label: str | None          # None when the event has no unique winner
    n_winners: int


def load_event_ladders(con) -> dict[date, EventLadder]:
    """Map a target date to its event's brackets and winner.

    Dates on which two events resolved differently are **excluded** rather than
    merged: the legacy data contains an ``arch-`` duplicate whose resolution
    contradicts its sibling, and averaging them would invent a bracket that no
    market ever paid out on. The exclusion is reported by the caller.
    """
    rows = con.execute(
        "SELECT event_id, target_date, bracket_label, bracket_low, bracket_high, is_winner "
        "FROM settlement_outcome ORDER BY event_id, market_id"
    ).fetchall()

    by_event: dict[str, dict] = {}
    for event_id, target_date, label, low, high, is_winner in rows:
        entry = by_event.setdefault(
            event_id,
            {"target_date": target_date, "brackets": [], "winners": []},
        )
        from weatherquant.markets.models import BracketKind

        try:
            if low is None and high is not None:
                b = Bracket(label=label, kind=BracketKind.BELOW, low=None, high=high)
            elif high is None and low is not None:
                b = Bracket(label=label, kind=BracketKind.ABOVE, low=low, high=None)
            else:
                b = Bracket(label=label, kind=BracketKind.RANGE, low=low, high=high)
        except ValueError:
            continue
        entry["brackets"].append(b)
        if is_winner:
            entry["winners"].append(label)

    ladders: dict[date, EventLadder] = {}
    for event_id, payload in by_event.items():
        winners = payload["winners"]
        ladders[payload["target_date"]] = EventLadder(
            event_id=event_id,
            target_date=payload["target_date"],
            brackets=tuple(payload["brackets"]),
            winning_label=winners[0] if len(winners) == 1 else None,
            n_winners=len(winners),
        )
    return ladders


def load_forecast_observation_pairs(
    con,
    *,
    model: str,
    observation_rule: str = "max of all readings",
    ladders: dict[date, EventLadder] | None = None,
) -> tuple[list[dict], list[str]]:
    """Join one model's daily forecasts to the observed daily maximum.

    Returns ``(pairs, problems)``. A pair is dropped, and listed in
    ``problems``, when:

    * the observation for that rule is missing;
    * the date's event has no unique winner (two events disagreed);
    * the date has no bracket ladder to score against.

    Dropping is recorded rather than silent - a shorter series you know about
    beats a longer one you do not.
    """
    forecasts = con.execute(
        "SELECT target_date, run_time_utc, predicted_max_f, n_hours, n_missing_hours "
        "FROM forecast_daily_max WHERE model = ? AND predicted_max_f IS NOT NULL",
        [model],
    ).fetchall()
    observations = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT target_date, max_temp_f FROM klga_daily_max "
            "WHERE aggregation_rule = ? AND max_temp_f IS NOT NULL",
            [observation_rule],
        ).fetchall()
    }
    if ladders is None:
        ladders = load_event_ladders(con)

    pairs: list[dict] = []
    problems: list[str] = []
    reported: set[date] = set()

    # Every run is kept as its own pair: runs for one date are correlated, but
    # dropping all but one would throw away most of the series and pick an
    # arbitrary survivor. Correlation is handled where it belongs - the split
    # is by date and the interval is over dates.
    for target_date, run_time, predicted, n_hours, n_missing in forecasts:
        observed = observations.get(target_date)
        if observed is None:
            if target_date not in reported:
                problems.append(f"{target_date}: no observation under {observation_rule!r}")
                reported.add(target_date)
            continue
        ladder = ladders.get(target_date)
        if ladder is None:
            if target_date not in reported:
                problems.append(f"{target_date}: no settlement ladder")
                reported.add(target_date)
            continue
        if ladder.winning_label is None:
            if target_date not in reported:
                problems.append(
                    f"{target_date}: {ladder.n_winners} events disagree on the winner "
                    f"(event {ladder.event_id}); excluded rather than merged"
                )
                reported.add(target_date)
            continue

        day_end = _end_of_local_day(target_date)
        pairs.append(
            {
                "target_date": target_date,
                "run_time_utc": run_time,
                "lead_hours": (day_end - run_time).total_seconds() / 3600.0,
                "predicted_max_f": predicted,
                "observed_max_f": observed,
                "n_hours": n_hours,
                "n_missing_hours": n_missing,
                "actual_bracket": ladder.winning_label,
                "event_id": ladder.event_id,
            }
        )

    pairs.sort(key=lambda p: p["target_date"])
    return pairs, problems


def _end_of_local_day(target_date: date) -> datetime:
    from weatherquant.weather.daily_max import local_day_bounds_utc

    return local_day_bounds_utc(target_date)[1]


@dataclass
class BaselineRun:
    """Result of evaluating one baseline."""

    name: str
    scores: dict[str, BaselineScore]
    problems: list[str]
    train_start: date | None
    train_end: date | None
    test_start: date | None
    test_end: date | None
    residual_summary: dict


def run_model_baselines(
    con,
    *,
    models: Sequence[str],
    observation_rule: str = "max of all readings",
    train_fraction: float = 0.7,
) -> dict[str, BaselineRun]:
    """Fit and score every model baseline on a strictly chronological split.

    The split and the residual fit happen **per model**, on the *training*
    dates only. Test dates never influence the spread that scores them.
    """
    from weatherquant.models.probability import bracket_probability  # local: avoids a cycle

    ladders = load_event_ladders(con)
    results: dict[str, BaselineRun] = {}

    loaded: dict[str, tuple[list[dict], list[str]]] = {}
    for model in models:
        loaded[model] = load_forecast_observation_pairs(
            con, model=model, observation_rule=observation_rule, ladders=ladders
        )

    # Candidate point forecasts: each raw model, their mean, and the calibrated
    # variant of whichever raw model is better on the training window.
    configs: list[tuple[str, str, bool]] = []
    for model in models:
        configs.append((f"{model}_raw", model, False))
    if len(models) >= 2:
        configs.append(("fusion", "+".join(models), False))
    configs.append(("calibrated", models[0], True))

    for name, source, apply_bias in configs:
        pairs, problems = _point_forecasts(loaded, source, models)
        if len(pairs) < 4:
            results[name] = BaselineRun(
                name=name, scores={}, problems=[*problems, "too few paired dates to evaluate"],
                train_start=None, train_end=None, test_start=None, test_end=None,
                residual_summary={},
            )
            continue

        train, test, train_start, test_start = chronological_split(
            pairs, train_fraction=train_fraction
        )
        residual = fit_residual_model(
            train, model_id=name, min_samples_per_bucket=3,
            bias=None if apply_bias else 0.0,
        )

        scores = _score_pairs(
            name=name,
            pairs=test,
            ladders=ladders,
            residual=residual,
            apply_bias=apply_bias,
            bracket_probability=bracket_probability,
        )
        results[name] = BaselineRun(
            name=name,
            scores={"brier_and_log_loss": scores} if scores else {},
            problems=problems,
            train_start=train_start,
            train_end=max(p["target_date"] for p in train),
            test_start=test_start,
            test_end=max(p["target_date"] for p in test),
            residual_summary=residual.summary(),
        )

    return results


def _point_forecasts(
    loaded: dict[str, tuple[list[dict], list[str]]],
    source: str,
    models: Sequence[str],
) -> tuple[list[dict], list[str]]:
    """Resolve a source name into per-date point forecasts."""
    problems: list[str] = []
    if source in loaded:
        pairs, own_problems = loaded[source]
        return [dict(p, point=p["predicted_max_f"]) for p in pairs], own_problems

    # Fusion: mean of every model's forecast for the date.
    by_date: dict[date, dict] = {}
    for model in models:
        if model not in loaded:
            continue
        pairs, _ = loaded[model]
        for p in pairs:
            slot = by_date.setdefault(p["target_date"], {"values": [], "base": p})
            slot["values"].append(p["predicted_max_f"])
    out = []
    for target_date, slot in sorted(by_date.items()):
        merged = dict(slot["base"])
        merged["point"] = sum(slot["values"]) / len(slot["values"])
        if len(slot["values"]) < len(models):
            problems.append(
                f"{target_date}: fusion used {len(slot['values'])}/{len(models)} models"
            )
        out.append(merged)
    if not out:
        problems.append(f"no data for source {source!r}")
    return out, problems


def _score_pairs(
    *,
    name: str,
    pairs: list[dict],
    ladders: dict[date, EventLadder],
    residual: ResidualModel,
    apply_bias: bool,
    bracket_probability,
) -> BaselineScore | None:
    """Turn point forecasts into bracket guesses and score them."""
    predicted: list[str] = []
    actual: list[str] = []
    dates: list[date] = []
    point: list[float] = []
    observed: list[float] = []

    for p in pairs:
        ladder = ladders.get(p["target_date"])
        if ladder is None or ladder.winning_label is None:
            continue
        point_forecast = p["point"] + (residual.bias_f if apply_bias else 0.0)

        best_label, best_p = None, -1.0
        for bracket in ladder.brackets:
            value = bracket_probability(
                point_forecast, bracket, model=residual, lead_hours=p["lead_hours"]
            )
            if value is None:
                return None  # no usable spread: refuse rather than guess
            if value > best_p:
                best_label, best_p = bracket.label, value
        if best_label is None:
            continue

        predicted.append(best_label)
        actual.append(ladder.winning_label)
        dates.append(p["target_date"])
        point.append(p["predicted_max_f"])
        observed.append(p["observed_max_f"])

    if not predicted:
        return None

    return score(
        name=name,
        predicted_brackets=predicted,
        actual_brackets=actual,
        classes=sorted({b.label for d in ladders.values() for b in d.brackets}),
        dates=dates,
        predicted_max_f=point,
        observed_max_f=observed,
    )

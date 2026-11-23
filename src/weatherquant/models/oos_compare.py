"""Walk-forward comparison of the four weather probability models.

The comparison is deliberately strict: forecasts must have been publishable
before the target New York day started, every model is scored on the same
``(target_date, run_time_utc, station)`` rows, and each fold is fitted only on
strictly earlier target dates. Uncertainty uses a paired target-date block
bootstrap, never a row bootstrap that would count correlated runs as independent.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from weatherquant.models.baselines import EventLadder, load_event_ladders, load_forecast_observation_pairs
from weatherquant.models.evaluation import one_hot
from weatherquant.models.factory import FIT_SPECS
from weatherquant.models.metrics import (
    brier_multiclass,
    calibration_bins,
    expected_calibration_error,
    log_loss,
    mae,
    rmse,
)
from weatherquant.models.probability import distribution_for_event
from weatherquant.models.residual import fit_residual_model
from weatherquant.weather.daily_max import point_in_time

__all__ = [
    "MODEL_NAMES",
    "ScoredForecast",
    "compare_models",
    "filter_point_in_time",
    "run_oos_comparison",
    "score_aligned_rows",
]

MODEL_NAMES = tuple(FIT_SPECS)
METRIC_NAMES = ("brier", "log_loss", "ece", "mae", "rmse", "top1_accuracy")
SOURCE_NAMES = tuple(dict.fromkeys(source for spec in FIT_SPECS.values() for source in spec.sources))


@dataclass(frozen=True, slots=True)
class ScoredForecast:
    """One aligned out-of-sample forecast and its realised outcome."""

    target_date: date
    run_time_utc: datetime
    station: str
    event_id: str
    actual_bracket: str
    probabilities: dict[str, float]
    predicted_max_f: float
    observed_max_f: float
    condition: str = "unknown"

    @property
    def key(self) -> tuple[date, datetime, str]:
        return self.target_date, self.run_time_utc, self.station


def _pair_key(pair: Mapping) -> tuple[date, datetime, str]:
    return pair["target_date"], pair["run_time_utc"], pair.get("station", "KLGA")


def filter_point_in_time(pairs: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    """Return eligible pairs and serialisable exclusion records.

    A missing/unknown publication time, publication before model initialisation,
    or publication after the start of the target day is excluded and reported.
    """

    kept: list[dict] = []
    excluded: list[dict] = []
    for pair in pairs:
        result = point_in_time(
            run_time_utc=pair.get("run_time_utc"),
            publishable_at_utc=pair.get("publishable_at_utc"),
            publishable_basis=pair.get("publishable_basis", "unknown"),
            target_date=pair["target_date"],
        )
        if result.eligible:
            kept.append(dict(pair, decision_deadline_utc=result.decision_deadline_utc))
        else:
            excluded.append(
                {
                    "target_date": str(pair["target_date"]),
                    "run_time_utc": str(pair.get("run_time_utc")),
                    "reason": result.ineligible_reason,
                }
            )
    return kept, excluded


def _load_source_pairs(con, *, observation_rule: str) -> tuple[dict[str, list[dict]], dict]:
    ladders = load_event_ladders(con)
    sources: dict[str, list[dict]] = {}
    problems: list[str] = []
    for source in SOURCE_NAMES:
        pairs, source_problems = load_forecast_observation_pairs(
            con, model=source, observation_rule=observation_rule, ladders=ladders
        )
        problems.extend(f"{source}: {problem}" for problem in source_problems)
        availability = {
            (row[0], row[1]): row[2:]
            for row in con.execute(
                "SELECT f.target_date, f.run_time_utc, f.station, r.publishable_at_utc, "
                "COALESCE(r.publishable_basis, 'unknown') "
                "FROM forecast_daily_max f "
                "LEFT JOIN forecast_runs r ON f.provider = r.provider AND f.model = r.model "
                "AND f.run_time_utc = r.run_time_utc WHERE f.model = ?",
                [source],
            ).fetchall()
        }
        enriched = []
        for pair in pairs:
            station, publishable_at, basis = availability.get(
                (pair["target_date"], pair["run_time_utc"]),
                ("KLGA", None, "unknown"),
            )
            enriched.append(
                dict(
                    pair,
                    station=station,
                    publishable_at_utc=publishable_at,
                    publishable_basis=basis,
                )
            )
        sources[source] = enriched
    return sources, {"problems": problems, "ladders": ladders}


def _models_from_sources(sources: Mapping[str, Sequence[dict]]) -> tuple[dict[str, list[dict]], dict]:
    eligible_sources: dict[str, list[dict]] = {}
    exclusions: dict[str, list[dict]] = {}
    for source in SOURCE_NAMES:
        eligible_sources[source], exclusions[source] = filter_point_in_time(list(sources.get(source, [])))

    ecmwf_source, gfs_source = FIT_SPECS["fusion"].sources
    ecmwf = {_pair_key(pair): pair for pair in eligible_sources[ecmwf_source]}
    gfs = {_pair_key(pair): pair for pair in eligible_sources[gfs_source]}
    fusion: list[dict] = []
    for key in sorted(ecmwf.keys() & gfs.keys(), key=lambda item: (item[0], item[1], item[2])):
        left, right = ecmwf[key], gfs[key]
        fusion.append(
            dict(
                left,
                predicted_max_f=(left["predicted_max_f"] + right["predicted_max_f"]) / 2.0,
                publishable_at_utc=max(left["publishable_at_utc"], right["publishable_at_utc"]),
                publishable_basis=(
                    "official" if left["publishable_basis"] == right["publishable_basis"] == "official" else "estimated"
                ),
            )
        )

    models = {
        "ecmwf_raw": [dict(pair) for pair in eligible_sources[ecmwf_source]],
        "gfs_raw": [dict(pair) for pair in eligible_sources[gfs_source]],
        "fusion": fusion,
        "calibrated": [dict(pair) for pair in eligible_sources[ecmwf_source]],
    }
    return models, {"source_exclusions": exclusions}


def _tercile_edges(values: Sequence[float]) -> tuple[float, float] | None:
    """Nearest-rank terciles, estimated only on the initial training window."""

    ordered = sorted(values)
    if len(ordered) < 3:
        return None
    first = ordered[math.ceil(len(ordered) / 3) - 1]
    second = ordered[math.ceil(2 * len(ordered) / 3) - 1]
    return first, second


def _condition(value: float, edges: tuple[float, float] | None) -> str:
    if edges is None:
        return "unknown"
    first, second = edges
    if value <= first:
        return "cold"
    if value <= second:
        return "moderate"
    return "hot"


def _walk_forward(
    models: Mapping[str, Sequence[dict]],
    ladders: Mapping[date, EventLadder],
    *,
    min_train_dates: int,
    min_samples_per_bucket: int,
) -> tuple[dict[str, list[ScoredForecast]], list[dict], dict]:
    keyed = {name: {_pair_key(pair): pair for pair in models[name]} for name in MODEL_NAMES}
    common_keys = set.intersection(*(set(keys) for keys in keyed.values()))
    if not common_keys:
        raise ValueError("no forecast runs shared by all four models after point-in-time filtering")

    all_dates = sorted({key[0] for key in common_keys})
    test_dates = all_dates[min_train_dates:]
    if not test_dates:
        raise ValueError(f"need more than min_train_dates={min_train_dates} aligned dates, got {len(all_dates)}")
    first_test_date = test_dates[0]
    training_observations = sorted(
        {(key[0], keyed[MODEL_NAMES[0]][key]["observed_max_f"]) for key in common_keys if key[0] < first_test_date}
    )
    edges = _tercile_edges([observed for _day, observed in training_observations])

    rows: dict[str, list[ScoredForecast]] = {name: [] for name in MODEL_NAMES}
    folds: list[dict] = []
    scoring_exclusions: dict[str, Counter] = {name: Counter() for name in MODEL_NAMES}
    alignment_dropped = 0

    for test_date in test_dates:
        fold_keys = sorted(
            (key for key in common_keys if key[0] == test_date),
            key=lambda item: (item[1], item[2]),
        )
        fold: dict = {"test_date": str(test_date), "models": {}}
        pending: dict[str, dict[tuple[date, datetime, str], ScoredForecast]] = {}
        for name in MODEL_NAMES:
            train = [pair for pair in models[name] if pair["target_date"] < test_date]
            if not train or max(pair["target_date"] for pair in train) >= test_date:
                raise RuntimeError(f"chronological leakage detected fitting {name} for {test_date}")
            spec = FIT_SPECS[name]
            residual = fit_residual_model(
                train,
                model_id=name,
                min_samples_per_bucket=min_samples_per_bucket,
                bias=None if spec.apply_bias else 0.0,
            )
            fold["models"][name] = {
                "train_start": str(residual.train_start),
                "train_end": str(residual.train_end),
                "train_dates": residual.n_dates,
                "train_events": residual.n_pairs,
                "bias_f": residual.bias_f,
                "metrics": None,
            }
            pending[name] = {}
            for key in fold_keys:
                pair = keyed[name][key]
                ladder = ladders[test_date]
                if ladder.winning_label is None:
                    scoring_exclusions[name]["no_unique_winner"] += 1
                    continue
                try:
                    distribution = distribution_for_event(
                        pair["predicted_max_f"],
                        ladder.brackets,
                        model=residual,
                        lead_hours=pair["lead_hours"],
                        station=key[2],
                        target_date=test_date,
                        model_id=name,
                        forecast_run_time_utc=key[1],
                    )
                except ValueError:
                    scoring_exclusions[name]["no_usable_probability_distribution"] += 1
                    continue
                predicted_point = pair["predicted_max_f"] + (residual.bias_f if spec.apply_bias else 0.0)
                observed = pair["observed_max_f"]
                pending[name][key] = ScoredForecast(
                    target_date=test_date,
                    run_time_utc=key[1],
                    station=key[2],
                    event_id=pair.get("event_id", ladder.event_id),
                    actual_bracket=ladder.winning_label,
                    probabilities=distribution.as_dict(),
                    predicted_max_f=predicted_point,
                    observed_max_f=observed,
                    condition=_condition(observed, edges),
                )

        scored_keys = set.intersection(*(set(model_rows) for model_rows in pending.values()))
        alignment_dropped += len(set(fold_keys) - scored_keys)
        fold["n_events"] = len(scored_keys)
        for name in MODEL_NAMES:
            selected = [pending[name][key] for key in sorted(scored_keys, key=lambda item: (item[1], item[2]))]
            rows[name].extend(selected)
            if selected:
                fold["models"][name]["metrics"] = _metric_values(selected)
        folds.append(fold)

    if not rows[MODEL_NAMES[0]]:
        raise ValueError("no aligned out-of-sample forecasts could be scored; inspect fold exclusions")
    diagnostics = {
        "scoring_exclusions": {name: dict(sorted(counts.items())) for name, counts in scoring_exclusions.items()},
        "alignment_dropped_events": alignment_dropped,
        "condition": {
            "name": "observed_temperature_tercile",
            "training_end": str(first_test_date),
            "lower_upper_f": list(edges) if edges is not None else None,
        },
    }
    return rows, folds, diagnostics


def _metric_values(rows: Sequence[ScoredForecast]) -> dict[str, float]:
    classes = sorted({label for row in rows for label in (*row.probabilities, row.actual_bracket)})
    probabilities = [[row.probabilities.get(label, 0.0) for label in classes] for row in rows]
    outcomes = one_hot([row.actual_bracket for row in rows], classes)
    confidence = [max(row) for row in probabilities]
    hits = [
        1.0 if row.index(max(row)) == truth.index(1.0) else 0.0
        for row, truth in zip(probabilities, outcomes, strict=True)
    ]
    bins = calibration_bins(confidence, hits, n_bins=min(10, max(2, len(rows) // 5)))
    observed = [row.observed_max_f for row in rows]
    predicted = [row.predicted_max_f for row in rows]
    return {
        "brier": brier_multiclass(probabilities, outcomes),
        "log_loss": log_loss(probabilities, outcomes),
        "ece": expected_calibration_error(bins, total_count=len(rows)),
        "mae": mae(observed, predicted),
        "rmse": rmse(observed, predicted),
        "top1_accuracy": math.fsum(hits) / len(hits),
    }


def _percentile_interval(values: list[float]) -> list[float]:
    ordered = sorted(values)
    return [
        ordered[int(0.025 * (len(ordered) - 1))],
        ordered[int(0.975 * (len(ordered) - 1))],
    ]


def _bootstrap_cis(
    rows_by_model: Mapping[str, Sequence[ScoredForecast]],
    *,
    samples: int,
    seed: int,
    reference_model: str,
    paired: bool,
) -> tuple[dict[str, dict[str, list[float] | None]], dict[str, dict[str, list[float] | None]]]:
    names = sorted(rows_by_model)
    dates = sorted({row.target_date for row in rows_by_model[names[0]]})
    intervals: dict[str, dict[str, list[float] | None]] = {
        name: {metric: None for metric in METRIC_NAMES} for name in names
    }
    differences: dict[str, dict[str, list[float] | None]] = {
        name: {metric: None for metric in METRIC_NAMES} for name in names if name != reference_model
    }
    if len(dates) < 2:
        return intervals, differences

    grouped = {
        name: {day: [row for row in rows_by_model[name] if row.target_date == day] for day in dates} for name in names
    }
    draws: dict[str, dict[str, list[float]]] = {name: {metric: [] for metric in METRIC_NAMES} for name in names}
    difference_draws: dict[str, dict[str, list[float]]] = {
        name: {metric: [] for metric in METRIC_NAMES} for name in differences
    }
    rng = random.Random(seed)
    for _ in range(samples):
        drawn_dates = [dates[rng.randrange(len(dates))] for _ in dates]
        metrics = {name: _metric_values([row for day in drawn_dates for row in grouped[name][day]]) for name in names}
        for name in names:
            for metric in METRIC_NAMES:
                draws[name][metric].append(metrics[name][metric])
                if paired and name != reference_model:
                    difference_draws[name][metric].append(metrics[name][metric] - metrics[reference_model][metric])

    for name in names:
        for metric in METRIC_NAMES:
            intervals[name][metric] = _percentile_interval(draws[name][metric])
            if paired and name != reference_model:
                differences[name][metric] = _percentile_interval(difference_draws[name][metric])
    return intervals, differences


def _validate_aligned(rows_by_model: Mapping[str, Sequence[ScoredForecast]]) -> None:
    if not rows_by_model or any(not rows for rows in rows_by_model.values()):
        raise ValueError("every model needs at least one scored row")
    names = sorted(rows_by_model)
    expected = {row.key: row for row in rows_by_model[names[0]]}
    for name in names[1:]:
        actual = {row.key: row for row in rows_by_model[name]}
        if actual.keys() != expected.keys():
            raise ValueError("all models must be scored on identical forecast runs")
        for key, row in actual.items():
            reference = expected[key]
            if (row.actual_bracket, row.observed_max_f) != (
                reference.actual_bracket,
                reference.observed_max_f,
            ):
                raise ValueError(f"outcome mismatch across models for {key}")


def _summarize_group(
    rows_by_model: Mapping[str, Sequence[ScoredForecast]],
    *,
    bootstrap_samples: int,
    seed: int,
    reference_model: str,
    min_bucket_dates: int,
    paired: bool,
) -> dict:
    _validate_aligned(rows_by_model)
    names = sorted(rows_by_model)
    intervals, difference_intervals = _bootstrap_cis(
        rows_by_model,
        samples=bootstrap_samples,
        seed=seed,
        reference_model=reference_model,
        paired=paired,
    )
    models: dict[str, dict] = {}
    for name in names:
        values = _metric_values(rows_by_model[name])
        models[name] = {
            "n_dates": len({row.target_date for row in rows_by_model[name]}),
            "n_events": len(rows_by_model[name]),
            "small_sample": len({row.target_date for row in rows_by_model[name]}) < min_bucket_dates,
            "metrics": {metric: {"value": values[metric], "ci95": intervals[name][metric]} for metric in METRIC_NAMES},
        }

    paired_differences: dict[str, dict] = {}
    if paired:
        reference_values = _metric_values(rows_by_model[reference_model])
        for name in names:
            if name == reference_model:
                continue
            values = _metric_values(rows_by_model[name])
            paired_differences[name] = {}
            for metric in METRIC_NAMES:
                ci = difference_intervals[name][metric]
                paired_differences[name][metric] = {
                    "value": values[metric] - reference_values[metric],
                    "ci95": ci,
                    "significant": ci is not None and (ci[0] > 0.0 or ci[1] < 0.0),
                }
    return {"models": models, "paired_differences": paired_differences}


def _ranking(summary: Mapping, reference_model: str) -> list[dict]:
    models = summary["models"]
    ordered = sorted(
        models,
        key=lambda name: (models[name]["metrics"]["brier"]["value"], models[name]["metrics"]["log_loss"]["value"]),
    )
    ranking = []
    for rank, name in enumerate(ordered, start=1):
        brier = models[name]["metrics"]["brier"]
        nll = models[name]["metrics"]["log_loss"]
        paired = summary["paired_differences"].get(name, {}).get("brier")
        ranking.append(
            {
                "rank": rank,
                "model": name,
                "brier": brier["value"],
                "brier_ci95": brier["ci95"],
                "log_loss": nll["value"],
                "log_loss_ci95": nll["ci95"],
                "brier_difference_vs_reference": None if name == reference_model else paired["value"],
                "brier_difference_ci95": None if name == reference_model else paired["ci95"],
                "significant_vs_reference": None if name == reference_model else paired["significant"],
            }
        )
    return ranking


def _breakdowns(
    rows_by_model: Mapping[str, Sequence[ScoredForecast]],
    *,
    bootstrap_samples: int,
    seed: int,
    reference_model: str,
    min_bucket_dates: int,
) -> dict:
    first_name = sorted(rows_by_model)[0]
    specifications = {
        "by_month": lambda row: row.target_date.strftime("%Y-%m"),
        "by_observed_temperature_tercile": lambda row: row.condition,
    }
    output: dict[str, dict] = {}
    for breakdown_name, bucket_for in specifications.items():
        buckets = sorted({bucket_for(row) for row in rows_by_model[first_name]})
        output[breakdown_name] = {}
        for bucket in buckets:
            subset = {name: [row for row in rows if bucket_for(row) == bucket] for name, rows in rows_by_model.items()}
            summary = _summarize_group(
                subset,
                bootstrap_samples=bootstrap_samples,
                seed=seed,
                reference_model=reference_model,
                min_bucket_dates=min_bucket_dates,
                paired=False,
            )
            output[breakdown_name][bucket] = summary["models"]
    return output


def score_aligned_rows(
    rows_by_model: Mapping[str, Sequence[ScoredForecast]],
    *,
    bootstrap_samples: int = 1000,
    seed: int = 36,
    reference_model: str = "ecmwf_raw",
    min_bucket_dates: int = 5,
) -> dict:
    """Score already aligned rows with deterministic paired block bootstrap CIs."""

    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be >= 1")
    if min_bucket_dates < 1:
        raise ValueError("min_bucket_dates must be >= 1")
    if reference_model not in rows_by_model:
        raise ValueError(f"reference model {reference_model!r} has no scored rows")
    _validate_aligned(rows_by_model)
    summary = _summarize_group(
        rows_by_model,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        reference_model=reference_model,
        min_bucket_dates=min_bucket_dates,
        paired=True,
    )
    return {
        "method": {
            "evaluation": "expanding_window_walk_forward",
            "alignment": "identical target_date/run_time_utc/station across models",
            "confidence_interval": "95% percentile paired bootstrap over target-date blocks",
            "bootstrap_samples": bootstrap_samples,
            "bootstrap_seed": seed,
            "reference_model": reference_model,
            "ranking": "Brier point estimate, Log Loss tie-break; paired CI shows significance",
            "small_bucket_min_dates": min_bucket_dates,
        },
        "models": summary["models"],
        "paired_differences": summary["paired_differences"],
        "ranking": _ranking(summary, reference_model),
        "breakdowns": _breakdowns(
            rows_by_model,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            reference_model=reference_model,
            min_bucket_dates=min_bucket_dates,
        ),
    }


def compare_models(
    models: Mapping[str, Sequence[dict]],
    ladders: Mapping[date, EventLadder],
    *,
    min_train_dates: int = 5,
    min_samples_per_bucket: int = 3,
    bootstrap_samples: int = 1000,
    seed: int = 36,
    reference_model: str = "ecmwf_raw",
    min_bucket_dates: int = 5,
    diagnostics: Mapping | None = None,
) -> dict:
    """Run the four-model expanding-window comparison on forecast/observation pairs."""

    if set(models) != set(MODEL_NAMES):
        raise ValueError(f"models must be exactly {list(MODEL_NAMES)}")
    if min_train_dates < 2:
        raise ValueError("min_train_dates must be >= 2")
    if min_samples_per_bucket < 2:
        raise ValueError("min_samples_per_bucket must be >= 2")

    eligible: dict[str, list[dict]] = {}
    model_exclusions: dict[str, list[dict]] = {}
    for name in MODEL_NAMES:
        eligible[name], model_exclusions[name] = filter_point_in_time(list(models[name]))
    rows, folds, walk_diagnostics = _walk_forward(
        eligible,
        ladders,
        min_train_dates=min_train_dates,
        min_samples_per_bucket=min_samples_per_bucket,
    )
    report = score_aligned_rows(
        rows,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        reference_model=reference_model,
        min_bucket_dates=min_bucket_dates,
    )
    common_keys = set.intersection(*({_pair_key(pair) for pair in eligible[name]} for name in MODEL_NAMES))
    report["coverage"] = {
        name: {
            "eligible_dates": len({pair["target_date"] for pair in eligible[name]}),
            "eligible_events": len(eligible[name]),
            "aligned_dates": len({key[0] for key in common_keys}),
            "aligned_events": len(common_keys),
            "scored_dates": report["models"][name]["n_dates"],
            "scored_events": report["models"][name]["n_events"],
        }
        for name in MODEL_NAMES
    }
    report["folds"] = folds
    report["exclusions"] = {
        "sources": dict((diagnostics or {}).get("source_exclusions", {})),
        "models": model_exclusions,
        **{key: value for key, value in walk_diagnostics.items() if key != "condition"},
    }
    report["condition"] = walk_diagnostics["condition"]
    report["problems"] = list((diagnostics or {}).get("problems", []))
    report["method"].update(
        {
            "decision_cutoff": "start of target date in America/New_York",
            "calibration": "refit on target dates strictly before each test fold",
            "min_train_dates": min_train_dates,
            "min_samples_per_lead_bucket": min_samples_per_bucket,
        }
    )
    return report


def run_oos_comparison(
    con,
    *,
    observation_rule: str = "max of all readings",
    min_train_dates: int = 5,
    min_samples_per_bucket: int = 3,
    bootstrap_samples: int = 1000,
    seed: int = 36,
    reference_model: str = "ecmwf_raw",
    min_bucket_dates: int = 5,
) -> dict:
    """Load a research DB through an existing connection and compare all models."""

    sources, diagnostics = _load_source_pairs(con, observation_rule=observation_rule)
    models, build_diagnostics = _models_from_sources(sources)
    return compare_models(
        models,
        diagnostics["ladders"],
        min_train_dates=min_train_dates,
        min_samples_per_bucket=min_samples_per_bucket,
        bootstrap_samples=bootstrap_samples,
        seed=seed,
        reference_model=reference_model,
        min_bucket_dates=min_bucket_dates,
        diagnostics={**diagnostics, **build_diagnostics},
    )

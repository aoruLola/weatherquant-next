"""Calibration and market-pricing drift over published performance episodes.

The three populations and three Edge tiers are deliberately different. An
observation is a priced ACCEPT evaluation, an episode is one opportunity, and
an executable sample is an episode for which ``simulate_single`` can size a
fill from a real book and a known fee. None is a substitute for another.

Calibration and market bias use the episode's first probability and only a
verified RESOLVED YES payout. Expected and realised USD come exclusively from
``execution/pricing.py`` through ``simulate_single``; this module never derives
a fee, a fill or a payoff itself.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from weatherquant.markets.models import OrderBook
from weatherquant.models.metrics import (
    CalibrationBin,
    brier_binary,
    calibration_bins,
    expected_calibration_error,
)
from weatherquant.performance.capital import simulate_single
from weatherquant.performance.episodes import Episode
from weatherquant.performance.winrate import SettlementAnswer

__all__ = [
    "ANOMALY_MIN_TRAILING_BUCKETS",
    "ANOMALY_THRESHOLD_MAD",
    "CALIBRATION_BIN_COUNT",
    "SMALL_SAMPLE_EPISODES",
    "build_research_drift",
]

CALIBRATION_BIN_COUNT = 10
SMALL_SAMPLE_EPISODES = 20
ANOMALY_THRESHOLD_MAD = 3.0
ANOMALY_MIN_TRAILING_BUCKETS = 4
CLIMATOLOGY_UNAVAILABLE_REASON = (
    "The performance export has no climatology probability; no baseline is imputed."
)

Granularity = Literal["iso_week", "month", "target_date"]
GRANULARITIES: tuple[Granularity, ...] = ("iso_week", "month", "target_date")


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _bucket_keys(episode: Episode) -> dict[Granularity, tuple[str, str] | None]:
    seen = _utc(episode.first_seen_utc)
    iso = seen.isocalendar()
    target = episode.target_date or episode.key.target_date
    return {
        "iso_week": (
            f"{iso.year:04d}-W{iso.week:02d}",
            date.fromisocalendar(iso.year, iso.week, 1).isoformat(),
        ),
        "month": (f"{seen.year:04d}-{seen.month:02d}", date(seen.year, seen.month, 1).isoformat()),
        "target_date": (target, target) if target else None,
    }


def _valid_probability(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0.0 <= value <= 1.0


def _verified_payout(
    episode: Episode,
    answers: Mapping[tuple[str, str | None], SettlementAnswer],
    verified: frozenset[tuple[str, str | None]],
) -> float | None:
    key = (episode.key.market_id, episode.key.target_date)
    answer = answers.get(key)
    if (
        answer is None
        or key not in verified
        or answer.status != "RESOLVED"
        or answer.payout not in (0.0, 1.0)
    ):
        return None
    return float(answer.payout)


def _empty_calibration() -> dict[str, Any]:
    return {"n": 0, "brier": None, "ece": None, "bins": []}


def _calibration(
    probabilities: list[float], outcomes: list[float], *, include_bins: bool
) -> dict[str, Any]:
    if not probabilities:
        return _empty_calibration()
    bins = calibration_bins(probabilities, outcomes, n_bins=CALIBRATION_BIN_COUNT)
    return {
        "n": len(probabilities),
        "brier": brier_binary(probabilities, outcomes),
        "ece": expected_calibration_error(bins, total_count=len(probabilities)),
        "bins": [_bin_dict(item) for item in bins] if include_bins else [],
    }


def _bin_dict(item: CalibrationBin) -> dict[str, float | int]:
    return {
        "lower": item.lower,
        "upper": item.upper,
        "predicted_mean": item.predicted_mean,
        "observed_frequency": item.observed_frequency,
        "count": item.count,
        "gap": item.gap,
    }


def _tier(n: int, total: float, *, unit: str) -> dict[str, Any]:
    return {"n": n, "sum": total if n else None, "mean": total / n if n else None, "unit": unit}


@dataclass(slots=True)
class _Bucket:
    period: str
    start_date: str
    observation_count: int = 0
    episode_count: int = 0
    executable_count: int = 0
    not_executable_count: int = 0
    fee_unknown_count: int = 0
    no_book_count: int = 0
    realised_count: int = 0
    observation_edge_sum: float = 0.0
    expected_net_usd_sum: float = 0.0
    realised_net_usd_sum: float = 0.0
    model_probabilities: list[float] = field(default_factory=list)
    model_outcomes: list[float] = field(default_factory=list)
    market_probabilities: list[float] = field(default_factory=list)
    market_outcomes: list[float] = field(default_factory=list)
    market_errors: list[float] = field(default_factory=list)
    fee_rates: dict[float, int] = field(default_factory=dict)
    not_executable_reason_counts: dict[str, int] = field(default_factory=dict)
    source_episode_ids: list[str] = field(default_factory=list)

    def add(
        self,
        episode: Episode,
        *,
        payout: float | None,
        book: OrderBook | None,
        reference_capital_usdc: float,
    ) -> None:
        self.episode_count += 1
        self.observation_count += episode.observation_count
        self.observation_edge_sum += episode.mean_edge * episode.observation_count
        self.source_episode_ids.append(episode.episode_id)

        if episode.fee_rate is None:
            self.fee_unknown_count += 1
        else:
            self.fee_rates[episode.fee_rate] = self.fee_rates.get(episode.fee_rate, 0) + 1
        if book is None:
            self.no_book_count += 1

        model_p = episode.first_model_probability
        market_p = episode.first_market_price
        if payout is not None and _valid_probability(model_p):
            self.model_probabilities.append(float(model_p))
            self.model_outcomes.append(payout)
        if payout is not None and _valid_probability(market_p):
            self.market_probabilities.append(float(market_p))
            self.market_outcomes.append(payout)
            self.market_errors.append(float(market_p) - payout)

        result = simulate_single(
            episode,
            capital_usdc=reference_capital_usdc,
            book=book,
            settlement_payout=payout,
        )
        if result.executable:
            self.executable_count += 1
            expected = result.theoretical_expected_net_edge_usdc
            if expected is not None:
                self.expected_net_usd_sum += expected
            realised = result.win_net_usdc if result.win_net_usdc is not None else result.loss_net_usdc
            if realised is not None:
                self.realised_count += 1
                self.realised_net_usd_sum += realised
        else:
            self.not_executable_count += 1
            reason = result.not_executable_reason or "UNKNOWN"
            self.not_executable_reason_counts[reason] = self.not_executable_reason_counts.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        calibration = _calibration(self.model_probabilities, self.model_outcomes, include_bins=True)
        market_baseline = _calibration(self.market_probabilities, self.market_outcomes, include_bins=False)
        market_n = len(self.market_errors)
        market_bias = {
            "n": market_n,
            "mean_market_minus_outcome": math.fsum(self.market_errors) / market_n if market_n else None,
            "mean_market_probability": (
                math.fsum(self.market_probabilities) / market_n if market_n else None
            ),
            "mean_outcome": math.fsum(self.market_outcomes) / market_n if market_n else None,
        }
        return {
            "period": self.period,
            "start_date": self.start_date,
            "observation_count": self.observation_count,
            "episode_count": self.episode_count,
            "executable_count": self.executable_count,
            "not_executable_count": self.not_executable_count,
            "fee_unknown_count": self.fee_unknown_count,
            "no_book_count": self.no_book_count,
            "not_executable_reason_counts": dict(sorted(self.not_executable_reason_counts.items())),
            "fee_assumption": {
                "source": "episode.fee_rate via execution/pricing.py",
                "known_count": self.episode_count - self.fee_unknown_count,
                "unknown_count": self.fee_unknown_count,
                "rates": [
                    {"fee_rate": rate, "episode_count": count}
                    for rate, count in sorted(self.fee_rates.items())
                ],
            },
            "calibration": calibration,
            "market_bias": market_bias,
            "baseline_comparison": {
                "market": {**market_baseline, "mean_market_minus_outcome": market_bias["mean_market_minus_outcome"]},
                "climatology": {
                    "available": False,
                    "n": 0,
                    "brier": None,
                    "ece": None,
                    "reason": CLIMATOLOGY_UNAVAILABLE_REASON,
                },
            },
            "edge_tiers": {
                "theoretical_observation_edge_per_share": _tier(
                    self.observation_count,
                    self.observation_edge_sum,
                    unit="probability_per_share",
                ),
                "executable_expected_net_usd": _tier(
                    self.executable_count,
                    self.expected_net_usd_sum,
                    unit="USD",
                ),
                "verified_realised_net_usd": _tier(
                    self.realised_count,
                    self.realised_net_usd_sum,
                    unit="USD",
                ),
            },
            "small_sample": self.episode_count < SMALL_SAMPLE_EPISODES,
            "source_episode_ids": list(self.source_episode_ids),
            "anomaly_metrics": [],
        }


def _metric_value(bucket: Mapping[str, Any], path: str) -> float | None:
    value: Any = bucket
    for part in path.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return float(value) if type(value) in (int, float) else None


ANOMALY_METRICS = (
    "calibration.brier",
    "calibration.ece",
    "market_bias.mean_market_minus_outcome",
    "edge_tiers.theoretical_observation_edge_per_share.mean",
    "edge_tiers.executable_expected_net_usd.mean",
    "edge_tiers.verified_realised_net_usd.mean",
)


def _anomalies(
    buckets: dict[Granularity, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    for granularity in GRANULARITIES:
        for metric in ANOMALY_METRICS:
            trailing: list[float] = []
            for bucket in buckets[granularity]:
                current = _metric_value(bucket, metric)
                if current is None:
                    continue
                if len(trailing) >= ANOMALY_MIN_TRAILING_BUCKETS:
                    median = statistics.median(trailing)
                    mad = statistics.median(abs(value - median) for value in trailing)
                    deviation = abs(current - median)
                    if deviation > ANOMALY_THRESHOLD_MAD * mad and (mad > 0.0 or deviation > 0.0):
                        anomaly = {
                            "granularity": granularity,
                            "period": bucket["period"],
                            "metric": metric,
                            "value": current,
                            "trailing_median": median,
                            "trailing_mad": mad,
                            "deviation": deviation,
                            "threshold": ANOMALY_THRESHOLD_MAD * mad,
                            "trailing_bucket_count": len(trailing),
                            "source_episode_ids": list(bucket["source_episode_ids"]),
                        }
                        anomalies.append(anomaly)
                        bucket["anomaly_metrics"].append(metric)
                trailing.append(current)
    return anomalies


def build_research_drift(
    episodes: Iterable[Episode],
    *,
    answers: Mapping[tuple[str, str | None], SettlementAnswer],
    verified: frozenset[tuple[str, str | None]],
    book_for: Callable[[Episode], OrderBook | None] | None = None,
    reference_capital_usdc: float = 100.0,
) -> dict[str, Any]:
    """Aggregate ISO-week, month and target-date research buckets.

    The same function drives the materialised and streaming publishers. It
    consumes each episode once and keeps no raw decisions or order books.
    """
    if reference_capital_usdc <= 0:
        raise ValueError("reference_capital_usdc must be > 0")
    resolver = book_for or (lambda _episode: None)
    mutable: dict[Granularity, dict[str, _Bucket]] = {name: {} for name in GRANULARITIES}
    episode_count = 0
    observation_count = 0
    target_date_missing_episode_count = 0

    for episode in episodes:
        episode_count += 1
        observation_count += episode.observation_count
        keys = _bucket_keys(episode)
        if keys["target_date"] is None:
            target_date_missing_episode_count += 1
        payout = _verified_payout(episode, answers, verified)
        book = resolver(episode)
        for granularity, key in keys.items():
            if key is None:
                continue
            period, start_date = key
            bucket = mutable[granularity].get(period)
            if bucket is None:
                bucket = _Bucket(period=period, start_date=start_date)
                mutable[granularity][period] = bucket
            bucket.add(episode, payout=payout, book=book, reference_capital_usdc=reference_capital_usdc)

    buckets = {
        granularity: [
            bucket.as_dict()
            for _period, bucket in sorted(rows.items(), key=lambda item: (item[1].start_date, item[0]))
        ]
        for granularity, rows in mutable.items()
    }
    anomalies = _anomalies(buckets)
    warnings = [
        "Observation Edge is a dimensionless per-share model expectation, not USD or realised profit.",
        "Expected and realised USD use an independent reference stake for each executable episode; "
        "their sums are not one shared account balance.",
        "Unknown fees and missing books are excluded from both USD tiers and counted separately; "
        "neither is imputed.",
        "Calibration, market bias and market baseline use each episode's first probability and "
        "verified RESOLVED YES payouts only; their n is an episode count, not an observation count.",
        CLIMATOLOGY_UNAVAILABLE_REASON,
        "SELL YES realised P&L remains unavailable because its position semantics are unproven.",
        "Small samples are labelled with their counts and are not ranked.",
    ]
    if target_date_missing_episode_count:
        warnings.append(
            f"{target_date_missing_episode_count} episode(s) have no target date and are absent "
            "from the target-date buckets only."
        )
    return {
        "totals": {
            "observation_count": observation_count,
            "episode_count": episode_count,
            "target_date_missing_episode_count": target_date_missing_episode_count,
        },
        "reference_capital_usdc": reference_capital_usdc,
        "fee_assumption": {
            "source": "episode.fee_rate via execution/pricing.py",
            "unknown_fee_policy": "exclude from executable, expected USD and realised USD; never assume zero",
            "missing_book_policy": "exclude from executable, expected USD and realised USD; never use a displayed quote as a fill",
        },
        "calibration_bin_count": CALIBRATION_BIN_COUNT,
        "small_sample_threshold_episodes": SMALL_SAMPLE_EPISODES,
        "anomaly_rule": {
            "statistic": "absolute deviation from the median of at least four previous chronological valid buckets",
            "threshold_mad": ANOMALY_THRESHOLD_MAD,
            "minimum_trailing_buckets": ANOMALY_MIN_TRAILING_BUCKETS,
            "comparison": "strictly greater than 3 MAD; when MAD is zero, any non-zero deviation is flagged",
            "metrics": list(ANOMALY_METRICS),
        },
        "buckets": buckets,
        "anomalies": anomalies,
        "statistical_scope": [
            "observation_all_accepts",
            "opportunity_episodes",
            "episode_first_observation_calibration",
            "resolved_verified_only",
            "reference_capital_hypothetical",
        ],
        "warnings": warnings,
    }

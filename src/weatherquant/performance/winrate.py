"""Realised win rate, from settled episodes only.

The whole discipline of this module is the denominator.

``Win Rate = WIN / (WIN + LOSS)`` - and nothing else enters it. Pending,
disputed, unavailable and unverifiable outcomes are counted and reported,
but they are not losses and they are not wins. A sample that quietly
dropped its unresolved half would produce a precise-looking number about a
market that has not finished happening.

Two further rules follow from that:

**Episodes are not independent.** The same market can produce several
episodes across days, and several markets share one target date. Weather on
a single day decides every bracket for that day, so treating each as its own
observation overstates how much evidence there is. The interval is therefore
bootstrapped by *target date* - resampling days, not rows - and every
result says which method produced it.

**Too few samples is an answer.** Below the threshold the rate is ``None``
with the contract's exact reason, not a number with a caveat attached. A
rate built from three resolved days is not weak evidence; it is an anecdote
with a decimal point.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from weatherquant.performance.episodes import Episode

__all__ = [
    "DEFAULT_BOOTSTRAP_SAMPLES",
    "DEFAULT_MIN_SAMPLES",
    "INSUFFICIENT_REASON",
    "Outcome",
    "WinRateResult",
    "assign_outcomes",
    "confidence_interval",
    "grouped_win_rate",
    "win_rate",
]

#: Below this the rate is reported as None with INSUFFICIENT_REASON.
#: Chosen against "resolved independent samples": it counts distinct target
#: dates too, so twenty episodes from one day still fail.
DEFAULT_MIN_SAMPLES = 20

#: Distinct target dates required for a clustered interval to mean anything.
DEFAULT_MIN_CLUSTERS = 2

DEFAULT_BOOTSTRAP_SAMPLES = 2000

#: Fixed so a re-run of the same data gives the same interval. An interval
#: that moves between reports would undermine the report.
BOOTSTRAP_SEED = 20261013

INSUFFICIENT_REASON = "N/A — insufficient resolved independent samples"


class Outcome:
    """What happened to a settled episode. Only two of these are a result."""

    WIN = "WIN"
    LOSS = "LOSS"
    PENDING = "PENDING"
    DISPUTED = "DISPUTED"
    UNVERIFIABLE = "UNVERIFIABLE"
    UNAVAILABLE = "UNAVAILABLE"


#: Only these two enter the denominator.
DECISIVE = frozenset({Outcome.WIN, Outcome.LOSS})


@dataclass(frozen=True, slots=True)
class SettlementAnswer:
    """What the platform said about one market on one target date."""

    status: str
    payout: float | None = None


@dataclass(slots=True)
class ScoredEpisode:
    """An episode with its outcome attached - or explicitly without one."""

    episode: Episode
    outcome: str
    reason: str | None = None

    @property
    def target_date(self) -> str | None:
        return self.episode.key.target_date

    @property
    def market_id(self) -> str:
        return self.episode.key.market_id

    @property
    def direction(self) -> str | None:
        return self.episode.key.direction

    @property
    def mode(self) -> str | None:
        return self.episode.key.mode

    @property
    def model_name(self) -> str | None:
        return self.episode.key.model_name

    @property
    def decisive(self) -> bool:
        return self.outcome in DECISIVE


@dataclass(slots=True)
class WinRateResult:
    """One win-rate figure, with everything needed to judge it."""

    win_rate: float | None
    counts: dict[str, int]
    valid_samples: int
    distinct_markets: int
    distinct_target_dates: int
    confidence_interval: tuple[float, float] | None
    method: str
    reason: str | None = None
    statistical_scope: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def sufficient(self) -> bool:
        return self.win_rate is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "win_rate": self.win_rate,
            "counts": dict(self.counts),
            "valid_samples": self.valid_samples,
            "distinct_markets": self.distinct_markets,
            "distinct_target_dates": self.distinct_target_dates,
            "confidence_interval": list(self.confidence_interval) if self.confidence_interval else None,
            "method": self.method,
            "reason": self.reason,
            "statistical_scope": list(self.statistical_scope),
            "warnings": list(self.warnings),
        }


def assign_outcomes(
    episodes: Sequence[Episode],
    *,
    answers: dict[tuple[str, str | None], SettlementAnswer],
    verified: frozenset[tuple[str, str | None]] | None = None,
) -> list[ScoredEpisode]:
    """Attach an outcome to every episode, refusing to guess the rest.

    ``answers`` is keyed by ``(market_id, target_date)``. An episode with no
    answer is ``UNAVAILABLE``; one whose answer is not RESOLVED becomes
    ``DISPUTED`` or ``PENDING`` as reported.

    ``verified`` is the set of answers that passed cross-checking against
    the observation. Without it, ``RESOLVED`` alone is not enough: the
    contract allows a platform settlement into the denominator only when it
    has also been verified.

    SELL YES is deliberately never scored. Its position semantics are
    unproven, so a correct direction call on it still cannot be called a
    win on a strategy.
    """
    verified = verified or frozenset()
    scored: list[ScoredEpisode] = []
    for episode in episodes:
        key = (episode.key.market_id, episode.key.target_date)
        answer = answers.get(key)

        if answer is None:
            scored.append(ScoredEpisode(episode, Outcome.UNAVAILABLE, "no settlement record"))
            continue
        if answer.status == "DISPUTED":
            scored.append(ScoredEpisode(episode, Outcome.DISPUTED, "settlement disputed"))
            continue
        if answer.status != "RESOLVED":
            scored.append(ScoredEpisode(episode, Outcome.PENDING, f"settlement {answer.status}"))
            continue
        if key not in verified:
            scored.append(
                ScoredEpisode(episode, Outcome.UNVERIFIABLE, "resolved but not verified against observation")
            )
            continue
        if answer.payout is None:
            scored.append(ScoredEpisode(episode, Outcome.UNVERIFIABLE, "resolved with no payout"))
            continue
        if episode.key.direction == "sell_yes":
            scored.append(
                ScoredEpisode(episode, Outcome.UNVERIFIABLE, "SELL YES position semantics unproven")
            )
            continue

        scored.append(ScoredEpisode(episode, Outcome.WIN if answer.payout == 1.0 else Outcome.LOSS))
    return scored


def _tally(scored: Sequence[ScoredEpisode]) -> dict[str, int]:
    counts = {
        Outcome.WIN: 0, Outcome.LOSS: 0, Outcome.PENDING: 0,
        Outcome.DISPUTED: 0, Outcome.UNVERIFIABLE: 0, Outcome.UNAVAILABLE: 0,
    }
    for item in scored:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    return counts


def confidence_interval(
    scored: Sequence[ScoredEpisode],
    *,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[tuple[float, float], str]:
    """Interval that resamples target dates, not rows.

    Every bracket on one day shares the same weather, so the rows within a
    day move together. Resampling rows would treat them as independent and
    narrow the interval by roughly the square root of how many brackets a
    day has - exactly the overconfidence the contract warns about.

    Falls back to Wilson for a single cluster, and says which method was
    used, because an interval without its method is not checkable.
    """
    wins = [s for s in scored if s.outcome == Outcome.WIN]
    losses = [s for s in scored if s.outcome == Outcome.LOSS]
    n = len(wins) + len(losses)
    if n == 0:
        raise ValueError("cannot form an interval with no decisive outcomes")

    by_date: dict[str, list[bool]] = {}
    for item in wins + losses:
        by_date.setdefault(item.target_date or "", []).append(item.outcome == Outcome.WIN)

    if len(by_date) < DEFAULT_MIN_CLUSTERS:
        return _wilson(len(wins), n), "wilson"

    rng = random.Random(seed)
    cluster_names = list(by_date)
    estimates: list[float] = []
    for _ in range(max(samples, 1)):
        drawn = [rng.choice(cluster_names) for _ in cluster_names]
        outcomes = [value for name in drawn for value in by_date[name]]
        if outcomes:
            estimates.append(sum(outcomes) / len(outcomes))
    if not estimates:
        return _wilson(len(wins), n), "wilson"
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return (low, high), "cluster_bootstrap_by_target_date"


def _wilson(wins: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval - well behaved where the normal one is not.

    At n=5 or with a rate near 0 or 1, `p ± 1.96·sqrt(p(1-p)/n)` produces
    bounds outside [0,1] or collapses to a point. Wilson stays inside the
    range and keeps a sensible width on small samples, which is precisely
    the regime a fresh research dataset lives in.
    """
    if n == 0:
        raise ValueError("Wilson interval needs at least one observation")
    p = wins / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denominator
    return (max(centre - margin, 0.0), min(centre + margin, 1.0))


def win_rate(
    scored: Sequence[ScoredEpisode],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_clusters: int = DEFAULT_MIN_CLUSTERS,
) -> WinRateResult:
    """Win rate over settled, verified episodes - or an explicit refusal."""
    counts = _tally(scored)
    wins = counts.get(Outcome.WIN, 0)
    losses = counts.get(Outcome.LOSS, 0)
    n = wins + losses
    markets = {s.market_id for s in scored if s.decisive}
    dates = {s.target_date for s in scored if s.decisive and s.target_date}

    scope = ["resolved_verified_only", "opportunity_episodes"]
    warnings = [
        "Only RESOLVED and verified episodes enter this denominator; pending, "
        "disputed and unverifiable outcomes are counted but never scored.",
        "Episodes are not independent samples: markets share target dates, "
        "and one market can produce several episodes.",
        "This is a win rate on hypothetical observations, not a realised "
        "return - the system places no orders.",
    ]

    if n == 0:
        return WinRateResult(
            win_rate=None, counts=counts, valid_samples=0,
            distinct_markets=0, distinct_target_dates=0,
            confidence_interval=None, method="none",
            reason=INSUFFICIENT_REASON, statistical_scope=scope, warnings=warnings,
        )
    if n < min_samples or len(dates) < min_clusters:
        return WinRateResult(
            win_rate=None, counts=counts, valid_samples=n,
            distinct_markets=len(markets), distinct_target_dates=len(dates),
            confidence_interval=None, method="none",
            reason=(
                f"{INSUFFICIENT_REASON} — {n} decisive, "
                f"{len(dates)} distinct target date(s); "
                f"needs {min_samples} and {min_clusters}"
            ),
            statistical_scope=scope, warnings=warnings,
        )

    interval, method = confidence_interval(scored)
    return WinRateResult(
        win_rate=wins / n, counts=counts, valid_samples=n,
        distinct_markets=len(markets), distinct_target_dates=len(dates),
        confidence_interval=interval, method=method,
        statistical_scope=scope, warnings=warnings,
    )


def grouped_win_rate(
    scored: Sequence[ScoredEpisode],
    *,
    dimension: str,
    **kwargs: object,
) -> dict[str, WinRateResult]:
    """Win rate split one dimension at a time.

    Each group is judged against the threshold on its own, so a model with
    three resolved days reports N/A rather than borrowing significance from
    the model beside it.
    """
    buckets: dict[str, list[ScoredEpisode]] = {}
    for item in scored:
        if dimension == "direction":
            key = item.direction or "unknown"
        elif dimension == "mode":
            key = item.mode or "unknown"
        elif dimension == "model":
            key = item.episode.key.model_name or "unknown"
        elif dimension == "target_date":
            key = item.target_date or "unknown"
        elif dimension == "edge_band":
            key = _edge_band(item.episode.first_edge)
        else:
            raise ValueError(f"unknown dimension {dimension!r}")
        buckets.setdefault(key, []).append(item)

    return {key: win_rate(items, **kwargs) for key, items in sorted(buckets.items())}  # type: ignore[arg-type]


def _edge_band(edge: float | None) -> str:
    """The contract's bands, so edge-versus-outcome lines up with the edge page."""
    if edge is None:
        return "unknown"
    if edge < 0.02:
        return "lt_0.02"
    if edge < 0.05:
        return "0.02_0.05"
    if edge < 0.10:
        return "0.05_0.10"
    return "gte_0.10"

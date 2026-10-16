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
    "ScoredEpisode",
    "WinRateAccumulator",
    "WinRateResult",
    "assign_outcomes",
    "confidence_interval",
    "group_key",
    "grouped_win_rate",
    "outcome_for",
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


def outcome_for(
    episode: Episode,
    *,
    answers: dict[tuple[str, str | None], SettlementAnswer],
    verified: frozenset[tuple[str, str | None]] | None = None,
) -> tuple[str, str | None]:
    """Decide one episode's outcome, refusing to guess.

    Split out from :func:`assign_outcomes` so the streaming engine can score a
    store cursor without materialising a list of scored episodes, while both
    paths still answer the question in exactly one place.
    """
    verified = verified or frozenset()
    key = (episode.key.market_id, episode.key.target_date)
    answer = answers.get(key)

    if answer is None:
        return Outcome.UNAVAILABLE, "no settlement record"
    if answer.status == "DISPUTED":
        return Outcome.DISPUTED, "settlement disputed"
    if answer.status != "RESOLVED":
        return Outcome.PENDING, f"settlement {answer.status}"
    if key not in verified:
        return Outcome.UNVERIFIABLE, "resolved but not verified against observation"
    if answer.payout is None:
        return Outcome.UNVERIFIABLE, "resolved with no payout"
    if episode.key.direction == "sell_yes":
        return Outcome.UNVERIFIABLE, "SELL YES position semantics unproven"
    return (Outcome.WIN if answer.payout == 1.0 else Outcome.LOSS), None


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
    scored: list[ScoredEpisode] = []
    for episode in episodes:
        outcome, reason = outcome_for(episode, answers=answers, verified=verified)
        scored.append(ScoredEpisode(episode, outcome, reason))
    return scored


def _tally(scored: Sequence[ScoredEpisode]) -> dict[str, int]:
    counts = {
        Outcome.WIN: 0, Outcome.LOSS: 0, Outcome.PENDING: 0,
        Outcome.DISPUTED: 0, Outcome.UNVERIFIABLE: 0, Outcome.UNAVAILABLE: 0,
    }
    for item in scored:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    return counts


def _bootstrap_interval(
    by_date: dict[str, tuple[int, int]],
    *,
    wins: int,
    n: int,
    samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[tuple[float, float], str]:
    """Resample clusters, not rows - one implementation, two callers.

    ``by_date`` maps a target date to ``(observations, wins)`` in the order the
    dates were first seen among decisive episodes. That order matters: the
    generator is seeded, so the drawn sequence depends on it, and a different
    order would produce a different interval from the same data.

    Only the counts are needed rather than the per-row booleans the original
    accumulated, because the statistic is an integer sum over a concatenation:
    summing wins and totals per drawn date gives exactly the same numerator and
    denominator as materialising every boolean.
    """
    if len(by_date) < DEFAULT_MIN_CLUSTERS:
        return _wilson(wins, n), "wilson"

    rng = random.Random(seed)
    cluster_names = list(by_date)
    estimates: list[float] = []
    for _ in range(max(samples, 1)):
        drawn = [rng.choice(cluster_names) for _ in cluster_names]
        total = 0
        won = 0
        for name in drawn:
            observations, date_wins = by_date[name]
            total += observations
            won += date_wins
        if total:
            estimates.append(won / total)
    if not estimates:
        return _wilson(wins, n), "wilson"
    estimates.sort()
    low = estimates[int(0.025 * (len(estimates) - 1))]
    high = estimates[int(0.975 * (len(estimates) - 1))]
    return (low, high), "cluster_bootstrap_by_target_date"


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
    wins = sum(1 for s in scored if s.outcome == Outcome.WIN)
    losses = sum(1 for s in scored if s.outcome == Outcome.LOSS)
    n = wins + losses
    if n == 0:
        raise ValueError("cannot form an interval with no decisive outcomes")

    # The cluster order is wins-then-losses, not row order. That is what the
    # original did (`for item in wins + losses`), and the bootstrap generator is
    # seeded, so the order is part of the answer rather than an implementation
    # detail. Reproduced deliberately; see WinRateAccumulator for the streaming
    # equivalent.
    by_date: dict[str, tuple[int, int]] = {}
    for outcome in (Outcome.WIN, Outcome.LOSS):
        for item in scored:
            if item.outcome != outcome:
                continue
            key = item.target_date or ""
            observations, date_wins = by_date.get(key, (0, 0))
            by_date[key] = (
                observations + 1,
                date_wins + (1 if outcome == Outcome.WIN else 0),
            )

    return _bootstrap_interval(by_date, wins=wins, n=n, samples=samples, seed=seed)


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


class WinRateAccumulator:
    """The win-rate state, fed one outcome at a time.

    One implementation, two drivers: :func:`win_rate` walks a materialised
    list, the streaming engine walks a store cursor. Neither holds the scored
    episodes, so memory is bounded by the number of distinct target dates
    rather than by the length of the history.

    Cluster order is preserved exactly as the original produced it - all WIN
    dates first, then the dates that only ever lost - because the bootstrap
    generator is seeded and ``rng.choice`` is called in cluster order.
    """

    def __init__(
        self,
        *,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        min_clusters: int = DEFAULT_MIN_CLUSTERS,
        samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
        seed: int = BOOTSTRAP_SEED,
    ) -> None:
        self.min_samples = min_samples
        self.min_clusters = min_clusters
        self.samples = samples
        self.seed = seed
        self.counts: dict[str, int] = {
            Outcome.WIN: 0, Outcome.LOSS: 0, Outcome.PENDING: 0,
            Outcome.DISPUTED: 0, Outcome.UNVERIFIABLE: 0, Outcome.UNAVAILABLE: 0,
        }
        self.wins = 0
        self.losses = 0
        self._markets: set[str] = set()
        self._dates: set[str] = set()
        self._win_dates: dict[str, list[int]] = {}
        self._loss_dates: dict[str, list[int]] = {}

    def add(self, *, outcome: str, market_id: str, target_date: str | None) -> None:
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        if outcome not in DECISIVE:
            return
        won = outcome == Outcome.WIN
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self._markets.add(market_id)
        if target_date:
            self._dates.add(target_date)
        table = self._win_dates if won else self._loss_dates
        entry = table.get(target_date or "")
        if entry is None:
            table[target_date or ""] = [1, 1 if won else 0]
        else:
            entry[0] += 1
            entry[1] += 1 if won else 0

    def clusters(self) -> dict[str, tuple[int, int]]:
        """``target_date -> (observations, wins)`` in the original's order."""
        merged: dict[str, tuple[int, int]] = {}
        for table in (self._win_dates, self._loss_dates):
            for key, (observations, date_wins) in table.items():
                if key in merged:
                    total, won = merged[key]
                    merged[key] = (total + observations, won + date_wins)
                else:
                    merged[key] = (observations, date_wins)
        return merged

    def result(self) -> WinRateResult:
        counts = dict(self.counts)
        n = self.wins + self.losses
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
        if n < self.min_samples or len(self._dates) < self.min_clusters:
            return WinRateResult(
                win_rate=None, counts=counts, valid_samples=n,
                distinct_markets=len(self._markets), distinct_target_dates=len(self._dates),
                confidence_interval=None, method="none",
                reason=(
                    f"{INSUFFICIENT_REASON} — {n} decisive, "
                    f"{len(self._dates)} distinct target date(s); "
                    f"needs {self.min_samples} and {self.min_clusters}"
                ),
                statistical_scope=scope, warnings=warnings,
            )

        interval, method = _bootstrap_interval(
            self.clusters(), wins=self.wins, n=n, samples=self.samples, seed=self.seed
        )
        return WinRateResult(
            win_rate=self.wins / n, counts=counts, valid_samples=n,
            distinct_markets=len(self._markets), distinct_target_dates=len(self._dates),
            confidence_interval=interval, method=method,
            statistical_scope=scope, warnings=warnings,
        )


def win_rate(
    scored: Sequence[ScoredEpisode],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_clusters: int = DEFAULT_MIN_CLUSTERS,
) -> WinRateResult:
    """Win rate over settled, verified episodes - or an explicit refusal."""
    accumulator = WinRateAccumulator(min_samples=min_samples, min_clusters=min_clusters)
    for item in scored:
        accumulator.add(
            outcome=item.outcome, market_id=item.market_id, target_date=item.target_date
        )
    return accumulator.result()


def group_key(episode: Episode, outcome: str, dimension: str) -> str:
    """Which bucket one scored episode falls into, for one dimension.

    Extracted so the streaming engine groups by exactly the same rule the
    list-based grouping uses - including ``unknown`` for a missing value,
    which is a bucket and not a dropped row.
    """
    if dimension == "direction":
        return episode.key.direction or "unknown"
    if dimension == "mode":
        return episode.key.mode or "unknown"
    if dimension == "model":
        return episode.key.model_name or "unknown"
    if dimension == "target_date":
        return episode.key.target_date or "unknown"
    if dimension == "edge_band":
        return _edge_band(episode.first_edge)
    raise ValueError(f"unknown dimension {dimension!r}")


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
        buckets.setdefault(group_key(item.episode, item.outcome, dimension), []).append(item)

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

"""Observation edge: the distribution of every ACCEPT the engine priced.

Two things this module is careful about.

**It counts evaluations, not opportunities.** The engine re-prices the same
market every cycle, so a wide distribution here is partly the same good
decision observed many times. Every summary carries
``statistical_scope = observation_all_accepts`` so a reader cannot mistake
it for a count of distinct chances. The deduplicated view lives in
``episodes.summarize``.

**A percentiles definition matters.** ``p05`` on 7,000 repeated evaluations
of one bracket is not a tail risk measure - it is that bracket, sampled
densely. Grouping by model, direction and target date is what makes the
shape readable, and the group sizes travel with the values so a four-sample
mean never passes as an established one.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from weatherquant.performance.episodes import _DecisionRow, _finite, row_from_mapping

__all__ = [
    "ACCEPT_STATE",
    "EDGE_BANDS",
    "DistributionParts",
    "band_of",
    "daily_edge",
    "daily_entry",
    "distribution",
    "distribution_from_parts",
    "edge_by_bin",
    "grouped_edge",
    "median_from_ranks",
    "observation_edge",
    "percentile",
    "quantile_from_ranks",
    "quantile_ranks",
]

ACCEPT_STATE = "ACCEPT"

#: Edges are USD per share on a 0-1 contract, so a percent of a dollar is
#: the honest unit for binning them later.
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)

#: The contract's bands, in the order they are published. Lower bound
#: inclusive, upper bound exclusive, open-ended on the last band so a
#: genuinely large edge lands in one rather than falling off the end.
EDGE_BANDS: dict[str, tuple[float | None, float | None]] = {
    "lt_0.02": (None, 0.02),
    "0.02_0.05": (0.02, 0.05),
    "0.05_0.10": (0.05, 0.10),
    "gte_0.10": (0.10, None),
}


def band_of(value: float) -> str:
    """Which band an edge falls into, by the same rule the list path uses."""
    for name, (low, high) in EDGE_BANDS.items():
        if (low is None or value >= low) and (high is None or value < high):
            return name
    raise ValueError(f"no band contains {value!r}")  # pragma: no cover - the bands are exhaustive


def quantile_ranks(count: int) -> dict[float, tuple[int, int]]:
    """The order statistics each published quantile needs, for one group."""
    return {q: _rank_pair(count, q) for q in QUANTILES}



def _rank_pair(count: int, q: float) -> tuple[int, int]:
    """The two order statistics a quantile interpolates between.

    One definition, two users: :func:`percentile` reads the values out of a
    sorted list, and the streaming engine asks the store for those same ranks.
    The arithmetic is deliberately identical (``q * (count - 1)``, floor and
    ceil) so both select the same pair, and only then is the interpolation
    applied - which is also why the store's own ``quantile_cont`` is not used:
    it interpolates with a different expression and disagrees in the last bit.
    """
    position = q * (count - 1)
    return math.floor(position), math.ceil(position)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, matching the usual definition.

    Explicit rather than delegated so the behaviour on a small sample is
    fixed: with one observation every quantile is that observation, and with
    none the answer is ``None`` rather than a fabricated zero.
    """
    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q!r}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    lower, upper = _rank_pair(len(ordered), q)
    if lower == upper:
        return ordered[lower]
    weight = q * (len(ordered) - 1) - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def quantile_from_ranks(count: int, ranks: Mapping[int, float], q: float) -> float | None:
    """``percentile``, given the order statistics instead of the sample.

    ``ranks`` maps a zero-based rank to the value at that rank. The caller is
    responsible for having fetched exactly the ranks ``_rank_pair`` asks for;
    a missing one is a bug in the caller rather than a value to invent, so it
    raises rather than returning a fabricated number.
    """
    if count <= 0:
        return None
    if count == 1:
        return ranks[0]
    lower, upper = _rank_pair(count, q)
    if lower == upper:
        return ranks[lower]
    weight = q * (count - 1) - lower
    return ranks[lower] * (1 - weight) + ranks[upper] * weight


def median_from_ranks(count: int, ranks: Mapping[int, float]) -> float | None:
    """``statistics.median``, given the order statistics.

    Deliberately not ``quantile_from_ranks(..., 0.5)``: the two disagree in the
    last bit on an even-sized sample, because ``statistics.median`` averages the
    middle pair as ``(a + b) / 2`` while the quantile interpolation evaluates
    ``a * 0.5 + b * 0.5``. The payload publishes both ``median`` and ``p50``
    from the same values, so both have to be reproduced by their own rule.
    """
    if count <= 0:
        return None
    if count % 2 == 1:
        return ranks[count // 2]
    middle = count // 2
    return (ranks[middle - 1] + ranks[middle]) / 2


@dataclass(slots=True)
class DistributionParts:
    """Everything a distribution needs, however it was obtained.

    The list-based path fills this from a materialised sample; the streaming
    path fills it from a store scan plus a streamed ``math.fsum`` and
    ``statistics.pstdev``. Both hand it to :func:`distribution_from_parts`, so
    the published shape has exactly one definition.
    """

    count: int
    total: float | None = None
    median: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    std: float | None = None
    quantiles: Mapping[float, float | None] = field(default_factory=dict)


def distribution_from_parts(parts: DistributionParts) -> dict[str, float | int | None]:
    """Format one group's statistics. The only place the shape is defined."""
    if parts.count == 0:
        return {
            "count": 0, "sum": None, "mean": None, "median": None, "min": None,
            "max": None, "std": None,
            **{f"p{int(q * 100):02d}": None for q in QUANTILES},
        }
    total = parts.total
    assert total is not None
    out: dict[str, float | int | None] = {
        "count": parts.count,
        "sum": total,
        "mean": total / parts.count,
        "median": parts.median,
        "min": parts.minimum,
        "max": parts.maximum,
        "std": parts.std,
    }
    for q in QUANTILES:
        out[f"p{int(q * 100):02d}"] = parts.quantiles.get(q)
    return out


def distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    """Summary statistics for one group of per-share edges.

    ``std`` is the population standard deviation, matching ``numpy.std``'s
    default. With these samples it is the right choice: we are describing
    the evaluations we actually observed, not estimating a wider population
    the engine never produced.
    """
    count = len(values)
    if count == 0:
        return distribution_from_parts(DistributionParts(count=0))
    return distribution_from_parts(
        DistributionParts(
            count=count,
            total=math.fsum(values),
            median=statistics.median(values),
            minimum=min(values),
            maximum=max(values),
            std=statistics.pstdev(values) if count > 1 else 0.0,
            quantiles={q: percentile(values, q) for q in QUANTILES},
        )
    )


def _rows(rows: Iterable[_DecisionRow | dict]) -> list[_DecisionRow]:
    return [r if isinstance(r, _DecisionRow) else row_from_mapping(r) for r in rows]


def _accept_edges(rows: Sequence[_DecisionRow]) -> tuple[list[float], int]:
    """Edges of priced ACCEPTs, plus how many were refused for having none."""
    edges: list[float] = []
    unpriced = 0
    for row in rows:
        if row.state != ACCEPT_STATE:
            continue
        value = _finite(row.net_edge_usd)
        if value is None:
            unpriced += 1
            continue
        edges.append(value)
    return edges, unpriced


def observation_edge(rows: Iterable[_DecisionRow | dict]) -> dict[str, object]:
    """The whole distribution, with its scope stated on the label.

    ``refused`` carries the rows that were excluded and why, so the total
    can be reconciled: nothing is dropped silently, and a caller can always
    answer "where did the other rows go".
    """
    normalised = _rows(rows)
    edges, unpriced = _accept_edges(normalised)
    stats = distribution(edges)

    return {
        "overall": stats,
        "scope": "observation_all_accepts",
        "sample_size": stats["count"],
        "decisions_seen": len(normalised),
        "accepts_seen": sum(1 for r in normalised if r.state == ACCEPT_STATE),
        "refused": {
            "accept_without_usable_edge": unpriced,
            "not_accept": sum(1 for r in normalised if r.state != ACCEPT_STATE),
        },
        "statistical_scope": ["observation_all_accepts", "database_lifetime"],
        "warnings": [
            "Observation Edge Sum sums repeated evaluations of the same "
            "opportunity: it is not profit, not asset growth and not the "
            "expected return of independent trades.",
            "Quantiles over repeated evaluations describe how often the "
            "engine re-priced a bracket, not how often a market behaved this way.",
        ],
    }


def _bucket_key(row: _DecisionRow, dimension: str) -> str:
    if dimension == "model":
        return row.model_name or "unknown"
    if dimension == "direction":
        return row.direction or "unknown"
    if dimension == "target_date":
        return row.target_date or "unknown"
    if dimension == "mode":
        return row.mode or "unknown"
    raise ValueError(f"unknown dimension {dimension!r}")


def grouped_edge(
    rows: Iterable[_DecisionRow | dict],
    *,
    dimension: str,
) -> dict[str, dict[str, float | int | None]]:
    """Edge distribution split by one dimension.

    Each group carries its own ``count``: a mean over four evaluations and a
    mean over four thousand look identical otherwise, and the whole point of
    grouping is to see which is which.
    """
    normalised = _rows(rows)
    buckets: dict[str, list[float]] = {}
    for row in normalised:
        if row.state != ACCEPT_STATE:
            continue
        value = _finite(row.net_edge_usd)
        if value is None:
            continue
        buckets.setdefault(_bucket_key(row, dimension), []).append(value)

    return {key: distribution(values) for key, values in sorted(buckets.items())}


def daily_entry(target_date: str, count: int, total: float) -> dict[str, object]:
    """One row of the daily trend, from its two numbers.

    ``daily_edge`` and the streaming engine both publish through this, so the
    wording of ``distinct_note`` cannot drift between them.
    """
    return {
        "target_date": target_date,
        "observation_count": count,
        "observation_edge_sum": total,
        "mean_edge": total / count,
        "distinct_note": (
            "counted per evaluation; a single market day re-priced on "
            "every cycle contributes once per cycle"
        ),
    }


def daily_edge(rows: Iterable[_DecisionRow | dict]) -> list[dict[str, object]]:
    """Per-target-date totals, ordered by date.

    Grouped by ``target_date`` rather than by when the decision was made:
    the contract's daily trend is "how much theoretical edge was attributed
    to each market day", which is the axis a settlement will eventually
    score against.
    """
    normalised = _rows(rows)
    buckets: dict[str, list[float]] = {}
    for row in normalised:
        if row.state != ACCEPT_STATE or row.target_date is None:
            continue
        value = _finite(row.net_edge_usd)
        if value is None:
            continue
        buckets.setdefault(str(row.target_date), []).append(value)

    out: list[dict[str, object]] = []
    for day in sorted(buckets):
        values = buckets[day]
        out.append(daily_entry(day, len(values), math.fsum(values)))
    return out


def edge_by_bin(
    rows: Iterable[_DecisionRow | dict],
    *,
    edges: Mapping[str, tuple[float | None, float | None]] | None = None,
) -> dict[str, list[float]]:
    """Split ACCEPT edges into bands, for comparing edge against outcome.

    The default bands are the contract's. Open-ended on the last band so a
    genuinely large edge lands in one rather than falling off the end.
    """
    bands = edges or EDGE_BANDS
    out: dict[str, list[float]] = {name: [] for name in bands}
    for row in _rows(rows):
        if row.state != ACCEPT_STATE:
            continue
        value = _finite(row.net_edge_usd)
        if value is None:
            continue
        for name, (low, high) in bands.items():
            if (low is None or value >= low) and (high is None or value < high):
                out[name].append(value)
                break
    return out



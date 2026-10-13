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

from weatherquant.performance.episodes import _DecisionRow, _finite, row_from_mapping

__all__ = [
    "ACCEPT_STATE",
    "daily_edge",
    "distribution",
    "edge_by_bin",
    "grouped_edge",
    "observation_edge",
    "percentile",
]

ACCEPT_STATE = "ACCEPT"

#: Edges are USD per share on a 0-1 contract, so a percent of a dollar is
#: the honest unit for binning them later.
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)


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
    position = q * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    """Summary statistics for one group of per-share edges.

    ``std`` is the population standard deviation, matching ``numpy.std``'s
    default. With these samples it is the right choice: we are describing
    the evaluations we actually observed, not estimating a wider population
    the engine never produced.
    """
    count = len(values)
    if count == 0:
        return {
            "count": 0, "sum": None, "mean": None, "median": None, "min": None,
            "max": None, "std": None,
            **{f"p{int(q * 100):02d}": None for q in QUANTILES},
        }

    total = math.fsum(values)
    out: dict[str, float | int | None] = {
        "count": count,
        "sum": total,
        "mean": total / count,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "std": statistics.pstdev(values) if count > 1 else 0.0,
    }
    for q in QUANTILES:
        out[f"p{int(q * 100):02d}"] = percentile(values, q)
    return out


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
        out.append(
            {
                "target_date": day,
                "observation_count": len(values),
                "observation_edge_sum": math.fsum(values),
                "mean_edge": math.fsum(values) / len(values),
                "distinct_note": (
                    "counted per evaluation; a single market day re-priced on "
                    "every cycle contributes once per cycle"
                ),
            }
        )
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
    bands = edges or {
        "lt_0.02": (None, 0.02),
        "0.02_0.05": (0.02, 0.05),
        "0.05_0.10": (0.05, 0.10),
        "gte_0.10": (0.10, None),
    }
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



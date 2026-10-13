"""Publishing performance snapshots: compute once, read many.

Nothing downstream is allowed to recompute these figures. The dashboard
reads them, the MCP gateway reads what the dashboard serves, and a number
that is derived twice is a number that will eventually disagree with
itself. So the whole performance surface is six files written atomically by
one builder.

The builder owns no database and no socket. Decisions arrive as rows and
settlements arrive as answers, which keeps it testable and keeps the rule
in the contract: the research process is the only writer, and it reads an
export rather than the engine's leased file.

Every payload carries the envelope from contract section 0, including
``statistical_scope`` and ``warnings``. Those two fields are what stop a
consumer from repeating an edge sum as a profit figure - they travel with
the number rather than living in a document nobody loads.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.performance.capital import simulate_portfolio
from weatherquant.performance.edge import daily_edge, edge_by_bin, grouped_edge, observation_edge
from weatherquant.performance.episodes import Episode, build_episodes, summarize
from weatherquant.performance.settlement import classify, parse_resolutions
from weatherquant.performance.winrate import (
    SettlementAnswer,
    assign_outcomes,
    grouped_win_rate,
    win_rate,
)
from weatherquant.shadow.publish import atomic_write_json

__all__ = [
    "PERFORMANCE_SCHEMA_VERSION",
    "SNAPSHOT_FILES",
    "build_performance",
    "publish_performance",
]

PERFORMANCE_SCHEMA_VERSION = 1

#: filename -> logical name, so a reader never invents a naming scheme.
SNAPSHOT_FILES: dict[str, str] = {
    "summary.json": "performance-summary",
    "edge.json": "performance-edge",
    "win-rate.json": "performance-win-rate",
    "opportunities.json": "performance-opportunities",
    "settlements.json": "performance-settlements",
    "models.json": "performance-models",
}


def _envelope(
    *,
    logical_name: str,
    data: dict[str, Any],
    now: datetime,
    source: str,
    sample_size: int,
    statistical_scope: Sequence[str],
    warnings: Sequence[str],
    stale_after_seconds: float = 300.0,
    dataset: str = "live",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
        "kind": logical_name,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "source": source,
        "as_of_utc": now.astimezone(UTC).isoformat(),
        "stale_after_seconds": stale_after_seconds,
        "freshness": {"state": "fresh", "age_seconds": 0.0, "reason": None},
        "dataset": dataset,
        "sample_size": sample_size,
        "statistical_scope": list(statistical_scope),
        "warnings": list(warnings),
        "data": data,
    }
    if extra:
        payload.update(extra)
    return payload


def _settlement_index(
    payloads: Iterable[Any],
) -> tuple[dict[tuple[str, str | None], SettlementAnswer], frozenset[tuple[str, str | None]]]:
    """Fold raw resolution bodies into ``(market, date) -> answer``.

    Accepts either a parsed mapping or a raw ``/v2/resolutions`` body, so a
    backfill can hand over exactly what the platform returned without
    re-encoding it first.
    """
    answers: dict[tuple[str, str | None], SettlementAnswer] = {}
    verified: set[tuple[str, str | None]] = set()
    for item in payloads:
        if not isinstance(item, Mapping):
            continue
        market_id = str(item.get("market_id") or "")
        if not market_id:
            continue
        target_date = item.get("target_date")
        status = str(item.get("settlement_status") or "UNAVAILABLE")
        payout = item.get("resolved_outcome")
        answers[(market_id, str(target_date) if target_date else None)] = SettlementAnswer(
            status=status,
            payout=float(payout) if payout in (0, 1, 0.0, 1.0) else None,
        )
        if status == "RESOLVED" and str(item.get("verification_status")) == "VERIFIED":
            verified.add((market_id, str(target_date) if target_date else None))
        # A raw resolutions body may be passed through untouched.
        if "data" in item and item.get("data") is not None:
            for record in parse_resolutions(item):
                key = (market_id, str(target_date) if target_date else None)
                status = classify(record)
                answers[key] = SettlementAnswer(status=status, payout=record.price)
    return answers, frozenset(verified)


def build_performance(
    *,
    decisions: Sequence[Mapping[str, Any]],
    settlements: Sequence[Any] = (),
    episodes: Sequence[Episode] | None = None,
    now: datetime | None = None,
    source: str = "weatherquant-research",
    dataset: str = "live",
    gap_window_seconds: float = 900.0,
    capital_usdc: float = 100.0,
    max_allocation_fraction: float = 0.10,
    book_for=None,
) -> dict[str, dict[str, Any]]:
    """Build all six snapshots from one pass over the data.

    ``episodes`` may be supplied to avoid rebuilding them; when it is not,
    they are derived from ``decisions`` under the same rules the rest of the
    package uses, so a caller cannot accidentally get a different episode
    definition here than in the tests.
    """
    now = now or datetime.now(tz=UTC)

    if episodes is None:
        episodes, episode_counters = build_episodes(
            decisions, gap_window_seconds=gap_window_seconds
        )
    else:
        episode_counters = {"episodes_built": len(episodes)}

    answers, verified = _settlement_index(settlements)
    scored = assign_outcomes(episodes, answers=answers, verified=verified)

    edge_summary = observation_edge(decisions)
    episode_summary = summarize(episodes)
    rate = win_rate(scored)

    # The portfolio is a hypothetical: no book resolver means no fills, and
    # the result says so rather than pricing against a displayed quote.
    capital = simulate_portfolio(
        episodes,
        capital_usdc=capital_usdc,
        max_allocation_fraction=max_allocation_fraction,
        book_for=book_for or (lambda _e: None),
    )

    scope = ["observation_all_accepts", "opportunity_episodes", "database_lifetime"]

    summary = {
        "observation_edge_sum": edge_summary["overall"]["sum"],
        "unique_opportunity_edge_sum": episode_summary["unique_opportunity_edge_sum"],
        "average_net_edge": edge_summary["overall"]["mean"],
        "realized_directional_win_rate": rate.win_rate,
        "verified_opportunities": rate.valid_samples,
        "settled_markets": rate.distinct_markets,
        "counts": rate.counts,
        "reference_capital_usdc": capital.capital_usdc,
        "reference_capital": capital.as_dict(),
        "accept_observations": episode_summary["accept_observations"],
        "opportunity_episodes": episode_summary["opportunity_episodes"],
        "distinct_markets": episode_summary["distinct_markets"],
        "distinct_target_dates": episode_summary["distinct_target_dates"],
        "episode_build": episode_counters,
        "confidence_interval": list(rate.confidence_interval) if rate.confidence_interval else None,
        "confidence_method": rate.method,
        "insufficient_reason": rate.reason,
        "statistical_scope": scope,
        "warnings": [
            "Observation Edge Sum counts repeated evaluations of one opportunity; "
            "it is not profit, not asset growth and not the expected return of independent trades.",
            "Unique Opportunity Edge Sum is a model expectation, not realised profit.",
            "Actual profit is N/A: this system places no orders.",
            *rate.warnings,
        ],
    }

    edge_payload = {
        "overall": edge_summary["overall"],
        "by_model": grouped_edge(decisions, dimension="model"),
        "by_direction": grouped_edge(decisions, dimension="direction"),
        "by_target_date": grouped_edge(decisions, dimension="target_date"),
        "daily": daily_edge(decisions),
        "by_edge_band": edge_by_bin(decisions),
        "refused": edge_summary["refused"],
        "scope": edge_summary["scope"],
        "statistical_scope": edge_summary["statistical_scope"],
        "warnings": edge_summary["warnings"],
    }

    win_rate_payload = {
        "overall": rate.as_dict(),
        "by_model": {k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="model").items()},
        "by_direction": {
            k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="direction").items()
        },
        "by_mode": {k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="mode").items()},
        "by_target_date": {
            k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="target_date").items()
        },
        "by_edge_band": {
            k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="edge_band").items()
        },
        "statistical_scope": rate.statistical_scope,
        "warnings": rate.warnings,
    }

    resolver = book_for or (lambda _e: None)
    opportunity_rows = []
    for episode in episodes:
        book = resolver(episode)
        row = {
            **episode.as_dict(),
            "outcome": next(
                (s.outcome for s in scored if s.episode.episode_id == episode.episode_id), None
            ),
        }
        # Levels travel with the episode so the API can size against a
        # different stake without re-deriving a book it no longer has. When
        # the levels were never available the key is absent, and absence is
        # what turns into NOT_EXECUTABLE rather than into an invented fill.
        if book is not None:
            row["book"] = {
                "bids": [[lvl.price_usd_per_share, lvl.size_shares] for lvl in book.bids],
                "asks": [[lvl.price_usd_per_share, lvl.size_shares] for lvl in book.asks],
            }
        opportunity_rows.append(row)

    opportunities = {
        "episodes": opportunity_rows,
        "total": len(episodes),
        "statistical_scope": ["opportunity_episodes"],
        "warnings": episode_summary["warnings"],
    }

    settlements_payload = {
        "answers": [
            {"market_id": m, "target_date": d, "status": a.status, "payout": a.payout}
            for (m, d), a in sorted(answers.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
        ],
        "verified": [{"market_id": m, "target_date": d} for m, d in sorted(verified)],
        "counts": rate.counts,
        "statistical_scope": ["resolved_verified_only"],
        "warnings": [
            "Platform settlement and weather observation are stored separately; "
            "neither substitutes for the other.",
        ],
    }

    models_payload = {
        "models": sorted({str(d.get("model_name") or "unknown") for d in decisions}),
        "by_model_edge": grouped_edge(decisions, dimension="model"),
        "by_model_win_rate": {
            k: v.as_dict() for k, v in grouped_win_rate(scored, dimension="model").items()
        },
        "note": (
            "Brier, Log Loss, ECE, MAE, RMSE and Top-1 are served from the "
            "research evaluation module; see /api/v1/performance/models"
        ),
        "statistical_scope": ["out_of_sample_chronological"],
        "warnings": ["Scores are out-of-sample and chronological; no future data is used."],
    }

    common = dict(now=now, source=source, dataset=dataset)
    return {
        "summary.json": _envelope(
            logical_name="performance-summary", data=summary,
            sample_size=edge_summary["sample_size"], statistical_scope=scope,
            warnings=summary["warnings"], **common,
        ),
        "edge.json": _envelope(
            logical_name="performance-edge", data=edge_payload,
            sample_size=edge_summary["sample_size"], statistical_scope=scope,
            warnings=edge_payload["warnings"], **common,
        ),
        "win-rate.json": _envelope(
            logical_name="performance-win-rate", data=win_rate_payload,
            sample_size=rate.valid_samples, statistical_scope=rate.statistical_scope,
            warnings=rate.warnings, **common,
        ),
        "opportunities.json": _envelope(
            logical_name="performance-opportunities", data=opportunities,
            sample_size=len(episodes), statistical_scope=["opportunity_episodes"],
            warnings=episode_summary["warnings"], **common,
        ),
        "settlements.json": _envelope(
            logical_name="performance-settlements", data=settlements_payload,
            sample_size=len(answers), statistical_scope=["resolved_verified_only"],
            warnings=settlements_payload["warnings"], **common,
        ),
        "models.json": _envelope(
            logical_name="performance-models", data=models_payload,
            sample_size=edge_summary["sample_size"],
            statistical_scope=["out_of_sample_chronological"],
            warnings=models_payload["warnings"], **common,
        ),
    }


def publish_performance(
    payloads: Mapping[str, dict[str, Any]],
    directory: str | Path,
) -> list[Path]:
    """Atomically publish every snapshot.

    Each file is written the same way the shadow writes its own reports -
    temp file, fsync, rename - so a reader never sees half a document. A
    failure on one file must not skip the others silently, so the paths
    returned are the ones that actually landed.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename in SNAPSHOT_FILES:
        if filename not in payloads:
            continue
        path = target / filename
        atomic_write_json(path, payloads[filename])
        written.append(path)
    return written


def write_json_debug(payload: Mapping[str, Any], path: str | Path) -> None:
    """Test helper: dump one payload for inspection."""
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

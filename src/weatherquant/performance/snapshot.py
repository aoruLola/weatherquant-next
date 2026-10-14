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

import hashlib
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
    "CURRENT_POINTER",
    "PERFORMANCE_SCHEMA_VERSION",
    "SNAPSHOT_FILES",
    "build_performance",
    "new_generation_id",
    "publish_performance",
    "resolve_generation",
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


CURRENT_POINTER = "current.json"


def new_generation_id(*, now: datetime, payloads: Mapping[str, dict[str, Any]]) -> str:
    """A stable id for one batch.

    Derived from the instant and the payloads themselves, so the same data
    republished under a new clock does not masquerade as a new generation,
    and two different generations can never share an id.
    """
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(json.dumps(payloads[name], sort_keys=True, default=str).encode("utf-8"))
    return f"{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{digest.hexdigest()[:12]}"


def publish_performance(
    payloads: Mapping[str, dict[str, Any]],
    directory: str | Path,
    *,
    generation_id: str | None = None,
    watermark: Any = None,
    coverage: Mapping[str, Any] | None = None,
) -> list[Path]:
    """Publish six snapshots as one generation, then switch the pointer.

    Six separate renames into one directory is not an atomic cross-file
    commit: a reader that arrives halfway sees a mixture of two generations,
    and a mixture of an old opportunities file with a new win-rate file is
    exactly the kind of inconsistency nobody would notice until it was
    scored against. So the batch goes into its own directory first, and only
    once every file is durable does a single rename publish it by name.

    A failure at any point before the pointer move leaves the previous
    generation current and intact. The half-written directory is inert - no
    reader follows it - and the next publish uses a fresh id.
    """
    target = Path(directory)
    now = datetime.now(tz=UTC)
    gid = generation_id or new_generation_id(now=now, payloads=payloads)
    generation_dir = target / "generations" / gid
    generation_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    files_meta: dict[str, Any] = {}
    for filename in SNAPSHOT_FILES:
        if filename not in payloads:
            continue
        payload = dict(payloads[filename])
        payload["generation_id"] = gid
        path = generation_dir / filename
        atomic_write_json(path, payload)
        written.append(path)
        files_meta[filename] = {"bytes": path.stat().st_size}

    missing = [name for name in SNAPSHOT_FILES if name not in files_meta]
    if missing:
        raise RuntimeError(
            f"generation {gid} is incomplete: missing {missing}; "
            "the previous generation stays current"
        )

    manifest = {
        "generation_id": gid,
        "published_at_utc": now.astimezone(UTC).isoformat(),
        "dataset": next(
            (p.get("dataset") for p in payloads.values() if p.get("dataset")), None
        ),
        "source": next(
            (p.get("source") for p in payloads.values() if p.get("source")), None
        ),
        "watermark": watermark,
        "coverage": dict(coverage or {}),
        "files": files_meta,
        "performance_schema_version": PERFORMANCE_SCHEMA_VERSION,
    }
    atomic_write_json(generation_dir / "manifest.json", manifest)

    # One rename publishes the batch. Before this line the previous
    # generation is still the current one; after it, this one is.
    atomic_write_json(target / CURRENT_POINTER, {"generation_id": gid})
    return written


def resolve_generation(directory: str | Path) -> tuple[Path | None, dict[str, Any] | None]:
    """Where to read from, and what that batch claims to be.

    Falls back to the flat layout when no pointer exists, so a deployment
    that predates generations keeps working - and so the answer to "which
    files" is one function rather than a rule each caller re-derives.
    """
    root = Path(directory)
    pointer = root / CURRENT_POINTER
    if not pointer.exists():
        return (root if root.exists() else None), None
    try:
        generation = json.loads(pointer.read_text(encoding="utf-8"))["generation_id"]
    except (OSError, ValueError, KeyError):
        return None, None
    generation_dir = root / "generations" / str(generation)
    manifest_path = generation_dir / "manifest.json"
    if not manifest_path.exists():
        # A pointer to a directory that is not there is a broken publish,
        # not a missing backfill - say so by returning nothing rather than
        # a path that will 404 inside.
        return None, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return generation_dir, None
    return generation_dir, manifest


def write_json_debug(payload: Mapping[str, Any], path: str | Path) -> None:
    """Test helper: dump one payload for inspection."""
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

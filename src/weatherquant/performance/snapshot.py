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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weatherquant.performance.capital import simulate_portfolio
from weatherquant.performance.edge import daily_edge, edge_by_bin, grouped_edge, observation_edge
from weatherquant.performance.episodes import (
    Episode,
    _timestamp,
    build_episodes,
    summarize,
)
from weatherquant.performance.settlement import classify, parse_resolutions
from weatherquant.performance.winrate import (
    SettlementAnswer,
    assign_outcomes,
    grouped_win_rate,
    win_rate,
)
from weatherquant.shadow.publish import (
    PartitionedArray,
    atomic_write_json,
    atomic_write_json_streaming,
)

__all__ = [
    "CURRENT_POINTER",
    "PERFORMANCE_SCHEMA_VERSION",
    "SNAPSHOT_FILES",
    "Generation",
    "build_performance",
    "new_generation_id",
    "publish_performance",
    "resolve_generation",
]

PERFORMANCE_SCHEMA_VERSION = 1

#: The detail window: how many episodes the dashboard can page through, and how
#: far back. The contract (§0.4) says a snapshot holds a window rather than the
#: whole table, and this is that window - the aggregates cover the whole
#: history, the browsable detail covers the recent end of it.
#:
#: It is bounded because a page request must be bounded: with 240,000 episodes
#: published as one array, serving fifty rows meant parsing 163 MB.
DETAIL_MAX_EPISODES = 20_000
DETAIL_WINDOW_DAYS = 90
#: Rows per published part. A page of 50 never reads more than one part, and a
#: filtered request reads them one at a time.
DETAIL_PARTITION_SIZE = 1_000
#: How many per-opportunity capital results travel with the summary. The counts
#: are complete; the list is an illustration.
CAPITAL_RESULT_SAMPLE = 50


#: filename -> logical name, so a reader never invents a naming scheme.
SNAPSHOT_FILES: dict[str, str] = {
    "summary.json": "performance-summary",
    "edge.json": "performance-edge",
    "win-rate.json": "performance-win-rate",
    "opportunities.json": "performance-opportunities",
    "settlements.json": "performance-settlements",
    "models.json": "performance-models",
}


def detail_window_descriptor(
    *,
    window_episodes: int,
    all_episodes: int,
    max_episodes: int,
    days: int,
    first_seen_utc: str | None,
    last_seen_utc: str | None,
) -> dict[str, Any]:
    """What the browsable detail covers, stated rather than implied.

    A reader that sees fifty rows out of 240,000 has to be told which fifty and
    what is missing. ``complete`` is the honest flag: false means the detail is
    a window and the aggregates - not this list - are the whole history.
    """
    return {
        "episodes": window_episodes,
        "episodes_all": all_episodes,
        "max_episodes": max_episodes,
        "days": days,
        "first_seen_utc_from": first_seen_utc,
        "last_seen_utc_to": last_seen_utc,
        "complete": window_episodes == all_episodes,
    }


def detail_window(
    episodes: Sequence[Episode],
    *,
    now: datetime,
    max_episodes: int = DETAIL_MAX_EPISODES,
    days: int = DETAIL_WINDOW_DAYS,
) -> tuple[list[Episode], dict[str, Any]]:
    """The most recent slice of a materialised episode set, newest first.

    The list-based twin of the store query in the streaming engine: same
    predicate, same order, same cap. Two implementations of "what the dashboard
    can page through" would be two answers, so the equivalence test compares
    them on the same input.
    """
    cutoff = now - timedelta(days=days)
    recent = [episode for episode in episodes if episode.first_seen_utc >= cutoff]
    recent.sort(key=lambda e: (e.first_seen_utc, e.episode_id), reverse=True)
    window = recent[:max_episodes]
    descriptor = detail_window_descriptor(
        window_episodes=len(window),
        all_episodes=len(episodes),
        max_episodes=max_episodes,
        days=days,
        first_seen_utc=window[0].first_seen_utc.isoformat() if window else None,
        last_seen_utc=window[-1].first_seen_utc.isoformat() if window else None,
    )
    return window, descriptor


def detail_warnings(window: Mapping[str, Any]) -> list[str]:
    """The sentence a windowed detail owes its reader."""
    if window.get("complete"):
        return []
    return [
        f"Only the most recent {window['episodes']} episode(s) "
        f"({window['days']} day window) are published in full; the totals, the "
        "edge distributions and the win rates cover the whole history.",
    ]


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


@dataclass(frozen=True, slots=True)
class SettlementRecord:
    """One settlement answer with the evidence that produced it.

    The verdict and the source are carried verbatim rather than folded into a
    boolean: "verified" is a conclusion, and a conclusion whose evidence was
    discarded cannot be re-examined when the rules change. ``fetched_at_utc``
    is what makes a *kept* answer distinguishable from a *fresh* one - the
    state store holds settlements across runs, which is the point, and a reader
    who cannot see the retrieval time cannot tell the two apart.
    """

    market_id: str
    target_date: str | None
    status: str
    payout: float | None
    verified: bool
    verification_status: str | None = None
    resolution_source: str | None = None
    fetched_at_utc: datetime | None = None


def settlement_records_from_index(payloads: Iterable[Any]) -> list[SettlementRecord]:
    """The published records for a settlement index, in a stable order."""
    answers, verified = _settlement_index(payloads)
    provenance = _settlement_provenance(payloads)
    records = [
        SettlementRecord(
            market_id=market_id,
            target_date=target_date,
            status=answer.status,
            payout=answer.payout,
            verified=(market_id, target_date) in verified,
            verification_status=provenance.get((market_id, target_date), (None, None, None))[0],
            resolution_source=provenance.get((market_id, target_date), (None, None, None))[1],
            fetched_at_utc=provenance.get((market_id, target_date), (None, None, None))[2],
        )
        for (market_id, target_date), answer in answers.items()
    ]
    return sorted(records, key=lambda r: (r.market_id, r.target_date or ""))


def _settlement_provenance(
    payloads: Iterable[Any],
) -> dict[tuple[str, str | None], tuple[str | None, str | None, datetime | None]]:
    """``(verification_status, resolution_source, fetched_at)`` per answer."""
    out: dict[tuple[str, str | None], tuple[str | None, str | None, datetime | None]] = {}
    for item in payloads:
        if not isinstance(item, Mapping):
            continue
        market_id = str(item.get("market_id") or "")
        if not market_id:
            continue
        raw = item.get("target_date")
        key = (market_id, str(raw) if raw else None)
        stamp = item.get("fetched_at_utc")
        fetched: datetime | None = None
        if stamp is not None:
            try:
                fetched = _timestamp(stamp)
            except (TypeError, ValueError):
                fetched = None
        verdict = item.get("verification_status")
        source = item.get("resolution_source")
        out[key] = (
            str(verdict) if verdict is not None else None,
            str(source) if source is not None else None,
            fetched,
        )
    return out


def settlement_freshness(records: Sequence[SettlementRecord]) -> dict[str, Any]:
    """When the settlement layer was last retrieved, and whether it can be told.

    A kept answer and a fresh one look identical from the win rate alone, so
    the retrieval times travel with the answers and this block summarises them,
    including how many have none at all.
    """
    stamps = [record.fetched_at_utc for record in records if record.fetched_at_utc is not None]
    known = [stamp.astimezone(UTC).isoformat() for stamp in stamps]
    return {
        "answers": len(records),
        "answers_without_retrieval_time": len(records) - len(known),
        "as_of_utc": max(known) if known else None,
        "oldest_utc": min(known) if known else None,
    }


def settlement_payload(
    records: Sequence[SettlementRecord],
    *,
    counts: Mapping[str, int],
) -> dict[str, Any]:
    """The ``settlements.json`` data block, shared by both engines."""
    freshness = settlement_freshness(records)
    warnings = [
        "Platform settlement and weather observation are stored separately; "
        "neither substitutes for the other.",
    ]
    if freshness["answers_without_retrieval_time"]:
        warnings.append(
            f"{freshness['answers_without_retrieval_time']} settlement answer(s) carry no "
            "retrieval time; their age cannot be judged and they must not be presented as "
            "freshly verified."
        )
    return {
        "answers": [
            {
                "market_id": record.market_id,
                "target_date": record.target_date,
                "status": record.status,
                "payout": record.payout,
                "verification_status": record.verification_status,
                "resolution_source": record.resolution_source,
                "fetched_at_utc": (
                    record.fetched_at_utc.astimezone(UTC).isoformat()
                    if record.fetched_at_utc is not None
                    else None
                ),
            }
            for record in records
        ],
        "verified": [
            {"market_id": record.market_id, "target_date": record.target_date}
            for record in records
            if record.verified
        ],
        "counts": dict(counts),
        "freshness": freshness,
        "statistical_scope": ["resolved_verified_only"],
        "warnings": warnings,
    }


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
    detail_max_episodes: int = DETAIL_MAX_EPISODES,
    detail_window_days: int = DETAIL_WINDOW_DAYS,
    detail_partition_size: int = DETAIL_PARTITION_SIZE,
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
    settlement_records = settlement_records_from_index(settlements)
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
        # Scoped to opportunities, not to everything read. The old name was
        # shared with `coverage`, which counts every decision in the input -
        # so one generation published 55 and 28 under the same word, and a
        # reader could not tell which population either described.
        "accept_opportunity_distinct_markets": episode_summary["distinct_markets"],
        "accept_opportunity_distinct_target_dates": episode_summary[
            "distinct_target_dates"
        ],
        # Deprecated aliases. A deployed dashboard still reads these, and a
        # payload cannot tell its readers to upgrade.
        "distinct_markets": episode_summary["distinct_markets"],
        "distinct_target_dates": episode_summary["distinct_target_dates"],
        "episode_build": episode_counters,
        # The settlement layer can be kept across runs by the streaming engine,
        # so when it was retrieved is part of the number: a win rate whose
        # settlements were fetched weeks ago is not the same claim as one
        # fetched this morning, and the rate alone cannot tell them apart.
        "settlement_freshness": settlement_freshness(settlement_records),
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
    outcomes = {item.episode.episode_id: item.outcome for item in scored}
    window_episodes, window = detail_window(
        episodes,
        now=now,
        max_episodes=detail_max_episodes,
        days=detail_window_days,
    )
    opportunity_rows = []
    for episode in window_episodes:
        book = resolver(episode)
        row = {
            **episode.as_dict(),
            "outcome": outcomes.get(episode.episode_id),
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
        "episodes": PartitionedArray(
            lambda: iter(opportunity_rows),
            directory="opportunities",
            collection="episodes",
            partition_size=detail_partition_size,
            key_of=lambda row: [row["first_seen_utc"], row["episode_id"]],
            label="opportunities.episodes",
        ),
        "total": len(window_episodes),
        "total_all": len(episodes),
        "window": window,
        "statistical_scope": ["opportunity_episodes"]
        + ([] if window["complete"] else [f"window:{window['days']}d"]),
        "warnings": [*episode_summary["warnings"], *detail_warnings(window)],
    }

    settlements_payload = settlement_payload(settlement_records, counts=rate.counts)

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
            sample_size=len(window_episodes),
            statistical_scope=opportunities["statistical_scope"],
            warnings=opportunities["warnings"], **common,
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
    extra_manifest: Mapping[str, Any] | None = None,
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

    The writer is the streaming one, so a payload may carry a
    ``StreamedArray`` for a collection too large to hold (the edge bands and
    the episode list). For a payload with none, the bytes written are
    identical to what ``atomic_write_json`` would have produced.
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
        atomic_write_json_streaming(path, payload, root=generation_dir)
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
        "generated_at_utc": now.astimezone(UTC).isoformat(),
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
    if extra_manifest:
        # Additive only. ``algorithm_version``, ``input_source`` and
        # ``input_range`` describe *how* this batch was produced; a reader that
        # predates them ignores them, and one that does not can tell two
        # differently-built generations apart without diffing the payloads.
        manifest.update(dict(extra_manifest))
    atomic_write_json(generation_dir / "manifest.json", manifest)

    # One rename publishes the batch. Before this line the previous
    # generation is still the current one; after it, this one is.
    atomic_write_json(target / CURRENT_POINTER, {"generation_id": gid})
    return written


@dataclass(frozen=True, slots=True)
class Generation:
    """Which published batch to read, and whether anything is wrong with it.

    Three states, and they are genuinely different facts:

    * ``directory is None`` and ``problem is None`` - no generation has ever
      been published here. The caller falls back to the flat layout, which is
      also what an older deployment has.
    * ``directory`` set - a batch is published and readable.
    * ``problem`` set - a pointer exists but cannot be honoured. That is a
      *broken publish*, and calling it "not published yet" would send an
      operator to re-run a backfill that already succeeded.

    A two-tuple could not hold these apart, and conflating the first and the
    third is exactly what this dataclass replaced: an empty performance
    directory was being reported as an incomplete publish.
    """

    directory: Path | None = None
    manifest: dict[str, Any] | None = None
    problem: str | None = None


def resolve_generation(directory: str | Path) -> Generation:
    """Where to read from, and what that batch claims to be.

    Falls back to the flat layout when no pointer exists, so a deployment
    that predates generations keeps working - and so the answer to "which
    files" is one function rather than a rule each caller re-derives.
    """
    root = Path(directory)
    if not root.exists():
        return Generation()

    pointer = root / CURRENT_POINTER
    if not pointer.exists():
        return Generation()  # nothing published yet, or the flat layout

    try:
        generation = json.loads(pointer.read_text(encoding="utf-8"))["generation_id"]
    except (OSError, ValueError, KeyError) as exc:
        return Generation(problem=f"the generation pointer is unreadable: {exc}")

    generation_dir = root / "generations" / str(generation)
    manifest_path = generation_dir / "manifest.json"
    if not manifest_path.exists():
        # A pointer to a directory with no manifest is a publish that died
        # before it could describe itself - not an absent backfill.
        return Generation(
            problem=(
                f"the pointer selects generation {generation}, which has no "
                "manifest; the publish is incomplete"
            )
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Generation(
            directory=generation_dir,
            problem=f"generation {generation} has an unreadable manifest: {exc}",
        )
    return Generation(directory=generation_dir, manifest=manifest)


def write_json_debug(payload: Mapping[str, Any], path: str | Path) -> None:
    """Test helper: dump one payload for inspection."""
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

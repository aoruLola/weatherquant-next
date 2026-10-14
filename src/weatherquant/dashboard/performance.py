"""Performance endpoints: read the published snapshots, never recompute.

Every figure here comes from a file the research process already wrote.
That is not a performance choice - it is the contract. The dashboard may not
open a database, and a number derived a second time is a number that will
eventually disagree with the first.

The one exception is the reference-capital endpoint, and it is an exception
that keeps the rule: it re-runs ``weatherquant.performance.capital``, the
*same* module the backfill used, over the episodes the backfill published.
Different stake, same arithmetic, one implementation - so MCP can be
guaranteed to agree with the dashboard without either of them reimplementing
anything.

Models are deliberately loose. The snapshot carries its own
``statistical_scope`` and ``warnings``; narrowing them into a fixed schema
would drop exactly the fields that stop an edge sum being read as a return.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.sources import JsonSource, SnapshotSources, freshness_of
from weatherquant.performance.capital import (
    book_from_mapping,
    simulate_independent,
    simulate_portfolio,
)
from weatherquant.performance.episodes import episode_from_mapping

__all__ = [
    "PerformanceState",
    "performance_models",
    "performance_opportunities",
    "performance_reference_capital",
    "performance_settlements",
    "performance_summary",
    "performance_win_rate",
]


class PerformancePayload(BaseModel):
    """One performance snapshot, passed through with its envelope checked.

    Loose on purpose: ``statistical_scope`` and ``warnings`` are part of the
    data, not metadata about it, and a consumer that wants to know whether a
    number is profit has to receive them with the number.
    """

    model_config = ConfigDict(extra="allow")

    performance_schema_version: int | None = None
    kind: str | None = None
    generated_at_utc: datetime | None = None
    source: str | None = None
    as_of_utc: datetime | None = None
    stale_after_seconds: float | None = None
    dataset: str | None = None
    sample_size: int | None = None
    statistical_scope: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class PerformanceState:
    """Holds the sources so an endpoint never reaches for a file itself."""

    def __init__(self, sources: SnapshotSources, settings: DashboardSettings) -> None:
        self.sources = sources
        self.settings = settings

    def snapshot(self, logical_name: str, filename: str, now) -> tuple[PerformancePayload, Any]:
        source: JsonSource = self.sources.performance_snapshot(logical_name, filename, now)
        if source.payload is None:
            return (
                PerformancePayload(kind=logical_name, source=source.reason),
                source.freshness,
            )
        payload = PerformancePayload.model_validate(source.payload)
        # Re-stamp freshness from the file's own clock: the snapshot is the
        # source of truth for how old it is, not the moment we read it.
        as_of = payload.as_of_utc or payload.generated_at_utc
        stale = payload.stale_after_seconds or 300.0
        freshness = freshness_of(
            as_of, now, stale_after_seconds=stale + 60.0, missing_reason=source.freshness.reason or ""
        )
        return payload, freshness


def _missing(reason: str) -> tuple[dict[str, Any], Any]:
    return {"reason": reason, "statistical_scope": [], "warnings": []}, None


def performance_summary(state: PerformanceState, now: datetime) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-summary", "summary.json", now)
    if payload.data == {} and payload.kind:
        return {"reason": payload.source or "performance summary not published", "statistical_scope": [], "warnings": []}, freshness
    return payload.model_dump(), freshness


def performance_edge(state: PerformanceState, now: datetime) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-edge", "edge.json", now)
    if payload.data == {} and payload.kind:
        return {"reason": payload.source or "performance edge not published", "statistical_scope": [], "warnings": []}, freshness
    return payload.model_dump(), freshness


def performance_win_rate(state: PerformanceState, now: datetime) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-win-rate", "win-rate.json", now)
    if payload.data == {} and payload.kind:
        return {"reason": payload.source or "win rate not published", "statistical_scope": [], "warnings": []}, freshness
    return payload.model_dump(), freshness


def _paginate(
    payload: PerformancePayload,
    *,
    rows: list[Any],
    limit: int,
    cursor: int,
    collection: str,
) -> dict[str, Any]:
    """Slice ``rows`` and build a response that carries only the slice.

    The snapshot's own ``data`` block holds the complete collection, so it
    is dropped rather than spread: otherwise ``limit`` would bound nothing
    and a large history would make one page cost the whole history - which
    is also how the gateway's 2 MiB ceiling gets tripped by a request that
    asked for fifty rows.

    ``data`` is not simply removed either. The UI decides "this snapshot is
    missing" from an empty ``data`` block, so an empty one here would make a
    successful page render as an unavailable one. It keeps the counts.
    """
    window = rows[cursor : cursor + limit]
    envelope = payload.model_dump()
    envelope.pop("data", None)
    envelope["items"] = window
    envelope["total"] = len(rows)
    envelope["next_cursor"] = (cursor + limit) if cursor + limit < len(rows) else None
    envelope["data"] = {
        "collection": collection,
        "returned": len(window),
        "total": len(rows),
        "cursor": cursor,
    }
    return envelope


def performance_opportunities(
    state: PerformanceState, now: datetime, *, limit: int = 50, cursor: int = 0
) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-opportunities", "opportunities.json", now)
    if payload.data == {} and payload.kind:
        return {
            "reason": payload.source or "opportunities not published",
            "items": [], "total": 0, "next_cursor": None,
            "statistical_scope": [], "warnings": [],
        }, freshness

    rows = list(payload.data.get("episodes") or [])
    return _paginate(payload, rows=rows, limit=limit, cursor=cursor, collection="episodes"), freshness


def performance_settlements(
    state: PerformanceState, now: datetime, *, limit: int = 50, cursor: int = 0
) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-settlements", "settlements.json", now)
    if payload.data == {} and payload.kind:
        return {
            "reason": payload.source or "settlements not published",
            "items": [], "total": 0, "next_cursor": None,
            "statistical_scope": [], "warnings": [],
        }, freshness

    rows = list(payload.data.get("answers") or [])
    return _paginate(payload, rows=rows, limit=limit, cursor=cursor, collection="answers"), freshness


def performance_models(state: PerformanceState, now: datetime) -> tuple[dict[str, Any], Any]:
    payload, freshness = state.snapshot("performance-models", "models.json", now)
    if payload.data == {} and payload.kind:
        return {"reason": payload.source or "model comparison not published", "statistical_scope": [], "warnings": []}, freshness
    return payload.model_dump(), freshness


def performance_reference_capital(
    state: PerformanceState,
    now: datetime,
    *,
    capital_usdc: float = 100.0,
    mode: str = "portfolio",
    max_allocation_fraction: float = 0.10,
) -> tuple[dict[str, Any], Any]:
    """Size the published episodes against a stake the caller chooses.

    Not a cached figure: the backfill publishes what a default stake would
    have done, and asking "what about 500?" is the whole reason this
    endpoint exists. It still does not compute anything of its own - it
    rebuilds the episodes the research process recorded and runs
    ``simulate_portfolio``, the module that produced the original answer.

    Books travel inside the opportunities snapshot, so a stake change is
    re-walked against the same levels rather than against a displayed
    quote. Where the backfill had no levels, the result says so.
    """
    if capital_usdc <= 0:
        raise ValueError("capital_usdc must be greater than 0")
    if mode not in ("portfolio", "single_opportunity"):
        raise ValueError("mode must be 'portfolio' or 'single_opportunity'")
    if not 0 < max_allocation_fraction <= 1:
        raise ValueError("max_allocation_fraction must be in (0, 1]")

    payload, freshness = state.snapshot("performance-opportunities", "opportunities.json", now)
    if payload.data == {} and payload.kind:
        return {
            "reason": payload.source or "opportunities not published",
            "statistical_scope": ["reference_capital_hypothetical"],
            "warnings": [],
        }, freshness

    rows = list(payload.data.get("episodes") or [])
    episodes = [episode_from_mapping(r) for r in rows if r.get("first_edge") is not None]
    books = {r.get("episode_id"): r.get("book") for r in rows}

    def _book_for(episode):
        return book_from_mapping(
            books.get(episode.episode_id),
            market_id=episode.key.market_id,
            token_id=episode.key.market_id,
        )

    if mode == "single_opportunity":
        # Each episode gets the whole stake, independently. Not the portfolio
        # simulator with the fraction dialled up: that still drains one
        # shared balance, so the second opportunity would be priced against
        # money the first one already spent.
        payload_out = simulate_independent(
            episodes, capital_usdc=capital_usdc, book_for=_book_for
        ).as_dict()
        source = "weatherquant.performance.capital.simulate_independent"
    else:
        payload_out = simulate_portfolio(
            episodes,
            capital_usdc=capital_usdc,
            max_allocation_fraction=max_allocation_fraction,
            book_for=_book_for,
        ).as_dict()
        source = "weatherquant.performance.capital.simulate_portfolio"

    payload_out["mode"] = mode
    payload_out["as_of_utc"] = now.isoformat()
    payload_out["generated_at_utc"] = now.isoformat()
    payload_out["sample_size"] = len(episodes)
    payload_out["source_endpoint"] = source
    return payload_out, freshness

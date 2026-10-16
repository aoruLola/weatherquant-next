"""Performance endpoints: read the published snapshots, never recompute.

Every figure here comes from a file the research process already wrote.
That is not a performance choice - it is the contract. The dashboard may not
open a database, and a number derived a second time is a number that will
eventually disagree with the first.

Two things changed when the detail stopped being one big array
--------------------------------------------------------------------------

**Detail is published as parts.** ``opportunities.json`` used to carry every
episode inline, so serving fifty rows meant parsing all 240,000 of them: a
163 MB response built from a 163 MB file, for a page of fifty. The rows are now
published under ``opportunities/part-NNNNN.json`` and the snapshot describes
where they are, so a page reads one part. That is the difference between a
bounded read and a read that grows with the history.

**Paging is over a window, and says so.** The published detail is the most
recent slice of the history (``window`` in the payload), because a page request
has to be bounded by something other than the size of the archive. The
aggregates - totals, edge distributions, win rates - still cover the whole
history, and the response carries the window so a reader cannot mistake one for
the other.

The one exception to "never recompute" is the reference-capital endpoint, and
it keeps the rule: it re-runs ``weatherquant.performance.capital``, the *same*
module the backfill used, over the episodes the backfill published. Different
stake, same arithmetic, one implementation - so MCP can be guaranteed to agree
with the dashboard without either of them reimplementing anything.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.contracts import Freshness
from weatherquant.dashboard.performance_freshness import (
    evaluate_layers,
    read_export_state,
    read_ingest_state,
)
from weatherquant.dashboard.sources import (
    JsonSource,
    SnapshotSources,
    parse_utc,
)
from weatherquant.performance.capital import (
    book_from_mapping,
    simulate_independent,
    simulate_portfolio,
    window_portfolio_payload,
)
from weatherquant.performance.episodes import Episode, episode_from_mapping

__all__ = [
    "OPPORTUNITY_FILTERS",
    "CursorGenerationChanged",
    "PerformanceState",
    "performance_models",
    "performance_opportunities",
    "performance_reference_capital",
    "performance_settlements",
    "performance_summary",
    "performance_win_rate",
]

#: The filters the opportunities endpoint accepts. A whitelist, so a filter can
#: never reach a file path or a SQL string - and so the response can echo
#: exactly what was applied.
OPPORTUNITY_FILTERS = (
    "model",
    "direction",
    "mode",
    "target_date",
    "outcome",
    "min_edge",
    "max_edge",
)

#: The outcome vocabulary, mirroring ``performance.winrate.Outcome``.
OUTCOMES = ("WIN", "LOSS", "PENDING", "DISPUTED", "UNVERIFIABLE", "UNAVAILABLE")
DIRECTIONS = ("buy_yes", "sell_yes")
MODES = ("LIVE", "REPLAY")

#: The largest page the API will serve, whatever a caller asks for.
MAX_PAGE = 200


class CursorGenerationChanged(RuntimeError):
    """The cursor was minted against a different published generation.

    Refused rather than honoured: offsets are positions in one batch, and a
    position in a batch that has been replaced is not a position in the new
    one. Serving it would silently skip or repeat rows across two generations,
    which is the failure the generation pointer exists to prevent.
    """


def _watermark_time(manifest: Mapping[str, Any]) -> str | None:
    """The business-time half of a manifest watermark, if it carries one.

    The manifest stores ``[timestamp, cursor_hash]``. A generation published
    before the watermark was recorded has ``None`` here, and the caller falls
    back to the payload's own ``as_of_utc``.
    """
    watermark = manifest.get("watermark")
    if isinstance(watermark, (list, tuple)) and watermark:
        return str(watermark[0]) if watermark[0] else None
    return None


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
    generation_id: str | None = None
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
        return payload, self._layered_freshness(payload, source, now)

    def _layered_freshness(self, payload: PerformancePayload, source: JsonSource, now) -> Freshness:
        """Freshness across the four layers, not just the file's age.

        The snapshot's own age is the least interesting of the four: with a
        20-minute publish cadence a healthy snapshot is routinely 19 minutes
        old. What an operator needs to know is *which* layer stopped, so the
        verdict names it.
        """
        generation = self.sources.performance_generation()
        manifest = generation.manifest or {}
        published_at = parse_utc(manifest.get("published_at_utc")) or payload.generated_at_utc
        consumed_at = payload.as_of_utc or parse_utc(_watermark_time(manifest))
        interval = (
            self.settings.performance_publish_interval_seconds
            + self.settings.performance_publish_grace_seconds
        )
        layers = evaluate_layers(
            now=now,
            export_state=read_export_state(self.settings.shadow_export_dir),
            ingest_state=read_ingest_state(self.settings.performance_snapshots_dir),
            snapshot_consumed_time=consumed_at,
            published_at_utc=published_at,
            publish_interval_seconds=interval,
            export_stale_after_seconds=self.settings.shadow_export_stale_seconds,
            ingest_stale_after_seconds=self.settings.performance_ingest_stale_seconds,
            publication_stale_after_seconds=self.settings.performance_publication_stale_seconds,
        )
        return Freshness(
            state=layers.state,
            # Kept for readers written before the layers existed: still the
            # snapshot's age, still the timestamp it is measured from.
            age_seconds=layers.snapshot_age_seconds,
            as_of_utc=published_at,
            stale_after_seconds=interval,
            reason=layers.reason or source.freshness.reason,
            export_data_lag_seconds=layers.export_data_lag_seconds,
            ingestion_lag_seconds=layers.ingestion_lag_seconds,
            publication_lag_seconds=layers.publication_lag_seconds,
            snapshot_age_seconds=layers.snapshot_age_seconds,
            export_watermark_utc=layers.export_watermark_time,
            consumed_watermark_utc=layers.consumed_watermark_time,
            published_at_utc=layers.published_at_utc,
            lagging_layer=layers.lagging_layer,
            publish_interval_seconds=interval,
        )

    def generation_directory(self) -> Path | None:
        """Where the parts of the current generation live, or None.

        Resolved through the same cached pointer the snapshot read used, so a
        publish landing mid-request cannot hand back one file from the old
        batch and the parts from the new one.
        """
        generation = self.sources.performance_generation()
        if generation.directory is not None:
            return generation.directory
        # The flat layout an older deployment still has: the files sit directly
        # in the performance directory, and so do the parts.
        return self.settings.performance_snapshots_dir


# ---------------------------------------------------------------------------
# reading a page out of the published detail
# ---------------------------------------------------------------------------


def _encode_cursor(generation_id: str | None, offset: int) -> str:
    raw = f"{generation_id or '-'}|{offset}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | int | None, generation_id: str | None) -> int:
    """The offset a cursor names, refusing one from another generation.

    A bare integer is the offset the endpoint accepted before generations were
    encoded in the cursor. It is still honoured - a client that has one keeps
    working - but it cannot be checked against the batch it came from, so it is
    treated as belonging to the current one.
    """
    if cursor is None or cursor == "":
        return 0
    if isinstance(cursor, int):
        return max(0, cursor)
    text = str(cursor)
    if text.isdigit():
        return max(0, int(text))
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"cursor is not a cursor this API issued: {cursor!r}") from exc
    stamped, _, offset = raw.partition("|")
    if stamped != (generation_id or "-"):
        raise CursorGenerationChanged(
            f"the cursor was issued for generation {stamped} but the current one is "
            f"{generation_id or '-'}; start again from the first page"
        )
    if not offset.isdigit():
        raise ValueError(f"cursor is not a cursor this API issued: {cursor!r}")
    return int(offset)


def _matches(row: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """Whether one published episode passes every filter that was set.

    Absent filters are not applied at all - they are not "empty string" and not
    "zero" - so an unset ``min_edge`` cannot quietly drop the rows whose edge is
    null.
    """
    for name, wanted in filters.items():
        if wanted is None:
            continue
        if name == "min_edge":
            value = row.get("first_edge")
            if value is None or value < wanted:
                return False
        elif name == "max_edge":
            value = row.get("first_edge")
            if value is None or value > wanted:
                return False
        elif name == "outcome":
            if (row.get("outcome") or "UNAVAILABLE") != wanted:
                return False
        elif name == "model":
            if (row.get("model_name") or "unknown") != wanted:
                return False
        else:
            if (row.get(name) or "unknown") != wanted:
                return False
    return True


def _iter_partition_rows(
    directory: Path, descriptor: Mapping[str, Any]
) -> Iterator[dict[str, Any]]:
    """Every row of a partitioned detail, one part at a time.

    Bounded by the part, not by the detail: this is what a filtered request
    scans, and what a page reads from.
    """
    base = directory / str(descriptor.get("directory") or "opportunities")
    for part in descriptor.get("partitions") or []:
        path = base / str(part.get("file"))
        try:
            with path.open(encoding="utf-8") as handle:
                rows = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"the published detail part {path.name} is unreadable: {exc}") from exc
        yield from rows


def _partition_slice(
    directory: Path, descriptor: Mapping[str, Any], offset: int, limit: int
) -> list[dict[str, Any]]:
    """Rows ``[offset, offset+limit)`` of an unfiltered page, part by part.

    Uses the per-part counts in the descriptor to skip straight to the parts
    that hold the page: a request for rows 4001-4050 opens one file, not the
    two hundred that came before it.
    """
    rows: list[dict[str, Any]] = []
    start = offset
    for part in descriptor.get("partitions") or []:
        items = int(part.get("items") or 0)
        if start >= items:
            start -= items
            continue
        base = directory / str(descriptor.get("directory") or "opportunities")
        with (base / str(part.get("file"))).open(encoding="utf-8") as handle:
            chunk = json.load(handle)
        take = chunk[start : start + (limit - len(rows))]
        rows.extend(take)
        start = 0
        if len(rows) >= limit:
            break
    return rows


def _opportunity_page(
    state: PerformanceState,
    payload: PerformancePayload,
    *,
    offset: int,
    limit: int,
    filters: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """``(page, total matches in the window)`` for one request."""
    data = payload.data
    detail = data.get("episodes")
    if isinstance(detail, list):
        # A snapshot published before the detail was partitioned. Still
        # readable, and still bounded: an older deployment's file is small.
        matched = [row for row in detail if _matches(row, filters)]
        return matched[offset : offset + limit], len(matched)

    if not isinstance(detail, Mapping) or not detail.get("partitioned"):
        raise RuntimeError("the opportunities snapshot carries no readable detail")

    directory = state.generation_directory()
    if directory is None:
        raise RuntimeError("the performance directory does not exist")

    if not filters:
        total = int(detail.get("total") or 0)
        return _partition_slice(directory, detail, offset, limit), total

    # A filtered page has to look at every row in the window: there is no index
    # over an immutable file, and inventing one would be a database. The scan
    # is bounded by the window, which is the point of publishing a window.
    page: list[dict[str, Any]] = []
    matched = 0
    for row in _iter_partition_rows(directory, detail):
        if not _matches(row, filters):
            continue
        if offset <= matched < offset + limit:
            page.append(row)
        matched += 1
    return page, matched


def _missing(reason: str) -> dict[str, Any]:
    return {"reason": reason, "statistical_scope": [], "warnings": []}


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


def performance_opportunities(
    state: PerformanceState,
    now: datetime,
    *,
    limit: int = 50,
    cursor: str | int | None = None,
    model: str | None = None,
    direction: str | None = None,
    mode: str | None = None,
    target_date: str | None = None,
    outcome: str | None = None,
    min_edge: float | None = None,
    max_edge: float | None = None,
) -> tuple[dict[str, Any], Any]:
    """One page of the published episode detail, filtered server-side.

    Server-side is the whole point: the browser never receives the history, so
    it cannot be asked to slice it, and the response is bounded by ``limit``
    whatever the archive has grown to.
    """
    payload, freshness = state.snapshot("performance-opportunities", "opportunities.json", now)
    if payload.data == {} and payload.kind:
        return {
            "reason": payload.source or "opportunities not published",
            "items": [], "total": 0, "next_cursor": None, "prev_cursor": None,
            "statistical_scope": [], "warnings": [],
        }, freshness

    limit = max(1, min(int(limit), MAX_PAGE))
    offset = _decode_cursor(cursor, payload.generation_id)
    filters = {
        "model": model,
        "direction": direction,
        "mode": mode,
        "target_date": target_date,
        "outcome": outcome,
        "min_edge": min_edge,
        "max_edge": max_edge,
    }
    filters = {name: value for name, value in filters.items() if value is not None}

    page, total = _opportunity_page(state, payload, offset=offset, limit=limit, filters=filters)

    data = payload.data
    window = data.get("window") or {}
    next_offset = offset + len(page)
    envelope = payload.model_dump()
    envelope.pop("data", None)
    envelope["items"] = page
    envelope["total"] = total
    envelope["next_cursor"] = (
        _encode_cursor(payload.generation_id, next_offset) if next_offset < total else None
    )
    envelope["prev_cursor"] = (
        _encode_cursor(payload.generation_id, max(0, offset - limit)) if offset > 0 else None
    )
    envelope["data"] = {
        "collection": "episodes",
        "returned": len(page),
        "total": total,
        "offset": offset,
        "limit": limit,
        "cursor": cursor if cursor not in (None, "") else None,
        "filters": filters,
        "window": window,
        "total_all": data.get("total_all"),
        "statistical_scope": data.get("statistical_scope") or ["opportunity_episodes"],
        "warnings": data.get("warnings") or [],
    }
    return envelope, freshness


def performance_settlements(
    state: PerformanceState,
    now: datetime,
    *,
    limit: int = 50,
    cursor: int = 0,
    status: str | None = None,
    market_id: str | None = None,
) -> tuple[dict[str, Any], Any]:
    """One page of the settlement answers.

    Bounded by construction - one row per market and target date, not per
    decision - so this stays an offset cursor rather than a keyset one.
    """
    payload, freshness = state.snapshot("performance-settlements", "settlements.json", now)
    if payload.data == {} and payload.kind:
        return {
            "reason": payload.source or "settlements not published",
            "items": [], "total": 0, "next_cursor": None,
            "statistical_scope": [], "warnings": [],
        }, freshness

    limit = max(1, min(int(limit), MAX_PAGE))
    cursor = max(0, int(cursor))
    rows = list(payload.data.get("answers") or [])
    if status:
        rows = [row for row in rows if str(row.get("status") or "") == status]
    if market_id:
        rows = [row for row in rows if str(row.get("market_id") or "") == market_id]

    window = rows[cursor : cursor + limit]
    envelope = payload.model_dump()
    envelope.pop("data", None)
    envelope["items"] = window
    envelope["total"] = len(rows)
    envelope["next_cursor"] = (cursor + limit) if cursor + limit < len(rows) else None
    envelope["prev_cursor"] = max(0, cursor - limit) if cursor > 0 else None
    envelope["data"] = {
        "collection": "answers",
        "returned": len(window),
        "total": len(rows),
        "offset": cursor,
        "limit": limit,
        "cursor": cursor,
        "filters": {k: v for k, v in (("status", status), ("market_id", market_id)) if v},
        "freshness": payload.data.get("freshness"),
        "statistical_scope": payload.data.get("statistical_scope") or ["resolved_verified_only"],
        "warnings": payload.data.get("warnings") or [],
    }
    return envelope, freshness


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

    Not a cached figure: the backfill publishes what a default stake would have
    done, and asking "what about 500?" is the whole reason this endpoint exists.
    It still does not compute anything of its own - it rebuilds the episodes the
    research process recorded and runs ``simulate_portfolio``, the module that
    produced the original answer.

    It answers over the *published detail window*, not the whole history, and
    says so: the payload carries the window and a warning naming the published
    aggregate that does cover everything. Re-simulating 240,000 episodes to
    answer a stake change is the load this round exists to remove, and a
    windowed answer that admits it is worth more than an unbounded one that
    does not.

    Books travel inside the opportunities snapshot, so a stake change is
    re-walked against the same levels rather than against a displayed quote.
    Where the backfill had no levels, the result says so.
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

    # Built one part at a time and converted immediately: the parsed rows are
    # dropped as they become episodes, so the peak is the episode set (bounded
    # by the window) rather than the window's JSON.
    episodes: list[Episode] = []
    books: dict[Any, Any] = {}
    for episode, book in _window_episodes(state, payload):
        episodes.append(episode)
        books[episode.episode_id] = book

    def _book_for(episode: Episode):
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
        result = simulate_independent(episodes, capital_usdc=capital_usdc, book_for=_book_for)
        source = "weatherquant.performance.capital.simulate_independent"
    else:
        result = simulate_portfolio(
            episodes,
            capital_usdc=capital_usdc,
            max_allocation_fraction=max_allocation_fraction,
            book_for=_book_for,
        )
        source = "weatherquant.performance.capital.simulate_portfolio"

    window = dict(payload.data.get("window") or {})
    payload_out = window_portfolio_payload(
        result,
        window=window,
        generation_id=payload.generation_id,
        mode=mode,
        initial_capital_usdc=capital_usdc,
    )
    payload_out["mode"] = mode
    payload_out["as_of_utc"] = now.isoformat()
    payload_out["generated_at_utc"] = now.isoformat()
    payload_out["sample_size"] = len(episodes)
    payload_out["source_endpoint"] = source
    return payload_out, freshness


def _window_rows(state: PerformanceState, payload: PerformancePayload) -> list[dict[str, Any]]:
    """Every row of the published detail window, reassembled from its parts."""
    detail = payload.data.get("episodes")
    if isinstance(detail, list):
        return list(detail)
    if not isinstance(detail, Mapping) or not detail.get("partitioned"):
        raise RuntimeError("the opportunities snapshot carries no readable detail")
    directory = state.generation_directory()
    if directory is None:
        raise RuntimeError("the performance directory does not exist")
    return list(_iter_partition_rows(directory, detail))


def _window_episodes(
    state: PerformanceState, payload: PerformancePayload
) -> Iterator[tuple[Episode, Any]]:
    """``(episode, book)`` for every row of the published window, streamed.

    One part at a time, converting each row as it arrives: the raw dicts never
    all exist, so the peak is the episode set - which is bounded by the window -
    rather than the window's JSON.
    """
    for row in _window_rows(state, payload):
        if row.get("first_edge") is None:
            continue
        yield episode_from_mapping(row), row.get("book")


def iter_published_episodes(state: PerformanceState, now: datetime) -> Iterable[dict[str, Any]]:
    """The published detail window as rows, for callers that need all of it."""
    payload, _freshness = state.snapshot("performance-opportunities", "opportunities.json", now)
    if payload.data == {} and payload.kind:
        return []
    return _window_rows(state, payload)

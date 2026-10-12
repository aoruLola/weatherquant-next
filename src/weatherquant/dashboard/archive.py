"""Bounded tail of the collector's raw JSONL archive -> a live market view.

Why this exists
---------------
The per-market view (bid / ask / spread / depth / freshness) is not published
by any snapshot today: the shadow engine holds it in a DuckDB file nobody else
may open, and the collector's own heartbeat is counters-only. The raw archive
*does* carry it - every REST book poll and every Gamma event is appended there
- so the dashboard reads it directly. Read-only, and strictly bounded:

* only the newest ``1 + archive_days_back`` file per source is opened;
* only the last ``archive_tail_bytes`` of each file are read (mid-file seeks
  drop the partial first line, so only complete JSONL lines are parsed);
* results are cached for ``archive_cache_seconds`` so a 5-second poll cannot
  turn into continuous parsing.

The parsing itself is *reused*, not re-implemented: ``weatherquant.shadow.
events`` already defines how an envelope, a Gamma event and a book frame are
read, and two readers agreeing on the format is the whole point of having the
format in one place.

Order book deltas (``price_change``) are skipped, exactly as the shadow skips
them: a single level of an unknown book is not a book.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.contracts import Freshness, FreshnessState
from weatherquant.dashboard.sources import age_seconds
from weatherquant.shadow.events import (
    BookObservation,
    CatalogMarket,
    EventKind,
    classify,
    parse_envelope,
    parse_gamma,
    parse_ws_frame,
    rest_book,
)

__all__ = ["ArchiveResult", "ArchiveTail"]


@dataclass
class ArchiveResult:
    catalog: dict[str, CatalogMarket] = field(default_factory=dict)
    market_of_token: dict[str, str] = field(default_factory=dict)
    books: dict[str, BookObservation] = field(default_factory=dict)
    freshness: Freshness = field(
        default_factory=lambda: Freshness(state=FreshnessState.MISSING, reason="archive not read")
    )
    lines_read: int = 0
    malformed_lines: int = 0
    books_unattached: int = 0
    reason: str | None = None

    @property
    def newest_received(self) -> Any:
        newest: Any = None
        for book in self.books.values():
            if newest is None or book.collector_received_at_utc > newest:
                newest = book.collector_received_at_utc
        return newest


class ArchiveTail:
    """Cached, bounded reader over ``raw/<source>/YYYY-MM-DD.jsonl``."""

    def __init__(self, settings: DashboardSettings) -> None:
        self.settings = settings
        self._cached: ArchiveResult | None = None
        self._cached_at = 0.0

    def read(self, now_any: Any) -> ArchiveResult:
        """``now_any``: datetime used for freshness (and to force distinct caches)."""
        ttl = self.settings.archive_cache_seconds
        if self._cached is not None and ttl > 0 and (time.monotonic() - self._cached_at) < ttl:
            return self._cached
        result = self._read_uncached(now_any)
        self._cached = result
        self._cached_at = time.monotonic()
        return result

    # -- internals ---------------------------------------------------------
    def _read_uncached(self, now: Any) -> ArchiveResult:
        result = ArchiveResult()
        raw_root = self.settings.collector_raw_dir
        gamma_files = self._files(raw_root, "gamma")
        book_files = self._files(raw_root, "rest") + self._files(raw_root, "ws")

        if not gamma_files and not book_files:
            result.freshness = Freshness(
                state=FreshnessState.MISSING,
                reason="collector raw archive not found (collector not running, or not mounted)",
            )
            result.reason = result.freshness.reason
            return result

        # Pass 1: market metadata, oldest file first so the newest record wins.
        newest: datetime | None = None
        for path in reversed(gamma_files):
            for line in _iter_lines(path, self.settings.archive_tail_bytes):
                result.lines_read += 1
                try:
                    envelope = parse_envelope(line)
                except ValueError:
                    result.malformed_lines += 1
                    continue
                if newest is None or envelope.received_at_utc > newest:
                    newest = envelope.received_at_utc
                if classify(envelope.source, envelope.payload) is not EventKind.MARKET_METADATA:
                    continue
                markets, _problems = parse_gamma(
                    envelope.payload, default_station=self.settings.station, run_id=envelope.run_id
                )
                for market in markets:
                    result.catalog[market.market_id] = market
                    result.market_of_token[market.token_yes] = market.market_id
                    result.market_of_token[market.token_no] = market.market_id

        # Pass 2: complete books only. Deltas and prints are ignored by classify.
        for path in book_files:
            for line in _iter_lines(path, self.settings.archive_tail_bytes):
                result.lines_read += 1
                try:
                    envelope = parse_envelope(line)
                except ValueError:
                    result.malformed_lines += 1
                    continue
                if newest is None or envelope.received_at_utc > newest:
                    newest = envelope.received_at_utc
                kind = classify(envelope.source, envelope.payload)
                if kind is not EventKind.BOOK_SNAPSHOT:
                    continue
                try:
                    observations = _book_observations(
                        envelope,
                        market_of_token=result.market_of_token,
                        now=now,
                    )
                except ValueError:
                    result.malformed_lines += 1
                    continue
                for observation in observations:
                    if not observation.market_id:
                        result.books_unattached += 1
                        continue
                    previous = result.books.get(observation.token_id)
                    if previous is None or observation.collector_received_at_utc > (
                        previous.collector_received_at_utc
                    ):
                        result.books[observation.token_id] = observation

        # Freshness of the *source*, measured from the newest complete line of
        # any kind: a quiet book market still shows a fresh archive.
        as_of = newest or self._newest_file_time(raw_root)
        age = age_seconds(as_of, now)
        limit = self.settings.book_stale_seconds
        if as_of is None or age is None:
            state = FreshnessState.MISSING
            reason = "no readable archive lines"
        elif age > limit:
            state = FreshnessState.STALE
            reason = f"newest archive line is {age:.0f}s old (limit {limit:.0f}s)"
        else:
            state = FreshnessState.FRESH
            reason = None
        result.freshness = Freshness(
            state=state,
            age_seconds=age,
            as_of_utc=as_of,
            stale_after_seconds=limit,
            reason=reason,
        )
        result.reason = reason
        if result.malformed_lines and not result.catalog and not result.books:
            result.freshness = Freshness(
                state=FreshnessState.CORRUPT,
                reason=f"all {result.malformed_lines} tail line(s) failed to parse",
            )
            result.reason = result.freshness.reason
        return result

    def _files(self, root: Path, source: str) -> list[Path]:
        directory = root / source
        try:
            candidates = sorted(directory.glob("*.jsonl"), reverse=True)
        except OSError:
            return []
        keep = 1 + max(0, self.settings.archive_days_back)
        return list(reversed(candidates[:keep]))

    def _newest_file_time(self, root: Path) -> datetime | None:
        newest: datetime | None = None
        for source in ("gamma", "rest", "ws"):
            for path in self._files(root, source):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
        return newest


def _iter_lines(path: Path, tail_bytes: int):
    """Yield complete JSONL lines from the file's tail.

    Only complete lines are yielded: when the read started mid-file the first
    line is a fragment and is dropped, and the (always partial) text after the
    final newline never reaches a parser.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - tail_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return
    lines = chunk.decode("utf-8", errors="replace").split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # first line is a fragment of an earlier record
    for line in lines:
        stripped = line.strip()
        if stripped:
            yield stripped


def _book_observations(envelope, *, market_of_token: dict[str, str], now) -> list[BookObservation]:
    if envelope.source == "rest":
        token_id = str(envelope.payload.get("asset_id") or "")
        if not token_id:
            raise ValueError("book payload has no asset_id")
        market_id = market_of_token.get(token_id, "")
        return [
            rest_book(
                envelope.payload,
                token_id=token_id,
                market_id=market_id,
                collector_received_at_utc=envelope.received_at_utc,
                shadow_received_at_utc=now,
                run_id=envelope.run_id,
            )
        ]
    observations, _notes = parse_ws_frame(
        envelope.payload,
        market_of_token=market_of_token,
        collector_received_at_utc=envelope.received_at_utc,
        shadow_received_at_utc=now,
        run_id=envelope.run_id,
    )
    return observations

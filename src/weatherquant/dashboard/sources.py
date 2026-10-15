"""Reading the published JSON sources: atomic, cached, freshness-first.

Every source the dashboard consumes is a small JSON file that its *producer*
renames into place, so a reader either sees a complete document or sees the
previous one. This module adds the three things a monitoring reader needs on
top of that:

* **A verdict, not a crash.** Missing, unparseable and stale each become a
  ``FreshnessState`` with a human reason; only the caller decides what to do.
* **An mtime cache.** A heartbeat rewritten in place would otherwise be
  re-parsed on every request; stat() is enough to skip that. Age, however, is
  recomputed on every request - freshness is a function of *now*, and a
  cached age would silently freeze it.
* **Logical names only.** Failures say ``collector-heartbeat``; they never
  echo a filesystem path into an API response.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.contracts import Freshness, FreshnessState
from weatherquant.performance.snapshot import resolve_generation

__all__ = ["JsonSource", "SnapshotSources", "age_seconds", "freshness_of", "parse_utc"]


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_seconds(as_of: datetime | None, now: datetime) -> float | None:
    if as_of is None:
        return None
    return round((now - as_of).total_seconds(), 3)


def freshness_of(
    as_of: datetime | None,
    now: datetime,
    *,
    stale_after_seconds: float,
    missing_reason: str,
    corrupt_reason: str | None = None,
    present: bool = True,
) -> Freshness:
    """Classify one source against ``now``.

    A timestamp that cannot be parsed is treated as *stale with unknown age*,
    never as fresh: unreadable is not evidence of recency.
    """
    if not present:
        return Freshness(state=FreshnessState.MISSING, reason=missing_reason)
    if corrupt_reason is not None:
        return Freshness(state=FreshnessState.CORRUPT, reason=corrupt_reason)
    age = age_seconds(as_of, now)
    if as_of is None or age is None:
        return Freshness(state=FreshnessState.STALE, reason="timestamp missing or unparseable")
    state = FreshnessState.STALE if age > stale_after_seconds else FreshnessState.FRESH
    return Freshness(
        state=state,
        age_seconds=age,
        as_of_utc=as_of,
        stale_after_seconds=stale_after_seconds,
        reason=None
        if state is FreshnessState.FRESH
        else f"{age:.0f}s old (limit {stale_after_seconds:.0f}s)",
    )


@dataclass
class JsonSource:
    """Result of reading one snapshot file."""

    logical_name: str
    payload: dict[str, Any] | None
    freshness: Freshness
    present: bool
    reason: str | None = None


@dataclass
class _CacheEntry:
    mtime_ns: int
    size: int
    payload: dict[str, Any] | None
    error: str | None
    as_of: datetime | None


@dataclass
class SnapshotSources:
    """All JSON snapshot reads for one configuration, with an mtime cache."""

    settings: DashboardSettings
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- public ------------------------------------------------------------
    def read(
        self,
        logical_name: str,
        path: Path,
        *,
        stale_after_seconds: float,
        timestamp_keys: tuple[str, ...] = ("updated_at_utc", "generated_at_utc"),
        missing_reason: str,
        now: datetime,
    ) -> JsonSource:
        entry = self._load(logical_name, path, timestamp_keys)
        if entry.error == "__missing__":
            return JsonSource(
                logical_name=logical_name,
                payload=None,
                freshness=freshness_of(
                    None,
                    now,
                    stale_after_seconds=stale_after_seconds,
                    missing_reason=missing_reason,
                    present=False,
                ),
                present=False,
                reason=missing_reason,
            )
        if entry.error is not None:
            return JsonSource(
                logical_name=logical_name,
                payload=None,
                freshness=freshness_of(
                    None,
                    now,
                    stale_after_seconds=stale_after_seconds,
                    missing_reason=missing_reason,
                    corrupt_reason=f"{logical_name} unreadable: {entry.error}",
                ),
                present=True,
                reason=entry.error,
            )
        freshness = freshness_of(
            entry.as_of,
            now,
            stale_after_seconds=stale_after_seconds,
            missing_reason=missing_reason,
        )
        return JsonSource(
            logical_name=logical_name,
            payload=entry.payload,
            freshness=freshness,
            present=True,
        )

    def collector_heartbeat(self, now: datetime) -> JsonSource:
        return self.read(
            "collector-heartbeat",
            self.settings.collector_health_path,
            stale_after_seconds=self.settings.heartbeat_stale_seconds,
            missing_reason="collector heartbeat not found (collector not running, or not mounted)",
            now=now,
        )

    def shadow_heartbeat(self, now: datetime) -> JsonSource:
        return self.read(
            "shadow-heartbeat",
            self.settings.shadow_health_path,
            stale_after_seconds=self.settings.heartbeat_stale_seconds,
            missing_reason="shadow heartbeat not found (shadow not running, or not mounted)",
            now=now,
        )

    def shadow_report(self, now: datetime) -> JsonSource:
        source = self.read(
            "shadow-report",
            self.settings.shadow_report_path,
            # The report carries its own window; the grace is added below so a
            # republish that is merely late is not called stale early.
            stale_after_seconds=300.0,
            missing_reason=(
                "shadow report snapshot not found (the engine publishes it every cycle "
                "while it runs)"
            ),
            now=now,
        )
        if source.payload is not None:
            return self._apply_report_window(source, now)
        return source

    def named_snapshot(self, logical_name: str, filename: str, now: datetime) -> JsonSource:
        """A producer-defined detail snapshot (see docs/dashboard-data-contract.md)."""
        return self.read(
            logical_name,
            self.settings.shadow_snapshots_dir / filename,
            stale_after_seconds=300.0,
            missing_reason=(
                f"{logical_name} not published yet: the shadow-side producer awaits "
                "integration (docs/dashboard-data-contract.md §4)"
            ),
            now=now,
        )

    def performance_snapshot(self, logical_name: str, filename: str, now: datetime) -> JsonSource:
        """A research-analytics snapshot (docs/performance-data-contract.md §7).

        Distinct from ``named_snapshot`` because it reads a *different*
        directory with a *different* missing reason: a performance file that
        is absent means the backfill has not run, not that a producer awaits
        integration. Saying "awaiting integration" about a research job that
        simply has not happened yet would send an operator looking for a
        deployment that is not the problem.

        Reads follow the generation pointer, so all six files come from one
        published batch. That is the whole reason they are published that way
        - six independent renames into one directory would let a reader pair
        an old opportunities file with a new win-rate file and score a
        mixture that never existed.
        """
        root = self.settings.performance_snapshots_dir
        missing_reason = (
            f"{logical_name} not published yet: the research analytics "
            "backfill has not produced this snapshot "
            "(docs/performance-data-contract.md §7)"
        )

        generation = resolve_generation(root)
        if generation.directory is None and generation.problem is None:
            # Nothing has ever been published here, or this deployment still
            # uses the flat layout the API always accepted.
            return self.read(
                logical_name,
                root / filename,
                stale_after_seconds=300.0,
                missing_reason=missing_reason,
                now=now,
            )
        if generation.directory is None:
            # A pointer that cannot be honoured is a broken publish, not an
            # absent one. Saying "missing" would send someone to re-run a
            # backfill that already succeeded.
            return self.read(
                logical_name,
                root / filename,
                stale_after_seconds=300.0,
                missing_reason=f"{logical_name}: {generation.problem}",
                now=now,
            )

        generation_dir = generation.directory
        manifest = generation.manifest or {}
        source = self.read(
            logical_name,
            generation_dir / filename,
            stale_after_seconds=300.0,
            missing_reason=missing_reason,
            now=now,
        )
        claimed = (source.payload or {}).get("generation_id")
        published = manifest.get("generation_id")
        if source.payload is not None and claimed and published and claimed != published:
            # The file is from another batch than the one the pointer names.
            # Refuse it rather than serve a mixture of two generations.
            reason = (
                f"{logical_name} belongs to generation {claimed} but the "
                f"pointer selects {published}; refusing to mix generations"
            )
            return JsonSource(
                logical_name=logical_name,
                payload=None,
                present=False,
                freshness=freshness_of(
                    None, now, stale_after_seconds=300.0, missing_reason=reason
                ),
                reason=reason,
            )
        return source

    # -- internals ---------------------------------------------------------
    def _apply_report_window(self, source: JsonSource, now: datetime) -> JsonSource:
        payload = source.payload or {}
        window = payload.get("stale_after_seconds")
        try:
            stale_after = float(window) + self.settings.report_stale_grace_seconds
        except (TypeError, ValueError):
            stale_after = 300.0 + self.settings.report_stale_grace_seconds
        as_of = parse_utc(payload.get("generated_at_utc"))
        source.freshness = freshness_of(
            as_of,
            now,
            stale_after_seconds=stale_after,
            missing_reason=source.freshness.reason or "",
        )
        return source

    def _load(self, logical_name: str, path: Path, timestamp_keys: tuple[str, ...]) -> _CacheEntry:
        try:
            stat = path.stat()
        except OSError:
            entry = _CacheEntry(mtime_ns=-1, size=-1, payload=None, error="__missing__", as_of=None)
            self._store(logical_name, entry)
            return entry

        with self._lock:
            cached = self._cache.get(logical_name)
            if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
                return cached

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            entry = _CacheEntry(stat.st_mtime_ns, stat.st_size, None, f"read error: {exc}", None)
            self._store(logical_name, entry)
            return entry
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            entry = _CacheEntry(stat.st_mtime_ns, stat.st_size, None, f"invalid JSON: {exc}", None)
            self._store(logical_name, entry)
            return entry
        if not isinstance(payload, dict):
            entry = _CacheEntry(
                stat.st_mtime_ns, stat.st_size, None, f"expected an object, got {type(payload).__name__}", None
            )
            self._store(logical_name, entry)
            return entry

        as_of = next(
            (parsed for key in timestamp_keys if (parsed := parse_utc(payload.get(key))) is not None),
            None,
        )
        entry = _CacheEntry(stat.st_mtime_ns, stat.st_size, payload, None, as_of)
        self._store(logical_name, entry)
        return entry

    def _store(self, logical_name: str, entry: _CacheEntry) -> None:
        with self._lock:
            self._cache[logical_name] = entry

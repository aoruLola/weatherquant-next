"""Collector heartbeat and health evaluation.

Design constraint from the deployment spec: **the health check must not touch
DuckDB.** The writer holds the database lock for its whole life, so a health
probe that opened the database would either block or fight the collector it is
meant to watch. Instead the worker publishes a small JSON file and the Docker
``HEALTHCHECK`` only reads that file.

Two clocks, two questions
-------------------------
``updated_at_utc`` answers *"is the process alive and looping?"* - it is
rewritten every tick even when there is nothing to do.

``last_upstream_contact_utc`` answers *"is collection actually working?"* - it
advances on **any** successful upstream interaction, including a REST poll that
returns an unchanged book, and on **any** WebSocket frame.

That split is what stops the checker from crying wolf. On a quiet night a
weather market's book does not move for hours; a checker keyed to *data
changing* would declare an outage. A checker keyed to *we successfully talked
to the exchange* correctly stays green. Real stalls - DNS failure, upstream 5xx
storm, dead WebSocket with no REST fallback - stop advancing
``last_upstream_contact_utc`` and are caught regardless of market activity.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "CollectionStats",
    "HealthStatus",
    "HealthVerdict",
    "Heartbeat",
    "HeartbeatWriter",
    "evaluate_health",
    "read_heartbeat",
]


# Upstream stall thresholds. Defined here so the worker and the probe read
# the same numbers - two copies would drift, and the file would then claim
# `healthy` while the probe said `degraded`.
UPSTREAM_STALL_SOFT_SECONDS = 900.0
UPSTREAM_STALL_HARD_SECONDS = 1800.0
DISK_WARN_PERCENT = 15.0
DISK_CRITICAL_PERCENT = 5.0


class HealthStatus(StrEnum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"
    FATAL = "fatal"


@dataclass
class CollectionStats:
    """Counters the worker maintains across flushes."""

    books_rows_inserted: int = 0
    books_rows_deduplicated: int = 0
    price_points_inserted: int = 0
    price_points_deduplicated: int = 0
    markets_upserted: int = 0
    events_upserted: int = 0
    ws_messages_received: int = 0
    ws_reconnects: int = 0
    ws_subscribe_failures: int = 0
    rest_requests_ok: int = 0
    rest_requests_failed: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    integrity_failures: int = 0
    one_sided_books: int = 0
    ws_rest_mismatches: int = 0
    archive_records: int = 0
    dedupe_hits_total: int = 0
    errors_total: int = 0


@dataclass
class Heartbeat:
    """The full health payload. Serialised to ``health/heartbeat.json``."""

    status: HealthStatus
    run_id: str
    pid: int
    hostname: str
    app_version: str
    updated_at_utc: datetime
    started_at_utc: datetime
    uptime_seconds: float

    # Collection liveness - see module docstring for why these differ.
    last_upstream_contact_utc: datetime | None = None
    last_data_change_utc: datetime | None = None

    ws_connected: bool = False
    ws_last_message_utc: datetime | None = None
    ws_endpoint: str = ""
    subscribed_tokens: int = 0

    rest_last_success_utc: datetime | None = None
    rest_consecutive_failures: int = 0

    stats: CollectionStats = field(default_factory=CollectionStats)

    db_bytes: int = 0
    raw_bytes: int = 0
    disk_free_bytes: int = 0
    disk_total_bytes: int = 0
    disk_free_percent: float = 100.0

    last_defect: str | None = None
    last_error_utc: datetime | None = None
    last_error: str | None = None
    fatal_error: str | None = None

    # Rolling operational telemetry. Kept as one nested mapping rather than
    # promoted to top-level fields because its members are heterogeneous - a
    # latency percentiles dict, a queue depth, a drop counter - and because
    # adding to it must not require touching this schema every time. Nested
    # values are encoded by `to_dict` like any other dict.
    #
    # Empty when the engine has not yet measured anything, which is a
    # different statement from "zero" and must not be conflated with it.
    telemetry: dict[str, Any] = field(default_factory=dict)
    git_sha: str | None = None
    image_tag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def enc(value: Any) -> Any:
            if isinstance(value, datetime):
                return value.astimezone(UTC).isoformat()
            if isinstance(value, HealthStatus):
                return str(value)
            if isinstance(value, CollectionStats):
                return asdict(value)
            if isinstance(value, dict):
                return {k: enc(v) for k, v in value.items()}
            if isinstance(value, list):
                return [enc(v) for v in value]
            return value

        return enc(asdict(self))  # type: ignore[arg-type]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


def read_heartbeat(path: str | Path) -> Heartbeat | None:
    """Read a heartbeat file. Returns ``None`` if absent or unparseable.

    A corrupt heartbeat is treated as *no heartbeat*, never as healthy: the
    writer renames atomically, so corruption means something is badly wrong.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def ts(key: str) -> datetime | None:
        value = data.get(key)
        if not value:
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    try:
        return Heartbeat(
            status=HealthStatus(data["status"]),
            run_id=str(data["run_id"]),
            pid=int(data["pid"]),
            hostname=str(data.get("hostname", "")),
            app_version=str(data.get("app_version", "")),
            git_sha=data.get("git_sha"),
            image_tag=data.get("image_tag"),
            updated_at_utc=ts("updated_at_utc") or datetime.min.replace(tzinfo=UTC),
            started_at_utc=ts("started_at_utc") or datetime.min.replace(tzinfo=UTC),
            uptime_seconds=float(data.get("uptime_seconds", 0.0)),
            last_upstream_contact_utc=ts("last_upstream_contact_utc"),
            last_data_change_utc=ts("last_data_change_utc"),
            ws_connected=bool(data.get("ws_connected", False)),
            ws_last_message_utc=ts("ws_last_message_utc"),
            ws_endpoint=str(data.get("ws_endpoint", "")),
            subscribed_tokens=int(data.get("subscribed_tokens", 0)),
            rest_last_success_utc=ts("rest_last_success_utc"),
            rest_consecutive_failures=int(data.get("rest_consecutive_failures", 0)),
            stats=CollectionStats(**data.get("stats", {})),
            db_bytes=int(data.get("db_bytes", 0)),
            raw_bytes=int(data.get("raw_bytes", 0)),
            disk_free_bytes=int(data.get("disk_free_bytes", 0)),
            disk_total_bytes=int(data.get("disk_total_bytes", 0)),
            disk_free_percent=float(data.get("disk_free_percent", 100.0)),
            last_defect=data.get("last_defect"),
            last_error_utc=ts("last_error_utc"),
            last_error=data.get("last_error"),
            fatal_error=data.get("fatal_error"),
        )
    except (KeyError, TypeError, ValueError):
        return None


class HeartbeatWriter:
    """Publishes the heartbeat atomically.

    Atomic rename matters: the health probe reads concurrently, and a
    half-written JSON would be indistinguishable from a dead process.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, heartbeat: Heartbeat, *, attempts: int = 20) -> None:
        payload = heartbeat.to_json()
        last_error: OSError | None = None
        for attempt in range(attempts):
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".heartbeat-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
                return
            except PermissionError as exc:
                # A reader had the destination open at the moment of the rename.
                # True on Windows and on some network filesystems; harmless to
                # retry because the next tick publishes a fresher heartbeat
                # anyway. Losing every write here would look like a dead process.
                last_error = exc
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                time.sleep(min(0.02 * (attempt + 1), 0.25))
            except BaseException:
                # The temp file is ours alone; leaving it behind would
                # accumulate hidden files in the health directory.
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        raise last_error if last_error else RuntimeError("heartbeat write failed")


# ---------------------------------------------------------------------------
# Health evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HealthVerdict:
    status: HealthStatus
    """Exit code the container health probe should return."""
    exit_code: int
    reasons: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.exit_code == 0


def evaluate_health(
    path: str | Path,
    *,
    now: datetime | None = None,
    heartbeat_stale_seconds: float = 120.0,
    upstream_stall_soft_seconds: float = 900.0,
    upstream_stall_hard_seconds: float = 1800.0,
    disk_free_warn_percent: float = 15.0,
    disk_free_critical_percent: float = 5.0,
    rest_failure_degraded: int = 10,
) -> HealthVerdict:
    """Turn a heartbeat file into a pass/fail verdict.

    Evaluated as a strict priority ladder - each step can only downgrade the
    verdict, never upgrade it back:

    1. no heartbeat, fatal state, or stale heartbeat  -> UNHEALTHY (exit 1)
    2. clean stop / starting up                       -> OK        (exit 0)
    3. any critical reason (upstream silent past the hard limit,
       disk below the critical floor)                 -> UNHEALTHY (exit 1)
    4. any warning reason (upstream soft limit, disk warn,
       repeated REST failures)                        -> DEGRADED  (exit 0)
    5. otherwise                                      -> HEALTHY   (exit 0)

    Thresholds are deliberately generous on the data side. The failure modes we
    must catch are *process death* and *total upstream loss*; a weather market
    that trades twice an hour is normal and must never trip the probe. That is
    why the upstream stall limits start at 15/30 minutes rather than seconds,
    and why a degraded verdict still exits 0 - restarting the container would
    only widen the gap it is complaining about.
    """
    now = now or datetime.now(tz=UTC)

    heartbeat = read_heartbeat(path)
    if heartbeat is None:
        return HealthVerdict(
            HealthStatus.UNHEALTHY,
            1,
            ("heartbeat file missing or unparseable",),
        )

    age = (now - heartbeat.updated_at_utc).total_seconds()

    if heartbeat.status is HealthStatus.STOPPED:
        # A clean stop is expected during a rolling update; the container will
        # be restarted. Failing here would flap the probe mid-handover.
        return HealthVerdict(HealthStatus.STOPPED, 0, ("clean shutdown in progress",))

    if heartbeat.status is HealthStatus.FATAL or heartbeat.fatal_error:
        detail = heartbeat.fatal_error or "worker reported fatal status"
        return HealthVerdict(HealthStatus.FATAL, 1, (f"fatal: {detail}",))

    if age > heartbeat_stale_seconds:
        # The process started but stopped ticking - hung, deadlocked, or killed
        # without removing the file.
        return HealthVerdict(
            HealthStatus.UNHEALTHY,
            1,
            (f"heartbeat stale for {age:.0f}s (limit {heartbeat_stale_seconds:.0f}s)",),
        )

    if heartbeat.status is HealthStatus.STARTING:
        return HealthVerdict(HealthStatus.STARTING, 0, ("worker starting up",))

    critical: list[str] = []
    warning: list[str] = []

    # --- upstream contact -------------------------------------------------
    contact = heartbeat.last_upstream_contact_utc
    contact_age = (now - contact).total_seconds() if contact else float("inf")
    if not contact:
        # Nothing has ever succeeded. Give the worker the full soft window
        # before calling it broken - DNS and TLS can be slow on a cold start.
        if age > upstream_stall_soft_seconds:
            critical.append(f"no successful upstream contact after {age:.0f}s")
        else:
            warning.append("no successful upstream contact yet")
    elif contact_age > upstream_stall_hard_seconds:
        critical.append(
            f"upstream silent for {contact_age:.0f}s (hard limit {upstream_stall_hard_seconds:.0f}s)"
        )
    elif contact_age > upstream_stall_soft_seconds:
        warning.append(
            f"upstream silent for {contact_age:.0f}s (soft limit {upstream_stall_soft_seconds:.0f}s)"
        )

    # --- disk -------------------------------------------------------------
    if heartbeat.disk_total_bytes > 0:
        if heartbeat.disk_free_percent < disk_free_critical_percent:
            critical.append(
                f"disk critically low: {heartbeat.disk_free_percent:.1f}% free "
                f"({heartbeat.disk_free_bytes} bytes)"
            )
        elif heartbeat.disk_free_percent < disk_free_warn_percent:
            warning.append(
                f"disk low: {heartbeat.disk_free_percent:.1f}% free "
                f"({heartbeat.disk_free_bytes} bytes)"
            )

    # --- upstream error rate ---------------------------------------------
    if heartbeat.rest_consecutive_failures >= rest_failure_degraded:
        # REST failing while the WebSocket still talks is recoverable; if both
        # are dead the upstream-contact clock above will escalate this anyway.
        warning.append(f"{heartbeat.rest_consecutive_failures} consecutive REST failures")

    if critical:
        return HealthVerdict(HealthStatus.UNHEALTHY, 1, tuple(critical + warning))
    if warning:
        return HealthVerdict(HealthStatus.DEGRADED, 0, tuple(warning))
    return HealthVerdict(HealthStatus.HEALTHY, 0, ("ok",))

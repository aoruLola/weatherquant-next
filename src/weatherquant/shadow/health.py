"""Health file: what the container probe reads without touching DuckDB.

Deliberately independent of the database, in the same way the collector's is:
the probe runs in a separate process, and if reading health required opening
the shadow's DuckDB it would contend for the single-writer lease with the very
process it is meant to be checking. So the engine rewrites one small JSON file
each cycle and the probe only reads that.

Two clocks, as everywhere else:

``updated_at_utc``
    the process is alive. Missing or stale -> ``unhealthy``, because nothing
    is writing the file and therefore nothing is deciding.

``last_decision_at_utc``
    the engine is actually reaching decisions. Stale -> ``degraded``, not
    unhealthy: a market with no events, or a collector outage, produces no
    decisions while the engine is perfectly healthy, and restarting it would
    widen the gap rather than fix it. That is why degradation exits 0.

A ``fatal_error`` is unhealthy regardless of freshness.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["HealthVerdict", "evaluate_shadow_health", "write_heartbeat"]

STALE_SECONDS = 120.0
DECISION_SILENCE_SECONDS = 1800.0


@dataclass(frozen=True, slots=True)
class HealthVerdict:
    status: str  # healthy | degraded | starting | unhealthy
    reasons: tuple[str, ...]
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def write_heartbeat(path: str | Path, payload: dict[str, Any]) -> None:
    """Write atomically: a reader never sees a half-written file.

    Then widened to 0644. ``mkstemp`` creates the temporary file 0600 and
    ``os.replace`` carries that mode onto the target, so without this the
    heartbeat is readable only by the engine's own uid - and the whole point
    of the file is that something *else* can read it: the container probe from
    inside, and an operator or CI job from the host. Nothing in the payload is
    secret (run id, pid, counters, disk headroom), so a private health file
    would be privacy without a beneficiary.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, sort_keys=True, default=str)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
        os.replace(tmp_name, target)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o644)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def evaluate_shadow_health(
    path: str | Path,
    *,
    stale_seconds: float = STALE_SECONDS,
    decision_silence_seconds: float = DECISION_SILENCE_SECONDS,
    disk_warn_percent: float = 15.0,
    disk_critical_percent: float = 5.0,
    now: datetime | None = None,
) -> HealthVerdict:
    """Read one heartbeat and return a verdict plus the exit code Docker uses."""
    target = Path(path)
    if not target.exists():
        return HealthVerdict("starting", ("heartbeat not written yet",), 0)

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return HealthVerdict("unhealthy", (f"heartbeat unreadable: {exc}",), 1)

    now = now or datetime.now(tz=UTC)
    reasons: list[str] = []
    fatal = payload.get("fatal_error")
    if fatal:
        reasons.append(f"fatal: {fatal}")

    updated = _parse(payload.get("updated_at_utc"))
    if updated is None:
        reasons.append("heartbeat has no updated_at_utc")
    else:
        age = (now - updated).total_seconds()
        if age > stale_seconds:
            reasons.append(f"heartbeat is {age:.0f}s old (limit {stale_seconds:.0f}s)")

    status_field = str(payload.get("status") or "")
    if status_field == "stopping":
        return HealthVerdict("degraded", ("shutting down",), 0)

    disk = payload.get("disk") or {}
    free = disk.get("free_percent")
    if isinstance(free, (int, float)):
        if free < disk_critical_percent:
            reasons.append(f"disk at {free:.1f}% free (critical {disk_critical_percent}%)")
        elif free < disk_warn_percent:
            reasons.append(f"disk at {free:.1f}% free (warn {disk_warn_percent}%)")

    last_decision = _parse(payload.get("last_decision_at_utc"))
    decision_stale = (
        last_decision is None
        or (now - last_decision).total_seconds() > decision_silence_seconds
    )

    if reasons:
        # A disk or staleness problem is unhealthy; the "no decisions lately"
        # condition is handled separately below because it is often benign.
        unhealthy = [r for r in reasons if not r.startswith("disk at")]
        if not unhealthy:
            return HealthVerdict("degraded", tuple(reasons), 0)
        return HealthVerdict("unhealthy", tuple(reasons), 1)

    if decision_stale:
        return HealthVerdict(
            "degraded",
            (f"no decision in {decision_silence_seconds:.0f}s",),
            0,
        )
    return HealthVerdict("healthy", (), 0)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def disk_free_percent(path: str | Path) -> float | None:
    """Free space as a percentage, or ``None`` when it cannot be determined.

    ``None`` is never treated as a failure: a filesystem that will not answer
    is not evidence of a full one.
    """
    try:
        usage = os.statvfs(path) if hasattr(os, "statvfs") else None
        if usage is None:
            import shutil

            total = shutil.disk_usage(path)
            return 100.0 * total.free / total.total if total.total else None
        if usage.f_blocks <= 0:
            return None
        return 100.0 * usage.f_bavail / usage.f_blocks
    except OSError:
        return None

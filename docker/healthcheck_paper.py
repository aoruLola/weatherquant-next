#!/usr/bin/env python3
"""Read Paper's heartbeat without opening its exclusively held DuckDB.

The native health file is ``$WQ_PAPER_HEALTH_DIR/heartbeat.json``. The current
serve loop refreshes only its single-instance lease, so until it publishes the
native file the probe falls back to that lease's ``heartbeat_at_utc``. A native
file always wins once present; a stale native file is never hidden by a fresh
lease.

Exit codes: 0 for fresh or still starting, 1 for unreadable or stale health.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HEALTH_DIR = Path(os.environ.get("WQ_PAPER_HEALTH_DIR", "/data/health"))
HEARTBEAT_PATH = HEALTH_DIR / "heartbeat.json"
DB_PATH = Path(os.environ.get("WQ_PAPER_DB_PATH", "/data/paper/paper.duckdb"))
LOCK_PATH = Path(os.environ.get("WQ_PAPER_LOCK_PATH", str(DB_PATH.parent / ".paper.lock")))
STALE_SECONDS = float(os.environ.get("WQ_PAPER_HEALTH_STALE_SECONDS", "120"))


def evaluate_heartbeat(path: Path, *, now: datetime | None = None) -> tuple[str, int]:
    """Return a human-readable verdict and Docker's exit code for one file."""
    if not path.exists():
        return "starting: heartbeat not written yet", 0
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"unhealthy: heartbeat unreadable: {exc}", 1

    if payload.get("fatal_error"):
        return f"unhealthy: fatal: {payload['fatal_error']}", 1
    raw_updated = payload.get("updated_at_utc") or payload.get("heartbeat_at_utc")
    try:
        updated = datetime.fromisoformat(str(raw_updated))
    except (TypeError, ValueError):
        return "unhealthy: heartbeat has no valid updated_at_utc or heartbeat_at_utc", 1
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    age = ((now or datetime.now(tz=UTC)) - updated).total_seconds()
    if age > STALE_SECONDS:
        return f"unhealthy: heartbeat is {age:.0f}s old (limit {STALE_SECONDS:.0f}s)", 1
    return f"healthy: heartbeat is {max(age, 0):.0f}s old ({path})", 0


def main() -> int:
    # Prefer the deployment contract's health dir. The lease fallback is the
    # heartbeat the current serve implementation actually writes each tick.
    path = HEARTBEAT_PATH if HEARTBEAT_PATH.exists() else LOCK_PATH
    message, exit_code = evaluate_heartbeat(path)
    print(message)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

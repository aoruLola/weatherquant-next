#!/usr/bin/env python3
"""Container probe for the shadow engine.

Same reasoning as the collector's probe: this runs in a *separate process*, so
reading health must not require opening the shadow's DuckDB - that file is
held by the very process this is meant to be checking, and a probe that
contended for the single-writer lease would fail for a reason unrelated to
health. It reads one JSON file the engine rewrites each cycle.

Exit codes
----------
0  healthy, degraded, or still starting
1  unhealthy

Degraded exits 0 on purpose. "No decisions for a while" and "the collector has
been quiet" are often the *same* fact reported twice, and restarting the
engine to make the symptom disappear would widen the gap it is reporting.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Defaults match the collector's probe so a host's expectation transfers.
DISK_WARN_PERCENT = float(os.environ.get("WQ_DISK_WARN_PERCENT", "15"))
DISK_CRITICAL_PERCENT = float(os.environ.get("WQ_DISK_CRITICAL_PERCENT", "5"))

HEALTH_DIR = Path(os.environ.get("WQ_SHADOW_HEALTH_DIR", "/data/health"))
HEARTBEAT_PATH = HEALTH_DIR / "heartbeat.json"


def main() -> int:
    # Imported lazily and by path-independent name: the probe must not import
    # anything that opens a connection on import.
    from weatherquant.shadow.health import evaluate_shadow_health

    verdict = evaluate_shadow_health(
        HEARTBEAT_PATH,
        disk_warn_percent=DISK_WARN_PERCENT,
        disk_critical_percent=DISK_CRITICAL_PERCENT,
    )
    message = verdict.status
    if verdict.reasons:
        message += ": " + "; ".join(verdict.reasons)
    print(message)
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())

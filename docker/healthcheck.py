#!/usr/bin/env python3
"""Container health probe.

Deliberately independent of the collector process and of DuckDB:

* it only *reads* a JSON file the worker rewrites every tick, so it never
  contends for the database lock the writer holds for its whole life;
* it imports nothing that opens a connection.

Exit codes follow the probe verdict: 0 healthy / degraded / starting / stopping,
1 unhealthy. A degraded probe returns 0 on purpose - restarting the collector
would only widen the gap it is complaining about, and Docker's only lever is
restart-on-nonzero.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HEARTBEAT_PATH = Path(os.environ.get("WQ_HEARTBEAT_PATH", "/data/health/heartbeat.json"))

# Disk thresholds are deployment configuration, not constants: "15% free" is
# 150 GB on a 1 TB NAS but only ~10 GB on a CI runner, and hosted runners ship
# nearly full by default. A workflow that overrides these is declaring its own
# environment's expectations - it is not weakening what the collector reports -
# so the defaults stay conservative for the deployment that actually matters.
DISK_WARN_PERCENT = float(os.environ.get("WQ_DISK_WARN_PERCENT", "15"))
DISK_CRITICAL_PERCENT = float(os.environ.get("WQ_DISK_CRITICAL_PERCENT", "5"))


def main() -> int:
    # Import lazily so a broken package still produces a readable message.
    try:
        from weatherquant.data.heartbeat import evaluate_health
    except Exception as exc:
        print(f"healthcheck: cannot import probe logic: {exc}", file=sys.stderr)
        return 1

    try:
        verdict = evaluate_health(
            HEARTBEAT_PATH,
            disk_free_warn_percent=DISK_WARN_PERCENT,
            disk_free_critical_percent=DISK_CRITICAL_PERCENT,
        )
    except Exception as exc:
        print(f"healthcheck: probe failed: {exc}", file=sys.stderr)
        return 1

    line = f"{verdict.status.value}: " + "; ".join(verdict.reasons)
    print(line)
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

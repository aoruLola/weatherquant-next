#!/usr/bin/env python3
"""Container health probe for the dashboard.

Reads nothing but this process's own HTTP endpoint - deliberately. The
dashboard's job is to *serve*; if it can serve /healthz, Docker's lever
(restart on non-zero) is aimed at the right process.

Source freshness is NOT part of this verdict: restarting the dashboard because
the collector died would fix nothing and lose the only component that can
explain the outage. The per-source states travel inside the response body for
humans and for the UI, not for the restart loop.

Exit codes: 0 healthy, 1 cannot be reached.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HEALTH_URL = os.environ.get("WQ_HEALTHCHECK_URL", "http://127.0.0.1:8787/healthz")
TIMEOUT_SECONDS = float(os.environ.get("WQ_HEALTHCHECK_TIMEOUT", "8"))


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                print(f"unhealthy: HTTP {response.status}", file=sys.stderr)
                return 1
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1

    sources = payload.get("data", {}).get("sources", {})
    freshness = payload.get("freshness", {}).get("state", "unknown")
    # Exit 0 regardless of source states (see module docstring); print them so
    # `docker inspect` output still says what the dashboard is looking at.
    print(f"ok: serving; sources={sources}; freshness={freshness}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

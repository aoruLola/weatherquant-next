"""``python -m weatherquant.dashboard`` - same entry point as the console script."""

from __future__ import annotations

from weatherquant.dashboard.api import main

if __name__ == "__main__":
    raise SystemExit(main())

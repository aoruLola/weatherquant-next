#!/usr/bin/env python3
"""Refresh the fixture dataset used for development, screenshots and CI.

    python dashboard/fixtures/generate.py [--root dashboard/fixtures/data]

What it writes (timestamps relative to *now*, so the UI looks alive):

    collector/health/heartbeat.json     a healthy collector, 5s old
    collector/raw/{gamma,rest}/<date>   one event + two live books
    shadow/health/heartbeat.json        LIVE mode, last decision 20s ago
    shadow/reports/report.json          decision aggregates, coverage, ...
    shadow/reports/*.json               the three producer detail snapshots

Honesty by construction: the server is started with ``WQ_DATASET=fixture``,
which stamps every envelope and puts a permanent banner in the UI. Fixture data
must never be able to pass itself off as production data.

The builders live in ``dashboard/tests/helpers.py`` - one shape, two
consumers (tests and this generator), so they cannot drift apart.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))

from helpers import build_tree  # noqa: E402  (path setup must come first)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=HERE / "data",
        help="output directory (default: dashboard/fixtures/data)",
    )
    args = parser.parse_args(argv)

    root: Path = args.root
    build_tree(root, producer_snapshots=True)
    written = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    print(f"fixture dataset written to {root}")
    for rel in written:
        print(f"  {rel}")
    print("\nserve it with:")
    print("  WQ_DATASET=fixture \\")
    print(f"  WQ_COLLECTOR_DATA_DIR={root / 'collector'} \\")
    print(f"  WQ_SHADOW_DATA_DIR={root / 'shadow'} \\")
    print("  WQ_STATIC_DIR=dashboard/frontend/dist \\")
    print("  weatherquant-dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

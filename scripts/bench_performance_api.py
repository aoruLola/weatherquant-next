#!/usr/bin/env python
"""Measure the dashboard's Performance endpoints against a published snapshot.

The number that matters is not throughput - this is a read-only page for one
operator - but whether a request's cost depends on the size of the archive. So
this reports, per endpoint:

* the response size (a page must be a page);
* the latency distribution, including p95;
* the peak allocation of one request, which is where the old reader died: it
  parsed the whole detail into a dict before serving fifty rows.

It runs against a snapshot the streaming engine already published, in an
isolated directory, and never touches the production NAS.

Usage::

    uv run python scripts/bench_performance_api.py --generation /path/to/performance
    uv run python scripts/bench_performance_api.py --rows 1500000 --work-dir /home/coral/bench
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

ENDPOINTS = (
    ("summary", "/api/v1/performance/summary"),
    ("edge", "/api/v1/performance/edge"),
    ("win-rate", "/api/v1/performance/win-rate"),
    ("opportunities page 1", "/api/v1/performance/opportunities?limit=50"),
    ("opportunities page 2", "/api/v1/performance/opportunities?limit=50&cursor=50"),
    ("opportunities filtered", "/api/v1/performance/opportunities?limit=50&model=calibrated"),
    ("opportunities by outcome", "/api/v1/performance/opportunities?limit=50&outcome=WIN"),
    ("opportunities max page", "/api/v1/performance/opportunities?limit=200"),
    ("settlements", "/api/v1/performance/settlements?limit=50"),
    ("models", "/api/v1/performance/models"),
    ("reference capital", "/api/v1/performance/reference-capital?capital_usdc=250"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generation", help="a published performance directory")
    parser.add_argument("--rows", type=int, help="publish this many synthetic rows first")
    parser.add_argument("--work-dir", default="/home/coral/bench")
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--report", help="write the JSON report here")
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from weatherquant.dashboard.api import create_app
    from weatherquant.dashboard.config import DashboardSettings

    if args.rows:
        # Reuse the streaming benchmark's generator and ingest, so the fixture
        # is the same shape the resource tests use.
        sys.path.insert(0, str(REPO / "scripts"))
        import bench_performance_streaming as bench

        work = Path(args.work_dir) / f"rows-{args.rows}"
        export = work / "export"
        if not (export / "parts").exists():
            bench.generate_export(export, rows=args.rows, markets=24, cycle_seconds=60, seed=20261015)
        from weatherquant.performance.streaming import build_performance_streaming

        build_performance_streaming(
            export=export,
            out_dir=work / "performance",
            state_db=work / "state.duckdb",
            dataset="fixture",
            settlements=bench.synthetic_settlements(),
            memory_limit="1GB",
        )
        generation = work / "performance"
    else:
        generation = Path(args.generation or (Path(args.work_dir) / "rows-1500000" / "performance"))
    if not generation.exists():
        print(f"no published snapshot at {generation}", file=sys.stderr)
        return 2

    settings = DashboardSettings(
        collector_data_dir=generation.parent / "collector",
        shadow_data_dir=generation.parent / "shadow",
        performance_data_dir=generation,
        rate_limit_per_minute=0,
    )
    app = create_app(settings)
    report: list[dict] = []
    with TestClient(app) as client:
        for name, path in ENDPOINTS:
            # One warm request, so the numbers describe steady state.
            first = client.get(path)
            if first.status_code != 200:
                report.append({"endpoint": name, "path": path, "status": first.status_code})
                continue
            samples: list[float] = []
            for _ in range(args.repeats):
                started = time.perf_counter()
                response = client.get(path)
                samples.append((time.perf_counter() - started) * 1000)
            tracemalloc.start()
            measured = client.get(path)
            _current, high = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            samples.sort()
            report.append(
                {
                    "endpoint": name,
                    "path": path,
                    "status": 200,
                    "bytes": len(measured.content),
                    "items": len(response.json().get("data", {}).get("items", []) or []),
                    "median_ms": round(statistics.median(samples), 1),
                    "p95_ms": round(samples[math.ceil(len(samples) * 0.95) - 1], 1),
                    "max_ms": round(samples[-1], 1),
                    "request_peak_mb": round(high / 1e6, 2),
                }
            )

    width = max(len(row["endpoint"]) for row in report)
    print(f"{'endpoint':{width}}  {'bytes':>10}  {'median':>8}  {'p95':>8}  {'peak':>8}")
    for row in report:
        if row.get("status") != 200:
            print(f"{row['endpoint']:{width}}  status {row['status']}")
            continue
        print(
            f"{row['endpoint']:{width}}  {row['bytes']:>10,}  {row['median_ms']:>7.1f}ms  "
            f"{row['p95_ms']:>7.1f}ms  {row['request_peak_mb']:>7.2f}MB"
        )
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

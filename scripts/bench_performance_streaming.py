#!/usr/bin/env python
"""Stress the streaming performance engine at scale, in isolation.

This is the evidence for the acceptance claim: 1.5M / 3M / 5M synthetic
decisions must update the performance snapshots on a 16 GB host without the
peak memory growing with the history, and without the host becoming
unresponsive.

It deliberately does *not* run the in-memory builder at those sizes - that
builder is what took the host down at 960,000 rows. It is run only where the
two engines are compared for correctness, in `tests/test_performance_streaming.py`.

Usage::

    uv run python scripts/bench_performance_streaming.py --rows 1500000
    uv run python scripts/bench_performance_streaming.py --all --work-dir /home/coral/bench

Each scale runs in its own process so that one run's peak RSS cannot be
inherited by the next. The peak is read from ``ru_maxrss``, which is the
kernel's own high-water mark for the process and includes DuckDB's native
allocations - not just Python's.

The synthetic history is *modelled*, not sampled from production: the shape is
stated in ``--help`` and in the report, because a benchmark whose data shape is
unknown cannot be compared with anything.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

#: Roughly the production export's part size (1,481,414 rows over 79 parts).
ROWS_PER_PART = 19_000


def generate_export(
    directory: Path,
    *,
    rows: int,
    markets: int,
    cycle_seconds: int,
    seed: int,
    rows_per_part: int = ROWS_PER_PART,
) -> dict:
    """Write a synthetic decisions export, in the exporter's own shape.

    The shape is a weather-market shadow: every market is re-evaluated on a
    fixed cycle, a signal stays on for a run of cycles and then stops, and
    occasionally the whole feed goes quiet for longer than the gap window
    (which is what makes ``gap_data_missing`` reachable). Models, directions
    and modes are mixed so every grouping dimension has more than one value.

    Returns a small description of what was written, for the report.
    """
    parts = directory / "parts"
    if parts.exists():
        shutil.rmtree(parts)
    parts.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    target_dates = [f"2026-10-{day:02d}" for day in range(1, 29)]
    models = ["calibrated", "gfs_raw", "fusion"]
    directions = ["buy_yes", "sell_yes"]
    modes = ["LIVE", "LIVE", "LIVE", "REPLAY"]

    instant = datetime(2026, 9, 1, tzinfo=UTC)
    written = 0
    part_index = 1
    handle = None
    accepted = 0
    unpriced = 0

    def close_part() -> None:
        nonlocal handle
        if handle is not None:
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None

    # Per-market state: (market, model, direction, mode, target_date, on_until).
    live = []
    quiet_until = None

    try:
        while written < rows:
            if handle is None:
                handle = (parts / f"part-{part_index:09d}.jsonl").open("w", encoding="utf-8")
            if rng.random() < 0.0006:
                quiet_until = instant + timedelta(seconds=cycle_seconds * rng.randint(20, 60))

            if quiet_until is None or instant >= quiet_until:
                quiet_until = None
                while len(live) < markets:
                    live.append(
                        {
                            "market_id": f"m-{rng.randrange(40)}",
                            "model_name": rng.choice(models),
                            "direction": rng.choice(directions),
                            "mode": rng.choice(modes),
                            "target_date": rng.choice(target_dates),
                            "state": rng.choice(["ACCEPT", "ACCEPT", "REJECT", "NO_TRADE"]),
                            "remaining": rng.randint(1, 40),
                        }
                    )
                for slot in live:
                    slot["remaining"] -= 1
                    if slot["remaining"] <= 0:
                        slot["state"] = rng.choice(["REJECT", "NO_TRADE"])
                    state = slot["state"]
                    edge = None
                    if state == "ACCEPT":
                        if rng.random() < 0.06:
                            edge = None
                            unpriced += 1
                        else:
                            edge = round(rng.gauss(0.05, 0.06), 6)
                            accepted += 1
                    row = {
                        "decision_id": f"d{written:09d}",
                        "decision_time_utc": instant.isoformat(),
                        "market_id": slot["market_id"],
                        "target_date": slot["target_date"],
                        "model_name": slot["model_name"],
                        "direction": slot["direction"],
                        "mode": slot["mode"],
                        "state": state,
                        "net_edge": edge,
                        "market_price": 0.6,
                        "model_probability": 0.65,
                        "available_depth_usd": 500.0,
                        "fee_rate": 0.0,
                        "station": "KLGA",
                        "bracket_label": "81°F",
                        "market_question": "Will it be 81F?",
                    }
                    handle.write(json.dumps(row) + "\n")
                    written += 1
                live = [slot for slot in live if slot["remaining"] > 0]
            else:
                # The feed is quiet: nothing is decided anywhere.
                pass

            if written and written % rows_per_part < markets:
                close_part()
                part_index += 1
            instant += timedelta(seconds=cycle_seconds)
            if written >= rows:
                break
    finally:
        close_part()

    return {
        "rows": written,
        "parts": part_index - 1 if written % rows_per_part else part_index,
        "accepts": accepted,
        "accepts_without_edge": unpriced,
        "markets": markets,
        "cycle_seconds": cycle_seconds,
        "seed": seed,
    }


def peak_rss_bytes() -> int:
    """The kernel's high-water mark for this process, in bytes."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def current_rss_bytes() -> int:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:  # pragma: no cover - not Linux
        pass
    return 0


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def synthetic_settlements(markets: int = 40, days: int = 28) -> list[dict]:
    """A settlement per (market, target date), so the win rate really runs.

    Without them the win rate is N/A on both sides and the clustered bootstrap -
    the most expensive part of publishing - never executes, which would make
    the benchmark's timings a fiction.
    """
    out = []
    for market in range(markets):
        for day in range(1, days + 1):
            out.append(
                {
                    "market_id": f"m-{market}",
                    "target_date": f"2026-10-{day:02d}",
                    "settlement_status": "RESOLVED",
                    "resolved_outcome": 1.0 if (market + day) % 3 else 0.0,
                    "verification_status": "VERIFIED",
                    "fetched_at_utc": "2026-10-15T12:00:00+00:00",
                }
            )
    return out


def run_scale(args: argparse.Namespace) -> dict:
    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import build_performance_streaming

    work = Path(args.work_dir) / f"rows-{args.rows}"
    work.mkdir(parents=True, exist_ok=True)
    export = work / "export"
    state_db = work / "state.duckdb"
    out = work / "performance"

    if args.reset:
        for path in (state_db, work / "state.duckdb.wal"):
            if path.exists():
                path.unlink()
        if out.exists():
            shutil.rmtree(out)

    shape = None
    if args.generate:
        started = time.monotonic()
        shape = generate_export(
            export,
            rows=args.rows,
            markets=args.markets,
            cycle_seconds=args.cycle_seconds,
            seed=args.seed,
        )
        shape["generated_seconds"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    result = build_performance_streaming(
        export=export,
        out_dir=out,
        state_db=state_db,
        dataset="fixture",
        settlements=synthetic_settlements(),
        memory_limit=args.memory_limit,
        temp_directory=work / "duckdb-tmp",
        reset=False,
        now=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
    )
    total_seconds = time.monotonic() - started
    ingest = result["ingest"] or {}

    with PerformanceStateStore(state_db, memory_limit=args.memory_limit) as store:
        store_summary = store.summary()
        counts = store_summary["counts"]
        state = store_summary["state"] or {}

    report = {
        "rows": args.rows,
        "work_dir": str(work),
        "memory_limit": args.memory_limit,
        "generated": shape,
        "ingest": {
            "parts_consumed": ingest.get("parts_consumed"),
            "rows_consumed": ingest.get("rows_consumed"),
        },
        "wall_seconds": round(total_seconds, 1),
        "peak_rss_mb": round(peak_rss_bytes() / 1e6, 1),
        "rss_after_mb": round(current_rss_bytes() / 1e6, 1),
        "export_mb": round(directory_bytes(export) / 1e6, 1),
        "state_db_mb": round(directory_bytes(state_db) / 1e6, 1),
        "snapshots_mb": round(directory_bytes(out) / 1e6, 1),
        "store": {
            "episodes": counts.get("episodes"),
            "open_episodes": counts.get("open_episodes"),
            "edge_observations": counts.get("edge_observations"),
            "parts": counts.get("parts"),
            "decisions_seen": state.get("decisions_seen"),
            "accepts_seen": state.get("accepts_seen"),
        },
        "summary": {
            key: result["summary"].get(key)
            for key in (
                "accept_observations",
                "opportunity_episodes",
                "observation_edge_sum",
                "unique_opportunity_edge_sum",
                "realized_directional_win_rate",
                "verified_opportunities",
            )
        },
        "generation_id": result["generation_id"],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=1_500_000)
    parser.add_argument("--markets", type=int, default=24, help="markets evaluated per cycle")
    parser.add_argument("--cycle-seconds", type=int, default=60, help="decision cadence per market")
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--work-dir", default="/home/coral/bench")
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--no-generate", dest="generate", action="store_false", help="reuse existing parts")
    parser.add_argument("--reset", action="store_true", help="drop the state store first")
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument(
        "--all",
        action="store_true",
        help="run 1.5M, 3M and 5M, each in its own process",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.all:
        reports = []
        for rows in (1_500_000, 3_000_000, 5_000_000):
            child = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--rows", str(rows),
                "--work-dir", args.work_dir,
                "--memory-limit", args.memory_limit,
                "--json",
                "--reset",
            ]
            print(f"=== {rows:,} rows ===", flush=True)
            completed = subprocess.run(child, capture_output=True, text=True)
            sys.stdout.write(completed.stdout)
            sys.stderr.write(completed.stderr)
            if completed.returncode != 0:
                print(f"scale {rows} failed", file=sys.stderr)
                return completed.returncode
            reports.append(json.loads(completed.stdout.strip().splitlines()[-1]))
        if args.report:
            Path(args.report).write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(json.dumps(reports, indent=2))
        return 0

    report = run_scale(args)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report))
    else:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

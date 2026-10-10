#!/usr/bin/env python3
"""Backfill historical ECMWF and GFS runs into the research database.

Usage:
    python scripts/backfill_forecasts.py --days 60 [--models ecmwf_ifs025,gfs]

Everything written is keyed, so the script is safe to interrupt and re-run: a
second pass overwrites rather than duplicates. Runs the API does not have are
recorded as ``unavailable`` with the venue's own error text and produce no
daily maximum - the gap is reported, never interpolated.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weatherquant.config import Settings
from weatherquant.data.http import ResilientHttp
from weatherquant.storage import ResearchDatabase
from weatherquant.weather.backfill import backfill
from weatherquant.weather.single_runs import SingleRunsClient

# Issue times for both centres: ECMWF and GFS each run at 00/06/12/18Z.
ISSUE_HOURS = (0, 6, 12, 18)


def plan(days: int, today: date, now: datetime) -> list[datetime]:
    """Run times for the window, oldest first, excluding the future.

    A run timestamped in the future is not an error to retry - it simply does
    not exist yet, so it is left out rather than requested and recorded failed.
    """
    out: list[datetime] = []
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        for hour in ISSUE_HOURS:
            run = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            if run <= now:
                out.append(run)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="days of run history to fetch")
    parser.add_argument("--models", default="ecmwf_ifs025,gfs")
    parser.add_argument("--db", help="research database path")
    parser.add_argument("--pause", type=float, default=1.7, help="seconds between requests")
    parser.add_argument("--horizon-days", type=int, default=10)
    args = parser.parse_args()

    settings = Settings()
    db_path = Path(args.db) if args.db else settings.research_db_path
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    now = datetime.now(tz=UTC)
    today = now.date()
    runs = plan(args.days, today, now)

    total = len(runs) * len(models)
    print(f"research db : {db_path}", flush=True)
    print(f"models      : {models}", flush=True)
    if runs:
        print(f"window      : {runs[0].date()} .. {runs[-1].date()}", flush=True)
    print(f"run slots   : {len(runs)} ({total} requests planned)", flush=True)

    http = ResilientHttp(timeout_seconds=30, retries=2)
    client = SingleRunsClient(http, raw_dir=settings.research_raw_dir)
    counter = {"n": 0}

    def progress(model: str, run_time: datetime, fetch) -> None:
        counter["n"] += 1
        mark = "ok  " if fetch.is_ok else f"{fetch.status[:4]}"
        if counter["n"] % 20 == 0 or not fetch.is_ok:
            print(
                f"  [{counter['n']:>4}/{total}] {model:14} {run_time:%Y-%m-%dT%H:%MZ} "
                f"{mark} pts={fetch.n_points} nulls={fetch.n_null}",
                flush=True,
            )
        time.sleep(args.pause)

    with ResearchDatabase(db_path) as db:
        db.open()
        run_id = db.start_run(
            "backfill-forecasts",
            {"days": args.days, "models": models, "run_slots": len(runs)},
        )
        started = time.monotonic()
        try:
            stats = backfill(
                db.con,
                client=client,
                models=models,
                run_times=runs,
                target_dates=[today + timedelta(days=i) for i in range(-args.days, 3)],
                station=settings.station,
                on_fetch=progress,
            )
        except KeyboardInterrupt:
            db.finish_run(run_id, status="failed", error="interrupted by user")
            print("\ninterrupted - progress is committed and resumable", file=sys.stderr)
            http.close()
            return 130
        except Exception as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            http.close()
            raise

        elapsed = time.monotonic() - started
        db.finish_run(run_id, status="completed", stats=stats.as_dict())
        print(f"\nbackfill finished in {elapsed:.0f}s", flush=True)
        for key, value in stats.as_dict().items():
            if key != "problems":
                print(f"  {key:20} {value}")
        if stats.problems:
            print(f"  problems: {len(stats.problems)}")
            for problem in stats.problems[:8]:
                print(f"    - {problem}")
        print("\n  table counts:")
        for name, count in sorted(db.table_counts().items()):
            if count:
                print(f"    {name:26} {count}")

    http.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

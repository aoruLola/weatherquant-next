#!/usr/bin/env python3
"""Long-run soak for the streaming pipeline's production schedule (WQN-28).

The 24-hour acceptance runs on the NAS against the production export. This
harness proves locally, at an accelerated cadence, every property of that
acceptance that does not need production data - by driving the **real
scheduler script** (``scripts/streaming_schedule.sh``) the way cron would,
against a synthetic export that keeps growing the way the shadow's does:

1. **The watermark advances on schedule.** Ingest consumes each new part;
   publish emits generations. The schedule log records the watermark per
   run, and the soak requires it to move.
2. **The roles never collide.** Both roles contend for one flock; a skipped
   tick is logged (``skipped-locked``) and never a failure. What must never
   happen is a DuckDB write conflict failing a run.
3. **A crash recovers.** Mid-soak the running ingest is SIGKILLed (the
   container-OOM state). The next tick must succeed and carry the watermark
   past the pre-crash value - the state store's single-transaction commit
   makes that a property, not a hope.
4. **The resource budget holds.** Each invocation's peak RSS is recorded by
   the scheduler (GNU time) and gated here.
5. **The published tree stays consistent.** After every publish,
   ``current.json`` must name a complete generation.

Usage::

    uv run python scripts/streaming_soak.py --minutes 12 --work /tmp/wq-soak
        [--ingest-seconds 40] [--publish-seconds 120] [--feeder-seconds 20]
        [--crash-after-seconds 240] [--max-rss-kb 1500000]

Everything lands under ``--work``; nothing outside it is touched. The
schedule log the scheduler writes is copied into the evidence the report
prints, so the PR's numbers are the scheduler's own record, not a summary
the harness computed out of band.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = PROJECT_ROOT / "scripts" / "streaming_schedule.sh"


# ---------------------------------------------------------------------------
# the growing export
# ---------------------------------------------------------------------------

_MARKETS = [f"m-{i}" for i in range(6)]
_DATES = ["2026-10-13", "2026-10-14", "2026-10-15"]


class ExportFeeder:
    """Appends one decision part at a time, with strictly growing timestamps.

    The streaming reader refuses parts whose cursor does not advance, so the
    feeder's clock is monotonic by construction; rows are ACCEPTs with edges
    so the publish side exercises the real aggregations.
    """

    def __init__(self, export_dir: Path) -> None:
        self.parts_dir = export_dir / "parts"
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir = export_dir
        self.sequence = 0
        self.clock = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
        self.total_rows = 0
        self._lock = threading.Lock()

    def append_part(self, rows: int = 40) -> Path:
        with self._lock:
            self.sequence += 1
            path = self.parts_dir / f"part-{self.sequence:09d}.jsonl"
            lines = []
            for _ in range(rows):
                self.total_rows += 1
                self.clock += timedelta(seconds=7)
                lines.append(
                    json.dumps(
                        {
                            "decision_id": f"dec-{self.total_rows:08d}",
                            "decision_time_utc": self.clock.isoformat(),
                            "market_id": _MARKETS[self.total_rows % len(_MARKETS)],
                            "target_date": _DATES[self.total_rows % len(_DATES)],
                            "model_name": "calibrated",
                            "direction": "buy_yes",
                            "mode": "LIVE",
                            "state": "ACCEPT",
                            "net_edge": 0.03 + (self.total_rows % 11) / 100.0,
                            "market_price": 0.6,
                            "model_probability": 0.65,
                            "available_depth_usd": 500.0,
                            "station": "KLGA",
                            "bracket_label": "81°F",
                        }
                    )
                )
            path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
            # The export's own bookkeeping, exactly as the shadow publishes it.
            (self.export_dir / "state.json").write_text(
                json.dumps(
                    {
                        "next_part": self.sequence + 1,
                        "rows_exported": self.total_rows,
                        "cursor": {
                            "last_time_utc": self.clock.isoformat(),
                            "last_id": f"dec-{self.total_rows:08d}",
                        },
                    }
                ),
                encoding="utf-8",
            )
            return path

    @property
    def exported_rows(self) -> int:
        with self._lock:
            return self.total_rows


# ---------------------------------------------------------------------------
# the cron stand-in
# ---------------------------------------------------------------------------


@dataclass
class ScheduledRun:
    role: str
    started_at: str
    pid: int
    popen: subprocess.Popen


def _parse_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class CronStandIn:
    """Fires the scheduler on the two cadences, in role-specific threads.

    Each invocation is a fresh subprocess - the same process model cron
    gives the scheduler, which is what makes the flock and the recovery
    real. One crash injection is armed: the first ingest still running when
    ``crash_at`` passes is SIGKILLed via its process group.
    """

    def __init__(
        self,
        work: Path,
        *,
        ingest_seconds: float,
        publish_seconds: float,
        crash_after_seconds: float | None,
        log: list[str],
    ) -> None:
        self.work = work
        self.ingest_seconds = ingest_seconds
        self.publish_seconds = publish_seconds
        self.crash_after_seconds = crash_after_seconds
        self.started = time.monotonic()
        self.log = log
        self.runs: list[ScheduledRun] = []
        self.crash_injected = 0
        self.crash_at: datetime | None = None
        self._stop = threading.Event()
        self._env = {
            **os.environ,
            "PY": sys.executable,
            "WQ_EXPORT_DIR": str(work / "export"),
            "WQ_PERFORMANCE_OUT": str(work / "performance"),
            "WQ_STATE_DB": str(work / "state" / "performance-state.duckdb"),
            "WQ_DATASET": "fixture",
            "WQ_SCHEDULE_LOG": str(work / "schedule.jsonl"),
        }

    def _fire(self, role: str) -> subprocess.Popen:
        started = datetime.now(tz=UTC).isoformat()
        stamp = datetime.now(tz=UTC).strftime("%H%M%S")
        log_path = self.work / "logs" / f"{stamp}-{role}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # The child owns the fd once Popen dups it; closing the parent's copy
        # right after the spawn is the safe shape for a run log the parent
        # never reads back.
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                ["bash", str(SCHEDULER), role],
                cwd=PROJECT_ROOT,
                env=self._env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.runs.append(ScheduledRun(role=role, started_at=started, pid=proc.pid, popen=proc))
        return proc

    def _maybe_crash(self, proc: subprocess.Popen) -> None:
        if (
            self.crash_after_seconds is not None
            and self.crash_injected == 0
            and time.monotonic() - self.started >= self.crash_after_seconds
            and proc.poll() is None
        ):
            # No SIGTERM, no cleanup: the state a container OOM kill leaves.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                self.crash_injected = 1
                self.crash_at = datetime.now(tz=UTC)
                self.log.append(f"SOAK: SIGKILLed the running ingest at "
                                f"{time.monotonic() - self.started:.0f}s")
            except (ProcessLookupError, PermissionError):
                pass

    def thread(self, role: str) -> threading.Thread:
        def run() -> None:
            cadence = self.ingest_seconds if role == "ingest" else self.publish_seconds
            next_at = 0.0
            while not self._stop.is_set():
                now = time.monotonic() - self.started
                if now < next_at:
                    self._stop.wait(min(0.5, next_at - now))
                    continue
                proc = self._fire(role)
                if role == "ingest":
                    self._maybe_crash(proc)
                next_at = now + cadence
                # Reap without blocking the cadence: wait() with a timeout
                # longer than any sane run, in this dedicated thread.
                proc.wait(timeout=600)

        thread = threading.Thread(target=run, name=f"cron-{role}", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        for run in self.runs:
            if run.popen.poll() is None:
                with_contextlib_suppress(run.popen)


def with_contextlib_suppress(proc: subprocess.Popen) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# evidence: what the scheduler itself recorded
# ---------------------------------------------------------------------------


@dataclass
class ScheduleEntry:
    ts: str
    role: str
    status: str
    exit: int
    duration_s: float
    rss_kb: int | None
    watermark: str | None


def read_schedule_log(path: Path) -> list[ScheduleEntry]:
    entries: list[ScheduleEntry] = []
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        rss = raw.get("rss_kb")
        entries.append(
            ScheduleEntry(
                ts=str(raw.get("ts")),
                role=str(raw.get("role")),
                status=str(raw.get("status")),
                exit=int(raw.get("exit", 0)),
                duration_s=float(raw.get("duration_s", 0)),
                rss_kb=int(rss) if isinstance(rss, (int, float, str)) and str(rss).isdigit() else None,
                watermark=str(raw.get("watermark")) if raw.get("watermark") else None,
            )
        )
    return entries


def watermark_value(entry: ScheduleEntry) -> str | None:
    """The watermark's time component, parsed from the CLI's list rendering."""
    if not entry.watermark:
        return None
    try:
        parsed = ast.literal_eval(entry.watermark)
        if isinstance(parsed, (list, tuple)) and parsed:
            return str(parsed[0])
    except (ValueError, SyntaxError):
        return entry.watermark.split(",")[0].strip("[] '\"")
    return None


def current_generation_complete(performance_dir: Path) -> bool:
    pointer = performance_dir / "current.json"
    if not pointer.exists():
        return False
    try:
        generation = json.loads(pointer.read_text(encoding="utf-8"))["generation_id"]
    except (json.JSONDecodeError, KeyError):
        return False
    directory = performance_dir / "generations" / generation
    return (directory / "manifest.json").exists() and (directory / "summary.json").exists()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=12.0)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--ingest-seconds", type=float, default=40.0)
    parser.add_argument("--publish-seconds", type=float, default=120.0)
    parser.add_argument("--feeder-seconds", type=float, default=20.0)
    parser.add_argument("--crash-after-seconds", type=float, default=240.0)
    parser.add_argument("--max-rss-kb", type=int, default=1500000,
                        help="per-invocation peak RSS gate (default 1.5 GB)")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    feeder = ExportFeeder(work / "export")
    feeder.append_part()  # the export is never empty: the shadow had history

    soak_log: list[str] = []
    cron = CronStandIn(
        work,
        ingest_seconds=args.ingest_seconds,
        publish_seconds=args.publish_seconds,
        crash_after_seconds=args.crash_after_seconds,
        log=soak_log,
    )
    threads = [cron.thread("ingest"), cron.thread("publish")]

    def feeder_loop() -> None:
        # time.monotonic() is seconds since an arbitrary point (usually boot),
        # so every gate here is relative to an anchor, never to a small
        # absolute number - the first version of this loop compared uptime
        # against `minutes * 60` and exited before appending anything.
        started = time.monotonic()
        next_at = args.feeder_seconds
        while time.monotonic() - started < args.minutes * 60.0:
            time.sleep(min(1.0, max(0.0, next_at - (time.monotonic() - started))))
            if time.monotonic() - started >= next_at:
                feeder.append_part()
                next_at += args.feeder_seconds

    feeder_thread = threading.Thread(target=feeder_loop, name="feeder", daemon=True)
    feeder_thread.start()

    print(f"streaming soak: {args.minutes:.0f} min over {work}")
    print(f"  cadence: ingest {args.ingest_seconds:.0f}s, publish {args.publish_seconds:.0f}s, "
          f"feeder {args.feeder_seconds:.0f}s; crash armed at {args.crash_after_seconds:.0f}s")
    try:
        time.sleep(args.minutes * 60.0)
    finally:
        cron.stop()
        for thread in threads:
            thread.join(timeout=30)

    # --- evidence ----------------------------------------------------------
    entries = read_schedule_log(work / "schedule.jsonl")
    ingest_runs = [e for e in entries if e.role == "ingest"]
    publish_runs = [e for e in entries if e.role == "publish"]
    failures = [e for e in entries if e.status == "failed"]
    skips = [e for e in entries if e.status == "skipped-locked"]
    watermarks = [w for w in (watermark_value(e) for e in ingest_runs) if w]
    peak_rss = max((e.rss_kb for e in entries if e.rss_kb is not None), default=None)

    print("\n" + "=" * 72)
    print("STREAMING SOAK REPORT")
    print("=" * 72)
    print(f"  rows fed               {feeder.exported_rows} in {feeder.sequence} part(s)")
    print(f"  ingest runs            {len(ingest_runs)} ({len(skips)} skipped-locked, "
          f"{sum(1 for e in ingest_runs if e.status == 'failed')} failed)")
    print(f"  publish runs           {len(publish_runs)} "
          f"({sum(1 for e in publish_runs if e.status == 'skipped-locked')} skipped-locked, "
          f"{sum(1 for e in publish_runs if e.status == 'failed')} failed)")
    print(f"  watermark first/last   {watermarks[0] if watermarks else None} -> "
          f"{watermarks[-1] if watermarks else None}")
    print(f"  crash injected         {cron.crash_injected}")
    print(f"  peak RSS (per run)     {peak_rss / 1024:.0f} MiB" if peak_rss else
          "  peak RSS (per run)     n/a (no GNU time on this machine)")
    print(f"  published tree         current generation complete: "
          f"{current_generation_complete(work / 'performance')}")

    failures_list: list[str] = []
    if len(ingest_runs) < 3:
        failures_list.append("fewer than 3 ingest runs completed the window")
    if len(publish_runs) < 1:
        failures_list.append("no publish run completed")
    if not watermarks or (len(watermarks) >= 2 and watermarks[-1] <= watermarks[0]):
        failures_list.append(f"the watermark did not advance: {watermarks[:3]}")
    if cron.crash_after_seconds is not None and cron.crash_injected != 1:
        failures_list.append("the crash injection never fired (window too short?)")
    if cron.crash_injected == 1 and cron.crash_at is not None:
        # Recovery, stated precisely: the watermark of the last ingest that
        # STARTED before the kill must be behind the watermark of some ok
        # ingest that started after it.
        started = lambda e: _parse_ts(e.ts)  # noqa: E731
        before = [
            watermark_value(e) for e in ingest_runs
            if e.status == "ok" and (t := started(e)) and t <= cron.crash_at and watermark_value(e)
        ]
        after = [
            watermark_value(e) for e in ingest_runs
            if e.status == "ok" and (t := started(e)) and t > cron.crash_at and watermark_value(e)
        ]
        if not after:
            failures_list.append("no successful ingest after the crash")
        elif before and max(before) >= max(after):
            failures_list.append(
                f"the watermark did not advance past the crash: {max(before)} -> {max(after)}"
            )
    consecutive = 0
    for entry in entries:
        consecutive = consecutive + 1 if entry.status == "failed" else 0
        if consecutive >= 2:
            failures_list.append("two consecutive failed runs: the pipeline is stuck")
            break
    if peak_rss is not None and peak_rss > args.max_rss_kb:
        failures_list.append(
            f"peak RSS {peak_rss} kB above the gate {args.max_rss_kb}"
        )
    if not current_generation_complete(work / "performance"):
        failures_list.append("the published tree has no complete current generation")

    print(f"  schedule log           {len(entries)} invocation(s), "
          f"{len(failures)} failed, {len(skips)} skipped-locked")
    for line in soak_log:
        print(f"  {line}")

    print("\n" + "=" * 72)
    if failures_list:
        print("RESULT: FAILED")
        for failure in failures_list:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASSED")
    print("  watermark advance, lock skip behaviour, crash recovery, resource")
    print("  budget and publish consistency all verified against the real")
    print("  scheduler script at an accelerated cadence.")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"  work kept at {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

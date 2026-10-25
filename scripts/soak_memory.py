#!/usr/bin/env python3
"""Memory soak: the collector under real collection, measured.

The functional soak (`scripts/soak_test.py`) proves the collector survives
faults. This one proves it *stays small while it does* - the acceptance
WQN-3 asks for, which a plain collection loop does not cover:

=================================  =====================================
Phase 1 - collection               Real upstream collection for
                                   ``--minutes``, sampled every few
                                   seconds: process RSS, kernel memory
                                   PSI, heartbeat status, row counters.
Phase 2 - the backup high-water    The daily backup is a distinct memory
mark                               peak a plain collection loop never
                                   reaches (PR #14: "5+ GB written"). It
                                   runs here against the same database,
                                   with the same budget, while RSS is
                                   sampled - synthetic rows in production
                                   flush cadence, then ``Writer.backup``.
=================================  =====================================

Gates (all measured, none assumed):

* peak RSS under ``--max-rss-mb`` (default 3072: the engine's 3GB budget plus
  Python overhead, well under the container's 6GB cap);
* the RSS trend over the second half of the run below ``--max-slope-mb-per-min``
  (default 8) - the slow-leak detector, since an OOM from growth shows up as
  a slope long before it shows up as a peak;
* kernel memory PSI ``full`` avg10 never above ``--max-psi-full`` (default 2.0):
  the host never stalled on memory while the collector ran;
* the backup verified (row counts re-read through an independent connection);
* the final health verdict clean.

Usage::

    uv run python scripts/soak_memory.py --minutes 30 --data /tmp/wq-mem-soak

The collector talks to the real upstream APIs. Nothing outside ``--data`` is
written, and the directory is removed on success unless ``--keep``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Sample:
    at: float  # monotonic seconds since phase start
    rss_mb: float
    hwm_mb: float
    psi_some: float
    psi_full: float
    status: str
    books: int
    prices: int


@dataclass
class PhaseReport:
    name: str
    duration_s: float = 0.0
    samples: list[Sample] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def peak_rss(self) -> float:
        return max((s.rss_mb for s in self.samples), default=0.0)

    def peak_psi_full(self) -> float:
        return max((s.psi_full for s in self.samples), default=0.0)

    def peak_psi_some(self) -> float:
        return max((s.psi_some for s in self.samples), default=0.0)

    def slope_mb_per_min(self) -> float:
        """Least-squares RSS slope over the second half - the leak detector.

        The first half warms every cache (DuckDB buffer pool, Python heap,
        OS page cache for a fresh file); growth there is expected. A leak
        shows up as a slope that persists after warm-up.
        """
        pts = self.samples[len(self.samples) // 2 :]
        if len(pts) < 10:
            return 0.0
        xs = [p.at for p in pts]
        ys = [p.rss_mb for p in pts]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        slope_per_s = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom
        return slope_per_s * 60.0


# --- sampling helpers --------------------------------------------------------


def read_psi() -> tuple[float, float]:
    """Kernel memory PSI: (some avg10, full avg10). 0.0 when unavailable."""
    try:
        text = Path("/proc/pressure/memory").read_text()
    except OSError:
        return 0.0, 0.0
    some = full = 0.0
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")
        values = dict(pair.split("=") for pair in rest.split())
        if kind == "some":
            some = float(values.get("avg10", 0.0))
        elif kind == "full":
            full = float(values.get("avg10", 0.0))
    return some, full


def read_rss_mb(pid: int) -> tuple[float, float]:
    """(VmRSS, VmHWM) in MiB; (0, 0) when the process is gone."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0.0, 0.0
    rss = re.search(r"VmRSS:\s+(\d+) kB", text)
    hwm = re.search(r"VmHWM:\s+(\d+) kB", text)
    return (int(rss.group(1)) / 1024 if rss else 0.0, int(hwm.group(1)) / 1024 if hwm else 0.0)


def read_heartbeat(data_dir: Path) -> dict | None:
    try:
        return json.loads((data_dir / "health" / "heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pump(proc: subprocess.Popen, sink: list[str], limit: int = 400) -> None:
    def run() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            sink.append(line.rstrip("\n"))
            if len(sink) > limit:
                del sink[: len(sink) - limit]

    threading.Thread(target=run, daemon=True, name="log-pump").start()


# --- phase 1: real collection ------------------------------------------------


def run_collection(data_dir: Path, minutes: float, interval_s: float) -> PhaseReport:
    report = PhaseReport(name="collection")
    env = dict(os.environ)
    env.update(
        {
            "WQ_DB_PATH": str(data_dir / "weatherquant.duckdb"),
            "WQ_RAW_DIR": str(data_dir / "raw"),
            "WQ_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        env.pop(key, None)

    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "weatherquant.data.worker"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    logs: list[str] = []
    _pump(proc, logs)
    deadline = started + minutes * 60.0

    try:
        while time.monotonic() < deadline:
            rss, hwm = read_rss_mb(proc.pid)
            psi_some, psi_full = read_psi()
            hb = read_heartbeat(data_dir) or {}
            stats = hb.get("stats", {})
            report.samples.append(
                Sample(
                    at=time.monotonic() - started,
                    rss_mb=rss,
                    hwm_mb=hwm,
                    psi_some=psi_some,
                    psi_full=psi_full,
                    status=str(hb.get("status", "unknown")),
                    books=int(stats.get("books_rows_inserted", 0)),
                    prices=int(stats.get("price_points_inserted", 0)),
                )
            )
            if proc.poll() is not None:
                report.events.append(
                    f"collector exited early with code {proc.returncode} "
                    f"after {(time.monotonic() - started):.0f}s"
                )
                break
            time.sleep(interval_s)
    finally:
        if proc.poll() is None:
            proc.send_signal(15)  # SIGTERM: the graceful path a container stop uses
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                report.events.append("collector ignored SIGTERM; SIGKILLed")
        report.duration_s = time.monotonic() - started
        report.events.append(f"collected {len(logs)} log lines (tail kept separately)")

    last = report.samples[-1] if report.samples else None
    if last and last.books == 0:
        report.events.append("no book rows were written - was upstream reachable?")
    return report


# --- phase 2: the backup high-water mark -------------------------------------


def _synthetic_books(batch_index: int, rows: int, run_id: str, base: datetime) -> list[tuple]:
    """Rows shaped exactly like `BookSnapshot.row`, dedup-stable per index."""
    stamp = base + timedelta(seconds=batch_index)
    out = []
    for i in range(rows):
        n = batch_index * rows + i
        token = f"SOAK-TOKEN-{n % 500:05d}"
        bids = '[[0.55,"500"],[0.54,"300"]]'
        asks = '[[0.57,"450"],[0.58,"250"]]'
        content = f"synthetic-{n:016x}"
        out.append(
            (
                content,           # book_id
                "SOAK-MARKET",     # market_id
                token,             # token_id
                bids, asks,        # levels json
                0.55, 0.57, 0.02,  # best bid / ask / spread
                800.0, 700.0,      # depths
                "",                # defects
                stamp,             # venue_ts
                stamp,             # fetched_at_utc
                "rest",            # source
                "venue",           # venue_ts_origin
                content,           # content_hash column
                "ok",              # quality
                run_id,            # run_id
            )
        )
    return out


def _synthetic_prices(batch_index: int, rows: int, run_id: str, base: datetime) -> list[tuple]:
    stamp = base + timedelta(seconds=batch_index)
    return [
        (
            "SOAK-MARKET",
            f"SOAK-TOKEN-{(batch_index * rows + i) % 500:05d}",
            stamp,
            0.56,
            "rest",
            stamp,
            "venue",
            run_id,
        )
        for i in range(rows)
    ]


def run_backup_cycle(
    db_path: Path, data_dir: Path, batches: int, rows: int, *, flush_rows: int = 500
) -> tuple[PhaseReport, dict]:
    """Feed rows at production flush cadence, then run the daily backup.

    ``flush_rows`` is the writer's batch threshold. 500 matches production;
    raising it (fewer, larger commits) trades cadence fidelity for wall
    clock when the operator only cares about the backup's memory high-water
    mark. Progress prints every 25 batches - this phase is bounded by
    ``--backup-batches``, not by ``--minutes``, and the acceptance found a
    ~11-minute silent stretch here indistinguishable from a hang.
    """
    from weatherquant.data.writer import CollectorWriter

    report = PhaseReport(name="backup-cycle")
    run_id = f"soak-mem-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%S')}"
    dest = data_dir / "backups"
    started = time.monotonic()
    base = datetime.now(tz=UTC) - timedelta(hours=1)

    # The collector releases its single-instance lock on graceful exit. Wait
    # for that rather than fight the lock: two writers on one DuckDB file is
    # exactly what the lock exists to prevent.
    lock_path = data_dir / ".collector.lock"
    lock_deadline = time.monotonic() + 90.0
    while lock_path.exists() and time.monotonic() < lock_deadline:
        time.sleep(2.0)
    if lock_path.exists():
        raise RuntimeError(
            "the collector's single-instance lock is still held; the backup "
            "cycle phase refuses to open a second writer"
        )

    writer = CollectorWriter(
        db_path,
        run_id=run_id,
        flush_rows=flush_rows,
        flush_seconds=5.0,
        checkpoint_seconds=300.0,
    )
    writer.open()
    writer.start_run(
        app_version="soak-memory",
        git_sha=os.environ.get("GIT_SHA"),
        image_tag="soak",
        hostname=os.uname().nodename,
        pid=os.getpid(),
    )
    print(
        f"  backup-cycle: {batches} batches x {rows} rows "
        f"(flush every {flush_rows}); time-bounded by work, not by --minutes"
    )
    backup_summary: dict = {}
    try:
        for batch in range(batches):
            for row in _synthetic_books(batch, rows, run_id, base):
                writer.stage_book(row)
            for row in _synthetic_prices(batch, rows, run_id, base):
                writer.stage_price(row)
            writer.flush(force=True)
            if batch % 20 == 19:
                writer.checkpoint_if_due(force=True)
            rss, _ = read_rss_mb(os.getpid())
            psi_some, psi_full = read_psi()
            report.samples.append(
                Sample(
                    at=time.monotonic() - started,
                    rss_mb=rss,
                    hwm_mb=0.0,
                    psi_some=psi_some,
                    psi_full=psi_full,
                    status="writing",
                    books=int(writer.stats["books_inserted"]),
                    prices=int(writer.stats["prices_inserted"]),
                )
            )
            if batch % 25 == 24 or batch == batches - 1:
                print(
                    f"  batch {batch + 1}/{batches}: rss {rss:.0f} MiB, "
                    f"books {writer.stats['books_inserted']}, "
                    f"elapsed {time.monotonic() - started:.0f}s"
                )

        rss_before, _ = read_rss_mb(os.getpid())
        backup_started = time.monotonic()
        backup_report = writer.backup(dest)
        backup_seconds = time.monotonic() - backup_started
        rss_after, hwm_after = read_rss_mb(os.getpid())
        report.events.append(
            f"backup: verified={backup_report.ok} bytes={backup_report.bytes_written or 0} "
            f"took {backup_seconds:.1f}s, rss {rss_before:.0f} -> {rss_after:.0f} MiB "
            f"(process high-water {hwm_after:.0f} MiB)"
        )
        backup_summary = {
            "verified": backup_report.ok,
            "error": backup_report.error,
            "bytes": backup_report.bytes_written or 0,
            "seconds": backup_seconds,
            "rss_before_mb": rss_before,
            "rss_after_mb": rss_after,
            "hwm_mb": hwm_after,
            "destination": str(backup_report.destination),
        }
        if not backup_report.ok:
            report.events.append(f"backup failed verification: {backup_report.error}")
        writer.close(status="stopped", reason="soak-memory phase end")
    except BaseException:
        if writer.is_open:
            writer.close(status="aborted", reason="soak-memory exception")
        raise
    report.duration_s = time.monotonic() - started
    return report, backup_summary


# --- reporting ----------------------------------------------------------------


def _fmt_mb(value: float) -> str:
    return f"{value:8.1f} MiB"


def _report(
    collection: PhaseReport,
    backup: PhaseReport,
    backup_summary: dict,
    data_dir: Path,
    gates: dict[str, object],
) -> int:
    print("\n" + "=" * 72)
    print("MEMORY SOAK REPORT")
    print("=" * 72)

    for phase in (collection, backup):
        print(f"\n--- {phase.name} ---")
        print(f"  duration            {phase.duration_s:7.0f}s")
        print(f"  samples             {len(phase.samples):7d}")
        print(f"  peak RSS            {_fmt_mb(phase.peak_rss())}")
        print(f"  RSS slope (2nd half){phase.slope_mb_per_min():7.1f} MiB/min")
        print(f"  peak PSI some/full  {phase.peak_psi_some():.2f} / {phase.peak_psi_full():.2f} (avg10 %)")
        if phase.samples:
            print(f"  final books/prices  {phase.samples[-1].books} / {phase.samples[-1].prices}")
        for event in phase.events:
            print(f"  event: {event}")

    spill = Path(os.environ.get("WQ_DUCKDB_TEMP_DIR", data_dir / "spill"))
    if spill.exists():
        total = sum(p.stat().st_size for p in spill.rglob("*") if p.is_file())
        print(f"\n  spill directory     {spill}: {total / 1024 / 1024:.1f} MiB "
              "(on disk, not in RAM - the budget's escape valve)")

    failures: list[str] = []
    if any("exited early" in e for e in collection.events):
        failures.append("the collector exited before the window closed")
    if collection.peak_rss() > float(gates["max_rss_mb"]):
        failures.append(
            f"peak RSS {collection.peak_rss():.0f} MiB above the gate {gates['max_rss_mb']} MiB"
        )
    if collection.slope_mb_per_min() > float(gates["max_slope_mb_per_min"]):
        failures.append(
            f"RSS slope {collection.slope_mb_per_min():.1f} MiB/min above the gate "
            f"{gates['max_slope_mb_per_min']} - memory is growing without bound"
        )
    if collection.peak_psi_full() > float(gates["max_psi_full"]):
        failures.append(
            f"memory PSI full avg10 peaked at {collection.peak_psi_full():.2f}% "
            f"(gate {gates['max_psi_full']}): the host stalled on memory"
        )
    if not backup_summary.get("verified"):
        failures.append(f"backup did not verify: {backup_summary.get('error')}")

    hb = read_heartbeat(data_dir)
    if hb is None:
        failures.append("no final heartbeat")
    else:
        from weatherquant.data.heartbeat import evaluate_health

        verdict = evaluate_health(data_dir / "health" / "heartbeat.json")
        print(f"\n  final health        {verdict.status.value} (exit {verdict.exit_code})")
        if verdict.exit_code != 0:
            failures.append(f"final health verdict not clean: {verdict.reasons}")

    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes",
        type=float,
        default=30.0,
        help=(
            "duration of the COLLECTION phase only. The backup-cycle phase "
            "that follows is bounded by --backup-batches (work, not time) "
            "and reports its own progress"
        ),
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0, help="sample interval, seconds")
    parser.add_argument("--max-rss-mb", type=float, default=3072.0)
    parser.add_argument("--max-slope-mb-per-min", type=float, default=8.0)
    parser.add_argument("--max-psi-full", type=float, default=2.0)
    parser.add_argument("--backup-batches", type=int, default=400,
                        help="synthetic batches fed before the backup (batch = 500 rows)")
    parser.add_argument("--flush-rows", type=int, default=500,
                        help="writer flush threshold for the backup-cycle phase; "
                             "500 is production cadence, raise it for fewer commits")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    data_dir = args.data.resolve()
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    print(f"memory soak: {args.minutes:.0f} min collection over {data_dir}")
    print(
        f"  phases: collection ~{args.minutes:.0f} min (time-bounded), then "
        f"backup-cycle {args.backup_batches} batches (work-bounded, ~"
        f"{args.backup_batches * 500 // 1000}k rows) - total runtime exceeds --minutes"
    )
    collection = run_collection(data_dir, args.minutes, args.interval)

    db_path = data_dir / "weatherquant.duckdb"
    if not db_path.exists():
        print("collector produced no database; nothing to run the backup against", file=sys.stderr)
        return 2
    backup, backup_summary = run_backup_cycle(
        db_path, data_dir, args.backup_batches, 500, flush_rows=args.flush_rows
    )

    gates = {
        "max_rss_mb": args.max_rss_mb,
        "max_slope_mb_per_min": args.max_slope_mb_per_min,
        "max_psi_full": args.max_psi_full,
    }
    code = _report(collection, backup, backup_summary, data_dir, gates)
    if not args.keep and code == 0:
        shutil.rmtree(data_dir, ignore_errors=True)
    else:
        print(f"\ndata kept at {data_dir}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

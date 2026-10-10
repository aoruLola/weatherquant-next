#!/usr/bin/env python3
"""Soak test: 30 minutes of real collection with injected faults.

Runs the collector as a **subprocess** (not a container - Docker is not
available on this machine) and drives three phases:

===========  ==========================================================
Phase A      healthy collection
Phase B      network outage: the process is restarted with a proxy pointed
             at a closed port, so every upstream call fails. The heartbeat
             must keep ticking and the health probe must degrade rather than
             the worker crash.
Phase C      recovery: restarted normally, collection resumes, and the gap
             left by the outage must be closed with a reason.
===========  ==========================================================

A hard kill is injected during Phase C to prove that a crash leaves a gap
which the *next* start closes, rather than a silent hole in the data.

Usage:
    python scripts/soak_test.py --minutes 30 --data <dir>

Everything it reports is measured, not asserted in advance: the point is the
numbers, so the script prints a table the operator can read.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Live children, so an interrupted harness cannot orphan a collector that would
# keep the data directory locked forever. `timeout`, Ctrl-C and a closed terminal
# all bypass the normal `finally` blocks.
_LIVE: list[subprocess.Popen] = []


def _kill_all() -> None:
    for proc in list(_LIVE):
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=10)
    _LIVE.clear()


atexit.register(_kill_all)


@dataclass
class Sample:
    at: float
    status: str
    books: int
    prices: int
    contact_age_s: float | None
    ws_connected: bool
    ws_reconnects: int
    rest_ok: int
    rest_failed: int
    errors: int
    db_bytes: int
    raw_bytes: int
    free_percent: float
    consecutive_rest_failures: int


@dataclass
class PhaseResult:
    name: str
    started_at: datetime
    ended_at: datetime
    samples: list[Sample] = field(default_factory=list)
    restarts: int = 0
    forced_kills: int = 0
    events: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


def _env(data_dir: Path, *, dead_network: bool) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "WQ_DB_PATH": str(data_dir / "weatherquant.duckdb"),
            "WQ_RAW_DIR": str(data_dir / "raw"),
            "WQ_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if dead_network:
        # A closed local port: every upstream connection fails immediately,
        # which is a complete outage without touching the host's networking.
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
    else:
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
            env.pop(key, None)
    return env


def _start(data_dir: Path, *, dead_network: bool, once: bool = False) -> subprocess.Popen:
    argv = [sys.executable, "-m", "weatherquant.data.worker"]
    if once:
        argv.append("--once")
    return subprocess.Popen(
        argv,
        cwd=PROJECT_ROOT,
        env=_env(data_dir, dead_network=dead_network),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _read_heartbeat(data_dir: Path) -> dict | None:
    path = data_dir / "health" / "heartbeat.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _sample(data_dir: Path) -> Sample | None:
    hb = _read_heartbeat(data_dir)
    if hb is None:
        return None
    stats = hb.get("stats", {})
    contact = hb.get("last_upstream_contact_utc")
    age: float | None = None
    if contact:
        when = datetime.fromisoformat(contact)
        age = (datetime.now(tz=UTC) - when).total_seconds()
    return Sample(
        at=time.monotonic(),
        status=str(hb.get("status")),
        books=int(stats.get("books_rows_inserted", 0)),
        prices=int(stats.get("price_points_inserted", 0)),
        contact_age_s=age,
        ws_connected=bool(hb.get("ws_connected")),
        ws_reconnects=int(stats.get("ws_reconnects", 0)),
        rest_ok=int(stats.get("rest_requests_ok", 0)),
        rest_failed=int(stats.get("rest_requests_failed", 0)),
        errors=int(stats.get("errors_total", 0)),
        db_bytes=int(hb.get("db_bytes", 0)),
        raw_bytes=int(hb.get("raw_bytes", 0)),
        free_percent=float(hb.get("disk_free_percent", 100.0)),
        consecutive_rest_failures=int(hb.get("rest_consecutive_failures", 0)),
    )


def _pump(proc: subprocess.Popen, sink: list[str], limit: int = 600) -> threading.Thread:
    """Drain the child's stdout on a background thread.

    Reading it from the main loop would block in ``readline()`` whenever the
    child goes quiet, so the sampling loop would never reach its deadline check
    and the harness would hang instead of reporting.
    """

    def run() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            sink.append(line.rstrip("\n"))
            if len(sink) > limit:
                del sink[: len(sink) - limit]

    thread = threading.Thread(target=run, daemon=True, name="log-pump")
    thread.start()
    return thread


# The single-instance lock is a *lease*: a crashed holder's lock expires after
# `stale_after_seconds` rather than being stolen immediately, so that two
# starters racing cannot both decide it is stale. The harness has to respect
# that - starting the next phase the instant a hard-killed one ends would be
# refused with exit 4, which is the lock doing its job, not a collector bug.
LEASE_WAIT_SECONDS = 90.0


def _await_lease_expiry(data_dir: Path, logs: list[str]) -> None:
    lock_path = data_dir / ".collector.lock"
    if not lock_path.exists():
        return
    deadline = time.monotonic() + LEASE_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # half-written or already gone
        age = datetime.now(tz=UTC) - datetime.fromisoformat(payload["heartbeat_at_utc"])
        if age.total_seconds() >= 65.0:
            return  # lease expired; the next start will take it over
        time.sleep(1.0)
    logs.append(f"lease still held after {LEASE_WAIT_SECONDS:.0f}s; starting anyway")


def run_phase(
    name: str,
    data_dir: Path,
    duration_s: float,
    *,
    dead_network: bool = False,
    kill_after_s: float | None = None,
    once: bool = False,
    logs: list[str],
) -> PhaseResult:
    result = PhaseResult(name=name, started_at=datetime.now(tz=UTC), ended_at=datetime.now(tz=UTC))
    _await_lease_expiry(data_dir, logs)
    proc = _start(data_dir, dead_network=dead_network, once=once)
    _LIVE.append(proc)
    pump = _pump(proc, logs)
    result.restarts = 1
    deadline = time.monotonic() + duration_s
    kill_at = time.monotonic() + kill_after_s if kill_after_s else None
    killed = False

    try:
        while time.monotonic() < deadline:
            sample = _sample(data_dir)
            if sample is not None:
                result.samples.append(sample)
            if kill_at is not None and not killed and time.monotonic() >= kill_at:
                # Hard kill: no SIGTERM, no flush, no checkpoint - the state a
                # power cut or an OOM kill would leave behind.
                proc.kill()
                result.forced_kills += 1
                killed = True
                result.events.append(f"forced SIGKILL at +{duration_s - (deadline - time.monotonic()):.0f}s")
                break
            if proc.poll() is not None and not killed:
                result.events.append(f"process exited early with code {proc.returncode}")
                break
            time.sleep(2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc, timeout=60)
        pump.join(timeout=10.0)
        result.ended_at = datetime.now(tz=UTC)
    with contextlib.suppress(ValueError):
        _LIVE.remove(proc)
    return result


def _terminate(proc: subprocess.Popen, timeout: float = 60) -> None:
    """SIGTERM and wait - the graceful path the container's stop signal uses."""
    try:
        proc.send_signal(signal.SIGTERM)
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _report(phases: list[PhaseResult], data_dir: Path, logs: list[str]) -> int:
    print("\n" + "=" * 78)
    print("SOAK TEST REPORT")
    print("=" * 78)

    total_books = total_prices = 0
    max_contact_age = 0.0
    reconnects = errors = rest_failed = 0
    statuses: dict[str, int] = {}

    for phase in phases:
        # Counters live in the worker process, so they reset on every restart and
        # the first sample after a restart can still be the *previous* process's
        # heartbeat. Peaks are therefore reported per phase; the authoritative
        # totals come from the database at the end.
        if phase.samples:
            d_books = max(s.books for s in phase.samples)
            d_prices = max(s.prices for s in phase.samples)
            d_db = max(s.db_bytes for s in phase.samples)
            d_raw = max(s.raw_bytes for s in phase.samples)
        else:
            d_books = d_prices = d_db = d_raw = 0
        print(f"\n--- {phase.name} ---")
        print(f"  duration            {phase.duration_s:7.0f}s")
        print(f"  samples             {len(phase.samples):7d}")
        print(f"  books (peak)        {d_books:7d}")
        print(f"  price points (peak) {d_prices:7d}")
        print(f"  db size (peak)      {d_db / 1024:7.0f} KiB")
        print(f"  raw archive (peak)  {d_raw / 1024:7.0f} KiB")
        print(f"  restarts            {phase.restarts:7d}")
        print(f"  forced kills        {phase.forced_kills:7d}")
        for event in phase.events:
            print(f"  event: {event}")
        for sample in phase.samples:
            statuses[sample.status] = statuses.get(sample.status, 0) + 1
            if sample.contact_age_s is not None:
                max_contact_age = max(max_contact_age, sample.contact_age_s)
        if phase.samples:
            last = phase.samples[-1]
            total_books = max(total_books, last.books)
            total_prices = max(total_prices, last.prices)
            reconnects = max(reconnects, last.ws_reconnects)
            errors = max(errors, last.errors)
            rest_failed = max(rest_failed, last.rest_failed)

    print("\n--- overall ---")
    print(f"  books rows (cumulative)      {total_books}")
    print(f"  price points (cumulative)    {total_prices}")
    print(f"  websocket reconnects         {reconnects}")
    print(f"  error count                  {errors}")
    print(f"  failed REST requests         {rest_failed}")
    print(f"  max upstream contact age     {max_contact_age:.0f}s")
    print(f"  heartbeat statuses seen      {statuses}")

    # Final state assertions - these are the acceptance criteria, checked
    # against measured data rather than assumed.
    hb = _read_heartbeat(data_dir)
    failures: list[str] = []
    if hb is None:
        failures.append("no final heartbeat")
    else:
        from weatherquant.data.heartbeat import evaluate_health

        verdict = evaluate_health(data_dir / "health" / "heartbeat.json")
        print(f"  final health verdict         {verdict.status.value} (exit {verdict.exit_code}): {verdict.reasons}")
        if verdict.exit_code != 0:
            failures.append(f"final health verdict not clean: {verdict.reasons}")

    try:
        from weatherquant.storage import connect

        con = connect(data_dir / "weatherquant.duckdb", read_only=True)
        rows = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                for t in ("markets", "order_books", "price_history", "collector_runs", "collection_gaps")}
        open_gaps = con.execute("SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL").fetchone()[0]
        reasons = [r[0] for r in con.execute("SELECT DISTINCT reason FROM collection_gaps").fetchall()]
        run_statuses = [r[0] for r in con.execute("SELECT status FROM collector_runs").fetchall()]
        stopped = sum(1 for s in run_statuses if s == "stopped")
        con.close()
        print(f"  table row counts             {rows}")
        print(f"  collection gap reasons       {reasons}")
        print(f"  collector run statuses       {run_statuses}")
        print(f"  runs marked stopped          {stopped}")
        print(f"  gaps still open              {open_gaps}")
        if rows["markets"] < 1 or rows["order_books"] < 1:
            failures.append("collector produced no market or book rows")
        if open_gaps > 0:
            failures.append(
                f"{open_gaps} collection gap(s) left open - the graceful phase "
                "should have closed them"
            )
        if stopped < 1:
            failures.append(
                "no run recorded status='stopped' - the graceful shutdown path did not execute"
            )
        if "start_after_restart" not in reasons:
            failures.append("restarts were not recorded as gaps")
    except Exception as exc:
        failures.append(f"could not inspect database: {exc}")

    print("\n--- log tail ---")
    for line in logs[-25:]:
        print(f"  {line}")

    print("\n" + "=" * 78)
    if failures:
        print("RESULT: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASSED")
    print("=" * 78)
    return 0


def _schedule(total_s: float) -> tuple[float, float, float, float, float]:
    """Split the run into five phases with positive, non-overlapping lengths.

    Every phase has a minimum: an outage shorter than a couple of minutes never
    exercises the soft health limit, and a crash followed by an immediate
    shutdown proves nothing about gap closure. When the budget cannot afford all
    five, the optional outage drops first, then the crash choreography shrinks -
    but the final *graceful* phase never drops, because it is what leaves the
    database in a clean end state.
    """
    phase_e = 120.0   # graceful exit: closes every gap, marks the run 'stopped'
    phase_d = 180.0   # post-crash restart
    phase_b = min(240.0, max(0.0, total_s * 0.12))
    if phase_b < 90.0:
        phase_b = 0.0  # too short to be a meaningful outage

    fixed = phase_b + phase_d + phase_e
    remaining = total_s - fixed
    if remaining < 360.0:  # need at least ~3 min healthy + ~3 min crash
        phase_b = 0.0
        fixed = phase_d + phase_e
        remaining = total_s - fixed

    phase_a = remaining * 0.42
    phase_c = remaining - phase_a
    if phase_c < 180.0:
        phase_c = 180.0
        phase_a = remaining - phase_c
    if phase_a < 0.0:
        phase_a = 0.0
        phase_c = max(0.0, remaining)
    return phase_a, phase_b, phase_c, phase_d, phase_e


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="keep the data directory afterwards")
    args = parser.parse_args()

    data_dir: Path = args.data.resolve()
    if data_dir.exists():
        for _attempt in range(20):
            try:
                shutil.rmtree(data_dir)
                break
            except OSError:
                time.sleep(0.5)
        else:
            print(f"could not clear {data_dir}; is a previous run still holding it?", file=sys.stderr)
            return 2
    data_dir.mkdir(parents=True)

    total_s = args.minutes * 60.0
    phase_a, phase_b, phase_c, phase_d, phase_e = _schedule(total_s)

    logs: list[str] = []
    print(f"soak: {args.minutes:.0f} minutes over {data_dir}")
    print(f"  phase A healthy  {phase_a:6.0f}s")
    print(f"  phase B outage   {phase_b:6.0f}s")
    print(f"  phase C crash    {phase_c:6.0f}s (SIGKILL at {phase_c * 0.55:.0f}s)")
    print(f"  phase D restart  {phase_d:6.0f}s")
    print(f"  phase E graceful {phase_e:6.0f}s (clean --once exit)")

    phases = []
    if phase_a > 0:
        phases.append(run_phase("A healthy collection", data_dir, phase_a, logs=logs))
    if phase_b > 0:
        phases.append(run_phase("B network outage", data_dir, phase_b, dead_network=True, logs=logs))
    if phase_c > 0:
        # Crash mid-run; the next phase starts a fresh process which must close
        # the gap the crash left.
        phases.append(
            run_phase("C recovery + crash", data_dir, phase_c, kill_after_s=phase_c * 0.55, logs=logs)
        )
    if phase_d > 0:
        phases.append(run_phase("D post-crash restart", data_dir, phase_d, logs=logs))
    if phase_e > 0:
        # A run that exits on its own, exercising the *graceful* shutdown path
        # end to end: close this run's gap, flush, checkpoint, record
        # status='stopped'. On Windows no signal can be delivered to a child
        # process at all (verified: SIGTERM kills without invoking the handler),
        # so this is how the shutdown path is driven here. It is the same code
        # afterwards - a signal handler only sets the flag the `--once` exit
        # sets - so proving it here proves the path the Linux container takes.
        phases.append(run_phase("E graceful exit", data_dir, phase_e, once=True, logs=logs))

    code = _report(phases, data_dir, logs)
    if not args.keep and code == 0:
        shutil.rmtree(data_dir, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""WQN-2: the heartbeat recognizes a 120s block, and the container recovers.

Three claims, three groups of tests:

* **The probe's 120s threshold is exact** - 121s stale is unhealthy, 119s is
  not. The number is the ticket's; the boundary is where it lives.
* **An independent watchdog notices a blocked loop** - a thread that owes
  nothing to the loop publishes a final heartbeat naming the stuck loop and
  ends the process, which is the only signal Docker's restart policy can act
  on. The recovery test uses a separate process and the real watchdog exit.
* **The recovery does not duplicate writes** - a fault-blocked run that dies
  and a restarted run over the same data directory converge on the same
  facts: one book per content hash, one price row per natural key, every
  collection gap closed.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from test_collector_worker import BOOK_PAYLOAD, FULL_EVENT
from weatherquant.config import Settings
from weatherquant.data import worker as worker_module
from weatherquant.data.heartbeat import HealthStatus, evaluate_health, read_heartbeat
from weatherquant.data.watchdog import LoopWatchdog
from weatherquant.data.worker import CollectorWorker, ExitCode

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, **_kwargs: Any) -> None:
        self.last_contact_utc = datetime.now(tz=UTC)
        self.requests_ok = 0
        self.requests_failed = 0
        self.retries_performed = 0
        self.rate_limit_waits = 0

    def get_json(self, url: str, **_options: Any) -> Any:
        self.last_contact_utc = datetime.now(tz=UTC)
        self.requests_ok += 1
        if "/events/" in url:
            return FULL_EVENT
        if "events" in url or "public-search" in url:
            return [FULL_EVENT]
        if "/book" in url:
            return dict(BOOK_PAYLOAD)
        raise AssertionError(f"unexpected url: {url}")

    def post_json(self, url: str, **_kwargs: Any) -> Any:
        raise AssertionError(f"POST not expected: {url}")

    def touch(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_path=tmp_path / "wq.duckdb",
        raw_dir=tmp_path / "raw",
        station="KLGA",
    )


def _worker(settings: Settings, **kwargs: Any) -> CollectorWorker:
    kwargs.setdefault("run_id", "watchdog-test")
    kwargs.setdefault("stream_enabled", False)
    kwargs.setdefault("heartbeat_seconds", 0.05)
    kwargs.setdefault("tick_seconds", 0.02)
    kwargs.setdefault("book_poll_seconds", 0.05)
    kwargs.setdefault("discovery_seconds", 0.0)
    kwargs.setdefault("integrity_seconds", 0.0)
    kwargs.setdefault("parquet_daily", False)
    kwargs.setdefault("backup_daily", False)
    return CollectorWorker(settings, **kwargs)


def _arm_recorder(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], threading.Event]:
    """Replace the watchdog's process exit with a recorder the test can see."""
    real = worker_module.LoopWatchdog
    fired = threading.Event()
    messages: list[str] = []

    def patched(worker, **kwargs):
        kwargs["on_stuck"] = lambda message: (messages.append(message), fired.set())
        return real(worker, **kwargs)

    monkeypatch.setattr(worker_module, "LoopWatchdog", patched)
    return messages, fired


def _run_in_thread(worker: CollectorWorker) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    return thread, result


def _write_fault_marker(settings: Settings, seconds: float) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    (settings.db_path.parent / CollectorWorker.FAULT_BLOCK_MARKER).write_text(str(seconds))


# ---------------------------------------------------------------------------
# 1. the probe's 120-second boundary
# ---------------------------------------------------------------------------


def _beat_file(tmp_path: Path) -> Path:
    from weatherquant.data.heartbeat import Heartbeat, HeartbeatWriter

    path = tmp_path / "health" / "heartbeat.json"
    HeartbeatWriter(path).write(
        Heartbeat(
            status=HealthStatus.HEALTHY,
            run_id="r",
            pid=1,
            hostname="h",
            app_version="t",
            updated_at_utc=NOW,
            started_at_utc=NOW,
            uptime_seconds=1.0,
            last_upstream_contact_utc=NOW,
        )
    )
    return path


def test_a_121_second_old_heartbeat_is_stale_but_a_119_second_one_is_not(tmp_path: Path) -> None:
    path = _beat_file(tmp_path)

    stale = evaluate_health(path, now=NOW + timedelta(seconds=121), heartbeat_stale_seconds=120.0)
    assert stale.exit_code == 1
    assert stale.status is HealthStatus.UNHEALTHY
    assert any("stale" in r for r in stale.reasons)

    fresh = evaluate_health(path, now=NOW + timedelta(seconds=119), heartbeat_stale_seconds=120.0)
    assert fresh.exit_code == 0, fresh.reasons
    assert fresh.status is HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# 2. the watchdog
# ---------------------------------------------------------------------------


def test_a_declared_pause_is_allowed_and_an_undecided_one_is_not() -> None:
    """allow() is a budget, not a switch: overrunning it still fires."""
    watchdog = LoopWatchdog(object(), stuck_seconds=0.15, check_interval=0.02, on_stuck=lambda msg: None)
    fired: list[str] = []
    watchdog._on_stuck = fired.append
    watchdog.start()
    try:
        watchdog.allow(0.5)
        time.sleep(0.25)
        assert not watchdog.fired, "fired inside a declared, still-open pause"

        watchdog.allow(0.15)
        deadline = time.monotonic() + 5.0
        while not watchdog.fired and time.monotonic() < deadline:
            time.sleep(0.02)
        assert watchdog.fired, "a pause that overran its own budget never fired"
        assert "stuck" in fired[0]
    finally:
        watchdog.stop()


def test_a_tick_clears_an_open_declaration() -> None:
    watchdog = LoopWatchdog(object(), stuck_seconds=0.3, check_interval=0.02, on_stuck=lambda msg: None)
    watchdog.start()
    try:
        for _ in range(8):  # 8 * 0.05s = 0.4s > stuck threshold, but ticking
            watchdog.allow(10.0)  # a stale declaration from a previous job
            time.sleep(0.05)
            watchdog.tick()
        assert not watchdog.fired, "a ticking loop must never fire the watchdog"
    finally:
        watchdog.stop()


def test_probe_only_fault_blocks_once_without_a_watchdog(settings, monkeypatch):
    worker = _worker(settings, loop_stuck_seconds=0)
    _write_fault_marker(settings, 150)
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    worker._maybe_inject_fault_block()
    worker._maybe_inject_fault_block()
    assert sleeps == [150]


def test_a_blocked_loop_is_detected_and_the_final_heartbeat_names_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages, fired = _arm_recorder(monkeypatch)
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    _write_fault_marker(settings, 1.0)  # the loop blocks for 1.0s...

    worker = _worker(settings, run_id="blocked", loop_stuck_seconds=0.4)  # ...limit 0.4s
    thread, result = _run_in_thread(worker)
    try:
        assert fired.wait(10.0), "the watchdog never fired at a blocked loop"
        assert "stuck" in messages[0]

        # The independent observer sees it too: the diagnostic heartbeat says
        # unhealthy, with the reason in fatal_error.
        heartbeat = read_heartbeat(settings.db_path.parent / "health" / "heartbeat.json")
        assert heartbeat is not None
        assert str(heartbeat.status) == "unhealthy"
        assert heartbeat.fatal_error and "stuck" in heartbeat.fatal_error

        # The marker was consumed before blocking: a restart runs clean.
        assert not (settings.db_path.parent / CollectorWorker.FAULT_BLOCK_MARKER).exists()
    finally:
        worker.shutdown.set("test complete")
        thread.join(30.0)
    assert result.get("code") == ExitCode.OK

    con = duckdb.connect(str(settings.db_path))
    try:
        kinds = [
            row[0] for row in con.execute("SELECT kind FROM collector_events WHERE kind = 'fault_injection'").fetchall()
        ]
        assert kinds == ["fault_injection"], kinds
    finally:
        con.close()


def test_a_healthy_loop_never_fires(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _, fired = _arm_recorder(monkeypatch)
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    worker = _worker(settings, run_id="healthy", loop_stuck_seconds=0.5)
    thread, result = _run_in_thread(worker)
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            assert not fired.is_set(), "the watchdog fired on a healthy, ticking loop"
            time.sleep(0.1)
    finally:
        worker.shutdown.set("test complete")
        thread.join(30.0)
    assert result.get("code") == ExitCode.OK
    assert not fired.is_set()


# ---------------------------------------------------------------------------
# 3. the restart after the kill does not duplicate
# ---------------------------------------------------------------------------


def _run_blocked_process(settings: Settings) -> None:
    worker_module.ResilientHttp = FakeHttp
    raise SystemExit(_worker(settings, run_id="blocked", loop_stuck_seconds=0.4).run())


def test_the_run_after_a_watchdog_kill_writes_no_duplicates(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay the same upstream facts over a real unclean exit and restart."""
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="seed", once=True).run() == ExitCode.OK
    _write_fault_marker(settings, 10)
    child = multiprocessing.get_context("spawn").Process(target=_run_blocked_process, args=(settings,))
    child.start()
    try:
        child.join(30)
        assert child.exitcode == ExitCode.FATAL
    finally:
        if child.is_alive():
            child.kill()
            child.join()
    heartbeat = read_heartbeat(settings.db_path.parent / "health" / "heartbeat.json")
    assert heartbeat is not None and heartbeat.run_id == "blocked"
    assert "collection loop stuck" in (heartbeat.fatal_error or "")

    # Foreground restart over the same data directory; `once` collects a
    # couple of cycles and shuts down cleanly, as a supervised restart would.
    after = _worker(settings, run_id="run-2", loop_stuck_seconds=1.0, once=True)
    code = after.run()
    assert code == ExitCode.OK

    con = duckdb.connect(str(settings.db_path))
    try:
        runs = con.execute("SELECT run_id, status, stop_reason FROM collector_runs ORDER BY started_at_utc").fetchall()
        assert [(key, status) for key, status, _ in runs] == [
            ("seed", "stopped"), ("blocked", "failed"), ("run-2", "stopped"),
        ]
        assert "did not exit cleanly" in runs[1][2]

        # No gap left open: the restart dated its outage instead of hiding it.
        open_gaps = con.execute("SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL").fetchone()[0]
        assert open_gaps == 0

        # The same books seen by both runs are stored once each: the fake
        # upstream serves yes/no tokens, so two distinct books - and no more,
        # no matter how many times both runs polled them.
        assert con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] == 2

        # And the natural key of price history has no duplicates.
        dupes = con.execute(
            "SELECT COUNT(*) FROM (SELECT market_id, token_id, ts, source FROM price_history "
            "GROUP BY 1, 2, 3, 4 HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        assert dupes == 0
        rows = con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        assert rows >= 1, "the restarted run must keep collecting"
    finally:
        con.close()

    # The raw archive survived the kill and was continued: the restarted run
    # appended new payloads rather than rewriting the files. (archive_seq
    # continuity across restarts is WQN-21's territory, in its own PR.)
    raw_files = list(settings.raw_dir.rglob("*.jsonl"))
    assert raw_files, "the restarted run must keep archiving payloads"
    archived_runs = set()
    for path in raw_files:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert lines, f"{path} unexpectedly empty"
        for line in lines:
            archived_runs.add(json.loads(line)["run_id"])
    assert {"seed", "run-2"} <= archived_runs


def test_blocked_diagnostic_filesystem_cannot_prevent_watchdog_exit():
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    class Worker:
        def publish_stuck_heartbeat(self, message):
            entered.set()
            release.wait(5)

    watchdog = LoopWatchdog(Worker(), stuck_seconds=1, on_stuck=lambda message: exited.set())
    thread = threading.Thread(target=lambda: watchdog._fire(121), daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        assert exited.wait(2), "exit must not wait for the stalled heartbeat mount"
    finally:
        release.set()
        thread.join(timeout=2)

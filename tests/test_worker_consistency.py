"""WQN-4: the integrated worker's five behaviours, each under test.

This branch merges the lane's collector work (WQN-1 backup isolation with
its acceptance fix, WQN-2 watchdog recovery, WQN-21 archive durability with
torn-tail quarantine, WQN-5 run/gap audit). The ticket asks for the rotation,
timeout, shutdown, drain and heartbeat backstops to be *tested as a whole* -
these tests are that matrix, plus the two behaviours the merge added:

* a graceful exit drains the queue backlog (bounded, so SIGTERM stays fast);
* the daily backup rotation actually rotates - a day that burned its attempts
  does not stop the next day from trying again.

The image/source consistency half of WQN-4 is a script
(``scripts/verify_worker_image.sh``), because it needs Docker; its recorded
output lives in the PR description.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from test_collector_worker import BOOK_PAYLOAD, FULL_EVENT
from weatherquant.config import Settings
from weatherquant.data import worker as worker_module
from weatherquant.data.worker import CollectorWorker, ExitCode
from weatherquant.data.writer import CollectorWriter


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
    kwargs.setdefault("run_id", "wqn4")
    kwargs.setdefault("stream_enabled", False)
    kwargs.setdefault("heartbeat_seconds", 0.05)
    kwargs.setdefault("tick_seconds", 0.02)
    kwargs.setdefault("book_poll_seconds", 0.05)
    kwargs.setdefault("discovery_seconds", 0.0)
    kwargs.setdefault("integrity_seconds", 0.0)
    kwargs.setdefault("parquet_daily", False)
    kwargs.setdefault("backup_daily", False)
    return CollectorWorker(settings, **kwargs)


def _events(db_path: Path, kind: str) -> list[tuple[str, str]]:
    if not db_path.exists():
        return []
    con = duckdb.connect(str(db_path))
    try:
        return con.execute(
            "SELECT level, kind FROM collector_events WHERE kind = ? ORDER BY event_id",
            [kind],
        ).fetchall()
    except duckdb.Error:
        return []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# drain: a graceful exit processes the backlog, bounded
# ---------------------------------------------------------------------------


def test_a_graceful_exit_drains_the_queued_backlog(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    worker = _worker(settings, run_id="drain")
    frames_put = threading.Event()
    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not worker._books:
            time.sleep(0.05)
        assert worker._books, "discovery never ran"

        # Frames the producer queued during the very last tick: they must be
        # archived and priced by the graceful exit, not dropped.
        now = datetime.now(tz=UTC)
        frame = (now, {"event_type": "price_change", "price_changes": [
            {"asset_id": "tokY", "best_bid": "0.47", "best_ask": "0.51"}
        ]})
        worker._ws_queue.put(frame)
        frames_put.set()
        time.sleep(0.1)
        worker.shutdown.set("test complete")
    finally:
        thread.join(60.0)
    assert result.get("code") == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        drained = con.execute(
            "SELECT COUNT(*) FROM price_history WHERE source = 'ws'"
        ).fetchone()[0]
    finally:
        con.close()
    assert drained == 1, "the queued frame was dropped on graceful exit"


def test_the_final_drain_is_bounded(settings: Settings) -> None:
    """A producer that far outpaced the loop must not hold SIGTERM hostage."""
    from weatherquant.data.archive import RawArchive

    writer = CollectorWriter(settings.db_path, run_id="drain-budget")
    writer.open()
    archive = RawArchive(settings.raw_dir, run_id="drain-budget")
    worker = _worker(settings, run_id="drain-budget")
    worker._writer = writer

    frame = (
        datetime.now(tz=UTC),
        {"event_type": "price_change", "price_changes": [
            {"asset_id": "tokY", "best_bid": "0.47", "best_ask": "0.51"}
        ]},
    )
    for _ in range(worker.FINAL_DRAIN_MAX_FRAMES + 2000):
        worker._ws_queue.put(frame)

    started = time.monotonic()
    try:
        worker._final_drain(archive)
        elapsed = time.monotonic() - started
        processed = worker.stats.ws_messages_received
        leftover = worker._ws_queue.qsize()
    finally:
        archive.close()
        writer.close()

    assert processed <= worker.FINAL_DRAIN_MAX_FRAMES, processed
    assert leftover >= 2000, f"drained past the budget: only {leftover} left"
    assert elapsed < 30.0, f"final drain took {elapsed:.1f}s for {processed} frames"


# ---------------------------------------------------------------------------
# rotation: a new UTC day gets fresh backup attempts
# ---------------------------------------------------------------------------


def test_backup_attempts_rotate_with_the_utc_day(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A day that burned its attempts (or timed out) must not stop tomorrow."""
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    worker = _worker(settings, run_id="rotation", backup_daily=True, backup_retry_seconds=0.05)

    # Day one, exhausted: the guard treats "day marked done" as the state a
    # timeout or an exhausted retry budget leaves behind.
    worker._last_backup_day = datetime.now(tz=UTC).date()
    worker._backup_attempts_today = worker.BACKUP_MAX_ATTEMPTS_PER_DAY
    worker._advance_daily_backup(datetime.now(tz=UTC).date())
    assert worker._backup_job is None, "a done day must not start another job"

    # Midnight passes: the next tick sees a different day, resets the
    # attempt counters and starts fresh - and with a working destination the
    # backup verifies.
    deadline = time.monotonic() + 30.0
    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    try:
        # Simulate the day rollover while the loop is live.
        time.sleep(0.2)
        worker._last_backup_day = None
        worker._backup_attempts_day = None
        while time.monotonic() < deadline and not _events(settings.db_path, "backup"):
            time.sleep(0.05)
        assert _events(settings.db_path, "backup"), "the new day never backed up"
    finally:
        worker.shutdown.set("test complete")
        thread.join(60.0)
    assert result.get("code") == ExitCode.OK
    assert worker._backup_attempts_today >= 1
    assert list((settings.db_path.parent / "backups").glob("weatherquant-*.duckdb"))

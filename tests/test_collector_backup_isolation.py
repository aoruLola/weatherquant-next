"""WQN-1: the daily backup must not stall collection.

The failure this file pins down: ``backup()`` used to run inline on the loop
thread, so a copy crawling over FUSE stopped the heartbeats, the polls and the
stream drain for as long as it took. The Docker probe then declared the
collector unhealthy while the process was merely busy - and a kill during that
window is what actually tore data.

Every test here injects the fault the ticket names - slow copy, hung copy,
failed rename, failed start - and asserts the two acceptance properties:
collection continues, and whatever backup is published is complete.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from weatherquant.config import Settings
from weatherquant.data import worker as worker_module
from weatherquant.data.heartbeat import read_heartbeat
from weatherquant.data.worker import CollectorWorker, ExitCode
from weatherquant.data.writer import CollectorWriter, book_content_hash, canonical_levels_json

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 10)


# ---------------------------------------------------------------------------
# Fixtures: a small populated database, and a fake upstream for the worker.
# ---------------------------------------------------------------------------


def _event() -> tuple:
    return ("evt-1", "slug", "Highest temperature in NYC", TODAY, "NWS", "rules", NOW, "{}", "gamma", "run-1")


def _market() -> tuple:
    return (
        "mkt-1", "evt-1", "cond-1", "q?", "60-61", "range", "degF", 60.0, 61.0, True, True,
        "nearest_integer_f", "tokY", "tokN", TODAY, "KLGA", 0.01, 5.0, 0.05, 1.0,
        "NWS", "rules", True, False, True, NOW, "{}", "gamma", "run-1",
    )


def _book(best_bid: float = 0.48, fetched_at: datetime = NOW) -> tuple:
    """One book row. ``best_bid`` varies the *content*, so two calls with
    different prices are two observations, not one deduplicated repeat."""
    bids = canonical_levels_json([(best_bid, 100.0), (round(best_bid - 0.03, 4), 400.0)])
    asks = canonical_levels_json([(round(best_bid + 0.04, 4), 100.0), (round(best_bid + 0.09, 4), 400.0)])
    digest = book_content_hash("tokY", bids, asks, None)
    return (
        digest, "mkt-1", "tokY", bids, asks, best_bid, round(best_bid + 0.04, 4), 0.04,
        best_bid * 100.0, (best_bid + 0.04) * 100.0, "",
        None, fetched_at, "rest", "missing", digest, "ok", "run-1",
    )


@pytest.fixture
def writer(tmp_path: Path) -> CollectorWriter:
    w = CollectorWriter(tmp_path / "wq.duckdb", run_id="run-1")
    w.open()
    w.stage_event(_event())
    w.stage_market(_market())
    w.stage_book(_book())
    w.flush(force=True)
    yield w
    w.close()


FULL_EVENT: dict[str, Any] = {
    "id": "7001",
    "slug": "highest-temperature-in-nyc-on-october-11-2026",
    "title": "Highest temperature in NYC on October 11, 2026",
    "eventDate": "2026-10-11",
    "seriesSlug": "nyc-daily-weather",
    "markets": [
        {
            "id": "m1",
            "conditionId": "0xc1",
            "question": "Will the highest temperature in NYC be 60-61\u00b0F?",
            "groupItemTitle": "60-61\u00b0F",
            "outcomes": '["Yes","No"]',
            "clobTokenIds": '["tokY","tokN"]',
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "orderPriceMinTickSize": 0.001,
            "orderMinSize": 5.0,
            "feeSchedule": {"rate": 0.05, "exponent": 1},
        }
    ],
}

BOOK_PAYLOAD = {
    "market": "0xc1",
    "asset_id": "tokY",
    "timestamp": "1791605394391",
    "bids": [{"price": "0.48", "size": "100"}],
    "asks": [{"price": "0.52", "size": "100"}],
}


class FakeHttp:
    def __init__(self, **_kwargs: Any) -> None:
        self.last_contact_utc: datetime | None = None
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
        self.last_contact_utc = datetime.now(tz=UTC)

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


def _events(db_path: Path, kind: str) -> list[tuple[str, str]]:
    """Committed collector_events rows of one kind, read while the worker runs.

    Read-write, not read-only: the worker's process holds the database open
    read-write, and DuckDB refuses a second, differently-configured handle to
    the same file within one process. A matching config reuses the instance.
    """
    if not db_path.exists():
        return []
    con = duckdb.connect(str(db_path))
    try:
        return con.execute(
            "SELECT level, kind FROM collector_events WHERE kind = ? ORDER BY event_id",
            [kind],
        ).fetchall()
    except duckdb.Error:
        # Migrations are still applying - nothing has been committed yet.
        return []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Writer level: the job itself
# ---------------------------------------------------------------------------


def test_an_off_thread_backup_is_verified_and_restorable(writer: CollectorWriter, tmp_path: Path) -> None:
    job = writer.start_backup(tmp_path / "backups")
    job.join(60.0)
    report = job.poll()
    assert report is not None and report.ok, report and report.error
    assert report.destination.exists()
    assert report.row_counts == report.expected_counts

    # Restorable: a fresh writer must accept the copy and take new writes.
    restored = tmp_path / "restored.duckdb"
    restored.write_bytes(report.destination.read_bytes())
    w2 = CollectorWriter(restored, run_id="run-restore")
    w2.open()
    w2.log_event("info", "restore", "post-restore event")
    w2.flush(force=True)
    assert w2.integrity_check().ok
    w2.close()


def test_collection_writes_commit_while_a_backup_is_in_flight(
    writer: CollectorWriter, tmp_path: Path
) -> None:
    """The single-writer lock must isolate, not interlock.

    While the copy is blocked on its hook, the writer's own connection keeps
    committing; the copy's snapshot simply predates those commits.
    """
    in_copy = threading.Event()
    release = threading.Event()
    job = writer.start_backup(
        tmp_path / "backups",
        before_copy=lambda: (in_copy.set(), release.wait(10))[1],
    )
    assert in_copy.wait(5.0), "the copy hook never ran"

    before = writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
    writer.stage_book(_book(best_bid=0.47))
    writer.flush(force=True)  # must not raise, block, or corrupt either side
    during = writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
    assert during == before + 1, "the writer must keep committing during the copy"

    release.set()
    job.join(60.0)
    report = job.poll()
    assert report is not None and report.ok, report and report.error
    # The copy is one MVCC point: whatever snapshot it took, the file it
    # published agrees with the counts it expected, exactly. A copy that
    # mixed two instants would differ here and restore inconsistent.
    assert report.row_counts == report.expected_counts
    assert report.expected_counts["order_books"] >= before
    # The main database, meanwhile, holds both commits.
    after = writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
    assert after == before + 1


def test_a_timed_out_backup_is_abandoned_and_discards_its_own_file(
    writer: CollectorWriter, tmp_path: Path
) -> None:
    release = threading.Event()
    job = writer.start_backup(
        tmp_path / "backups",
        timeout_seconds=0.05,
        before_copy=lambda: release.wait(10),
    )
    deadline = time.monotonic() + 5.0
    while not job.expired() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.expired(), "the job never noticed its own deadline"
    assert job.abandon("test timeout"), "an expired running job must be abandonable"

    release.set()
    job.join(60.0)
    report = job.poll()
    assert report is not None
    assert not report.ok
    assert "abandoned" in (report.error or "")
    # The thread completed its copy, verified it even - and then threw it away
    # rather than publish a file its owner had given up on.
    assert not report.destination.exists()
    leftovers = [p for p in (tmp_path / "backups").iterdir() if p.name.startswith(".incomplete-")]
    assert leftovers == []
    # The next day's job is not blocked by the abandoned alias.
    again = writer.start_backup(tmp_path / "backups")
    again.join(60.0)
    second = again.poll()
    assert second is not None and second.ok, second and second.error


def test_abandoning_a_job_that_already_finished_keeps_the_backup(
    writer: CollectorWriter, tmp_path: Path
) -> None:
    job = writer.start_backup(tmp_path / "backups")
    job.join(60.0)
    assert job.done
    assert job.abandon("too late") is False
    report = job.poll()
    assert report is not None and report.ok
    assert report.destination.exists()


def test_publication_is_ordered_after_durability(
    writer: CollectorWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync(tmp) -> rename -> fsync(dir), and nothing else may publish."""
    order: list[str] = []
    real_replace = os.replace

    def traced_replace(source: Any, target: Any) -> Any:
        order.append(f"replace:{Path(source).name}->{Path(target).name}")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", traced_replace)

    import weatherquant.data.writer as writer_module

    real_fsync_file = writer_module.fsync_file
    real_fsync_dir = writer_module.fsync_directory
    writer_module.fsync_file = lambda path: (order.append(f"fsync:{Path(path).name}"), real_fsync_file(path))[1]
    writer_module.fsync_directory = lambda path: (order.append("fsync:dir"), real_fsync_dir(path))[1]
    try:
        job = writer.start_backup(tmp_path / "backups")
        job.join(60.0)
    finally:
        writer_module.fsync_file = real_fsync_file
        writer_module.fsync_directory = real_fsync_dir

    report = job.poll()
    assert report is not None and report.ok, report and report.error
    tmp_name = order[0].split(":", 1)[1] if order and order[0].startswith("fsync:") else None
    assert tmp_name and tmp_name.startswith(".incomplete-"), order
    renames = [step for step in order if step.startswith("replace:")]
    assert len(renames) == 1, order
    assert order == [
        f"fsync:{tmp_name}",
        renames[0],
        "fsync:dir",
    ], "bytes must be durable before the name exists, and the name flushed after"


def test_a_failed_rename_publishes_nothing(writer: CollectorWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FUSE can fail the rename itself; the report must say so, honestly."""
    def broken_replace(source: Any, target: Any) -> Any:
        raise OSError("injected EIO on rename")

    monkeypatch.setattr(os, "replace", broken_replace)
    job = writer.start_backup(tmp_path / "backups")
    job.join(60.0)
    report = job.poll()
    assert report is not None
    assert not report.ok
    assert "EIO" in (report.error or "")
    assert not report.destination.exists()
    leftovers = [p for p in (tmp_path / "backups").iterdir() if p.name.startswith(".incomplete-")]
    assert leftovers == []


def test_the_next_backup_sweeps_tmp_files_a_crash_left_behind(
    writer: CollectorWriter, tmp_path: Path
) -> None:
    dest = tmp_path / "backups"
    dest.mkdir()
    stale = dest / ".incomplete-20260101T000000Z-deadbeef.duckdb"
    stale.write_bytes(b"torn bytes from a killed process")
    report = writer.backup(dest)
    assert report.ok, report.error
    assert not stale.exists(), "a .incomplete- file is never a backup; it is litter"
    assert report.destination.exists()


def test_the_synchronous_api_still_blocks_until_verified(writer: CollectorWriter, tmp_path: Path) -> None:
    report = writer.backup(tmp_path / "backups")
    assert report.ok, report.error
    assert report.verified
    assert report.row_counts == report.expected_counts


# ---------------------------------------------------------------------------
# Worker level: the loop, with faults injected
# ---------------------------------------------------------------------------


def _start_worker(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> CollectorWorker:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    worker = CollectorWorker(
        settings,
        run_id=f"run-{kwargs.pop('run_label', 'x')}",
        stream_enabled=False,
        heartbeat_seconds=kwargs.pop("heartbeat_seconds", 0.05),
        tick_seconds=kwargs.pop("tick_seconds", 0.02),
        book_poll_seconds=kwargs.pop("book_poll_seconds", 0.05),
        discovery_seconds=kwargs.pop("discovery_seconds", 0.0),
        integrity_seconds=kwargs.pop("integrity_seconds", 0.0),
        parquet_daily=False,
        backup_daily=True,
        backup_retry_seconds=kwargs.pop("backup_retry_seconds", 3600.0),
        backup_timeout_seconds=kwargs.pop("backup_timeout_seconds", 60.0),
        **kwargs,
    )
    return worker


def _heartbeat_state(settings: Settings) -> tuple[datetime, int]:
    """(heartbeat timestamp, REST requests so far) - the two liveness proofs."""
    heartbeat = read_heartbeat(settings.db_path.parent / "health" / "heartbeat.json")
    assert heartbeat is not None
    return heartbeat.updated_at_utc, heartbeat.stats.rest_requests_ok


def test_the_loop_keeps_collecting_while_a_backup_copies(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WQN-1 acceptance: with the copy deliberately stalled, heartbeats and
    polls continue; once released, the published backup is complete."""
    backup_started = threading.Event()
    release = threading.Event()
    real_start = CollectorWriter.start_backup

    def stalled_start(self: CollectorWriter, dest_dir: Any, **kwargs: Any) -> Any:
        backup_started.set()
        return real_start(self, dest_dir, **kwargs, before_copy=lambda: release.wait(30))

    monkeypatch.setattr(CollectorWriter, "start_backup", stalled_start)

    # Seed one committed set of rows so "what the backup must contain" is
    # deterministic: rows committed before the worker starts are in *every*
    # snapshot, whatever the batch timing during the run.
    seed = CollectorWriter(settings.db_path, run_id="seed")
    seed.open()
    seed.stage_event(_event())
    seed.stage_market(_market())
    seed.stage_book(_book())
    seed.flush(force=True)
    seed.close()

    worker = _start_worker(settings, monkeypatch, run_label="isolation")
    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    try:
        assert backup_started.wait(10.0), "the daily backup never started"
        samples: list[tuple[datetime, int]] = []
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            hb = read_heartbeat(settings.db_path.parent / "health" / "heartbeat.json")
            if hb is not None:
                samples.append((hb.updated_at_utc, hb.stats.rest_requests_ok))
            time.sleep(0.1)
        during = [s for s in samples]
        stamps = [s[0] for s in during]
        assert len(set(stamps)) >= 5, f"heartbeat froze during the backup copy: {during}"
        assert stamps == sorted(stamps), "heartbeat timestamps must move forward"
        assert during[-1][1] > during[0][1], "polls stopped while the backup copied"
    finally:
        release.set()
        worker.shutdown.set("test complete")
        thread.join(60.0)

    assert result.get("code") == ExitCode.OK
    assert ("info", "backup") in _events(settings.db_path, "backup")
    assert ("info", "backup_started") in _events(settings.db_path, "backup_started")

    # 备份恢复完整: the published copy holds everything committed before the
    # worker began, plus the discovery cycle flushed inline ahead of the copy.
    # (Rows still sitting in the writer's in-memory batch at snapshot time are
    # the *next* day's backup - comparing the live-at-shutdown database against
    # the snapshot would compare two different MVCC points.)
    backups = list((settings.db_path.parent / "backups").glob("weatherquant-*.duckdb"))
    assert backups, "a verified backup file must exist after the run"
    con = duckdb.connect(str(backups[0]), read_only=True)
    try:
        live = duckdb.connect(str(settings.db_path))
        try:
            for table in ("events", "markets"):
                got = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                want = live.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                assert got == want, f"{table}: backup {got} vs live {want}"
            seeded = con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
            assert seeded >= 1, "the pre-seeded book must be in every backup"
        finally:
            live.close()
    finally:
        con.close()


def test_a_failed_backup_is_recorded_and_retried_the_same_day(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup that cannot even start (full disk, lost mount) is an event,
    not a crash - and it does not burn the whole day."""
    real_start = CollectorWriter.start_backup
    attempts: list[int] = []

    def flaky_start(self: CollectorWriter, dest_dir: Any, **kwargs: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("injected ENOSPC on the backups mount")
        return real_start(self, dest_dir, **kwargs)

    monkeypatch.setattr(CollectorWriter, "start_backup", flaky_start)
    worker = _start_worker(settings, monkeypatch, run_label="retry", backup_retry_seconds=0.2)

    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if _events(settings.db_path, "backup"):
                break  # the retry succeeded and logged the verified event
            time.sleep(0.05)
    finally:
        worker.shutdown.set("test complete")
        thread.join(60.0)

    assert result.get("code") == ExitCode.OK
    failed = _events(settings.db_path, "backup_failed")
    assert failed and failed[0] == ("error", "backup_failed"), failed
    assert _events(settings.db_path, "backup_started"), "the retry must run the same day"
    assert _events(settings.db_path, "backup"), "the retry must publish a verified backup"
    assert len(attempts) >= 2
    assert (settings.db_path.parent / "backups").glob("weatherquant-*.duckdb")


def test_a_hung_backup_times_out_and_the_day_is_not_retry_burned(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy stuck past its deadline is abandoned loudly; the loop lives on
    and no second copy is stacked behind the stuck one."""
    release = threading.Event()
    real_start = CollectorWriter.start_backup

    def hung_start(self: CollectorWriter, dest_dir: Any, **kwargs: Any) -> Any:
        return real_start(self, dest_dir, **kwargs, before_copy=lambda: release.wait(30))

    monkeypatch.setattr(CollectorWriter, "start_backup", hung_start)
    worker = _start_worker(
        settings, monkeypatch, run_label="timeout", backup_timeout_seconds=0.1
    )

    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if _events(settings.db_path, "backup_timeout"):
                break
            time.sleep(0.05)
        assert _events(settings.db_path, "backup_timeout"), "the watchdog must fire"
        # The loop is alive *after* the timeout: the heartbeat keeps moving.
        first = _heartbeat_state(settings)
        time.sleep(0.3)
        second = _heartbeat_state(settings)
        assert second[0] > first[0], "the loop died with the backup"
    finally:
        release.set()
        worker.shutdown.set("test complete")
        thread.join(60.0)

    assert result.get("code") == ExitCode.OK
    # Only one attempt: a timeout means no same-day retry.
    starts = _events(settings.db_path, "backup_started")
    assert len(starts) == 1, starts
    time.sleep(0.2)
    assert not list((settings.db_path.parent / "backups").glob("weatherquant-*.duckdb")), (
        "an abandoned backup must never publish"
    )

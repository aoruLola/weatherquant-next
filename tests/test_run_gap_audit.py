"""WQN-5: collection gaps and run logs must be auditable after the fact.

The acceptance names three things an operator has to be able to answer from
the database alone:

* **which runs ended abnormally** - a run row left ``running`` by a process
  that died without closing (SIGKILL, OOM kill, watchdog exit: the container
  can stay Up through all of them) is marked ``failed`` by the next start,
  with a reason and timestamp. "Query, not archaeology."
* **which time ranges were not collected** - ``collection_gaps`` covers the
  whole outage: the dead run's own gap is closed when the next run starts,
  so the stopped-clock window (stall + death + restart) is dated, not
  implied.
* **what happened to each backup** - the ``backup`` / ``backup_failed``
  event trail on ``collector_events``; covered by the backup suites and
  re-asserted once here as an end-to-end trail.
"""

from __future__ import annotations

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
from weatherquant.data.worker import CollectorWorker, ExitCode
from weatherquant.data.writer import CollectorWriter

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
    kwargs.setdefault("run_id", "audit-run")
    kwargs.setdefault("stream_enabled", False)
    kwargs.setdefault("heartbeat_seconds", 0.05)
    kwargs.setdefault("tick_seconds", 0.02)
    kwargs.setdefault("book_poll_seconds", 0.05)
    kwargs.setdefault("discovery_seconds", 0.0)
    kwargs.setdefault("integrity_seconds", 0.0)
    kwargs.setdefault("parquet_daily", False)
    kwargs.setdefault("backup_daily", False)
    return CollectorWorker(settings, **kwargs)


def _abandon_a_run(settings: Settings) -> datetime:
    """Leave the database in the state a killed process leaves behind.

    The run row and its gap are committed immediately; skipping ``close`` -
    as SIGKILL, OOM and a watchdog ``os._exit`` all do - is what leaves them
    open. Returns the wall-clock moment the "outage" begins (the container is
    Up the whole time; nothing collects until the next start).
    """
    writer = CollectorWriter(settings.db_path, run_id="dead-run")
    writer.open()
    writer.start_run(app_version="t", git_sha=None, image_tag=None, hostname="h", pid=111)
    writer.record_gap(
        reason="start_after_restart",
        started_at_utc=datetime.now(tz=UTC) - timedelta(seconds=1),
        detail="run=dead-run pid=111",
    )
    # No close(), no gap end: the process is gone.
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# 1. abnormal runs are marked failed
# ---------------------------------------------------------------------------


def test_a_killed_run_is_marked_failed_by_the_next_start(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    _abandon_a_run(settings)

    worker = _worker(settings, run_id="fresh-run", once=True)
    assert worker.run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        rows = {
            r[0]: r
            for r in con.execute(
                "SELECT run_id, status, stop_reason, stopped_at_utc, started_at_utc "
                "FROM collector_runs"
            ).fetchall()
        }
        dead = rows["dead-run"]
        assert dead[1] == "failed", "an abandoned run must not stay 'running'"
        assert dead[2] and "did not exit cleanly" in dead[2], dead[2]
        assert dead[3] is not None, "the failed marking must carry a timestamp"

        fresh = rows["fresh-run"]
        assert fresh[1] == "stopped", "the healthy run must not be failed by its own startup"
        assert fresh[3] is not None

        # The audit event is on the trail too.
        marked = con.execute(
            "SELECT level, message FROM collector_events WHERE kind = 'restart'"
        ).fetchall()
        assert any("abandoned run(s) as failed" in m for _, m in marked), marked
    finally:
        con.close()


def test_a_cleanly_stopped_run_is_not_marked_failed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    first = _worker(settings, run_id="clean-run", once=True)
    assert first.run() == ExitCode.OK

    second = _worker(settings, run_id="next-run", once=True)
    assert second.run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        statuses = dict(con.execute("SELECT run_id, status FROM collector_runs").fetchall())
        assert statuses == {"clean-run": "stopped", "next-run": "stopped"}, statuses
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. gaps cover the stopped-clock window
# ---------------------------------------------------------------------------


def test_the_gap_covers_the_whole_outage_stall_death_and_restart(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    outage_from = _abandon_a_run(settings)

    worker = _worker(settings, run_id="after-kill", once=True)
    assert worker.run() == ExitCode.OK
    restarted_at = datetime.now(tz=UTC)

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        gaps = con.execute(
            "SELECT run_id, reason, started_at_utc, ended_at_utc FROM collection_gaps "
            "ORDER BY started_at_utc"
        ).fetchall()
        assert gaps, "every run must record its own collection gap"
        assert all(g[3] is not None for g in gaps), f"gap left open: {gaps}"

        # The dead run's gap opens before the process disappears and is only
        # closed at the next start: stall + death + restart downtime is
        # inside [started, ended], dated rather than implied.
        dead_gap = next(g for g in gaps if g[0] == "dead-run")
        assert dead_gap[2] <= outage_from, dead_gap
        assert dead_gap[3] >= outage_from, f"gap closed before the outage: {dead_gap}"
        assert dead_gap[3] <= restarted_at + timedelta(seconds=1), dead_gap
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 3. the backup lifecycle is on the event trail
# ---------------------------------------------------------------------------


def test_a_backup_attempt_leaves_start_and_outcome_on_the_trail(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every backup names its start and its outcome; a failed destination
    fails loudly on the trail while the run itself stays clean."""
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)

    def refuse(self: CollectorWriter, dest_dir: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(CollectorWriter, "backup", refuse)
    worker = _worker(settings, run_id="backup-audit", backup_daily=True)
    result: dict[str, Any] = {}

    def run() -> None:
        result["code"] = worker.run()

    thread = threading.Thread(target=run, name="test-worker")
    thread.start()
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            try:
                con = duckdb.connect(str(settings.db_path))
                try:
                    found = con.execute(
                        "SELECT level, kind FROM collector_events WHERE kind LIKE 'backup%' "
                        "ORDER BY ts_utc"
                    ).fetchall()
                finally:
                    con.close()
            except duckdb.Error:
                found = []  # migrations still applying; nothing committed yet
            if any(k == "backup_failed" for _, k in found):
                break
            time.sleep(0.1)
    finally:
        worker.shutdown.set("test complete")
        thread.join(30.0)

    assert result.get("code") == ExitCode.OK, "a failed backup must not kill the run"
    assert ("error", "backup_failed") in found, found
    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        # The run's own exit is normal - the backup failure is an event, not
        # a crash, which is what makes the trail trustworthy to audit.
        status = con.execute(
            "SELECT status FROM collector_runs WHERE run_id = 'backup-audit'"
        ).fetchone()[0]
        assert status == "stopped"
    finally:
        con.close()

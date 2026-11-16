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

import json
import subprocess
import sys
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
                "SELECT run_id, status, stop_reason, stopped_at_utc, started_at_utc FROM collector_runs"
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
        marked = con.execute("SELECT level, message FROM collector_events WHERE kind = 'restart'").fetchall()
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
            "SELECT run_id, reason, started_at_utc, ended_at_utc FROM collection_gaps ORDER BY started_at_utc"
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


def test_close_open_gaps_returns_the_affected_row_count(settings: Settings) -> None:
    """DuckDB 1.5.6 returns affected rows for UPDATE without RETURNING.

    The plain two-row path also accepts the old RETURNING form. The separate
    spawn-based watchdog recovery test is the WAL regression: with RETURNING
    it fails on DuckDB 1.5.6 while rebuilding the gap table's indexes.
    """
    writer = CollectorWriter(settings.db_path, run_id="gap-count")
    writer.open()
    try:
        for _ in range(2):
            writer.record_gap(
                reason="start_after_restart",
                started_at_utc=datetime.now(tz=UTC),
                detail="open",
            )
        assert writer.close_open_gaps(detail="recovered") == 2
        gaps = writer.con.execute("SELECT ended_at_utc, detail FROM collection_gaps").fetchall()
        assert len(gaps) == 2
        # WQN-5: closing appends the closing reason to the opening evidence.
        assert all(ended_at is not None and detail == "open; recovered" for ended_at, detail in gaps)
        assert writer.close_open_gaps(detail="nothing to close") == 0
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 3. the backup lifecycle is on the event trail
# ---------------------------------------------------------------------------


def test_a_backup_attempt_leaves_start_and_outcome_on_the_trail(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every backup names its start and its outcome; a failed destination
    fails loudly on the trail while the run itself stays clean."""
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)

    def refuse(self: CollectorWriter, dest_dir: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    # The integrated worker starts backups via start_backup (WQN-1); failing
    # it at the source is the same audit event either way (WQN-5).
    monkeypatch.setattr(CollectorWriter, "start_backup", refuse)
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
                        "SELECT level, kind FROM collector_events WHERE kind LIKE 'backup%' ORDER BY ts_utc"
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
        trail = con.execute(
            "SELECT kind, details_json FROM collector_events WHERE kind LIKE 'backup%' ORDER BY ts_utc"
        ).fetchall()
        assert [kind for kind, _ in trail] == ["backup_started", "backup_failed"]
        started, failed = (json.loads(details) for _, details in trail)
        assert started["attempt_id"] == failed["attempt_id"] and started["attempt_id"]
        assert started["outcome"] == "started" and started["destination"].endswith("backups")
        assert failed["outcome"] == "failed" and "No space left" in failed["error"]
        # The run's own exit is normal - the backup failure is an event, not
        # a crash, which is what makes the trail trustworthy to audit.
        status = con.execute("SELECT status FROM collector_runs WHERE run_id = 'backup-audit'").fetchone()[0]
        assert status == "stopped"
    finally:
        con.close()


def test_clean_restart_records_downtime_before_new_process(settings, monkeypatch):
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    first = _worker(settings, run_id="first", once=True)
    assert first.run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        stopped = con.execute("SELECT stopped_at_utc FROM collector_runs").fetchone()[0]
        # The very first start has no earlier run to measure an interruption
        # from, so it must not invent one.
        assert con.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0] == 0
    second = _worker(settings, run_id="second", once=True)
    assert second.run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        start, end = con.execute(
            "SELECT started_at_utc, ended_at_utc FROM collection_gaps WHERE run_id='second'"
        ).fetchone()
    assert start == stopped < second.started_at <= end


def test_offline_starts_keep_the_same_gap_until_contact(settings, monkeypatch):
    class OfflineHttp(FakeHttp):
        def get_json(self, url, **options):
            raise RuntimeError("offline")

    monkeypatch.setattr(worker_module, "ResilientHttp", OfflineHttp)
    for run_id in ("offline-1", "offline-2"):
        assert _worker(settings, run_id=run_id, once=True).run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        # The first start has nothing to date. The second opens one gap that the
        # later offline start must reuse rather than stack a second marker.
        assert con.execute("SELECT run_id, ended_at_utc FROM collection_gaps").fetchall() == [("offline-2", None)]
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="resumed", once=True).run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        assert con.execute("SELECT ended_at_utc IS NOT NULL FROM collection_gaps").fetchall() == [(True,)]


def test_legacy_open_gap_uses_last_contact_and_backup_outcome_is_recovered(settings, monkeypatch):
    from weatherquant.data.heartbeat import CollectionStats, HealthStatus, Heartbeat, HeartbeatWriter

    started = datetime.now(tz=UTC) - timedelta(hours=5)
    contact = started + timedelta(hours=4)
    w = CollectorWriter(settings.db_path, run_id="dead")
    w.open()
    w.start_run(app_version="test", git_sha=None, image_tag=None, hostname="test", pid=1)
    w.con.execute("UPDATE collector_runs SET started_at_utc=?", [started])
    legacy_id = w.record_gap(reason="start_after_restart", started_at_utc=started, detail="legacy marker")
    for kind, attempt in [("backup_started", "one"), ("backup_failed", "one"), ("backup_started", "two")]:
        w.log_event("info", kind, kind, {"attempt_id": attempt}, immediate=True)
    w.con.close()  # leave the run open, as it would be after a hard stop
    HeartbeatWriter(settings.db_path.parent / "health" / "heartbeat.json").write(
        Heartbeat(
            status=HealthStatus.UNHEALTHY,
            run_id="dead",
            pid=1,
            hostname="test",
            app_version="test",
            started_at_utc=started,
            updated_at_utc=datetime.now(tz=UTC),
            uptime_seconds=18000,
            last_upstream_contact_utc=contact,
            stats=CollectionStats(rest_requests_ok=1),
        )
    )
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="back", once=True).run() == ExitCode.OK
    assert _worker(settings, run_id="later", once=True).run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        since, detail = con.execute("SELECT started_at_utc, detail FROM collection_gaps WHERE run_id='dead'").fetchone()
        assert since == contact and legacy_id in detail and "legacy marker" in detail
        assert con.execute(
            "SELECT details_json->>'outcome', details_json->>'attempt_id' "
            "FROM collector_events WHERE kind='backup_interrupted'"
        ).fetchall() == [("interrupted", "two")]


def test_recover_legacy_wal_using_committed_price_when_heartbeat_is_missing(settings, monkeypatch):
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="seed", once=True).run() == ExitCode.OK
    script = """
import os, sys
from datetime import UTC, datetime
from pathlib import Path
from weatherquant.data.writer import CollectorWriter

w = CollectorWriter(Path(sys.argv[1]), run_id="legacy")
w.open()
w.start_run(app_version="old", git_sha=None, image_tag=None, hostname="test", pid=os.getpid())
start = datetime.now(tz=UTC)
w.record_gap(reason="start_after_restart", started_at_utc=start, detail="legacy marker")
observed = datetime.now(tz=UTC)
w.stage_price(("legacy-market", "legacy-token", observed, 0.5, "rest", observed, "venue", "legacy"))
w.flush(force=True)
os._exit(77)
"""
    child = subprocess.run([sys.executable, "-c", script, str(settings.db_path)], timeout=30)
    assert child.returncode == 77
    assert Path(str(settings.db_path) + ".wal").stat().st_size > 0
    (settings.db_path.parent / "health" / "heartbeat.json").unlink()
    recovered = _worker(settings, run_id="back", once=True)
    assert recovered.run() == ExitCode.OK
    with duckdb.connect(str(settings.db_path)) as con:
        observed = next(
            ts
            for run, ts in con.execute("SELECT run_id, fetched_at_utc FROM price_history").fetchall()
            if run == "legacy"
        )
        started, ended, detail = con.execute(
            "SELECT started_at_utc, ended_at_utc, detail FROM collection_gaps WHERE run_id='legacy'"
        ).fetchone()
        indexed = con.execute("SELECT fetched_at_utc FROM price_history WHERE run_id='legacy'").fetchall()
        assert indexed == [(observed,)]
        assert started == observed < recovered.started_at <= ended
        assert "last persisted observation" in detail


# ---------------------------------------------------------------------------
# 4. gap boundaries: first start, exactly one row, UTC across midnight
# ---------------------------------------------------------------------------


def test_a_first_start_and_a_continuous_run_record_no_gap(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was interrupted before the first start, and one healthy run
    never stops collecting - neither is a gap."""
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="continuous", once=True).run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM collector_runs WHERE status='stopped'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] >= 1
    finally:
        con.close()


def test_an_interrupted_restart_records_exactly_one_gap_with_its_bounds(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="before", once=True).run() == ExitCode.OK

    # A process that dies hard leaves its run row 'running' and no heartbeat;
    # its last committed observation is the only evidence of how long it lived.
    killed = CollectorWriter(settings.db_path, run_id="killed")
    killed.open()
    killed.start_run(app_version="t", git_sha=None, image_tag=None, hostname="h", pid=123)
    observed = datetime.now(tz=UTC)
    killed.stage_price(("mkt-1", "tok-1", observed, 0.5, "rest", observed, "venue", "killed"))
    killed.flush(force=True)
    killed.con.close()  # no close(): SIGKILL leaves exactly this state
    (settings.db_path.parent / "health" / "heartbeat.json").unlink(missing_ok=True)

    assert _worker(settings, run_id="after", once=True).run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        gaps = con.execute("SELECT run_id, started_at_utc, ended_at_utc FROM collection_gaps").fetchall()
        assert len(gaps) == 1, gaps
        run_id, started, ended = gaps[0]
        assert run_id == "after"
        assert started == observed, "the gap must start at the last known activity"
        assert ended is not None and ended >= observed, "contact resumed, so the gap is closed"
        status = con.execute("SELECT status FROM collector_runs WHERE run_id='killed'").fetchone()[0]
        assert status == "failed"
    finally:
        con.close()


def test_a_gap_across_utc_midnight_keeps_its_instants(settings: Settings) -> None:
    """23:55 -> 00:05 is ten minutes, not a day or a shifted local time."""
    writer = CollectorWriter(settings.db_path, run_id="today")
    writer.open()
    try:
        writer.start_run(app_version="t", git_sha=None, image_tag=None, hostname="h", pid=1)
        before_midnight = datetime(2026, 10, 10, 23, 55, tzinfo=UTC)
        after_midnight = datetime(2026, 10, 11, 0, 5, tzinfo=UTC)
        writer.con.execute(
            "INSERT INTO collector_runs "
            "(run_id, started_at_utc, stopped_at_utc, app_version, status) "
            "VALUES ('yesterday', ?, ?, 't', 'stopped')",
            [before_midnight - timedelta(minutes=30), before_midnight],
        )
        writer.begin_restart_gap(None, None)
        writer.close_open_gaps(detail="contact", ended_at_utc=after_midnight)

        started, ended, zone = writer.con.execute(
            "SELECT started_at_utc, ended_at_utc, current_setting('TimeZone') FROM collection_gaps WHERE run_id='today'"
        ).fetchone()
        assert (started, ended) == (before_midnight, after_midnight)
        assert ended - started == timedelta(minutes=10)
        assert zone == "UTC"
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# 5. a backup's outcome is queryable, including the one a crash cut short
# ---------------------------------------------------------------------------


def test_a_successful_backup_records_start_and_verified_outcome(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="backup-ok", backup_daily=True, once=True).run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        trail = con.execute(
            "SELECT kind, details_json FROM collector_events WHERE kind LIKE 'backup%' ORDER BY ts_utc"
        ).fetchall()
    finally:
        con.close()
    assert [kind for kind, _ in trail] == ["backup_started", "backup"], trail
    started, done = (json.loads(details) for _, details in trail)
    assert started["attempt_id"] == done["attempt_id"] and started["attempt_id"]
    assert done["outcome"] == "ok"
    assert done["bytes"] > 0, "a verified copy must report its size"
    assert done["elapsed_seconds"] >= 0
    assert done["destination"].endswith(".duckdb")
    assert list((settings.db_path.parent / "backups").glob("weatherquant-*.duckdb"))


def test_an_audit_insert_failure_does_not_stop_collection(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit trail describes collection; it must never be able to end it."""

    def refuse(self: CollectorWriter, dest_dir: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    real_log_event = CollectorWriter.log_event

    def broken_log_event(
        self: CollectorWriter,
        level: str,
        kind: str,
        message: str,
        details: dict | None = None,
        *,
        immediate: bool = False,
    ) -> None:
        if kind.startswith("backup"):
            raise RuntimeError("injected audit insert failure")
        real_log_event(self, level, kind, message, details, immediate=immediate)

    monkeypatch.setattr(CollectorWriter, "start_backup", refuse)
    monkeypatch.setattr(CollectorWriter, "log_event", broken_log_event)
    assert _worker(settings, run_id="audit-broken", backup_daily=True, once=True).run() == ExitCode.OK

    con = duckdb.connect(str(settings.db_path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] >= 1
        status = con.execute("SELECT status FROM collector_runs WHERE run_id='audit-broken'").fetchone()[0]
        assert status == "stopped"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 6. schema compatibility and a single database open during recovery
# ---------------------------------------------------------------------------


def test_an_old_schema_database_upgrades_in_place(tmp_path: Path, settings: Settings) -> None:
    """A database created before the collector tables existed keeps its rows
    and gains the new schema on open - no rebuild, no data loss."""
    import shutil

    from weatherquant.storage.migrator import MIGRATIONS_DIR, migrate

    old = tmp_path / "old-migrations"
    old.mkdir()
    for name in ("0001_initial_schema.sql", "0002_price_history.sql", "0003_legacy_import.sql"):
        shutil.copy(MIGRATIONS_DIR / name, old / name)

    path = settings.db_path
    con = duckdb.connect(str(path))
    try:
        assert migrate(con, old)[-1].startswith("0003")
        ts = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        con.execute(
            "INSERT INTO price_history "
            "(market_id, token_id, ts, mid_usd_per_share, source, fetched_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["legacy-market", "legacy-token", ts, 0.42, "rest", ts],
        )
    finally:
        con.close()

    writer = CollectorWriter(path, run_id="upgrade")
    assert writer.open() == ["0004_collector"]
    try:
        assert writer.con.execute("SELECT mid_usd_per_share FROM price_history").fetchall() == [(0.42,)]
        writer.start_run(app_version="t", git_sha=None, image_tag=None, hostname="h", pid=1)
        now = datetime.now(tz=UTC)
        writer.record_gap(reason="update", started_at_utc=now, detail="upgrade probe", ended_at_utc=now)
        writer.log_event("info", "upgrade_probe", "readable", {"attempt_id": "probe"}, immediate=True)
        writer.flush(force=True)
    finally:
        writer.close()

    con = duckdb.connect(str(path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM collection_gaps").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM collector_events WHERE kind='upgrade_probe'").fetchone()[0] == 1
        assert con.execute("SELECT status FROM collector_runs WHERE run_id='upgrade'").fetchone()[0] == "stopped"
        # The pre-upgrade row survives untouched; only the new columns are NULL.
        assert con.execute("SELECT mid_usd_per_share, run_id FROM price_history").fetchall() == [(0.42, None)]
    finally:
        con.close()


def test_recovery_replays_the_wal_in_a_single_database_open(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    assert _worker(settings, run_id="seed", once=True).run() == ExitCode.OK

    script = """
import os, sys
from datetime import UTC, datetime
from weatherquant.data.writer import CollectorWriter

w = CollectorWriter(sys.argv[1], run_id="waled")
w.open()
w.start_run(app_version="old", git_sha=None, image_tag=None, hostname="h", pid=1)
now = datetime.now(tz=UTC)
w.stage_price(("legacy-market", "legacy-token", now, 0.5, "rest", now, "venue", "waled"))
w.flush(force=True)
os._exit(9)
"""
    child = subprocess.run([sys.executable, "-c", script, str(settings.db_path)], timeout=30)
    assert child.returncode == 9
    assert Path(str(settings.db_path) + ".wal").stat().st_size > 0

    from weatherquant.data import writer as writer_module

    real_connect = writer_module.connect
    opened: list[str] = []

    def counting_connect(path: Any, **kwargs: Any) -> Any:
        opened.append(str(path))
        return real_connect(path, **kwargs)

    monkeypatch.setattr(writer_module, "connect", counting_connect)
    writer = CollectorWriter(settings.db_path, run_id="recovered")
    assert writer.open() == []
    try:
        assert opened == [str(settings.db_path)], f"recovery opened {len(opened)} connections"
        assert writer.con.execute("SELECT COUNT(*) FROM price_history WHERE run_id='waled'").fetchone()[0] == 1, (
            "the WAL's committed row must be visible to the rebuilt schema"
        )
    finally:
        writer.close()

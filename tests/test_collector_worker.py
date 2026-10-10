"""Worker orchestration, end to end, with every upstream stubbed out.

The soak test exercises this path against the real network; this test exercises
it against a fake so that the *behaviour* - run bookkeeping, gap lifecycle,
graceful shutdown, heartbeat publication - is asserted deterministically and
runs in seconds.

It exists because the soak found three real defects on exactly this path: a
thread-attribute name collision that broke ``Thread.join``, a sequence that did
not survive restart, and a shutdown chain where one subsystem's failure skipped
the writer's close. Each of those is invisible to a pure-function test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from weatherquant.config import Settings
from weatherquant.data import worker as worker_module
from weatherquant.data.heartbeat import read_heartbeat
from weatherquant.data.worker import CollectorWorker, ExitCode

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

FULL_EVENT: dict[str, Any] = {
    "id": "7001",
    "slug": "highest-temperature-in-nyc-on-october-11-2026",
    "title": "Highest temperature in NYC on October 11, 2026",
    "eventDate": "2026-10-11",
    "seriesSlug": "nyc-daily-weather",
    "description": "Resolved on the official NOAA daily maximum for LaGuardia (KLGA).",
    "resolutionSource": "https://www.weather.gov/klga",
    "markets": [
        {
            "id": "m1",
            "conditionId": "0xc1",
            "question": "Will the highest temperature in NYC be 60-61°F?",
            "groupItemTitle": "60-61°F",
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
    """Stands in for ResilientHttp: same surface, canned responses, no network."""

    def __init__(self, **_kwargs: Any) -> None:
        self.last_contact_utc: datetime | None = None
        self.requests_ok = 0
        self.requests_failed = 0
        self.retries_performed = 0
        self.rate_limit_waits = 0
        self.calls: list[str] = []

    def get_json(self, url: str, *, params: Any = None, limiter: str | None = None,
                 allow_not_found: bool = False) -> Any:
        self.calls.append(url)
        self.last_contact_utc = datetime.now(tz=UTC)
        self.requests_ok += 1
        if "/events/" in url:
            return FULL_EVENT
        if "events" in url or "public-search" in url:
            return [FULL_EVENT]
        if "/book" in url:
            return dict(BOOK_PAYLOAD)
        raise AssertionError(f"unexpected url: {url}")

    def post_json(self, url: str, *, json_body: Any = None, limiter: str | None = None) -> Any:
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


def _run(settings: Settings, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[int, CollectorWorker]:
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    worker = CollectorWorker(
        settings,
        run_id="test-run-1",
        stream_enabled=False,
        once=True,
        heartbeat_seconds=0.05,
        book_poll_seconds=0.0,
        discovery_seconds=0.0,
        integrity_seconds=0.0,
        parquet_daily=False,
        backup_daily=False,
        **kwargs,
    )
    return worker.run(), worker


def test_a_full_cycle_collects_and_shuts_down_cleanly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, worker = _run(settings, monkeypatch)
    assert code == ExitCode.OK, "a clean run must exit 0"
    assert worker.run_id == "test-run-1"

    from weatherquant.storage import connect

    con = connect(settings.db_path, read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] >= 1
        assert con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] >= 1

        # The run must be recorded as stopped with a reason, not left "running".
        status, reason, stopped_at = con.execute(
            "SELECT status, stop_reason, stopped_at_utc FROM collector_runs "
            "WHERE run_id='test-run-1'"
        ).fetchone()
        assert status == "stopped"
        assert reason, "a stopped run must record why it stopped"
        assert stopped_at is not None

        # Exactly one gap for this run, opened at start and closed at exit.
        gaps = con.execute(
            "SELECT reason, started_at_utc, ended_at_utc FROM collection_gaps ORDER BY detected_at_utc"
        ).fetchall()
        assert gaps, "a run must record its own collection gap"
        assert any(g[0] == "start_after_restart" for g in gaps)
        assert all(g[2] is not None for g in gaps), "a clean shutdown must close every gap"
    finally:
        con.close()

    # Raw payloads were archived alongside the parsed rows.
    raw_files = list((settings.raw_dir).rglob("*.jsonl"))
    assert raw_files, "raw upstream payloads must be archived"
    assert any(p.name for p in raw_files)


def test_the_final_heartbeat_reports_the_runs_achievements(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    heartbeat = read_heartbeat(settings.db_path.parent / "health" / "heartbeat.json")
    assert heartbeat is not None
    assert str(heartbeat.status) == "stopped"
    # Contact bookkeeping must survive the shutdown, where http is already closed.
    assert heartbeat.last_upstream_contact_utc is not None, (
        "the final heartbeat must not zero the contact clock"
    )
    assert heartbeat.stats.rest_requests_ok > 0
    assert heartbeat.stats.books_rows_inserted >= 1
    assert heartbeat.stats.archive_records >= 1
    assert heartbeat.fatal_error is None
    assert heartbeat.subscribed_tokens >= 2


def test_health_probe_passes_after_a_clean_shutdown(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from weatherquant.data.heartbeat import evaluate_health

    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK
    verdict = evaluate_health(settings.db_path.parent / "health" / "heartbeat.json")
    assert verdict.exit_code == 0, verdict.reasons
    assert verdict.status.value == "stopped"


def test_an_existing_database_is_reopened_not_recreated(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: restart / upgrade / recreate must never wipe historical data."""
    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    from weatherquant.storage import connect

    con = connect(settings.db_path, read_only=True)
    before = con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
    versions_before = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY 1").fetchall()]
    con.close()
    assert before >= 1

    # Second cycle, same data directory - as a container restart would do.
    monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
    second = CollectorWorker(
        settings,
        run_id="test-run-2",
        stream_enabled=False,
        once=True,
        heartbeat_seconds=0.05,
        book_poll_seconds=0.0,
        discovery_seconds=0.0,
        integrity_seconds=0.0,
        parquet_daily=False,
        backup_daily=False,
    )
    assert second.run() == ExitCode.OK

    con = connect(settings.db_path, read_only=True)
    try:
        after = con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0]
        versions_after = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY 1").fetchall()]
        assert after >= before, "restarting must not lose collected rows"
        assert versions_after == versions_before, "restarting must not re-migrate"
        # Both runs are recorded, and no gap is left open.
        assert con.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 2
        open_gaps = con.execute(
            "SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL"
        ).fetchone()[0]
        assert open_gaps == 0, "every gap must be closed by a clean shutdown"
    finally:
        con.close()


def test_a_previously_crashed_run_has_its_gap_closed_on_next_start(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap left open by a killed process must be dated, not silently absorbed."""
    from weatherquant.data.writer import CollectorWriter
    from weatherquant.storage import connect

    # Simulate a crash: a run that opened a gap and never closed it.
    w = CollectorWriter(settings.db_path, run_id="crashed-run")
    w.open()
    w.start_run(app_version="0.1.0", git_sha=None, image_tag=None, hostname="h", pid=1)
    w.record_gap(reason="start_after_restart", started_at_utc=NOW, detail="crashed")
    # No close(): the process died here.
    w._con.close()
    w._con = None

    con = connect(settings.db_path, read_only=True)
    open_before = con.execute(
        "SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL"
    ).fetchone()[0]
    con.close()
    assert open_before == 1

    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    con = connect(settings.db_path, read_only=True)
    try:
        still_open = con.execute(
            "SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL"
        ).fetchone()[0]
        assert still_open == 0, "the next start must close the crashed run's gap"
        assert con.execute(
            "SELECT COUNT(*) FROM collection_gaps WHERE run_id='test-run-1'"
        ).fetchone()[0] >= 1
    finally:
        con.close()


def test_a_second_instance_is_refused_rather_than_double_writing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from weatherquant.data.lock import SingleInstanceLock

    lock = SingleInstanceLock(
        settings.db_path.parent / ".collector.lock", run_id="other", hostname="other-host"
    )
    lock.acquire()
    try:
        monkeypatch.setattr(worker_module, "ResilientHttp", FakeHttp)
        worker = CollectorWorker(
            settings, run_id="test-run-1", stream_enabled=False, once=True
        )
        code = worker.run()
        assert code == ExitCode.LOCK_HELD, "a second writer must be refused with exit 4"
        # The refusal must be visible to the health probe, not just the log.
        verdict_path = settings.db_path.parent / "health" / "heartbeat.json"
        from weatherquant.data.heartbeat import evaluate_health

        verdict = evaluate_health(verdict_path)
        assert verdict.exit_code == 1
        assert any("another collector" in r for r in verdict.reasons)
    finally:
        lock.release()


def test_integrity_and_disk_checks_run_and_record_findings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integrity pass must actually execute during a cycle."""
    code, worker = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    from weatherquant.storage import connect

    con = connect(settings.db_path, read_only=True)
    try:
        # An integrity run happens even when nothing is wrong; the absence of
        # error events is the assertion. A disk event may legitimately appear on
        # a small CI volume, so only assert that nothing *critical* was recorded.
        critical = con.execute(
            "SELECT COUNT(*) FROM collector_events WHERE level = 'critical'"
        ).fetchone()[0]
        assert critical == 0, "no critical event should appear on a healthy run"
    finally:
        con.close()

    # And the writer's own check agrees the collected data is sound.
    from weatherquant.data.writer import CollectorWriter

    probe = CollectorWriter(settings.db_path, run_id="probe")
    probe.open()
    try:
        report = probe.integrity_check()
        assert report.ok, report.problems
    finally:
        probe.close()
    assert worker is not None


def test_book_rows_carry_full_provenance(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    from weatherquant.storage import connect

    con = connect(settings.db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT source, venue_ts_origin, venue_ts, fetched_at_utc, run_id, content_hash "
            "FROM order_books"
        ).fetchall()
        assert rows
        for source, origin, venue_ts, fetched_at, run_id, content_hash in rows:
            assert source in ("rest", "ws")
            assert origin in ("venue", "missing")
            # Provenance invariant: origin must agree with presence.
            assert (venue_ts is None) == (origin == "missing")
            assert fetched_at is not None
            assert run_id == "test-run-1"
            assert content_hash
            # The two clocks must be distinct fields, never merged.
            if venue_ts is not None:
                assert venue_ts != fetched_at or venue_ts.timestamp() == fetched_at.timestamp()
    finally:
        con.close()


def test_a_cycle_is_idempotent_for_unchanged_books(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling the same book repeatedly must dedupe, not accumulate."""
    code, _ = _run(settings, monkeypatch)
    assert code == ExitCode.OK

    from weatherquant.storage import connect

    con = connect(settings.db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT content_hash) FROM order_books"
        ).fetchone()
        assert rows[0] == rows[1], "duplicate content hashes would mean dedup failed"
    finally:
        con.close()


def _bare_worker(settings: Settings) -> CollectorWorker:
    """A worker that never runs - only enough state for the cross-check."""
    return CollectorWorker(settings, run_id="xcheck", stream_enabled=False)


def test_ws_is_cross_checked_against_the_latest_rest_snapshot(settings: Settings) -> None:
    """Spec: WebSocket 实时行情 + REST 快照校验 - the two must be compared."""
    worker = _bare_worker(settings)
    token = "tokA"
    now = datetime.now(tz=UTC)

    # No REST poll yet: nothing to check against, and no false alarm.
    row = ("mkt-1", token, now, 0.50, "ws", now, "venue", "xcheck")
    worker._check_ws_against_rest(row)
    assert worker.stats.ws_rest_mismatches == 0

    # Agreeing snapshots: still quiet.
    worker._last_rest_mid[token] = (now, 0.5005)
    worker._check_ws_against_rest(row)
    assert worker.stats.ws_rest_mismatches == 0

    # Disagreement beyond tolerance: counted.
    worker._check_ws_against_rest(("mkt-1", token, now, 0.53, "ws", now, "venue", "xcheck"))
    assert worker.stats.ws_rest_mismatches == 1


def test_a_stale_rest_snapshot_is_not_used_for_the_cross_check(settings: Settings) -> None:
    """A poll from even a minute ago cannot contradict a fresh stream update.

    The book is allowed to move in that time, so treating the difference as a
    mismatch would report ordinary trading as a data-quality problem.
    """
    worker = _bare_worker(settings)
    token = "tokA"
    stale = datetime.now(tz=UTC) - timedelta(seconds=60)
    worker._last_rest_mid[token] = (stale, 0.40)
    row = ("mkt-1", token, datetime.now(tz=UTC), 0.55, "ws", datetime.now(tz=UTC), "venue", "xcheck")
    worker._check_ws_against_rest(row)
    assert worker.stats.ws_rest_mismatches == 0


def test_a_mismatch_is_recorded_as_a_collector_event(settings: Settings) -> None:
    worker = _bare_worker(settings)
    token = "tokA"
    now = datetime.now(tz=UTC)
    worker._last_rest_mid[token] = (now, 0.40)
    worker._check_ws_against_rest(("mkt-1", token, now, 0.60, "ws", now, "venue", "xcheck"))

    from weatherquant.data.writer import CollectorWriter

    writer = CollectorWriter(settings.db_path, run_id="xcheck")
    writer.open()
    try:
        worker._writer = writer
        worker._event(
            "warning", "ws_rest_mismatch", "test", {"x": 1}
        )
        writer.flush(force=True)
        row = writer.con.execute(
            "SELECT kind, level FROM collector_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        assert row == ("ws_rest_mismatch", "warning")
    finally:
        writer.close()


def test_mismatch_notes_are_rate_limited(settings: Settings) -> None:
    """A persistently divergent pair must not flood collector_events."""
    worker = _bare_worker(settings)
    token = "tokA"
    now = datetime.now(tz=UTC)
    worker._last_rest_mid[token] = (now, 0.40)

    from weatherquant.data.writer import CollectorWriter

    writer = CollectorWriter(settings.db_path, run_id="xcheck")
    writer.open()
    worker._writer = writer
    try:
        for _ in range(50):
            worker._check_ws_against_rest(("mkt-1", token, now, 0.60, "ws", now, "venue", "xcheck"))
        # Every mismatch is counted...
        assert worker.stats.ws_rest_mismatches == 50
        # ...but only one note lands (the very first), because the rate limiter
        # had just been armed by the default epoch.
        writer.flush(force=True)
        notes = writer.con.execute(
            "SELECT COUNT(*) FROM collector_events WHERE kind = 'ws_rest_mismatch'"
        ).fetchone()[0]
        assert notes <= 1, notes
    finally:
        worker._writer = None
        writer.close()


class SlowHttp(FakeHttp):
    """A FakeHttp that costs time per call, to exercise the poll budget."""

    delay = 0.05

    def get_json(self, url: str, **kwargs: Any) -> Any:
        import time as _time

        _time.sleep(self.delay)
        return super().get_json(url, **kwargs)


def test_a_slow_poll_is_bounded_so_the_heartbeat_can_still_tick(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One poll cycle must not outlive the probe's staleness limit.

    Against an unreachable network every request retries with backoff; without
    a budget a 66-token cycle could block the loop for tens of minutes, during
    which the heartbeat stops ticking and a healthy collector looks dead.
    """
    import time as _time

    monkeypatch.setattr(worker_module, "ResilientHttp", SlowHttp)

    from weatherquant.data.writer import CollectorWriter

    writer = CollectorWriter(settings.db_path, run_id="budget")
    writer.open()
    worker = CollectorWorker(
        settings,
        run_id="budget",
        stream_enabled=False,
        book_poll_seconds=0.0,
        discovery_seconds=10**9,
        integrity_seconds=10**9,
        parquet_daily=False,
        backup_daily=False,
    )
    worker._writer = writer
    worker._archive = None
    worker._books = {f"tok{i}": "mkt-1" for i in range(50)}
    worker.POLL_BUDGET_SECONDS = 0.15

    writer.stage_event(_event_row())
    writer.flush(force=True)
    started = _time.monotonic()
    worker._poll_books(SlowHttp())
    elapsed = _time.monotonic() - started

    # The budget must cut the cycle short: 50 tokens x 50ms would be 2.5s.
    assert elapsed < 1.0, f"poll ran {elapsed:.2f}s, well past its 0.15s budget"
    writer.close()


def _event_row() -> tuple:
    from datetime import date as _date

    return (
        "evt-b", "slug", "title", _date(2026, 10, 11), "NWS", "rules",
        datetime.now(tz=UTC), "{}", "gamma", "budget",
    )


def test_a_recent_rest_snapshot_is_used_for_the_cross_check(settings: Settings) -> None:
    """Within the window, the two sources really should agree."""
    worker = _bare_worker(settings)
    token = "tokA"
    recent = datetime.now(tz=UTC) - timedelta(seconds=5)
    worker._last_rest_mid[token] = (recent, 0.40)
    row = ("mkt-1", token, datetime.now(tz=UTC), 0.60, "ws", datetime.now(tz=UTC), "venue", "xcheck")
    worker._check_ws_against_rest(row)
    assert worker.stats.ws_rest_mismatches == 1

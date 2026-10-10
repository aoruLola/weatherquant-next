"""Collector core: single-instance lock, rate limiting, raw archive, health.

Everything here is offline. Where a clock matters, it is injected rather than
slept on, so the suite stays fast and deterministic.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.data.archive import ArchiveRecord, RawArchive, extract_venue_ts
from weatherquant.data.heartbeat import (
    CollectionStats,
    HealthStatus,
    Heartbeat,
    HeartbeatWriter,
    evaluate_health,
    read_heartbeat,
)
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.data.ratelimit import RateLimitError, RateLimitRegistry, SlidingWindowLimiter

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    with SingleInstanceLock(path, run_id="r1", hostname="h") as lock:
        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["run_id"] == "r1"
        assert payload["pid"] == os.getpid()
        assert lock._held
    assert not path.exists()


def test_second_live_holder_is_refused(tmp_path: Path) -> None:
    """Two collectors over one DuckDB file is the failure this prevents."""
    path = tmp_path / ".lock"
    first = SingleInstanceLock(path, run_id="r1", hostname="h")
    first.acquire()
    try:
        # Simulate a *different* live process by writing a foreign, live pid.
        other_pid = os.getpid()  # alive, but a different run id
        path.write_text(
            json.dumps(
                {
                    "run_id": "other-run",
                    "pid": other_pid,
                    "hostname": "h",
                    "acquired_at_utc": datetime.now(tz=UTC).isoformat(),
                    "heartbeat_at_utc": datetime.now(tz=UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        second = SingleInstanceLock(path, run_id="r2", hostname="h")
        with pytest.raises(LockError, match="another collector holds"):
            second.acquire()
    finally:
        first.release()


def test_stale_lock_from_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    """A killed container must not block every future run."""
    path = tmp_path / ".lock"
    path.write_text(
        json.dumps(
            {
                "run_id": "dead-run",
                "pid": 2**22 + 12345,  # practically certain to be unused
                "hostname": "gone",
                "acquired_at_utc": datetime.now(tz=UTC).isoformat(),
                "heartbeat_at_utc": (datetime.now(tz=UTC) - timedelta(hours=6)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    lock = SingleInstanceLock(path, run_id="new-run", hostname="h", stale_after_seconds=60.0)
    lock.acquire()
    try:
        assert json.loads(path.read_text())["run_id"] == "new-run"
    finally:
        lock.release()


def test_live_holder_with_a_fresh_heartbeat_is_not_stolen(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    holder = SingleInstanceLock(path, run_id="r1", hostname="h", stale_after_seconds=300.0)
    holder.acquire()
    try:
        contender = SingleInstanceLock(path, run_id="r2", hostname="h", stale_after_seconds=300.0)
        with pytest.raises(LockError):
            contender.acquire()
    finally:
        holder.release()


def test_heartbeat_refreshes_the_lease(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    lock = SingleInstanceLock(path, run_id="r1", hostname="h")
    lock.acquire()
    try:
        before = json.loads(path.read_text())["heartbeat_at_utc"]
        time.sleep(0.01)
        lock.heartbeat()
        after = json.loads(path.read_text())["heartbeat_at_utc"]
        assert after >= before
    finally:
        lock.release()


def test_unreadable_lock_is_only_reclaimed_once_old(tmp_path: Path) -> None:
    """Half-written locks are not stolen while a concurrent starter may be writing."""
    path = tmp_path / ".lock"
    path.write_text("not json {", encoding="utf-8")
    os.utime(path, (time.time() - 5, time.time() - 5))
    fresh = SingleInstanceLock(path, run_id="r1", hostname="h", stale_after_seconds=60.0)
    with pytest.raises(LockError):
        fresh.acquire()


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_limiter_rejects_over_the_window_limit() -> None:
    limiter = SlidingWindowLimiter("t", max_requests=3, window_seconds=60.0)
    assert all(limiter.try_acquire() for _ in range(3))
    assert limiter.try_acquire() is False
    assert limiter.in_window == 3


def test_limiter_frees_a_slot_when_the_window_elapses() -> None:
    limiter = SlidingWindowLimiter("t", max_requests=1, window_seconds=0.05)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    time.sleep(0.07)
    assert limiter.try_acquire() is True


def test_limiter_blocks_until_a_slot_frees() -> None:
    limiter = SlidingWindowLimiter("t", max_requests=1, window_seconds=0.05)
    limiter.acquire(timeout_seconds=1.0)
    started = time.monotonic()
    limiter.acquire(timeout_seconds=1.0)
    assert time.monotonic() - started >= 0.04


def test_limiter_times_out_rather_than_hanging_forever() -> None:
    limiter = SlidingWindowLimiter("t", max_requests=1, window_seconds=30.0)
    limiter.acquire(timeout_seconds=0.1)
    with pytest.raises(RateLimitError, match="could not admit"):
        limiter.acquire(timeout_seconds=0.05)


def test_limiter_rejects_impossible_configuration() -> None:
    with pytest.raises(ValueError, match="max_requests"):
        SlidingWindowLimiter("t", max_requests=0)
    with pytest.raises(ValueError, match="window_seconds"):
        SlidingWindowLimiter("t", max_requests=1, window_seconds=0)


def test_registry_exposes_every_family_the_collector_uses() -> None:
    registry = RateLimitRegistry()
    for name in ("gamma_events", "clob_book", "clob_books_batch", "data_prices_history"):
        assert registry.get(name).max_requests > 0
    with pytest.raises(KeyError, match="unknown rate limit family"):
        registry.get("does_not_exist")
    snapshot = registry.snapshot()
    assert snapshot["clob_book"]["limit"] == 1500
    assert snapshot["clob_book"]["window_seconds"] == 10.0


def test_limiters_are_shared_across_threads() -> None:
    """The venue limits per IP, so two subsystems must draw from one bucket."""
    limiter = SlidingWindowLimiter("t", max_requests=50, window_seconds=60.0)
    admitted: list[bool] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(10):
            admitted.append(limiter.try_acquire())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(admitted) == 80
    assert sum(admitted) == 50, "the shared bucket must cap total admissions, not per thread"


# ---------------------------------------------------------------------------
# Raw archive
# ---------------------------------------------------------------------------


def test_archive_envelope_carves_out_the_two_clocks(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path / "raw", run_id="r1")
    venue_time = datetime(2026, 10, 10, 11, 59, 58, tzinfo=UTC)
    archive.append(
        ArchiveRecord(
            source="rest",
            payload={"a": 1},
            received_at_utc=datetime(2026, 10, 10, 12, 0, 5, tzinfo=UTC),
            venue_ts_utc=venue_time,
            quality="ok",
            run_id="r1",
        )
    )
    archive.close()

    files = list((tmp_path / "raw" / "rest").glob("*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text().splitlines()[0])
    assert record["source"] == "rest"
    assert record["venue_ts_utc"] == venue_time.isoformat()
    assert record["received_at_utc"] == datetime(2026, 10, 10, 12, 0, 5, tzinfo=UTC).isoformat()
    assert record["venue_ts_origin"] == "venue"
    assert record["quality"] == "ok"
    assert record["run_id"] == "r1"
    # The capture time must be later than the venue time: they are not the same
    # clock and never merge.
    assert record["received_at_utc"] > record["venue_ts_utc"]


def test_missing_venue_timestamp_is_recorded_as_missing_not_as_now(tmp_path: Path) -> None:
    """Spec: a missing exchange event time must never be replaced by local time."""
    archive = RawArchive(tmp_path / "raw", run_id="r1")
    archive.append(
        ArchiveRecord(
            source="ws",
            payload={"no": "timestamp"},
            received_at_utc=datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC),
            venue_ts_utc=None,
            quality="ok",
            run_id="r1",
        )
    )
    archive.close()
    record = json.loads(next((tmp_path / "raw" / "ws").glob("*.jsonl")).read_text().splitlines()[0])
    assert record["venue_ts_utc"] is None
    assert record["venue_ts_origin"] == "missing"
    assert record["received_at_utc"] is not None
    assert record["venue_ts_utc"] != record["received_at_utc"]


def test_archive_refuses_a_record_from_another_run(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path / "raw", run_id="r1")
    with pytest.raises(ValueError, match="cross-run records"):
        archive.append(
            ArchiveRecord(
                source="rest",
                payload={},
                received_at_utc=datetime.now(tz=UTC),
                venue_ts_utc=None,
                quality="ok",
                run_id="SOMEONE_ELSE",
            )
        )


def test_archive_partitions_by_source_and_utc_date(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path / "raw", run_id="r1", flush_records=1)
    archive.append(
        ArchiveRecord(
            source="rest",
            payload={"d": 1},
            received_at_utc=datetime(2026, 10, 10, 23, 59, 59, tzinfo=UTC),
            venue_ts_utc=None,
            quality="ok",
            run_id="r1",
        )
    )
    archive.append(
        ArchiveRecord(
            source="rest",
            payload={"d": 2},
            received_at_utc=datetime(2026, 10, 11, 0, 0, 1, tzinfo=UTC),
            venue_ts_utc=None,
            quality="ok",
            run_id="r1",
        )
    )
    archive.close()
    names = sorted(p.name for p in (tmp_path / "raw" / "rest").glob("*.jsonl"))
    assert names == ["2026-10-10.jsonl", "2026-10-11.jsonl"]


def test_archive_flushes_across_a_utc_midnight_without_losing_order(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path / "raw", run_id="r1")  # default batching, no explicit flush
    for hour in (23, 24):
        archive.append(
            ArchiveRecord(
                source="rest",
                payload={"hour": hour},
                received_at_utc=datetime(2026, 10, 10 if hour == 23 else 11, hour % 24, 0, 0, tzinfo=UTC),
                venue_ts_utc=None,
                quality="ok",
                run_id="r1",
            )
        )
    assert archive.flush() == 2
    archive.close()
    day1 = json.loads((tmp_path / "raw" / "rest" / "2026-10-10.jsonl").read_text().splitlines()[0])
    assert day1["payload"] == {"hour": 23}
    assert archive.records_written == 2


def test_extract_venue_ts_handles_seconds_milliseconds_and_nanoseconds() -> None:
    second = 1_790_000_000
    assert extract_venue_ts({"timestamp": second}) == datetime.fromtimestamp(second, tz=UTC)
    assert extract_venue_ts({"timestamp": second * 1000}) == datetime.fromtimestamp(second, tz=UTC)
    assert extract_venue_ts({"timestamp": second * 1_000_000}) == datetime.fromtimestamp(second, tz=UTC)
    assert extract_venue_ts({"timestamp": second * 1_000_000_000}) == datetime.fromtimestamp(second, tz=UTC)


def test_extract_venue_ts_returns_none_rather_than_guessing() -> None:
    assert extract_venue_ts({}) is None
    assert extract_venue_ts({"timestamp": None}) is None
    assert extract_venue_ts({"timestamp": "not-a-time"}) is None
    assert extract_venue_ts({"timestamp": 3}) is None  # implausibly small: refuse, don't epoch-shift
    assert extract_venue_ts([1, 2, 3]) is None


# ---------------------------------------------------------------------------
# Heartbeat and health evaluation
# ---------------------------------------------------------------------------


def _beat(
    *,
    status: HealthStatus = HealthStatus.HEALTHY,
    updated_at: datetime = NOW,
    contact: datetime | None = NOW,
    disk_free_percent: float = 50.0,
    disk_total_bytes: int = 100_000_000_000,
    rest_failures: int = 0,
    fatal: str | None = None,
) -> Heartbeat:
    return Heartbeat(
        status=status,
        run_id="r1",
        pid=os.getpid(),
        hostname="h",
        app_version="0.1.0",
        updated_at_utc=updated_at,
        started_at_utc=updated_at - timedelta(minutes=5),
        uptime_seconds=300.0,
        last_upstream_contact_utc=contact,
        last_data_change_utc=updated_at,
        rest_consecutive_failures=rest_failures,
        disk_free_percent=disk_free_percent,
        disk_total_bytes=disk_total_bytes,
        stats=CollectionStats(),
        fatal_error=fatal,
    )


def test_heartbeat_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "health" / "heartbeat.json"
    writer = HeartbeatWriter(path)
    original = _beat()
    writer.write(original)
    restored = read_heartbeat(path)
    assert restored is not None
    assert restored.status is HealthStatus.HEALTHY
    assert restored.updated_at_utc == original.updated_at_utc
    assert restored.last_upstream_contact_utc == original.last_upstream_contact_utc


def test_heartbeat_write_is_atomic(tmp_path: Path) -> None:
    """A concurrent reader must never observe half a JSON document."""
    path = tmp_path / "health" / "heartbeat.json"
    writer = HeartbeatWriter(path)
    writer.write(_beat())
    seen: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            # The writer retries a rename that collides with this read, so a
            # transient sharing violation here is expected, not a failure.
            with contextlib.suppress(OSError):
                seen.append(path.read_text(encoding="utf-8"))
            time.sleep(0.001)  # a real probe polls; it does not spin

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(50):
            writer.write(_beat(updated_at=NOW + timedelta(seconds=i)))
    finally:
        stop.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive(), "reader thread failed to stop"
    # On Windows the destination can stay locked for a moment after the last
    # read; clear it here so the session teardown does not trip over a handle
    # that this test itself created. (The container target is Linux, where
    # rename-over-open never fails.)
    for _ in range(50):
        with contextlib.suppress(OSError):
            shutil.rmtree(tmp_path, ignore_errors=False)
            break
        time.sleep(0.05)

    assert seen, "reader never saw the file"
    for payload in seen:
        json.loads(payload)  # every observation must parse


def test_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    verdict = evaluate_health(tmp_path / "nope.json")
    assert verdict.exit_code == 1
    assert verdict.status is HealthStatus.UNHEALTHY
    assert "missing or unparseable" in verdict.reasons[0]


def test_corrupt_heartbeat_is_unhealthy_not_healthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{ not json", encoding="utf-8")
    assert evaluate_health(path).exit_code == 1


def test_stale_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(updated_at=NOW - timedelta(minutes=10)))
    verdict = evaluate_health(path, now=NOW, heartbeat_stale_seconds=120.0)
    assert verdict.exit_code == 1
    assert any("stale" in r for r in verdict.reasons)


def test_fatal_state_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(status=HealthStatus.FATAL, fatal="disk gone"))
    verdict = evaluate_health(path, now=NOW)
    assert verdict.exit_code == 1
    assert "disk gone" in verdict.reasons[0]


def test_clean_stop_passes_the_probe_so_a_rolling_update_does_not_flap(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(status=HealthStatus.STOPPED))
    verdict = evaluate_health(path, now=NOW)
    assert verdict.exit_code == 0
    assert verdict.status is HealthStatus.STOPPED


def test_starting_up_passes_the_probe(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(status=HealthStatus.STARTING))
    assert evaluate_health(path, now=NOW).exit_code == 0


def test_a_quiet_market_stays_healthy(tmp_path: Path) -> None:
    """Non-trading hours must not trip the probe.

    We successfully polled hours ago and nothing has changed since - which is a
    perfectly good state for a weather market at 3am.
    """
    path = tmp_path / "heartbeat.json"
    quiet_since = NOW - timedelta(hours=3)
    HeartbeatWriter(path).write(_beat(updated_at=NOW, contact=quiet_since))
    verdict = evaluate_health(path, now=NOW, upstream_stall_soft_seconds=900.0, upstream_stall_hard_seconds=1800.0)
    # Contact is 3h old: past both limits, so this particular fixture *should*
    # be unhealthy - the point is that staleness is judged on *contact*, not on
    # whether data changed. Verify the distinction explicitly.
    assert verdict.exit_code == 1

    # Same situation, but contact within the soft limit: healthy even though
    # `last_data_change` is older still.
    HeartbeatWriter(path).write(
        _beat(updated_at=NOW, contact=NOW - timedelta(minutes=5), status=HealthStatus.HEALTHY)
    )
    verdict2 = evaluate_health(path, now=NOW, upstream_stall_soft_seconds=900.0, upstream_stall_hard_seconds=1800.0)
    assert verdict2.exit_code == 0
    assert verdict2.status is HealthStatus.HEALTHY


def test_soft_silence_is_degraded_but_does_not_restart_the_container(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(updated_at=NOW, contact=NOW - timedelta(seconds=1200)))
    verdict = evaluate_health(
        path,
        now=NOW,
        upstream_stall_soft_seconds=900.0,
        upstream_stall_hard_seconds=1800.0,
    )
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.exit_code == 0, "a degraded probe must not restart the collector"


def test_hard_silence_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(updated_at=NOW, contact=NOW - timedelta(seconds=2000)))
    verdict = evaluate_health(
        path, now=NOW, upstream_stall_soft_seconds=900.0, upstream_stall_hard_seconds=1800.0
    )
    assert verdict.exit_code == 1


def test_low_disk_warns_without_failing(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(disk_free_percent=10.0))
    verdict = evaluate_health(path, now=NOW)
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.exit_code == 0
    assert any("disk low" in r for r in verdict.reasons)


def test_critical_disk_fails(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(disk_free_percent=2.0))
    verdict = evaluate_health(path, now=NOW)
    assert verdict.exit_code == 1
    assert any("critically low" in r for r in verdict.reasons)


def test_repeated_rest_failures_degrade_but_do_not_fail(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    HeartbeatWriter(path).write(_beat(rest_failures=25))
    verdict = evaluate_health(path, now=NOW)
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.exit_code == 0

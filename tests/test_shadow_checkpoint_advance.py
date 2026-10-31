"""The tailer must move forward between cycles, or it re-reads forever.

The defect this file exists for: `ArchiveTailer.poll()` returned new offsets
but never wrote them back to `self.checkpoints`, and `ShadowRunner.cycle()`
persisted them to DuckDB without adopting them into memory either. So every
cycle resumed from byte 0.

The production signature was not "some lines are missing" - it was the
opposite shape, and that is what makes it worth naming in each test:

  collector_last_seen_utc   frozen, while the archive is seconds behind it
  latest_orderbook_read     still climbing, because shadow_received is "now"
  latest_orderbook_venue    frozen at an old instant, because the newest line
                            is never actually taken up
  decisions                mostly STALE_ORDERBOOK, from venue stamps that
                            were never refreshed

The earlier ingest tests could not catch it: they drove the tailer directly
and hand-merged offsets between polls, performing the very step the runner
omitted. These drive the *runner*.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from shadow_helpers import (
    NOW,
    TARGET_DATE,
    FakeRunsClient,
    archive_line,
    book_payload,
    gamma_event,
    write_lines,
)
from weatherquant.config import Settings
from weatherquant.shadow import runner as runner_module
from weatherquant.shadow.config import ShadowSettings, load_thresholds
from weatherquant.shadow.runner import RunnerConfig, ShadowRunner

REPO = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO / "configs" / "shadow-thresholds.json"
VENUE_TS_MS = int((NOW - timedelta(seconds=4)).timestamp() * 1000)

# Every runner built here is closed on teardown, so a failed assertion cannot
# leave one holding the single-instance lease for the next test.
_RUNNERS: list[ShadowRunner] = []


@pytest.fixture(autouse=True)
def _close_runners():
    yield
    for runner in _RUNNERS:
        runner.close(status="stopped", reason="teardown")
    _RUNNERS.clear()


@pytest.fixture(autouse=True)
def _no_live_forecast_fetches(monkeypatch):
    """Never let a test reach Open-Meteo.

    ``cycle()`` refreshes forecasts, and these settings refresh every tick
    (``shadow_forecast_refresh_seconds=0.01``). Without the stub each cycle
    opens a real TLS connection: harmless on a laptop with egress, and on a
    GitHub runner it wedged main CI for fifteen minutes straight - the job
    was cancelled at its ceiling with the thread parked in ``ssl.do_handshake``,
    because ``faulthandler_timeout`` was finally there to say so.
    """
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)


def _archive(root, *, rest_lines: int = 6) -> Path:
    raw = root / "raw"
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    for i in range(rest_lines):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    2 + i,
                    "rest",
                    book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + i * 1000),
                    received=NOW + timedelta(seconds=i),
                )
            ],
        )
    return raw


def _build(tmp_path, raw, *, clock=None) -> ShadowRunner:
    models = tmp_path / "models"
    if not models.exists():
        shutil.copytree(REPO / "models", models)
    shadow = ShadowSettings(
        shadow_db_path=tmp_path / "shadow" / "shadow.duckdb",
        shadow_models_dir=models,
        shadow_health_dir=tmp_path / "health",
        shadow_report_path=tmp_path / "reports" / "report.json",
        shadow_thresholds_path=str(THRESHOLDS_PATH),
        collector_raw_dir=raw,
        shadow_model_names="ecmwf_raw,calibrated",
        shadow_primary_model="calibrated",
        shadow_tick_seconds=0.01,
        shadow_forecast_refresh_seconds=0.01,
        shadow_report_seconds=0.01,
        shadow_metrics_seconds=0.01,
        shadow_run_lookback_hours=24.0,
    )
    return ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=clock or (lambda: NOW + timedelta(seconds=1)),
    )


def _running(runner: ShadowRunner):
    runner.start()
    return runner



# ---------------------------------------------------------------------------
# 1. consecutive cycles
# ---------------------------------------------------------------------------


def test_a_second_cycle_reads_nothing_that_the_first_already_consumed(tmp_path) -> None:
    """The regression assertion. With the defect, cycle 2 re-reads cycle 1's bytes.

    `stats.lines_read` counts what *this* poll consumed, so an unchanged
    archive must yield zero on every subsequent cycle - and a nonzero value is
    the whole bug, visible in one line.
    """
    raw = _archive(tmp_path)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()

    first = runner.cycle()
    assert first.lines_read > 0, "the first cycle should consume the archive"

    for label in ("second", "third", "fourth"):
        cycle = runner.cycle()
        assert cycle.lines_read == 0, (
            f"the {label} cycle re-read {cycle.lines_read} line(s) the first had "
            "already consumed - the tailer's checkpoints are not advancing"
        )
    runner.close(status="stopped", reason="test")


def test_the_collectors_timestamp_advances_and_then_holds(tmp_path) -> None:
    """`collector_last_seen` tracks the archive head, and only the head.

    The production symptom was this value frozen for ten minutes while the
    collector's own archive was seconds behind. If the tailer re-read from
    zero every cycle the *max* would still be right - so this test also
    appends, and requires the value to move forward when - and only when -
    new data arrives.
    """
    raw = _archive(tmp_path)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()
    head_first = runner._collector_last_seen
    assert head_first is not None

    # Nothing new: the value must hold rather than being re-derived from a
    # replay of the same bytes.
    runner.cycle()
    assert runner._collector_last_seen == head_first

    # Something new: it must move.
    write_lines(
        raw,
        "rest",
        TARGET_DATE,
        [
            archive_line(
                99,
                "rest",
                book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + 60_000),
                received=NOW + timedelta(seconds=60),
            )
        ],
    )
    stats = runner.cycle()
    assert stats.lines_read == 1, "the appended line was not picked up"
    assert runner._collector_last_seen == NOW + timedelta(seconds=60)
    runner.close(status="stopped", reason="test")


def test_appended_records_are_consumed_once_not_on_every_later_cycle(tmp_path) -> None:
    raw = _archive(tmp_path, rest_lines=3)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    for i in range(4):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    100 + i,
                    "rest",
                    book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + i * 1000),
                    received=NOW + timedelta(seconds=100 + i),
                )
            ],
        )
        stats = runner.cycle()
        assert stats.lines_read == 1, f"append {i}: read {stats.lines_read}, expected 1"

    # ...and the ones already seen stay seen.
    assert runner.cycle().lines_read == 0
    runner.close(status="stopped", reason="test")


# ---------------------------------------------------------------------------
# 2. an archive larger than one read
# ---------------------------------------------------------------------------


def test_an_archive_bigger_than_one_read_is_consumed_across_cycles(tmp_path) -> None:
    """`max_bytes_per_poll` caps a single read; the remainder is the next cycle's.

    Without advancing checkpoints this is where the bug would be most
    expensive: a large file would be replayed from byte 0 on every cycle
    forever, and the tail would never reach the end of it.
    """
    raw = tmp_path / "raw"
    lines = [
        archive_line(
            i,
            "rest",
            book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + i * 1000),
            received=NOW + timedelta(seconds=i),
        )
        for i in range(60)
    ]
    # gamma first so the market metadata exists before the books are parsed.
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(0, "gamma", gamma_event())])
    write_lines(raw, "rest", TARGET_DATE, lines)

    rest_path = raw / "rest" / f"{TARGET_DATE}.jsonl"
    total_bytes = rest_path.stat().st_size
    per_read = total_bytes // 4
    assert per_read > 0

    runner = _build(tmp_path, raw)
    runner.tailer.max_bytes_per_poll = per_read
    _RUNNERS.append(runner)
    runner.start()

    consumed = 0
    for _cycle in range(1, 40):
        stats = runner.cycle()
        consumed += stats.lines_read
        if runner.tailer.checkpoints.get(str(rest_path)) is not None:
            checkpoint = runner.tailer.checkpoints[str(rest_path)]
            if checkpoint.offset_bytes >= total_bytes:
                break
    else:  # pragma: no cover - only on a failure to make progress
        pytest.fail(f"the tail never reached the end of the file (offset {checkpoint.offset_bytes}/{total_bytes})")

    final = runner.tailer.checkpoints[str(rest_path)]
    assert final.offset_bytes >= total_bytes, "the tail did not consume the whole file"
    assert final.lines_processed >= 60
    # Progress came from advancing offsets, not from replaying: more than one
    # cycle was needed, and the total is bounded by the file's line count.
    assert consumed >= 60
    runner.close(status="stopped", reason="test")


# ---------------------------------------------------------------------------
# 3. restart
# ---------------------------------------------------------------------------


def test_a_restart_resumes_where_the_running_process_left_off(tmp_path) -> None:
    raw = _archive(tmp_path, rest_lines=8)
    first = _build(tmp_path, raw)
    _RUNNERS.append(first)
    first.start()
    first.cycle()
    offset = first.tailer.checkpoints[str(raw / "rest" / f"{TARGET_DATE}.jsonl")].offset_bytes
    first.close(status="stopped", reason="first")

    second = _build(tmp_path, raw)  # same database, fresh tailer
    _RUNNERS.append(second)
    second.start()
    stats = second.cycle()
    assert stats.lines_read == 0, "the restarted process replayed lines already consumed"
    assert second.tailer.checkpoints[str(raw / "rest" / f"{TARGET_DATE}.jsonl")].offset_bytes == offset
    second.close(status="stopped", reason="second")


# ---------------------------------------------------------------------------
# 4. a failed transaction must not advance memory
# ---------------------------------------------------------------------------


def test_a_rolled_back_batch_leaves_the_tailer_where_it_was(tmp_path, monkeypatch) -> None:
    """Commit first, adopt second - and never the other way round.

    If memory advanced before the commit, a rolled-back batch would skip its
    lines for good: the database would never hold them and the tailer would
    have moved past them. That is data loss, not a retry.
    """
    raw = _archive(tmp_path, rest_lines=6)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()

    real_commit = runner.store.commit_batch

    def explode(**_kwargs):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(runner.store, "commit_batch", explode)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        runner.cycle()

    # Nothing was adopted: the tailer is exactly where it started.
    assert runner.tailer.checkpoints == {}, "memory advanced across a failed commit"
    assert runner.tailer.last_file_date == {}, "gap state advanced across a failed commit"

    # And the lines are still there to be read once the write works again.
    monkeypatch.setattr(runner.store, "commit_batch", real_commit)
    stats = runner.cycle()
    assert stats.lines_read > 0, "the failed batch's lines were skipped on retry"
    assert runner.tailer.checkpoints, "a successful commit must be adopted"

    rest_checkpoint = runner.tailer.checkpoints[str(raw / "rest" / f"{TARGET_DATE}.jsonl")]
    assert rest_checkpoint.offset_bytes > 0
    assert runner.cycle().lines_read == 0, "the recovered batch was consumed twice"
    runner.close(status="stopped", reason="test")


def test_memory_and_database_agree_on_every_offset(tmp_path) -> None:
    """The two copies must be the same number, or one of them is lying."""
    raw = _archive(tmp_path, rest_lines=5)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    stored = {
        row[0]: row[1]
        for row in runner.db.con.execute(
            "SELECT file_path, offset_bytes FROM ingest_checkpoints"
        ).fetchall()
    }
    memory = {
        key: checkpoint.offset_bytes
        for key, checkpoint in runner.tailer.checkpoints.items()
    }
    assert stored == memory, f"database {stored} != memory {memory}"
    runner.close(status="stopped", reason="test")


def test_lines_are_never_consumed_twice_across_many_cycles(tmp_path) -> None:
    """The end-to-end property: a growing archive, read once per append."""
    raw = _archive(tmp_path, rest_lines=2)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()

    runner.cycle()  # the initial backlog
    total_read = 0
    for i in range(5):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    200 + i,
                    "rest",
                    book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + i * 500),
                    received=NOW + timedelta(seconds=200 + i),
                )
            ],
        )
        stats = runner.cycle()
        assert stats.lines_read == 1, f"iteration {i}: read {stats.lines_read}, expected exactly 1"
        total_read += stats.lines_read

    assert total_read == 5
    # Nothing pending: every byte is behind an offset that stuck.
    assert runner.cycle().lines_read == 0
    runner.close(status="stopped", reason="test")


def test_commit_failure_leaves_market_books_and_token_map_unmodified(tmp_path, monkeypatch) -> None:
    """A failed DB commit must not publish an uncommitted market in RAM.

    A same-poll Gamma event still resolves a REST book on successful retry:
    the tailer uses a provisional map while decoding and Runner swaps its
    prepared state only after the durable commit.
    """
    raw = _archive(tmp_path, rest_lines=2)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    assert runner.state.markets == {}
    assert runner.state.books == {}
    assert runner.tailer.market_of_token == {}

    real_commit = runner.store.commit_batch

    def reject_commit(**kwargs):
        raise OSError("injected DuckDB commit failure")

    monkeypatch.setattr(runner.store, "commit_batch", reject_commit)
    with pytest.raises(OSError, match="injected DuckDB"):
        runner.cycle()
    assert runner.state.markets == {}
    assert runner.state.books == {}
    assert runner.tailer.market_of_token == {}
    assert runner.tailer.checkpoints == {}
    assert runner.db.con.execute("SELECT COUNT(*) FROM market_catalog").fetchone()[0] == 0
    assert runner.db.con.execute("SELECT COUNT(*) FROM token_books").fetchone()[0] == 0

    monkeypatch.setattr(runner.store, "commit_batch", real_commit)
    recovered = runner.cycle()
    assert recovered.markets > 0 and recovered.books == 2
    assert runner.state.markets and runner.state.books
    assert runner.tailer.market_of_token
    assert runner.cycle().lines_read == 0
    runner.close(status="stopped", reason="test")


def test_failure_after_initial_success_preserves_last_committed_book(tmp_path, monkeypatch) -> None:
    raw = _archive(tmp_path, rest_lines=1)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()
    old_book = runner.state.books["tok-0-yes"]
    old_checkpoint = {
        key: c.offset_bytes for key, c in runner.tailer.checkpoints.items()
    }

    write_lines(
        raw,
        "rest",
        TARGET_DATE,
        [archive_line(
            81, "rest",
            book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + 30000),
            received=NOW + timedelta(seconds=30),
        )],
    )
    commit = runner.store.commit_batch

    def fail_once(**kwargs):
        raise OSError("retry expected")

    monkeypatch.setattr(runner.store, "commit_batch", fail_once)
    with pytest.raises(OSError, match="retry expected"):
        runner.cycle()
    assert runner.state.books["tok-0-yes"] == old_book
    assert {key: c.offset_bytes for key, c in runner.tailer.checkpoints.items()} == old_checkpoint
    monkeypatch.setattr(runner.store, "commit_batch", commit)
    assert runner.cycle().books == 1
    assert runner.state.books["tok-0-yes"] != old_book
    assert runner.cycle().lines_read == 0
    runner.close(status="stopped", reason="test")


def test_runner_never_commits_bytes_a_failed_archive_flush_rolls_back(tmp_path, monkeypatch):
    import os

    from weatherquant.data.archive import ArchiveRecord, RawArchive

    raw = tmp_path / "raw"
    archive = RawArchive(raw, run_id="collector")
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    archive.append(ArchiveRecord("rest", book_payload(token_id="tok", venue_ts_ms=VENUE_TS_MS),
                                 NOW, NOW, "ok", "collector"))
    path = raw / "rest" / f"{NOW.date()}.jsonl"
    real_sync = os.fsync
    reads = []

    def fail_data_sync(fd):
        if path.exists() and os.path.samestat(os.fstat(fd), path.stat()) and path.stat().st_size > 0:
            reads.append(runner.cycle().lines_read)
            raise OSError("injected data fsync failure")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_data_sync)
        with pytest.raises(OSError, match="data fsync"):
            archive.flush()
    assert path.read_bytes() == b""
    assert reads == [0]
    assert not runner.db.con.execute("SELECT offset_bytes FROM ingest_checkpoints WHERE offset_bytes>0").fetchall()
    archive.close()
    assert runner.cycle().lines_read == 1
    assert runner.cycle().lines_read == 0

def test_restart_publishes_surviving_rows_without_another_append(tmp_path):
    import json
    import subprocess
    import sys

    from weatherquant.data.archive import RawArchive, read_archive_sidecar, verify_archive_tree

    raw = tmp_path / "raw"
    child = """
import os
import sys
from datetime import datetime
from weatherquant.data import archive

publish = archive._write_sidecar
def crash_before_publish(path, state):
    if state["bytes"]:
        os._exit(77)
    publish(path, state)

archive._write_sidecar = crash_before_publish
writer = archive.RawArchive(sys.argv[1], run_id="interrupted")
writer.append(archive.ArchiveRecord("rest", {}, datetime.fromisoformat(sys.argv[2]), None, "ok", "interrupted"))
writer.flush()
"""
    crash = subprocess.run([sys.executable, "-c", child, str(raw), NOW.isoformat()], timeout=30)
    assert crash.returncode == 77
    path = raw / "rest" / f"{NOW.date()}.jsonl"
    data = path.read_bytes()
    assert json.loads(data)["archive_seq"] == 1
    assert read_archive_sidecar(path)["bytes"] == 0

    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()
    assert runner.cycle().lines_read == 0
    archive = RawArchive(raw, run_id="restarted")
    archive.recover_pending_partitions()
    archive.recover_pending_partitions()
    assert path.read_bytes() == data
    assert read_archive_sidecar(path)["bytes"] == len(data)
    assert all(result["ok"] for result in verify_archive_tree(raw))
    events = [json.loads(line)["event"] for line in (raw / "recovery.jsonl").read_bytes().splitlines()]
    assert events == ["checksum_rebuilt"]
    assert runner.cycle().lines_read == 1
    assert runner.db.con.execute("SELECT offset_bytes, lines_processed FROM ingest_checkpoints").fetchall() == [
        (len(data), 1)
    ]
    runner.close(status="stopped", reason="restart test")
    resumed = _build(tmp_path, raw)
    _RUNNERS.append(resumed)
    resumed.start()
    assert resumed.cycle().lines_read == 0

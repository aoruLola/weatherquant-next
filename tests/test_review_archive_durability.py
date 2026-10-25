"""Fault-injection regression tests for durable append-only JSONL archives."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.data.archive import ArchiveRecord, RawArchive

NOW = datetime(2026, 10, 10, 23, 59, 59, tzinfo=UTC)


def _record(day: int = 0, *, source: str = "rest") -> ArchiveRecord:
    return ArchiveRecord(
        source=source,
        payload={"day": day},
        received_at_utc=NOW + timedelta(days=day),
        venue_ts_utc=None,
        quality="ok",
        run_id="review-test",
    )


def _read(root, source="rest"):
    output = []
    for path in sorted((root / source).glob("*.jsonl")):
        raw = path.read_bytes()
        assert raw.endswith(b"\n"), path
        output.extend(json.loads(line) for line in raw.splitlines())
    return output


def test_short_os_write_is_completed_without_cut_lines(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record())
    archive.append(_record(1))
    real_write = os.write
    calls = 0

    def short_write(fd, data):
        nonlocal calls
        calls += 1
        return real_write(fd, data[:7] if calls == 1 else data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short_write)
        assert archive.flush() == 2
    assert calls > 2
    assert sorted(x["payload"]["day"] for x in _read(root)) == [0, 1]
    assert archive.records_written == 2
    archive.close()


def test_write_failure_rolls_back_partial_record_and_retry_is_exact_once(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record())
    real_write = os.write
    calls = 0

    def interrupted_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:9])
        raise OSError("injected ENOSPC after a short write")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", interrupted_write)
        with pytest.raises(OSError, match="ENOSPC"):
            archive.flush()
    assert archive.records_written == 0
    assert len(archive._buffer) == 1
    assert (root / "rest" / "2026-10-10.jsonl").read_bytes() == b""
    assert archive.flush() == 1
    assert archive.flush() == 0
    assert len(_read(root)) == 1
    archive.close()


def test_fsync_failure_leaves_records_retryable(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record())
    real_fsync = os.fsync
    failures = 0

    def fail_once(fd):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise OSError("injected fsync failure")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_once)
        with pytest.raises(OSError, match="fsync failure"):
            archive.flush()
    assert archive.records_written == 0
    assert archive.flush() == 1
    assert len(_read(root)) == 1
    archive.close()


def test_successful_partition_is_not_replayed_after_next_partition_fails(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record())
    archive.append(_record(1))
    real_write = os.write

    # The fault is aimed at the second partition's data write rather than "the
    # second fsync call": since the integrity trail landed, a flush also fsyncs
    # sidecars and the recovery log, so call order no longer identifies the
    # data files. Targeting the payload keeps the scenario the same - the
    # first partition is durable when the second one fails.
    def fail_second_partition(fd, data):
        if b'"day":1' in data:
            raise OSError("second UTC partition failed")
        return real_write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_second_partition)
        with pytest.raises(OSError, match="second UTC"):
            archive.flush()
    assert archive.records_written == 1
    assert len(archive._buffer) == 1
    assert archive.flush() == 1
    assert [x["payload"]["day"] for x in _read(root)] == [0, 1]
    archive.close()


def test_uncertain_rollback_blocks_duplicate_prone_retry(tmp_path, monkeypatch):
    archive = RawArchive(tmp_path / "raw", run_id="review-test", flush_records=1000)
    archive.append(_record())

    def fail_write(fd, data):
        raise OSError("write failed")

    def fail_rollback(fd, length):
        raise OSError("truncate failed")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_write)
        patch.setattr(os, "ftruncate", fail_rollback)
        with pytest.raises(OSError, match="write failed"):
            archive.flush()
    with pytest.raises(RuntimeError, match="uncertain"):
        archive.flush()
    assert len(archive._buffer) == 1


def test_crash_torn_line_is_detected_without_erasing_historical_bytes(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record())
    assert archive.flush() == 1
    archive.close()
    path = root / "rest" / "2026-10-10.jsonl"
    with path.open("ab") as file:
        file.write(b'{"crash":"unfinished"')
    before = path.read_bytes()

    restarted = RawArchive(root, run_id="review-test", flush_records=1000)
    restarted.append(_record())
    with pytest.raises(RuntimeError, match="unterminated"):
        restarted.flush()
    assert path.read_bytes() == before
    # Operator repair of an incomplete suffix preserves the previous valid line.
    path.write_bytes(before[: before.rfind(b"\n") + 1])
    assert restarted.flush() == 1
    assert len(_read(root)) == 2
    restarted.close()


def test_close_fsyncs_pending_records(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="review-test", flush_records=1000)
    archive.append(_record(source="ws"))
    archive.close()
    assert len(_read(root, "ws")) == 1
    assert archive.records_written == 1

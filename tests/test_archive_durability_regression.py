"""WQN-21: raw JSONL / checkpoint write durability, under injected faults.

The acceptance scenarios, one per group:

* 写入错误 / 磁盘满 - every write fails: nothing durable, nothing lost,
  nothing duplicated; the retry writes each record exactly once.
* 重启重放 - a kill -9 between flushes: durable lines survive untouched, the
  replay appends (never rewrites), and ``archive_seq`` continues from the
  file instead of minting ids the dead run already used.
* 部分文件损坏 - a torn tail is refused *and logged*; corruption inside a
  durable line never rewrites history but is provable from the checksum
  trail; bytes beyond the trail are distinguished from corruption.
* 留存校验和与恢复日志 - every durable batch advances a per-line chained
  digest sidecar, and every recovery action leaves a line in
  ``recovery.jsonl``.

The last group drives the whole collector once: disk full must be a loud
death (FATAL exit), and the restart after it must continue the same archive
without duplicating a line.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_collector_worker import BOOK_PAYLOAD, FULL_EVENT
from weatherquant.data.archive import EMPTY_CHAIN, ArchiveRecord, RawArchive, verify_archive_tree

NOW = datetime(2026, 10, 10, 23, 59, 0, tzinfo=UTC)


def _record(n: int = 0, *, source: str = "rest", run_id: str = "wqn-21") -> ArchiveRecord:
    return ArchiveRecord(
        source=source,
        payload={"n": n},
        received_at_utc=NOW + timedelta(seconds=n),
        venue_ts_utc=None,
        quality="ok",
        run_id=run_id,
    )


def _lines(root: Path, source: str = "rest") -> list[dict]:
    out: list[dict] = []
    for path in sorted((root / source).glob("*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return out


def _recovery_events(root: Path) -> list[dict]:
    path = root / "recovery.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _all_ok(root: Path) -> bool:
    results = verify_archive_tree(root)
    return bool(results) and all(r["ok"] for r in results)


# ---------------------------------------------------------------------------
# 写入错误 / 磁盘满
# ---------------------------------------------------------------------------


def test_full_disk_fails_clean_and_the_retry_writes_each_record_once(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1000)
    for i in range(4):
        archive.append(_record(i))

    def no_space(fd, data):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", no_space)
        with pytest.raises(OSError, match="No space left"):
            archive.flush()

    assert archive.records_written == 0
    assert len(archive._buffer) == 4
    # The data file exists (open created it) but carries zero record bytes.
    data_file = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    assert data_file.exists() and data_file.stat().st_size == 0

    # Space returns: exactly the four buffered records land, exactly once.
    assert archive.flush() == 4
    assert [r["payload"]["n"] for r in _lines(root)] == [0, 1, 2, 3]
    assert archive.flush() == 0
    assert _all_ok(root)
    archive.close()


def test_full_disk_at_open_keeps_the_batch_retryable(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1000)
    archive.append(_record())

    real_open = os.open

    def refuse_jsonl(path, flags, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            raise OSError(28, "No space left on device")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", refuse_jsonl)
        with pytest.raises(OSError, match="No space left"):
            archive.flush()
    assert archive.records_written == 0
    assert archive.flush() == 1
    assert len(_lines(root)) == 1
    archive.close()


# ---------------------------------------------------------------------------
# 重启重放
# ---------------------------------------------------------------------------


def test_a_crash_between_flushes_loses_no_durable_line_and_replays_none_twice(tmp_path):
    root = tmp_path / "raw"
    first = RawArchive(root, run_id="run-a", flush_records=1000)
    for i in range(3):
        first.append(_record(i, run_id="run-a"))
    assert first.flush() == 3
    # kill -9: no close(), no further flush. Two more records were buffered
    # and die with the process - the documented loss window - but nothing
    # durable may move.
    first.append(_record(99, run_id="run-a"))
    del first

    second = RawArchive(root, run_id="run-b", flush_records=1000)
    for i in range(10, 12):
        second.append(_record(i, run_id="run-b"))
    assert second.flush() == 2
    second.close()

    lines = _lines(root)
    assert [r["payload"]["n"] for r in lines] == [0, 1, 2, 10, 11]
    # Sequence numbers continue from the dead run's last durable line: the
    # replay never mints an archive_seq the file already contains.
    seqs = [r["archive_seq"] for r in lines]
    assert seqs == sorted(set(seqs)), f"archive_seq repeated across the restart: {seqs}"
    assert _all_ok(root)
    events = [e["event"] for e in _recovery_events(root)]
    assert "seq_continued_from_file" in events


def test_archive_seq_explicitly_continues_from_the_last_durable_line(tmp_path):
    root = tmp_path / "raw"
    first = RawArchive(root, run_id="run-a", flush_records=1000)
    for _ in range(3):
        first.append(_record(run_id="run-a"))
    first.flush()
    first.close()

    restarted = RawArchive(root, run_id="run-b", flush_records=1000)
    restarted.append(_record(run_id="run-b"))
    restarted.flush()
    restarted.close()

    seqs = [line["archive_seq"] for line in _lines(root)]
    assert seqs == [1, 2, 3, 4], f"expected a continuation, got {seqs}"


def test_a_sidecar_left_behind_is_resumed_not_rebuilt(tmp_path):
    root = tmp_path / "raw"
    first = RawArchive(root, run_id="run-a", flush_records=1000)
    first.append(_record(run_id="run-a"))
    first.flush()
    first.close()
    sidecar = root / "rest" / f"{NOW.date().isoformat()}.jsonl.sha256"
    assert sidecar.exists(), "a durable flush must publish a checksum sidecar"

    second = RawArchive(root, run_id="run-b", flush_records=1000)
    second.append(_record(1, run_id="run-b"))
    second.flush()
    second.close()

    # Resuming an intact sidecar is bookkeeping, not a recovery: nothing was
    # repaired, so nothing may be logged as such.
    assert [e["event"] for e in _recovery_events(root)] == ["seq_continued_from_file"]
    assert _all_ok(root)


# ---------------------------------------------------------------------------
# 部分文件损坏
# ---------------------------------------------------------------------------


def test_a_torn_tail_is_quarantined_automatically_and_appending_continues(tmp_path):
    """WQN-21 acceptance regression: a kill -9 mid-batch must not wedge the
    collector until an operator trims the file. The fragment is quarantined
    (fsynced before the truncate, sha256 recorded), the surviving complete
    lines are untouched, and the archive keeps appending - to the same file."""
    import hashlib

    root = tmp_path / "raw"
    first = RawArchive(root, run_id="run-a", flush_records=1000)
    first.append(_record(run_id="run-a"))
    first.flush()
    first.close()

    path = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    durable = path.read_bytes()
    torn = b'{"crash":"half a line"'
    with path.open("ab") as fh:
        fh.write(torn)

    restarted = RawArchive(root, run_id="run-b", flush_records=1000)
    restarted.append(_record(1, run_id="run-b"))
    assert restarted.flush() == 1, "a torn tail must not require manual repair"
    restarted.close()

    # Historical bytes byte-for-byte intact; fragment gone from the data file,
    # intact under quarantine/, with its checksum in the recovery log.
    data = path.read_bytes()
    assert data.startswith(durable)
    assert data.endswith(b"\n") and torn not in data
    quarantined_files = list((root / "quarantine" / "rest").glob("*.torn"))
    assert len(quarantined_files) == 1
    assert quarantined_files[0].read_bytes() == torn

    events = _recovery_events(root)
    by_kind = [e["event"] for e in events]
    assert "torn_tail_quarantined" in by_kind
    record = next(e for e in events if e["event"] == "torn_tail_quarantined")
    assert record["quarantined_sha256"] == hashlib.sha256(torn).hexdigest()

    assert [r["payload"]["n"] for r in _lines(root)] == [0, 1]
    assert _all_ok(root)


def test_a_torn_fragment_longer_than_the_tail_window_is_still_quarantined(tmp_path):
    """The backward scan for the last newline must step past one window: a
    whole batch dying between two bytes leaves a fragment far bigger than the
    64 KiB tail read, and mis-locating the boundary would truncate history."""
    from weatherquant.data import archive as archive_module

    root = tmp_path / "raw"
    first = RawArchive(root, run_id="run-a", flush_records=1000)
    for i in range(3):
        first.append(_record(i, run_id="run-a"))
    first.flush()
    first.close()

    path = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    durable = path.read_bytes()
    huge_torn = b'{"crash":"' + b"x" * (2 * archive_module._TAIL_READ_BYTES) + b'"'
    with path.open("ab") as fh:
        fh.write(huge_torn)

    restarted = RawArchive(root, run_id="run-b", flush_records=1000)
    restarted.append(_record(50, run_id="run-b"))
    assert restarted.flush() == 1
    restarted.close()

    data = path.read_bytes()
    assert data.startswith(durable), "complete lines were truncated with the fragment"
    assert data.endswith(b"\n")
    quarantined = list((root / "quarantine" / "rest").glob("*.torn"))
    assert len(quarantined) == 1
    assert quarantined[0].stat().st_size == len(huge_torn)
    assert [r["payload"]["n"] for r in _lines(root)] == [0, 1, 2, 50]
    assert _all_ok(root)


def test_corruption_inside_a_durable_line_is_provable_from_the_trail(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1000)
    for i in range(3):
        archive.append(_record(i))
    archive.flush()
    archive.close()

    # Silently alter the first durable line's payload - same length, still
    # valid JSON, newline intact. Nothing on the write path can catch this;
    # only the checksum trail can prove it.
    path = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    raw = path.read_bytes()
    first_newline = raw.index(b"\n")
    first_line, remainder = raw[:first_newline], raw[first_newline + 1 :]
    assert b'"n":0' in first_line
    path.write_bytes(first_line.replace(b'"n":0', b'"n":9', 1) + b"\n" + remainder)

    # The archive still appends (O_APPEND; history untouched). The corrupted
    # value reads back as 9 - the file really was altered - and that is
    # precisely why the trail, not the write path, has to catch it.
    later = RawArchive(root, run_id="run-b", flush_records=1000)
    later.append(_record(50, run_id="run-b"))
    later.flush()
    later.close()
    lines = _lines(root)
    assert [r["payload"]["n"] for r in lines] == [9, 1, 2, 50]

    # ...but the trail now disagrees with the bytes, by name and file.
    results = {r["path"]: r for r in verify_archive_tree(root)}
    verdict = results[str(path)]
    assert not verdict["ok"]
    assert any("chain mismatch" in p for p in verdict["problems"]), verdict["problems"]


def test_bytes_beyond_the_trail_are_reported_as_a_crash_window_not_corruption(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1000)
    archive.append(_record())
    archive.flush()
    archive.close()

    # A crash between the data fsync and the sidecar publish: complete lines
    # on disk that the trail does not cover yet.
    path = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    extra = json.dumps({"archive_seq": 99, "source": "rest", "run_id": "ghost"}).encode() + b"\n"
    with path.open("ab") as fh:
        fh.write(extra)

    verdict = {r["path"]: r for r in verify_archive_tree(root)}[str(path)]
    assert not verdict["ok"]
    assert len(verdict["problems"]) == 1
    assert "beyond" in verdict["problems"][0]


def test_a_forged_sidecar_is_rejected_by_verification(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1000)
    archive.append(_record())
    archive.flush()
    archive.close()

    sidecar = root / "rest" / f"{NOW.date().isoformat()}.jsonl.sha256"
    data = json.loads(sidecar.read_text())
    data["chain_sha256"] = hashlib.sha256(b"nope").hexdigest()
    sidecar.write_text(json.dumps(data))

    verdict = {r["path"]: r for r in verify_archive_tree(root)}[
        str(root / "rest" / f"{NOW.date().isoformat()}.jsonl")
    ]
    assert not verdict["ok"]
    assert any("chain mismatch" in p for p in verdict["problems"])


def test_the_chain_matches_an_independent_recomputation(tmp_path):
    root = tmp_path / "raw"
    archive = RawArchive(root, run_id="wqn-21", flush_records=1)  # flush per line
    for i in range(3):
        archive.append(_record(i))
    archive.close()

    path = root / "rest" / f"{NOW.date().isoformat()}.jsonl"
    sidecar = json.loads((root / "rest" / f"{NOW.date().isoformat()}.jsonl.sha256").read_text())
    # The fold, written out longhand: the sidecar must equal the digest chain
    # of exactly the file's line bytes, regardless of how they were batched.
    digest = hashlib.sha256(b"").digest()
    assert digest.hex() == EMPTY_CHAIN
    for line in path.read_bytes().splitlines(keepends=True):
        digest = hashlib.sha256(digest + line).digest()
    assert sidecar["chain_sha256"] == digest.hex()
    assert sidecar["bytes"] == path.stat().st_size
    assert sidecar["lines"] == 3


# ---------------------------------------------------------------------------
# 端到端: 磁盘满 → 响亮地死 → 重启续写
# ---------------------------------------------------------------------------


class _DiskFullHttp:
    """Same surface as ResilientHttp; answers everything, touches nothing."""

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


def test_disk_full_kills_the_collector_loudly_and_the_restart_continues_the_archive(
    tmp_path, monkeypatch
):
    from weatherquant.config import Settings
    from weatherquant.data import worker as worker_module
    from weatherquant.data.worker import CollectorWorker, ExitCode

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "wq.duckdb",
        raw_dir=tmp_path / "raw",
        station="KLGA",
    )
    monkeypatch.setattr(worker_module, "ResilientHttp", _DiskFullHttp)

    def once(**kwargs):
        kwargs.update(
            stream_enabled=False,
            once=True,
            heartbeat_seconds=0.05,
            book_poll_seconds=0.0,
            discovery_seconds=0.0,
            integrity_seconds=0.0,
            parquet_daily=False,
            backup_daily=False,
        )
        return CollectorWorker(settings, **kwargs)

    real_write = os.write

    def enospc_on_archive_lines(fd, data):
        if b'"archive_seq"' in data:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    # Run 1: the disk fills. The archive refuses to pretend it wrote; the run
    # must end FATAL (visible to the restart policy), not OK-with-holes.
    monkeypatch.setattr(os, "write", enospc_on_archive_lines)
    code = once(run_id="run-full").run()
    assert code == ExitCode.FATAL, "a failed raw archive must be a fatal exit"
    monkeypatch.setattr(os, "write", real_write)

    # Run 2 on the same data directory: collection continues the very same
    # archive files, appending - and the trail verifies end to end.
    assert once(run_id="run-after").run() == ExitCode.OK

    raw_files = sorted((tmp_path / "raw").rglob("*.jsonl"))
    assert raw_files, "the restarted run must archive payloads"
    results = verify_archive_tree(tmp_path / "raw")
    assert results and all(r["ok"] for r in results), [
        (r["path"], r["problems"]) for r in results if not r["ok"]
    ]
    seqs = [
        json.loads(line)["archive_seq"]
        for path in raw_files
        for line in path.read_text().splitlines()
        if line
    ]
    assert seqs, "the restarted run must have written archive lines"
    assert len(seqs) == len(set(seqs)), f"archive_seq repeated across restart: {sorted(seqs)}"
    # The recovery log itself is a durable JSONL file the operator reads.
    for event in _recovery_events(tmp_path / "raw"):
        assert set(event) >= {"ts_utc", "event", "run_id"}, event


def test_restart_after_large_complete_record_keeps_sequence(tmp_path):
    root = tmp_path / "raw"
    record = _record()
    with RawArchive(root, run_id=record.run_id) as first:
        first.append(ArchiveRecord(record.source, "x" * 150000, record.received_at_utc, None, "ok", record.run_id))
    with RawArchive(root, run_id="next") as restarted:
        restarted.append(_record(1, run_id="next"))
    assert [row["archive_seq"] for row in _lines(root)] == [1, 2]
    assert _all_ok(root)


def test_restart_refuses_to_recertify_changed_prefix(tmp_path):
    with RawArchive(tmp_path, run_id="wqn-21") as writer:
        writer.append(_record())
    path = next(tmp_path.glob("rest/*.jsonl"))
    receipt = path.with_suffix(".jsonl.sha256")
    previous_receipt = receipt.read_bytes()
    data = path.read_bytes().replace(b'"n":0', b'"n":9') + b'{"archive_seq":2}\n'
    path.write_bytes(data)
    with pytest.raises(ValueError, match="checksum prefix"):
        RawArchive(tmp_path, run_id="restart").recover_pending_partitions()
    assert receipt.read_bytes() == previous_receipt
    assert path.read_bytes() == data
    assert _recovery_events(tmp_path)[-1]["event"] == "partition_recovery_failed"


def test_failed_sequence_read_is_retried_before_minting_an_id(tmp_path, monkeypatch):
    from weatherquant.data import archive as archive_module

    with RawArchive(tmp_path, run_id="wqn-21") as writer:
        writer.append(_record())
    writer = RawArchive(tmp_path, run_id="restart")

    def fail_read(fd, size):
        raise OSError("injected sequence read failure")

    with monkeypatch.context() as patch:
        patch.setattr(archive_module, "_read_full", fail_read)
        with pytest.raises(OSError, match="sequence read failure"):
            writer.append(_record(run_id="restart"))
    writer.append(_record(run_id="restart"))
    writer.close()
    assert [row["archive_seq"] for row in _lines(tmp_path)] == [1, 2]
    assert _all_ok(tmp_path)


@pytest.mark.parametrize("receipt", [b"{", b'{"bytes":-1,"lines":1,"chain_sha256":"bad"}'])
def test_unreadable_receipt_is_preserved_and_reported(tmp_path, receipt):
    with RawArchive(tmp_path, run_id="wqn-21") as writer:
        writer.append(_record())
    path = next(tmp_path.glob("rest/*.jsonl.sha256"))
    path.write_bytes(receipt)
    with pytest.raises(ValueError, match="invalid checksum sidecar"):
        RawArchive(tmp_path, run_id="restart").recover_pending_partitions()
    assert path.read_bytes() == receipt
    assert not verify_archive_tree(tmp_path)[0]["ok"]

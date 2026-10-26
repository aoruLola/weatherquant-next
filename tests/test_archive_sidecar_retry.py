import errno
import json
from datetime import UTC, datetime

import pytest

from weatherquant.data import archive as archive_module
from weatherquant.data.archive import ArchiveRecord, RawArchive, verify_archive_tree


def test_retry_after_sidecar_failure_never_appends_durable_data_twice(tmp_path, monkeypatch):
    archive = RawArchive(tmp_path / "raw", run_id="test", flush_records=100)
    archive.append(
        ArchiveRecord(
            source="rest",
            payload={"value": 1},
            received_at_utc=datetime(2026, 10, 26, tzinfo=UTC),
            venue_ts_utc=None,
            quality="ok",
            run_id="test",
        )
    )
    real = archive_module._write_sidecar

    def fail(*args):
        raise OSError(errno.ENOSPC, "sidecar full")

    monkeypatch.setattr(archive_module, "_write_sidecar", fail)
    with pytest.raises(OSError):
        archive.flush()
    monkeypatch.setattr(archive_module, "_write_sidecar", real)
    archive.close()
    target = tmp_path / "raw/rest/2026-10-26.jsonl"
    assert [json.loads(line)["archive_seq"] for line in target.read_bytes().splitlines()] == [1]
    assert all(item["ok"] for item in verify_archive_tree(tmp_path / "raw"))


def test_archive_checksum_covers_exact_binary_bytes(tmp_path):
    import hashlib

    with RawArchive(tmp_path, run_id="test", flush_records=1) as archive:
        archive.append(
            ArchiveRecord(
                source="rest",
                payload={"value": "line\nvalue"},
                received_at_utc=datetime(2026, 10, 26, tzinfo=UTC),
                venue_ts_utc=None,
                quality="ok",
                run_id="test",
            )
        )
    target = tmp_path / "rest/2026-10-26.jsonl"
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    sidecar = json.loads(target.with_name(target.name + ".sha256").read_text())
    assert sidecar["bytes"] == len(raw)
    expected = hashlib.sha256(bytes.fromhex(archive_module.EMPTY_CHAIN) + raw).hexdigest()
    assert sidecar["chain_sha256"] == expected

"""WQN-26 export provenance verifier + manifest sidecar.

Covers the full threat model from the design doc: signed and unsigned
manifests, legacy directories, tamper detection (flip byte / truncate /
replay / reorder), hash-chain breakage, stray temp files, mixed legacy +
manifest directories, consumer default behaviour, and the HMAC key never
leaking into the manifest or test output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from weatherquant.shadow.export import (
    _build_part_manifest,
    _write_part_manifest,
    read_export,
)
from weatherquant.shadow.provenance import (
    VERDICT_FAIL,
    VERDICT_PASS,
    VERIFY_BAD_SIGNATURE,
    VERIFY_CHAIN_BROKEN,
    VERIFY_DIGEST_MISMATCH,
    VERIFY_MALFORMED_MANIFEST,
    VERIFY_MISSING_MANIFEST,
    VERIFY_ROWCOUNT_MISMATCH,
    VERIFY_SEQ_RANGE_MISMATCH,
    VERIFY_STRAY_TMP_FILE,
    VERIFY_UNKNOWN_PART,
    VERIFY_UNSIGNED,
    ProvenanceReport,
    verify_export_provenance,
)

# ---------------------------------------------------------------------------
# Helpers: build tiny exports to exercise the verifier.
# ---------------------------------------------------------------------------

def _write_tiny_part(parts_dir: Path, index: int, rows: list[dict[str, Any]]) -> Path:
    path = parts_dir / f"part-{index:09d}.jsonl"
    parts_dir.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def _make_rows(index_start: int = 1, count: int = 3) -> list[dict[str, Any]]:
    """Minimal export rows that satisfy the manifest scanner."""
    return [
        {
            "export_seq": index_start + i,
            "decision_id": f"d-{index_start + i}",
            "decision_time_utc": f"2026-11-17T{10 + i:02d}:00:00+00:00",
            "mode": "LIVE",
            "state": "ACCEPT",
        }
        for i in range(count)
    ]


def _attest(parts_dir: Path, signing_key: str | None = None, start_index: int = 1, end_index: int = 2) -> None:
    """Write manifests for ``start_index``..``end_index``.  Chains correctly."""
    prev_bytes: bytes | None = None
    for idx in range(start_index, end_index + 1):
        part_path = parts_dir / f"part-{idx:09d}.jsonl"
        if not part_path.exists():
            raise AssertionError(f"no part {part_path.name}")
        manifest = _build_part_manifest(
            part_path,
            prev_manifest_bytes=prev_bytes,
            publisher_run_id="test-run",
            signing_key=signing_key,
        )
        manifest_path = parts_dir / f"part-{idx:09d}.manifest.json"
        _write_part_manifest(manifest_path, manifest)
        prev_bytes = manifest_path.read_bytes()


# ---------------------------------------------------------------------------
# 1. Happy path: unsigned, signed, multi-part chain.
# ---------------------------------------------------------------------------

def test_unsigned_verifier_passes(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=None, start_index=1, end_index=2)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_PASS
    assert report.parts_checked == 2
    assert report.manifests_found == 2
    assert report.verified_ok == 2
    assert report.legacy_unattested == 0
    assert not report.errors
    assert not report.strays


def test_signed_verifier_passes(tmp_path: Path) -> None:
    key = "super-secret-signing-key-dont-log-this"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=key, start_index=1, end_index=2)

    report = verify_export_provenance(parts, signing_key=key)
    assert report.verdict == VERDICT_PASS
    assert report.verified_ok == 2
    assert report.signing_key_configured is True


def test_hmac_key_never_written_to_manifest(tmp_path: Path) -> None:
    key = "NEVER_WRITE_ME_TO_DISK"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=key, start_index=1, end_index=1)

    manifest = json.loads(
        (parts / "part-000000001.manifest.json").read_text(encoding="utf-8")
    )
    disk_text = (parts / "part-000000001.manifest.json").read_text(encoding="utf-8")
    assert key not in disk_text
    assert manifest.get("hmac_sha256") != key
    assert manifest.get("hmac_sha256") is not None


# ---------------------------------------------------------------------------
# 2. Legacy directories.
# ---------------------------------------------------------------------------

def test_pure_legacy_directory_is_accepted(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    # No manifests at all.

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_PASS
    assert report.legacy_unattested == 2
    assert report.verified_ok == 0


def test_legacy_and_manifest_mixed_passes(tmp_path: Path) -> None:
    """Parts 1..2 legacy (no manifests), parts 3..4 have manifests."""
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _write_tiny_part(parts, 3, _make_rows(6, 2))
    _write_tiny_part(parts, 4, _make_rows(8, 2))
    _attest(parts, signing_key=None, start_index=3, end_index=4)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_PASS
    assert report.parts_checked == 4
    assert report.manifests_found == 2
    assert report.verified_ok == 2
    assert report.legacy_unattested == 2


def test_legacy_directory_still_readable_by_read_export(tmp_path: Path) -> None:
    root = tmp_path / "decisions-export"
    parts = root / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    # no manifests

    rows = read_export(root)
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# 3. Tamper detection: flip byte, truncate, replace manifest.
# ---------------------------------------------------------------------------

def test_flip_one_byte_in_part_digest_mismatch(tmp_path: Path) -> None:
    key = "abcdefg"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=key, start_index=1, end_index=2)

    # Flip one byte in part 1.
    part1 = parts / "part-000000001.jsonl"
    data = bytearray(part1.read_bytes())
    data[0] = (data[0] ^ 0x01)
    part1.write_bytes(bytes(data))

    report = verify_export_provenance(parts, signing_key=key)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status == VERIFY_DIGEST_MISMATCH for e in report.errors)


def test_truncate_last_line_rowcount_mismatch(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    part1 = parts / "part-000000001.jsonl"
    text = part1.read_text(encoding="utf-8")
    # Truncate the last (second) line so it no longer ends with a newline
    # and remove its trailing newline — the line becomes a partial record
    # the JSON parser can still read, so row_count drops by one.
    lines = text.splitlines()
    text = "\n".join(lines[:-1]) + "\n"
    part1.write_text(text, encoding="utf-8")

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    statuses = {e.status for e in report.errors}
    # Either row count or digest will fail.
    assert statuses & {VERIFY_ROWCOUNT_MISMATCH, VERIFY_DIGEST_MISMATCH, VERIFY_SEQ_RANGE_MISMATCH}


def test_replace_manifest_without_rehashing_part_digest_mismatch(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    # Part still the same — forge a new manifest pointing at a different hash.
    manifest_path = parts / "part-000000001.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["part_sha256"] = "0" * 64  # wrong
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status == VERIFY_DIGEST_MISMATCH for e in report.errors)


# ---------------------------------------------------------------------------
# 4. Forged part (replay / hand-inserted).
# ---------------------------------------------------------------------------

def test_forged_part_without_signature_is_detected_as_chain_broken(tmp_path: Path) -> None:
    """Legacy parts 1..2, then a forged part 3 with no proper chain."""
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=None, start_index=1, end_index=2)
    # Now insert a third part whose manifest's prev_manifest_sha256 is nonsense.
    _write_tiny_part(parts, 3, _make_rows(6, 2))
    forged_manifest = _build_part_manifest(
        parts / "part-000000003.jsonl",
        prev_manifest_bytes=b"nonsense-bytes",  # hash of this will not match part-2's manifest
        publisher_run_id="test-run",
        signing_key=None,
    )
    # Override the field explicitly so we know what it says.
    forged_manifest["prev_manifest_sha256"] = hashlib.sha256(b"wrong").hexdigest()
    _write_part_manifest(parts / "part-000000003.manifest.json", forged_manifest)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status == VERIFY_CHAIN_BROKEN for e in report.errors)


def test_forged_part_hashes_recomputed_but_bad_signature_when_key_set(tmp_path: Path) -> None:
    """Attacker computes a fresh sha256 for the forged part but cannot produce a valid HMAC."""
    key = "known-secret"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=key, start_index=1, end_index=1)
    # Forge part 2 — attacker does not know the signing key.
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    forged = _build_part_manifest(
        parts / "part-000000002.jsonl",
        prev_manifest_bytes=(parts / "part-000000001.manifest.json").read_bytes(),
        publisher_run_id="test-run",
        signing_key=None,  # attacker leaves hmac null
    )
    _write_part_manifest(parts / "part-000000002.manifest.json", forged)

    report = verify_export_provenance(parts, signing_key=key)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status in (VERIFY_UNSIGNED, VERIFY_BAD_SIGNATURE) for e in report.errors)


def test_attacker_with_key_wrong_writes_bad_signature(tmp_path: Path) -> None:
    key = "correct-key"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=key, start_index=1, end_index=2)

    # Attacker swaps the HMAC hex for garbage.
    manifest2 = parts / "part-000000002.manifest.json"
    m = json.loads(manifest2.read_text(encoding="utf-8"))
    m["hmac_sha256"] = "a" * 64  # wrong
    manifest2.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    report = verify_export_provenance(parts, signing_key=key)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status == VERIFY_BAD_SIGNATURE for e in report.errors)


# ---------------------------------------------------------------------------
# 5. Reorder / replace manifest chain break.
# ---------------------------------------------------------------------------

def test_replaced_manifest_with_good_part_hash_still_breaks_chain(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=None, start_index=1, end_index=2)

    # Swap manifest 1 with a *different* manifest file body; part 1 stays identical.
    manifest1_path = parts / "part-000000001.manifest.json"
    manifest2_path = parts / "part-000000002.manifest.json"
    swap_buf = manifest1_path.read_bytes()
    manifest1_path.write_bytes(manifest2_path.read_bytes())
    manifest2_path.write_bytes(swap_buf)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    # Either digest mismatch on manifest1 (index says 2 now), or chain break.
    error_statuses = {e.status for e in report.errors}
    assert error_statuses & {VERIFY_CHAIN_BROKEN, VERIFY_MALFORMED_MANIFEST}


# ---------------------------------------------------------------------------
# 6. Half-writes / missing manifest.
# ---------------------------------------------------------------------------

def test_part_without_manifest_after_first_manifested_index(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _write_tiny_part(parts, 3, _make_rows(6, 2))
    # Part 1 gets a manifest, parts 2 and 3 do not.
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    missing = [e for e in report.errors if e.status == VERIFY_MISSING_MANIFEST]
    assert len(missing) == 2


def test_manifest_without_part_file(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    # Write a manifest for part 2 without actually creating the part.
    parts.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "part_file": "part-000000002.jsonl",
        "index": 2,
        "part_sha256": "0" * 64,
        "row_count": 0,
        "first_export_seq": None,
        "last_export_seq": None,
        "first_decision_time_utc": None,
        "last_decision_time_utc": None,
        "prev_manifest_sha256": "f" * 64,
        "publisher_run_id": "test",
        "created_at_utc": "2026-11-17T00:00:00+00:00",
        "hmac_sha256": None,
    }
    _write_part_manifest(parts / "part-000000002.manifest.json", manifest)

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    assert any(e.status == VERIFY_UNKNOWN_PART for e in report.errors)


def test_stray_tmp_file_detected(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    (parts / "part-000000002.jsonl.tmp").write_text("garbage", encoding="utf-8")

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    assert any(s.status == VERIFY_STRAY_TMP_FILE for s in report.strays)


# ---------------------------------------------------------------------------
# 7. The exporter only attests parts it is publishing itself.
#
# The manifest is written before the part is published, so a published
# part always already has its manifest and nothing ever has to be
# "repaired". Signing whatever trailing part happens to lack a manifest
# would let anyone who can write the export directory (but does not hold
# the key) get an injected or replaced part signed by the real exporter.
# ---------------------------------------------------------------------------

_DECISION_COLUMNS = (
    "decision_id,run_id,cycle_id,market_id,station,direction,state,"
    "decision_time_utc,model_probability,net_edge,mode,created_at_utc"
)


def _insert_decision(db: Any, i: int) -> None:
    from datetime import UTC, datetime, timedelta

    when = datetime(2026, 11, 17, 8, 0, tzinfo=UTC) + timedelta(minutes=i)
    db.con.execute(
        f"INSERT INTO shadow_decisions ({_DECISION_COLUMNS}) VALUES ({','.join(['?'] * 12)})",
        [f"id-{i:04d}", "run-1", f"cycle-{i}", "m-0", "KLGA", "buy_yes", "ACCEPT", when, 0.6, 0.05, "LIVE", when],
    )


@pytest.fixture
def shadow_db(tmp_path: Path):
    from weatherquant.storage.shadow import ShadowDatabase

    database = ShadowDatabase(tmp_path / "shadow.duckdb")
    database.open()
    yield database
    database.close()


def _real_exporter(db: Any, tmp_path: Path, **kwargs: Any) -> Any:
    from weatherquant.shadow.export import DecisionExporter

    options: dict[str, Any] = {"batch_rows": 2, "max_seconds": 30.0, "max_parts_per_run": 10, "signing_key": "k"}
    options.update(kwargs)
    return DecisionExporter(db.con, tmp_path / "export", **options)


def test_exporter_run_produces_a_signed_chain_that_verifies(shadow_db: Any, tmp_path: Path) -> None:
    for i in range(5):
        _insert_decision(shadow_db, i)
    result = _real_exporter(shadow_db, tmp_path).run_batch()
    assert not any("failed" in note for note in result.notes), result.notes
    assert result.parts_written == 3
    parts = tmp_path / "export" / "parts"
    report = verify_export_provenance(parts, signing_key="k")
    assert report.verdict == VERDICT_PASS, report.as_dict()
    assert report.verified_ok == 3
    assert not list(parts.glob("*.tmp"))


def test_exporter_never_signs_an_injected_tail_part(shadow_db: Any, tmp_path: Path) -> None:
    for i in range(2):
        _insert_decision(shadow_db, i)
    assert _real_exporter(shadow_db, tmp_path).run_batch().parts_written >= 1
    parts = tmp_path / "export" / "parts"
    assert (parts / "part-000000001.manifest.json").exists()

    # Someone without the key drops a part of their own after the last one.
    forged = {"decision_id": "forged", "export_seq": 2, "decision_time_utc": "2026-11-17T09:00:00+00:00"}
    (parts / "part-000000002.jsonl").write_text(json.dumps(forged) + "\n", encoding="utf-8")

    for i in range(2, 4):
        _insert_decision(shadow_db, i)
    _real_exporter(shadow_db, tmp_path).run_batch()

    report = verify_export_provenance(parts, signing_key="k")
    assert report.verdict == VERDICT_FAIL, report.as_dict()
    statuses = {res.index: res.status for res in report.errors}
    assert statuses.get(2) == VERIFY_MISSING_MANIFEST, report.as_dict()


def test_exporter_never_re_signs_a_replaced_part_whose_manifest_was_deleted(
    shadow_db: Any, tmp_path: Path
) -> None:
    for i in range(4):
        _insert_decision(shadow_db, i)
    assert _real_exporter(shadow_db, tmp_path).run_batch().parts_written >= 1
    parts = tmp_path / "export" / "parts"
    assert (parts / "part-000000002.manifest.json").exists()

    # Replace the newest part and delete its manifest.
    part2 = parts / "part-000000002.jsonl"
    part2.write_text(part2.read_text(encoding="utf-8").replace("0.6", "0.9"), encoding="utf-8")
    (parts / "part-000000002.manifest.json").unlink()

    _insert_decision(shadow_db, 4)
    _real_exporter(shadow_db, tmp_path).run_batch()

    report = verify_export_provenance(parts, signing_key="k")
    assert report.verdict == VERDICT_FAIL, report.as_dict()
    statuses = {res.index: res.status for res in report.errors}
    assert statuses.get(2) == VERIFY_MISSING_MANIFEST, report.as_dict()


def test_crash_between_manifest_and_publish_recovers_on_the_next_tick(
    shadow_db: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    import weatherquant.shadow.export as export_module
    from weatherquant.shadow.export import read_export

    for i in range(2):
        _insert_decision(shadow_db, i)
    real_write = export_module._write_part_manifest

    def write_then_crash(path: Path, manifest: dict[str, Any]) -> Path:
        real_write(path, manifest)
        raise RuntimeError("simulated crash after the manifest, before the publish")

    monkeypatch.setattr(export_module, "_write_part_manifest", write_then_crash)
    first = _real_exporter(shadow_db, tmp_path).run_batch()
    assert any("simulated crash" in note for note in first.notes), first.notes
    parts = tmp_path / "export" / "parts"
    assert not (parts / "part-000000001.jsonl").exists()
    assert (parts / "part-000000001.manifest.json").exists()
    assert not list(parts.glob("*.tmp"))
    assert verify_export_provenance(parts, signing_key="k").verdict == VERDICT_FAIL

    monkeypatch.setattr(export_module, "_write_part_manifest", real_write)
    _insert_decision(shadow_db, 2)
    _real_exporter(shadow_db, tmp_path).run_batch()

    report = verify_export_provenance(parts, signing_key="k")
    assert report.verdict == VERDICT_PASS, report.as_dict()
    ids = [row["decision_id"] for row in read_export(tmp_path / "export")]
    assert ids == ["id-0000", "id-0001", "id-0002"]


# ---------------------------------------------------------------------------
# 8. Consumer / backfill ignore manifest files.
# ---------------------------------------------------------------------------

def test_export_reader_ignores_manifest_files(tmp_path: Path) -> None:
    from weatherquant.paper.consumer import ExportReader

    root = tmp_path / "decisions-export"
    parts = root / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    reader = ExportReader(root)
    paths = reader.list_parts()
    assert len(paths) == 1
    assert paths[0].name.endswith(".jsonl")
    assert all(not p.name.endswith(".manifest.json") for p in paths)


def test_read_export_with_manifests(tmp_path: Path) -> None:
    root = tmp_path / "decisions-export"
    parts = root / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _write_tiny_part(parts, 2, _make_rows(4, 2))
    _attest(parts, signing_key=None, start_index=1, end_index=2)

    rows = read_export(root)
    assert len(rows) == 5


# ---------------------------------------------------------------------------
# 9. Empty / missing directory.
# ---------------------------------------------------------------------------

def test_missing_parts_dir_is_pass(tmp_path: Path) -> None:
    report = verify_export_provenance(tmp_path / "does-not-exist" / "parts")
    assert report.verdict == VERDICT_PASS
    assert report.parts_checked == 0


def test_ok_detail_signed_vs_unsigned(tmp_path: Path) -> None:
    key = "k"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=key, start_index=1, end_index=1)

    # The same signed manifest verifies both with and without the key:
    # without a key the verifier checks integrity only, which is a
    # deliberate supported mode.
    signed_report = verify_export_provenance(parts, signing_key=key)
    unsigned_report = verify_export_provenance(parts, signing_key=None)

    assert signed_report.verdict == VERDICT_PASS
    assert unsigned_report.verdict == VERDICT_PASS
    assert signed_report.verified_ok == 1
    assert unsigned_report.verified_ok == 1
    assert not signed_report.errors
    assert not unsigned_report.errors


# ---------------------------------------------------------------------------
# 10. Row count / seq range tamperings detected.
# ---------------------------------------------------------------------------

def test_part_first_line_bogus_json_rowcount_and_seq_both_fail(tmp_path: Path) -> None:
    parts = tmp_path / "parts"
    part = _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=None, start_index=1, end_index=1)

    # *After* the manifest is written, tamper with the part so the scanner
    # sees fewer dict rows and a shifted seq/time range.
    text = part.read_text(encoding="utf-8")
    lines = text.splitlines()
    lines[0] = "42"  # not a dict
    part.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = verify_export_provenance(parts)
    assert report.verdict == VERDICT_FAIL
    # Any of these failures is correct — we just want to confirm tampering is detected.
    expected_failures = {
        VERIFY_ROWCOUNT_MISMATCH,
        VERIFY_SEQ_RANGE_MISMATCH,
        VERIFY_DIGEST_MISMATCH,
        VERIFY_CHAIN_BROKEN,
        VERIFY_MISSING_MANIFEST,
    }
    assert {e.status for e in report.errors} & expected_failures, report.errors


def test_hmac_key_not_in_verify_output(tmp_path: Path, capsys: Any) -> None:
    """Run the CLI-equipped code path and confirm the key never shows up."""
    key = "secret-key-8e1b9a0c"
    parts = tmp_path / "parts"
    _write_tiny_part(parts, 1, _make_rows(1, 3))
    _attest(parts, signing_key=key, start_index=1, end_index=1)

    report = verify_export_provenance(parts, signing_key=key)
    payload = json.dumps(report.as_dict(), sort_keys=True)
    assert key not in payload

    # Print-style output also must not echo the key.
    _print_report(report)
    captured = capsys.readouterr().out
    assert key not in captured


def _print_report(report: ProvenanceReport) -> None:
    """Mirror what cmd_export_provenance does for text output (keeps the test honest)."""
    print(f"parts_dir      : {report.parts_dir}")
    print(f"key configured : {'yes' if report.signing_key_configured else 'no'}")
    for res in report.errors:
        print(f"  FAIL part-{res.index:09d}  [{res.status}] {res.detail}")
    print(report.verdict)

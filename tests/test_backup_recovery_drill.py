"""The backup-recovery drill: what it verifies, and what it refuses to fake.

The drill's stages are exercised here end to end on synthesized sources. The
one stage this suite never runs is ``slow_io`` with a real ``io.max`` - it
needs root and a cgroup - so the script records it as a *skip* here and the
acceptance evidence comes from running the script under sudo (see the PR).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DRILL = REPO_ROOT / "scripts" / "backup_recovery_drill.py"


def _run_drill(work: Path, *extra: str) -> tuple[int, str, dict]:
    result = subprocess.run(
        [sys.executable, str(DRILL), "--work", str(work), *extra],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=REPO_ROOT,
    )
    return result.returncode, result.stdout + result.stderr, {}


def _ledger_entry(work: Path, ledger: Path) -> dict:
    lines = ledger.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "the drill must append exactly one ledger entry per run"
    return json.loads(lines[-1])


def test_the_full_drill_passes_and_writes_a_well_formed_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger" / "drills.jsonl"
    code, output, _ = _run_drill(tmp_path / "work", "--ledger", str(ledger))
    assert code == 0, output

    entry = _ledger_entry(tmp_path / "work", ledger)
    assert entry["verdict"] == "PASSED"
    assert entry["tool_version"]
    assert entry["source"]["sha256"] and entry["source"]["bytes"] > 0
    assert entry["source"]["synthesized"] is True

    by_name = {stage["name"]: stage for stage in entry["stages"]}
    for name in ("restore", "backup_cycle", "clean_shutdown", "wal_recovery", "slow_io", "incomplete_files"):
        assert name in by_name, f"stage {name} missing from the ledger"
    # slow_io needs root; the unprivileged run must record a skip, not a pass.
    assert by_name["slow_io"]["status"] == "skip"
    for name in ("restore", "backup_cycle", "clean_shutdown", "wal_recovery", "incomplete_files"):
        assert by_name[name]["status"] == "pass", f"{name}: {by_name[name]['detail']}"

    # The recovery point is a fact about the data, not a summary string.
    point = entry["recovery_point"]
    assert point["tables"]["order_books"]["rows"] > 0
    assert point["tables"]["price_history"]["rows"] > 0
    assert point["watermark"]["name"] == "latest_price_utc"
    assert point["watermark"]["value"] is not None


def test_a_shadow_shaped_source_reports_a_decision_watermark(tmp_path: Path) -> None:
    """The watermark answers 'latest decision' when the database has decisions.

    Built here with the real shadow migrations, so the drill's branch choice
    is tested against the schema production uses, not a lookalike.
    """
    from weatherquant.storage import initialize_shadow

    # Keep the input copy separate from the new work directory.
    source = tmp_path / "shadow.duckdb"
    con, _applied = initialize_shadow(source)
    con.execute(
        "INSERT INTO shadow_decisions (decision_id, run_id, cycle_id, market_id, "
        "station, direction, state, decision_time_utc, created_at_utc) VALUES "
        "('d1', 'r1', 'c1', 'm1', 'KLGA', 'buy_yes', 'ACCEPT', ?, ?)",
        [datetime(2026, 10, 20, 12, 0, tzinfo=UTC), datetime.now(tz=UTC)],
    )
    con.close()

    # The drill treats every source the same way; hand it the shadow file.
    code, output, _ = _run_drill(tmp_path / "work", "--source", str(source), "--source-is-copy", "--keep")
    assert code == 0, output
    entry = _ledger_entry(tmp_path / "work", tmp_path / "work-recovery-drills.jsonl")
    assert entry["recovery_point"]["watermark"] == {
        "source": "shadow_decisions.decision_time_utc",
        "value": "2026-10-20 12:00:00+00:00",
        "name": "latest_decision_utc",
    }
    # The collector's write path does not apply to a shadow database - recorded
    # as its own fact, never as a pass and never as a silent skip.
    by_name = {stage["name"]: stage for stage in entry["stages"]}
    assert by_name["backup_cycle"]["status"] == "not_applicable"
    assert by_name["clean_shutdown"]["status"] == "not_applicable"
    assert by_name["restore"]["status"] == "pass"
    assert by_name["wal_recovery"]["status"] == "pass"


def test_an_external_source_needs_the_copy_confirmation(tmp_path: Path) -> None:
    """A byte-copy of a live database is torn; the drill refuses one by default."""
    outside = tmp_path / "elsewhere.duckdb"
    outside.write_bytes(b"not even a database")
    code, output, _ = _run_drill(tmp_path / "work", "--source", str(outside))
    assert code == 2
    assert "--source-is-copy" in output


def test_incomplete_files_are_never_restore_candidates(tmp_path: Path) -> None:
    """The 2026-10-17 class of leftovers: present, huge, and not backups."""
    drill = _load_drill()

    backups = tmp_path / "backups"
    backups.mkdir(parents=True)
    for name in (
        ".incomplete-20261017T000017Z-0f671df2.duckdb",
        ".incomplete-halfwritten.duckdb",
        "weatherquant-20261026T000000Z-deadbeef.duckdb",
        "weatherquant-20261025T000000Z-cafebabe.duckdb",
    ):
        (backups / name).write_bytes(b"x")

    candidates = drill.discover_backups(backups)
    assert [p.name for p in candidates] == [
        "weatherquant-20261025T000000Z-cafebabe.duckdb",
        "weatherquant-20261026T000000Z-deadbeef.duckdb",
    ]
    leftovers = drill.report_incomplete_files(backups)
    assert {item["name"] for item in leftovers} == {
        ".incomplete-20261017T000017Z-0f671df2.duckdb",
        ".incomplete-halfwritten.duckdb",
    }


def _load_drill():
    import importlib.util

    spec = importlib.util.spec_from_file_location("wq_recovery_drill", DRILL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_recovery_point_detects_a_content_change(tmp_path: Path) -> None:
    """Row counts alone would miss a rewrite; the digest must catch it."""
    drill = _load_drill()
    from weatherquant.data.writer import CollectorWriter

    db = tmp_path / "a.duckdb"
    writer = CollectorWriter(db, run_id="digest-test")
    writer.open()
    base = datetime.now(tz=UTC)
    for row in drill._synthetic_books(0, 50, "digest-test", base):
        writer.stage_book(row)
    writer.flush(force=True)
    writer.close(status="stopped", reason="digest test")

    point_a = drill._recovery_point(db)

    # One price point changes content without changing table existence.
    writer = CollectorWriter(db, run_id="digest-test-2")
    writer.open()
    writer.stage_price(
        (
            "DRILL-M",
            "DRILL-T-000",
            base + timedelta(seconds=5),
            0.9,
            "rest",
            base + timedelta(seconds=5),
            "venue",
            "digest-test-2",
        )
    )
    writer.flush(force=True)
    writer.close(status="stopped", reason="digest test 2")
    point_b = drill._recovery_point(db)

    assert point_a["tables"]["price_history"]["rows"] != point_b["tables"]["price_history"]["rows"]
    failures = drill._recovery_points_agree(point_a, point_b)
    assert failures, "a content change must not pass the recovery-point comparison"


def test_a_corrupt_source_fails_the_restore_stage_itself(tmp_path: Path) -> None:
    """The acceptance's fault injection, asserted at the ledger level: an
    unreadable source must fail the restore stage with its evidence - not
    surface as a pass on restore plus a vague 'drill' stage."""
    # Synthesize a real database, then truncate a separate copy.
    drill = _load_drill()
    source = tmp_path / "plain.duckdb"
    source.parent.mkdir(parents=True, exist_ok=True)
    drill.synthesize_source(source)

    outside = tmp_path / "corrupt.duckdb"
    data = source.read_bytes()
    # Truncate rather than overwrite a mid-file block: on a small synthesized
    # database the halfway point can land in a free block, which the drill
    # correctly PASSES (no live data hurt - the acceptance saw the same).
    # A cut file is unreadable no matter the block layout.
    outside.write_bytes(data[: int(len(data) * 0.6)])

    corrupt_ledger = tmp_path / "corrupt-ledger" / "drills.jsonl"
    code, output, _ = _run_drill(
        tmp_path / "work-corrupt",
        "--source",
        str(outside),
        "--source-is-copy",
        "--ledger",
        str(corrupt_ledger),
        "--keep",
    )
    assert code == 1, output

    entry = _ledger_entry(tmp_path / "work-corrupt", corrupt_ledger)
    by_name = {stage["name"]: stage for stage in entry["stages"]}
    assert by_name["restore"]["status"] == "fail", by_name["restore"]
    assert by_name["restore"]["detail"], "the failure evidence must be on the stage"
    assert "drill" not in by_name, f"no catch-all stage expected: stages were {sorted(by_name)}"


def test_default_lifecycle_ledger_survives_successful_cleanup_and_repeated_runs(tmp_path, monkeypatch):
    drill = _load_drill()
    work = tmp_path / "work"
    ledger = tmp_path / "work-recovery-drills.jsonl"

    def synthesize(path):
        path.write_bytes(b"fixture")
        return {}

    monkeypatch.setattr(drill, "synthesize_source", synthesize)
    monkeypatch.setattr(drill, "stage_restore", lambda report, work, source: (source, {}, {}))
    for name in (
        "stage_backup_cycle",
        "stage_clean_shutdown",
        "stage_wal_recovery",
        "stage_slow_io",
        "stage_incomplete_files",
    ):
        monkeypatch.setattr(drill, name, lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["drill", "--work", str(work)])
    for _ in range(2):
        assert drill.main() == 0
        assert not work.exists()
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_drill_refuses_existing_work_directory(tmp_path):
    existing = tmp_path / "existing.duckdb"
    existing.write_bytes(b"keep")
    code, output, _ = _run_drill(tmp_path)
    assert code == 2
    assert "--work must be a new directory" in output
    assert existing.read_bytes() == b"keep"


def test_required_slow_io_cannot_pass_with_a_skipped_stage(tmp_path, monkeypatch):
    drill = _load_drill()
    ledger = tmp_path / "ledger.jsonl"
    work = tmp_path / "work"
    def synthesize(path):
        path.write_bytes(b"fixture")
        return {}

    monkeypatch.setattr(drill, "synthesize_source", synthesize)
    monkeypatch.setattr(drill, "stage_restore", lambda report, work, source: (source, {}, {}))
    for name in ("stage_backup_cycle", "stage_clean_shutdown", "stage_wal_recovery", "stage_incomplete_files"):
        monkeypatch.setattr(drill, name, lambda *args: None)

    def skip(report, work, source):
        report.stage("slow_io").status = "skip"

    monkeypatch.setattr(drill, "stage_slow_io", skip)
    monkeypatch.setattr(sys, "argv", ["drill", "--work", str(work), "--ledger", str(ledger), "--require-slow-io"])
    assert drill.main() == 1
    entry = _ledger_entry(work, ledger)
    assert entry["verdict"] == "FAILED"
    assert entry["stages"][0]["status"] == "skip"


def test_chain_rejects_changed_audit_rows(tmp_path, monkeypatch):
    import duckdb

    drill = _load_drill()
    source = tmp_path / "source.duckdb"
    drill.synthesize_source(source, batches=0)
    copy = drill.child_restore

    def change_audit_row(src, dest):
        copy(src, dest)
        with duckdb.connect(str(dest)) as con:
            con.execute("UPDATE collector_runs SET hostname='changed after copying'")

    monkeypatch.setattr(drill, "child_restore", change_audit_row)
    monkeypatch.setattr(drill, "_synthetic_prices", lambda *args: [])
    monkeypatch.setattr(drill, "_synthetic_books", lambda *args: [])
    report = drill.DrillReport(drill_id="audit", started_at=datetime.now(tz=UTC))
    drill.stage_backup_cycle(report, tmp_path, source)
    assert report.stages[0].status == "fail"
    assert "collector_runs" in report.stages[0].detail

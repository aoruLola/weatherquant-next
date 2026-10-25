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
    for name in ("restore", "backup_cycle", "clean_shutdown", "wal_recovery",
                 "slow_io", "incomplete_files"):
        assert name in by_name, f"stage {name} missing from the ledger"
    # slow_io needs root; the unprivileged run must record a skip, not a pass.
    assert by_name["slow_io"]["status"] == "skip"
    for name in ("restore", "backup_cycle", "clean_shutdown", "wal_recovery",
                 "incomplete_files"):
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

    # Outside --work on purpose: the drill clears its work directory, and a
    # source inside it would be deleted before the drill ever opened it.
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
    code, output, _ = _run_drill(
        tmp_path / "work", "--source", str(source), "--source-is-copy", "--keep"
    )
    assert code == 0, output
    entry = _ledger_entry(tmp_path / "work", tmp_path / "work" / "recovery-drills.jsonl")
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
    writer.stage_price(("DRILL-M", "DRILL-T-000", base + timedelta(seconds=5), 0.9,
                        "rest", base + timedelta(seconds=5), "venue", "digest-test-2"))
    writer.flush(force=True)
    writer.close(status="stopped", reason="digest test 2")
    point_b = drill._recovery_point(db)

    assert point_a["tables"]["price_history"]["rows"] != point_b["tables"]["price_history"]["rows"]
    failures = drill._recovery_points_agree(point_a, point_b)
    assert failures, "a content change must not pass the recovery-point comparison"

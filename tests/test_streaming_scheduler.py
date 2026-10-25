"""The streaming production scheduler, at process level where it lives.

The scheduler is a bash script driven by cron, so the properties worth
testing - the flock skipping a second arrival, the cold-start publish skip,
the JSONL record - are proven by running it, not by importing it. The parse
helpers the soak shares with the report are tested here too, so a change to
the scheduler's log shape breaks a test instead of silently blinding the
acceptance evidence.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = REPO_ROOT / "scripts" / "streaming_schedule.sh"
SOAK = REPO_ROOT / "scripts" / "streaming_soak.py"


def _load_soak_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("wq_streaming_soak", SOAK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_export(work: Path, *, parts: int, rows: int) -> Path:
    """A real export in the shadow's layout, via the soak's own feeder."""
    soak = _load_soak_module()
    feeder = soak.ExportFeeder(work / "export")
    for _ in range(parts):
        feeder.append_part(rows=rows)
    return work / "export"


def _env(work: Path) -> dict:
    return {
        "PY": sys.executable,
        "WQ_EXPORT_DIR": str(work / "export"),
        "WQ_PERFORMANCE_OUT": str(work / "performance"),
        "WQ_STATE_DB": str(work / "state" / "performance-state.duckdb"),
        "WQ_DATASET": "fixture",
        "WQ_SCHEDULE_LOG": str(work / "schedule.jsonl"),
    }


def _schedule(env: dict, role: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCHEDULER), role],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
    )


def _log_entries(work: Path) -> list[dict]:
    log = work / "schedule.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_a_publish_before_any_ingest_is_skipped_not_failed(tmp_path: Path) -> None:
    """Cold start: the state store does not exist yet. A scheduling artifact
    is recorded as its own status - a failed publish would page an operator
    for a condition that self-heals on the next tick."""
    work = tmp_path / "cold"
    work.mkdir()
    _make_export(work, parts=1, rows=8)

    result = _schedule(_env(work), "publish")
    assert result.returncode == 0, result.stderr
    assert _log_entries(work)[-1]["status"] == "skipped-empty-state"
    assert not (work / "state" / "performance-state.duckdb").exists()


def test_a_second_arrival_is_skipped_while_the_lock_is_held(tmp_path: Path) -> None:
    """Both roles contend for one flock; the loser skips and logs."""
    work = tmp_path / "lock"
    work.mkdir()
    _make_export(work, parts=1, rows=8)

    # Hold the lock exactly the way the script does, from outside the script.
    lock_path = work / "state" / ".pipeline.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = _schedule(_env(work), "ingest")
        assert result.returncode == 0, result.stderr

    entries = _log_entries(work)
    assert entries[-1]["status"] == "skipped-locked"
    assert not (work / "state" / "performance-state.duckdb").exists(), (
        "a skipped run must not have touched the state store"
    )


def test_an_ingest_then_publish_cycle_records_watermarks(tmp_path: Path) -> None:
    """One full cycle: ingest consumes, publish emits, both log the cursor."""
    work = tmp_path / "cycle"
    work.mkdir()
    _make_export(work, parts=2, rows=40)
    env = _env(work)

    assert _schedule(env, "ingest").returncode == 0
    assert _schedule(env, "publish").returncode == 0

    entries = _log_entries(work)
    assert [e["status"] for e in entries] == ["ok", "ok"]
    assert all(e["watermark"] for e in entries), "both roles record the cursor"

    # The cursor both runs report is the export's own, from its state.json.
    expected_cursor = json.loads((work / "export" / "state.json").read_text())["cursor"]
    soak = _load_soak_module()
    parsed = soak.read_schedule_log(work / "schedule.jsonl")
    assert soak.watermark_value(parsed[0]) == expected_cursor["last_time_utc"]
    assert soak.current_generation_complete(work / "performance"), (
        "the publish left a complete current generation"
    )


def test_the_feeder_grows_the_export_with_monotonic_cursors(tmp_path: Path) -> None:
    soak = _load_soak_module()
    feeder = soak.ExportFeeder(tmp_path / "export")
    feeder.append_part(rows=10)
    feeder.append_part(rows=10)
    parts = sorted((tmp_path / "export" / "parts").glob("part-*.jsonl"))
    assert [p.name for p in parts] == ["part-000000001.jsonl", "part-000000002.jsonl"]
    assert feeder.total_rows == 20
    # The reader refuses non-advancing cursors: the second part's last row
    # must be strictly later than the first's.
    first_last = json.loads(parts[0].read_text(encoding="utf-8").strip().splitlines()[-1])
    second_last = json.loads(parts[1].read_text(encoding="utf-8").strip().splitlines()[-1])
    assert second_last["decision_time_utc"] > first_last["decision_time_utc"]


def test_a_missing_role_argument_is_refused() -> None:
    result = subprocess.run(
        ["bash", str(SCHEDULER)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
    assert "ingest|publish" in result.stderr

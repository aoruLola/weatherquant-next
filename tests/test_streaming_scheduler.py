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


# --- acceptance round two: timeout, cadence gate, RSS gate ---------------------


def test_a_hung_run_is_killed_and_logged_as_timeout(tmp_path: Path) -> None:
    """A run that ignores its wall clock must die by it - otherwise one hung
    ingest holds the lock and every later tick reads skipped-locked forever
    (local acceptance). The fake PY ignores its arguments and sleeps."""
    work = tmp_path / "hang"
    work.mkdir()
    _make_export(work, parts=1, rows=8)
    fake_py = work / "fakepy"
    fake_py.write_text("#!/bin/bash\nexec sleep 999\n", encoding="utf-8")
    fake_py.chmod(0o755)

    env = _env(work)
    env["PY"] = str(fake_py)
    env["WQ_RUN_TIMEOUT_SECONDS"] = "3"

    result = _schedule(env, "ingest", timeout=60)
    # A timed-out run is a failure: the scheduler must exit non-zero so CI
    # and cron mail see it, while the log records it as its own status.
    assert result.returncode in (124, 137)
    entries = _log_entries(work)
    assert entries[-1]["status"] == "timeout", entries[-1]
    assert entries[-1]["exit"] in (124, 137)


def test_publish_cadence_gate_uses_successful_runs(tmp_path: Path) -> None:
    """The observed publish cadence must stay within 1.5x the schedule - the
    acceptance measured 75% skipped publishes under a same-minute crontab."""
    soak = _load_soak_module()

    def entry(ts: str, status: str = "ok") -> soak.ScheduleEntry:
        return soak.ScheduleEntry(ts=ts, role="publish", status=status,
                                  exit=0, duration_s=1, rss_kb=None, watermark=None)

    # 120s cadence, runs 100s and 400s apart: the second gap breaches 1.5x.
    entries = [
        entry("2026-10-26T10:00:00+00:00"),
        entry("2026-10-26T10:01:40+00:00"),
        entry("2026-10-26T10:08:20+00:00"),
    ]
    intervals, warning = soak.publish_interval_observations(entries)
    assert warning is None
    assert max(intervals) == 400.0

    # A skipped publish is not a successful run and does not count.
    entries.append(entry("2026-10-26T10:09:00+00:00", status="skipped-locked"))
    intervals, _ = soak.publish_interval_observations(entries)
    assert max(intervals) == 400.0

    # Fewer than two successes: cadence unmeasurable, reported as a warning.
    intervals, warning = soak.publish_interval_observations([entry("2026-10-26T10:00:00+00:00")])
    assert intervals == [] and warning


def test_an_unmeasurable_rss_fails_unless_explicitly_allowed() -> None:
    soak = _load_soak_module()

    failure = soak.rss_gate_failure(None, allow_missing=False)
    assert failure and "apt install time" in failure

    assert soak.rss_gate_failure(None, allow_missing=True) is None
    assert soak.rss_gate_failure(123456, allow_missing=False) is None

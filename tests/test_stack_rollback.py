"""Rollback is a dry run until asked, then rewrites only the .env files.

The round trip is the point: snapshot a stack, change its ``.env`` (and its
compose file and its database, to prove what is *not* restored), roll back, and
require the ``.env`` to be byte-identical to the snapshot while every other
file keeps the change.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_SCRIPT = REPO_ROOT / "scripts" / "stack_snapshot.py"
ROLLBACK_SCRIPT = REPO_ROOT / "scripts" / "stack_rollback.py"
OLD_SHA = "5410c75d584771b4477de07f4b1d7b83b764bfe1"
NEW_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GENERATION = "20261115T120000Z-abcdef012345"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot():
    return _load("wq_stack_snapshot", SNAPSHOT_SCRIPT)


@pytest.fixture(scope="module")
def rollback():
    return _load("wq_stack_rollback", ROLLBACK_SCRIPT)


def _stack(root: Path, name: str, variable: str, sha: str, repo: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "compose.yaml").write_text(
        f"x-common: &common\n  image: {repo}:sha-${{{variable}:?set {variable}}}\n", encoding="utf-8"
    )
    (directory / ".env").write_text(f"# pinned build\n{variable}={sha}\nWQ_LOG_LEVEL=INFO\n", encoding="utf-8")
    data = directory / "data"
    data.mkdir()
    (data / "weatherquant.duckdb").write_bytes(b"database-at-snapshot-time")
    (data / "weatherquant.duckdb.wal").write_bytes(b"wal-at-snapshot-time")
    return directory


def _snapshot(tmp_path: Path, snapshot, stacks: list[Path], *, performance: Path | None = None) -> Path:
    parsed = [snapshot.parse_stack_spec(f"{path.name}={path}") for path in stacks]
    return snapshot.write_snapshot(
        tmp_path / "snapshots",
        parsed,
        inspect_documents=[],
        performance=snapshot.performance_generation(performance),
        created_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )


def _change_after_snapshot(directory: Path, variable: str) -> None:
    (directory / ".env").write_text(f"# pinned build\n{variable}={NEW_SHA}\nWQ_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    (directory / "compose.yaml").write_text("# edited after the snapshot\n", encoding="utf-8")
    (directory / "data" / "weatherquant.duckdb").write_bytes(b"database-after-change")


def test_dry_run_prints_the_env_diff_and_writes_nothing(tmp_path: Path, snapshot, rollback, capsys) -> None:
    collector = _stack(tmp_path / "stacks", "collector", "COLLECTOR_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next")
    snapshot_dir = _snapshot(tmp_path, snapshot, [collector])
    _change_after_snapshot(collector, "COLLECTOR_SHA")
    changed_env = (collector / ".env").read_bytes()

    code = rollback.main(["--snapshot", str(snapshot_dir)])
    output = capsys.readouterr().out

    assert code == 0
    assert f"-COLLECTOR_SHA={NEW_SHA}" in output
    assert f"+COLLECTOR_SHA={OLD_SHA}" in output
    assert "dry run: nothing was written" in output
    assert (collector / ".env").read_bytes() == changed_env


def test_apply_restores_the_env_exactly_and_leaves_everything_else_alone(tmp_path: Path, snapshot, rollback) -> None:
    collector = _stack(tmp_path / "stacks", "collector", "COLLECTOR_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next")
    snapshot_dir = _snapshot(tmp_path, snapshot, [collector])
    expected_env = (collector / ".env").read_bytes()
    _change_after_snapshot(collector, "COLLECTOR_SHA")

    code = rollback.main(["--snapshot", str(snapshot_dir), "--apply"])

    assert code == 0
    assert (collector / ".env").read_bytes() == expected_env
    # Only .env may be rewritten: the compose edit and the database change stay.
    assert (collector / "compose.yaml").read_bytes() == b"# edited after the snapshot\n"
    assert (collector / "data" / "weatherquant.duckdb").read_bytes() == b"database-after-change"
    assert (collector / "data" / "weatherquant.duckdb.wal").read_bytes() == b"wal-at-snapshot-time"


def test_apply_rolls_back_every_stack_in_the_snapshot(tmp_path: Path, snapshot, rollback) -> None:
    collector = _stack(tmp_path / "stacks", "collector", "COLLECTOR_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next")
    shadow = _stack(tmp_path / "stacks", "shadow", "SHADOW_SHA", NEW_SHA, "ghcr.io/aorulola/weatherquant-next-shadow")
    snapshot_dir = _snapshot(tmp_path, snapshot, [collector, shadow])
    expected = {
        collector: (collector / ".env").read_bytes(),
        shadow: (shadow / ".env").read_bytes(),
    }
    _change_after_snapshot(collector, "COLLECTOR_SHA")
    _change_after_snapshot(shadow, "SHADOW_SHA")

    assert rollback.main(["--snapshot", str(snapshot_dir), "--apply"]) == 0
    for directory, env_bytes in expected.items():
        assert (directory / ".env").read_bytes() == env_bytes


def test_unchanged_stacks_report_unchanged_and_apply_writes_nothing(tmp_path: Path, snapshot, rollback, capsys) -> None:
    collector = _stack(tmp_path / "stacks", "collector", "COLLECTOR_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next")
    snapshot_dir = _snapshot(tmp_path, snapshot, [collector])
    before = (collector / ".env").stat().st_mtime_ns

    assert rollback.main(["--snapshot", str(snapshot_dir), "--apply"]) == 0
    output = capsys.readouterr().out
    assert "unchanged - the pinned SHAs already match the snapshot" in output
    assert "nothing to restore." in output
    assert (collector / ".env").stat().st_mtime_ns == before


def test_paired_performance_generation_and_dashboard_are_printed(tmp_path: Path, snapshot, rollback, capsys) -> None:
    dashboard = _stack(
        tmp_path / "stacks", "dashboard", "DASHBOARD_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next-dashboard"
    )
    performance = tmp_path / "performance"
    performance.mkdir()
    (performance / "current.json").write_text(json.dumps({"generation_id": GENERATION}), encoding="utf-8")
    snapshot_dir = _snapshot(tmp_path, snapshot, [dashboard], performance=performance)
    _change_after_snapshot(dashboard, "DASHBOARD_SHA")

    assert rollback.main(["--snapshot", str(snapshot_dir)]) == 0
    output = capsys.readouterr().out
    assert f"generation pinned at snapshot: {GENERATION}" in output
    assert f"--performance {performance}" in output
    assert f"--generation {GENERATION} --apply" in output
    assert "Dashboard image and the generation are a pair" in output


def test_a_missing_stack_directory_is_an_error_and_nothing_is_written(
    tmp_path: Path, snapshot, rollback, capsys
) -> None:
    collector = _stack(tmp_path / "stacks", "collector", "COLLECTOR_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next")
    shadow = _stack(tmp_path / "stacks", "shadow", "SHADOW_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next-shadow")
    snapshot_dir = _snapshot(tmp_path, snapshot, [collector, shadow])
    _change_after_snapshot(collector, "COLLECTOR_SHA")
    _change_after_snapshot(shadow, "SHADOW_SHA")
    changed_env = (collector / ".env").read_bytes()

    shutil.rmtree(shadow)

    assert rollback.main(["--snapshot", str(snapshot_dir), "--apply"]) == 2
    assert "stack directory no longer exists" in capsys.readouterr().err
    # The failure happened before any write: the other stack still has its change.
    assert (collector / ".env").read_bytes() == changed_env


def test_a_snapshot_without_snapshot_json_is_rejected(tmp_path: Path, rollback) -> None:
    snapshot_dir = tmp_path / "snapshots" / "empty"
    snapshot_dir.mkdir(parents=True)
    assert rollback.main(["--snapshot", str(snapshot_dir)]) == 2

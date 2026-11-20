"""Snapshotting the pinned build state: copies, SHAs, digests, generation id.

The snapshot exists so a rollback is one command. The properties that matter
are that it captures the files byte-for-byte, records the variables the image
reference resolves from, and never reads or copies a data file. The tests run
against tmp dirs with a planted ``.duckdb``/``.wal`` pair, so "read-only on
data" is demonstrated rather than asserted in prose.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "stack_snapshot.py"
SHA = "5410c75d584771b4477de07f4b1d7b83b764bfe1"
OLD_SHA = "0123456789abcdef0123456789abcdef01234567"
GENERATION = "20261115T120000Z-abcdef012345"


def _load():
    spec = importlib.util.spec_from_file_location("wq_stack_snapshot", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot():
    return _load()


def _stack_dir(root: Path, name: str, variable: str, sha: str, repo: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "compose.yaml").write_text(
        f"x-common: &common\n  image: {repo}:sha-${{{variable}:?set {variable}}}\n", encoding="utf-8"
    )
    (directory / ".env").write_text(
        f"# pinned by the release workflow\n{variable}={sha}\nWQ_LOG_LEVEL=INFO\n", encoding="utf-8"
    )
    data = directory / "data"
    data.mkdir()
    (data / "weatherquant.duckdb").write_bytes(b"not-a-real-database")
    (data / "weatherquant.duckdb.wal").write_bytes(b"wal-bytes")
    return directory


def _inspect(container: str, image: str, sha: str) -> dict:
    return {
        "Name": f"/{container}",
        "Config": {"Image": image, "Env": [f"GIT_SHA={sha}", f"IMAGE_TAG={sha}"]},
        "RepoDigests": [f"{image.rsplit(':', 1)[0]}@sha256:{'a' * 64}"],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(tmp_path: Path, snapshot):
    collector = _stack_dir(tmp_path / "stacks", "collector", "COLLECTOR_SHA", SHA, "ghcr.io/aorulola/weatherquant-next")
    shadow = _stack_dir(
        tmp_path / "stacks", "shadow", "SHADOW_SHA", OLD_SHA, "ghcr.io/aorulola/weatherquant-next-shadow"
    )
    performance = tmp_path / "performance"
    performance.mkdir()
    (performance / "current.json").write_text(json.dumps({"generation_id": GENERATION}), encoding="utf-8")
    stacks = [
        snapshot.parse_stack_spec(f"collector={collector}"),
        snapshot.parse_stack_spec(f"shadow={shadow}"),
    ]
    documents = [
        _inspect("weatherquant-collector", f"ghcr.io/aorulola/weatherquant-next:sha-{SHA}", SHA),
        _inspect("weatherquant-shadow", f"ghcr.io/aorulola/weatherquant-next-shadow:sha-{OLD_SHA}", OLD_SHA),
        {"Name": "/something-else", "Config": {"Image": "example.com/other:sha-abc"}},
    ]
    destination = snapshot.write_snapshot(
        tmp_path / "snapshots",
        stacks,
        inspect_documents=documents,
        performance=snapshot.performance_generation(performance),
        created_at=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )
    return destination, collector, shadow, performance


def test_snapshot_copies_the_compose_and_env_files_byte_for_byte(tmp_path: Path, snapshot) -> None:
    destination, collector, shadow, _ = _build(tmp_path, snapshot)
    assert destination.name == "20261120T120000Z"
    for name, directory in (("collector", collector), ("shadow", shadow)):
        assert (destination / name / "compose.yaml").read_bytes() == (directory / "compose.yaml").read_bytes()
        assert (destination / name / ".env").read_bytes() == (directory / ".env").read_bytes()


def test_snapshot_records_the_pinned_shas_and_resolved_images(tmp_path: Path, snapshot) -> None:
    destination, _, _, _ = _build(tmp_path, snapshot)
    payload = json.loads((destination / "snapshot.json").read_text(encoding="utf-8"))
    collector = payload["stacks"]["collector"]
    shadow = payload["stacks"]["shadow"]
    assert collector["pinned"] == {"COLLECTOR_SHA": SHA}
    assert collector["image"] == f"ghcr.io/aorulola/weatherquant-next:sha-{SHA}"
    assert shadow["pinned"] == {"SHADOW_SHA": OLD_SHA}
    assert collector["missing_variables"] == []


def test_snapshot_attaches_digests_from_inspect_json(tmp_path: Path, snapshot) -> None:
    destination, _, _, _ = _build(tmp_path, snapshot)
    payload = json.loads((destination / "snapshot.json").read_text(encoding="utf-8"))
    containers = payload["stacks"]["collector"]["containers"]
    assert [container["container"] for container in containers] == ["weatherquant-collector"]
    assert containers[0]["repo_digests"] == [f"ghcr.io/aorulola/weatherquant-next@sha256:{'a' * 64}"]
    assert containers[0]["git_sha"] == SHA
    # A container whose image belongs to no stack is recorded, not silently dropped.
    assert payload["unmatched_containers"][0]["container"] == "something-else"


def test_snapshot_records_the_pinned_performance_generation(tmp_path: Path, snapshot) -> None:
    destination, _, _, _ = _build(tmp_path, snapshot)
    payload = json.loads((destination / "snapshot.json").read_text(encoding="utf-8"))
    assert payload["performance"]["generation_id"] == GENERATION


def test_snapshot_never_reads_or_copies_data_files(tmp_path: Path, snapshot) -> None:
    collector = _stack_dir(tmp_path / "stacks", "collector", "COLLECTOR_SHA", SHA, "ghcr.io/aorulola/weatherquant-next")
    database = collector / "data" / "weatherquant.duckdb"
    wal = collector / "data" / "weatherquant.duckdb.wal"
    before = (_sha256(database), _sha256(wal))
    stacks = [snapshot.parse_stack_spec(f"collector={collector}")]
    destination = snapshot.write_snapshot(tmp_path / "snapshots", stacks, inspect_documents=[], performance=None)
    assert (_sha256(database), _sha256(wal)) == before
    assert not list(destination.rglob("*.duckdb"))
    assert not list(destination.rglob("*.wal"))


def test_snapshot_reports_a_pin_variable_missing_from_the_env(tmp_path: Path, snapshot) -> None:
    directory = tmp_path / "collector"
    directory.mkdir()
    (directory / "compose.yaml").write_text(
        "image: ghcr.io/aorulola/weatherquant-next:sha-${COLLECTOR_SHA:?set it}\n", encoding="utf-8"
    )
    (directory / ".env").write_text("WQ_LOG_LEVEL=INFO\n", encoding="utf-8")
    stack = snapshot.parse_stack_spec(f"collector={directory}")
    payload = snapshot.build_snapshot(
        [stack], inspect_documents=[], performance=None, created_at=datetime(2026, 11, 20, tzinfo=UTC)
    )
    assert payload["stacks"]["collector"]["missing_variables"] == ["COLLECTOR_SHA"]
    assert payload["stacks"]["collector"]["pinned"] == {}


def test_snapshot_does_not_overwrite_an_existing_snapshot(tmp_path: Path, snapshot) -> None:
    collector = _stack_dir(tmp_path / "stacks", "collector", "COLLECTOR_SHA", SHA, "ghcr.io/aorulola/weatherquant-next")
    stacks = [snapshot.parse_stack_spec(f"collector={collector}")]
    moment = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    first = snapshot.write_snapshot(
        tmp_path / "snapshots", stacks, inspect_documents=[], performance=None, created_at=moment
    )
    second = snapshot.write_snapshot(
        tmp_path / "snapshots", stacks, inspect_documents=[], performance=None, created_at=moment
    )
    assert first != second
    assert second.name == "20261120T120000Z-1"
    assert (first / "collector" / ".env").exists() and (second / "collector" / ".env").exists()


def test_snapshot_cli_writes_a_snapshot(tmp_path: Path, snapshot, capsys) -> None:
    collector = _stack_dir(tmp_path / "stacks", "collector", "COLLECTOR_SHA", SHA, "ghcr.io/aorulola/weatherquant-next")
    inspect = tmp_path / "inspect.json"
    inspect.write_text(
        json.dumps([_inspect("weatherquant-collector", f"ghcr.io/aorulola/weatherquant-next:sha-{SHA}", SHA)]),
        encoding="utf-8",
    )
    code = snapshot.main(
        [
            "--out",
            str(tmp_path / "snapshots"),
            "--stack",
            f"collector={collector}",
            "--inspect",
            str(inspect),
            "--now",
            "2026-11-20T12:00:00+00:00",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "snapshot written:" in output
    assert f"COLLECTOR_SHA={SHA}" in output
    assert "data directories were not read, copied or modified." in output

"""Publish, pin and prune must agree on the current generation during a write."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from weatherquant.performance import snapshot
from weatherquant.performance.generation_lock import generation_lock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _publish(root: Path, generation_id: str, *, legacy: bool = False) -> None:
    """Publish one generation; ``legacy`` inlines the detail the old reader expects."""
    payloads = snapshot.build_performance(decisions=[], dataset="fixture")
    if legacy:
        payloads["opportunities.json"]["data"]["episodes"] = []
        payloads["summary.json"]["data"]["reference_capital"]["results"] = []
    snapshot.publish_performance(payloads, root, generation_id=generation_id)


def _damage(source: Path, target: Path) -> None:
    """Copy ``source`` under the name ``target.name`` and truncate one core file."""
    shutil.copytree(source, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["generation_id"] = target.name
    manifest_path.write_text(json.dumps(manifest))
    (target / "edge.json").write_bytes(b"{")


def _prune(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "prune_performance_generations.py"),
         "--performance", str(root), "--keep-recent", "1", "--keep-monthly", "0", *extra],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_pin_and_prune_cannot_change_an_inflight_publish(tmp_path, monkeypatch):
    current, pending = "20260101T000000Z-old", "20260102T000000Z-next"
    payloads = snapshot.build_performance(decisions=[], dataset="fixture")
    snapshot.publish_performance(payloads, tmp_path, generation_id=current)
    original = snapshot.atomic_write_json_streaming
    results = []

    def write(path, *args, **kwargs):
        original(path, *args, **kwargs)
        if path.name != "summary.json":
            return
        for script, arguments in [
            ("prune_performance_generations.py", []),
            ("pin_performance_generation.py", ["--generation", pending]),
        ]:
            run = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--performance", str(tmp_path), "--apply", *arguments],
                capture_output=True,
                text=True,
                timeout=30,
            )
            results.append((run.returncode, run.stderr))
        assert path.is_file(), "prune removed the generation being published"

    monkeypatch.setattr(snapshot, "atomic_write_json_streaming", write)
    snapshot.publish_performance(payloads, tmp_path, generation_id=pending)
    assert all(code == 75 and "busy" in error for code, error in results), results
    assert len(results) == 2
    assert json.loads((tmp_path / "current.json").read_text())["generation_id"] == pending


def test_prune_requires_a_complete_legacy_rollback_point(tmp_path):
    payloads = snapshot.build_performance(decisions=[], dataset="fixture")
    ids = ["20260101T000000Z-first", "20260102T000000Z-current"]
    for generation_id in ids:
        snapshot.publish_performance(payloads, tmp_path, generation_id=generation_id)
    command = [
        sys.executable,
        str(SCRIPTS / "prune_performance_generations.py"),
        "--performance",
        str(tmp_path),
        "--keep-recent",
        "1",
        "--keep-monthly",
        "0",
    ]
    preview = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert preview.returncode == 0 and "Will delete: 1" in preview.stdout
    applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, timeout=30)
    assert applied.returncode != 0 and "No complete legacy-detail" in applied.stderr
    assert all((tmp_path / "generations" / generation_id).is_dir() for generation_id in ids)


def test_a_current_generation_cannot_omit_research_drift(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from pin_performance_generation import inspect

    generation_id = "20260102T000000Z-current"
    _publish(tmp_path, generation_id)
    generation = tmp_path / "generations" / generation_id
    (generation / "research-drift.json").unlink()

    info = inspect(generation)
    assert not info.complete
    assert info.missing_files == ("research-drift.json",)


def test_a_manifest_cannot_waive_a_core_snapshot_file(tmp_path, monkeypatch):
    """Only research-drift.json is optional (historical generations predate it).

    A manifest that no longer lists a core file - and whose core file is gone -
    must not make the generation look complete: prune keeps "complete"
    generations as rollback points and treats incomplete ones as deletable.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from pin_performance_generation import inspect

    generation_id = "20260102T000000Z-damaged"
    _publish(tmp_path, generation_id)
    generation = tmp_path / "generations" / generation_id
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["files"]["edge.json"]
    manifest_path.write_text(json.dumps(manifest))
    (generation / "edge.json").unlink()

    info = inspect(generation)
    assert not info.complete
    assert "edge.json" in info.missing_files


def test_a_historical_generation_without_research_drift_stays_complete(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from pin_performance_generation import inspect

    generation_id = "20260101T000000Z-historical"
    _publish(tmp_path, generation_id, legacy=True)
    generation = tmp_path / "generations" / generation_id
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["files"]["research-drift.json"]
    manifest_path.write_text(json.dumps(manifest))
    (generation / "research-drift.json").unlink()

    info = inspect(generation)
    assert info.complete, info.as_dict()


def test_dry_run_takes_no_lock_and_deletes_nothing(tmp_path):
    _publish(tmp_path, "20260101T000000Z-legacy", legacy=True)
    _publish(tmp_path, "20260102T000000Z-stale")
    _publish(tmp_path, "20260103T000000Z-current")
    with generation_lock(tmp_path):
        preview = _prune(tmp_path)
    assert preview.returncode == 0, preview.stderr
    assert "Will delete: 1" in preview.stdout
    assert all(path.is_dir() for path in (tmp_path / "generations").iterdir())


def test_a_damaged_newer_legacy_copy_does_not_replace_the_rollback_point(tmp_path):
    legacy = "20260101T000000Z-legacy"
    broken = "20260103T000000Z-broken"
    _publish(tmp_path, legacy, legacy=True)
    _publish(tmp_path, "20260102T000000Z-current")
    _damage(tmp_path / "generations" / legacy, tmp_path / "generations" / broken)

    pin = subprocess.run(
        [sys.executable, str(SCRIPTS / "pin_performance_generation.py"),
         "--performance", str(tmp_path), "--generation", broken, "--apply"],
        capture_output=True, text=True, timeout=30,
    )
    assert pin.returncode == 2 and "incomplete" in pin.stderr
    assert json.loads((tmp_path / "current.json").read_text())["generation_id"] == (
        "20260102T000000Z-current"
    )

    applied = _prune(tmp_path, "--apply")
    assert applied.returncode == 0, applied.stderr
    assert (tmp_path / "generations" / legacy).is_dir()
    assert not (tmp_path / "generations" / broken).exists()


def test_prune_refuses_when_the_only_legacy_copy_is_damaged(tmp_path):
    source = "20260101T000000Z-legacy"
    broken = "20260103T000000Z-broken"
    _publish(tmp_path, source, legacy=True)
    _publish(tmp_path, "20260102T000000Z-current")
    _damage(tmp_path / "generations" / source, tmp_path / "generations" / broken)
    (tmp_path / "generations" / source).rename(tmp_path / "generations" / "20251231T000000Z-removed")

    applied = _prune(tmp_path, "--apply")
    assert applied.returncode == 1 and "No complete legacy-detail" in applied.stderr
    assert (tmp_path / "generations" / broken).is_dir()


def test_deletion_failures_report_only_removed_generations(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import prune_performance_generations as prune

    _publish(tmp_path, "20260101T000000Z-legacy", legacy=True)
    stale = ["20260102T000000Z-stale", "20260103T000000Z-stale"]
    for generation_id in stale:
        _publish(tmp_path, generation_id)
    _publish(tmp_path, "20260104T000000Z-current")

    sizes = {g.generation_id: g.size_bytes for g in prune.list_generations(tmp_path)}
    original = prune.shutil.rmtree

    def partially_denied(path):
        if path.name == stale[0]:
            raise PermissionError("read only")
        original(path)

    monkeypatch.setattr(prune.shutil, "rmtree", partially_denied)
    monkeypatch.setattr(
        sys, "argv",
        ["prune", "--performance", str(tmp_path), "--keep-recent", "1",
         "--keep-monthly", "0", "--apply"],
    )
    assert prune.main() == 1
    output = capsys.readouterr()
    assert (tmp_path / "generations" / stale[0]).is_dir()
    assert not (tmp_path / "generations" / stale[1]).exists()
    assert f"Freed {prune.format_size(sizes[stale[1]])}" in output.out
    assert "FAILED to delete" in output.err

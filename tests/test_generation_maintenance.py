"""Publish, pin and prune must agree on the current generation during a write."""

import json
import subprocess
import sys
from pathlib import Path

from weatherquant.performance import snapshot

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


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

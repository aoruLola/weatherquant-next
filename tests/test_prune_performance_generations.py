"""Retention CLI checks using disposable generation directories."""

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import prune_performance_generations as prune

    paths = [tmp_path / "generations" / f"2026100{n}T000000Z-test" for n in (1, 2, 3)]
    from weatherquant.performance.snapshot import build_performance, publish_performance

    for path in paths:
        payloads = build_performance(decisions=[], dataset="fixture")
        if path == paths[0]:
            payloads["opportunities.json"]["data"]["episodes"] = []
            payloads["summary.json"]["data"]["reference_capital"]["results"] = []
        publish_performance(payloads, tmp_path, generation_id=path.name)
    (tmp_path / "current.json").write_text(json.dumps({"generation_id": paths[0].name}))
    monkeypatch.setattr(sys, "argv", ["prune", "--performance", str(tmp_path), "--keep-recent", "1",
                                      "--keep-monthly", "0", "--apply"])
    return prune, paths


@pytest.mark.parametrize("pointer", [None, "{", "[]", '{"generation_id":"absent"}', '{"generation_id":[]}'])
def test_apply_requires_identifiable_current(tree, pointer, tmp_path):
    prune, paths = tree
    target = tmp_path / "current.json"
    if pointer is None:
        target.unlink()
    else:
        target.write_text(pointer)
    assert prune.main() == 1
    assert all(path.is_dir() for path in paths)


def test_preview_and_apply_keep_current_and_newest(tree, monkeypatch):
    prune, (pinned, stale, newest) = tree
    monkeypatch.setattr(sys, "argv", [*sys.argv[:-1], "--dry-run"])
    assert prune.main() == 0 and stale.is_dir()
    monkeypatch.setattr(sys, "argv", [*sys.argv[:-1], "--apply"])
    assert prune.main() == 0
    assert pinned.is_dir() and newest.is_dir() and not stale.exists()


def test_failed_removal_is_reported(tree, monkeypatch, capsys):
    prune, paths = tree

    def denied(path):
        raise PermissionError("read only")

    monkeypatch.setattr(prune.shutil, "rmtree", denied)
    assert prune.main() == 1
    assert "Freed 0.0 B" in capsys.readouterr().out
    assert all(path.is_dir() for path in paths)

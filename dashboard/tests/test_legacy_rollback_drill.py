"""Run both sides of the rollback with the actual historical Python source."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LEGACY_SHA = "e6698dfd39930bf1dd219410ae911b1cf496a982"
LEGACY_ID = "20260101T000000Z-legacy"


def test_historical_dashboard_serves_the_repinned_legacy_generation(tmp_path, monkeypatch):
    # Shallow PR checkouts often lack this object. Never use current source
    # as a substitute for the historical reader.
    if subprocess.run(["git", "cat-file", "-e", LEGACY_SHA], cwd=REPO).returncode:
        pytest.skip(f"paired drill needs historical Git commit {LEGACY_SHA}")
    archive = tmp_path / "legacy.zip"
    subprocess.run(["git", "archive", "--format=zip", "-o", archive, LEGACY_SHA, "src"], cwd=REPO, check=True)
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path / "legacy")
    legacy_src = tmp_path / "legacy" / "src"
    root = tmp_path / "performance"
    env = dict(os.environ, PYTHONPATH=str(legacy_src), PYTHONUTF8="1")
    worker = [sys.executable, str(Path(__file__).resolve()), str(legacy_src), str(root)]
    subprocess.run([*worker, "publish"], env=env, check=True, timeout=60)

    from weatherquant.performance.snapshot import build_performance, publish_performance

    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    from pin_performance_generation import current_pointer
    from pin_performance_generation import main as pin
    from prune_performance_generations import compute_retention, list_generations

    bounded = "20260102T000000Z-bounded"
    publish_performance(build_performance(decisions=[]), root, generation_id=bounded)
    # A broken, newer legacy copy must not displace the verified rollback point.
    broken = root / "generations" / "20260103T000000Z-broken"
    shutil.copytree(root / "generations" / LEGACY_ID, broken)
    manifest_path = broken / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["generation_id"] = broken.name
    manifest_path.write_text(json.dumps(manifest))
    (broken / "edge.json").write_bytes(b"{")
    kept, candidates = compute_retention(
        list_generations(root), current_generation_id=bounded, keep_recent=1, keep_monthly=0
    )
    assert {g.generation_id for g in kept} == {LEGACY_ID, bounded}
    assert [g.generation_id for g in candidates] == [broken.name]
    assert pin(["--performance", str(root), "--generation", broken.name, "--apply"]) == 2
    assert current_pointer(root) == bounded
    assert pin(["--performance", str(root), "--generation", LEGACY_ID, "--apply"]) == 0
    subprocess.run([*worker, "serve"], env=env, check=True, timeout=60)


if __name__ == "__main__":
    src, root = map(Path, sys.argv[1:3])
    from weatherquant.dashboard import api
    from weatherquant.dashboard.config import DashboardSettings
    from weatherquant.performance import snapshot

    assert Path(api.__file__).is_relative_to(src) and Path(snapshot.__file__).is_relative_to(src)
    if sys.argv[3] == "publish":
        rows = [
            {
                "market_id": name,
                "target_date": "2026-01-02",
                "station": "KLGA",
                "model_name": "calibrated",
                "direction": "buy_yes",
                "mode": "LIVE",
                "state": "ACCEPT",
                "decided_at_utc": "2026-01-01T12:00:00+00:00",
                "net_edge_usd": 0.05,
                "market_price": 0.6,
                "model_probability": 0.65,
                "available_depth_usd": 500.0,
            }
            for name in ["old-A", "old-B"]
        ]
        snapshot.publish_performance(
            snapshot.build_performance(decisions=rows, dataset="fixture"), root, generation_id=LEGACY_ID
        )
    else:
        from fastapi.testclient import TestClient

        settings = DashboardSettings(
            performance_data_dir=root,
            collector_data_dir=root.parent / "collector",
            shadow_data_dir=root.parent / "shadow",
            rate_limit_per_minute=0,
        )
        with TestClient(api.create_app(settings)) as client:
            summary = client.get("/api/v1/performance/summary").json()["data"]
            assert summary["generation_id"] == LEGACY_ID
            assert len(summary["data"]["reference_capital"]["results"]) == 2
            page = client.get("/api/v1/performance/opportunities?limit=2").json()["data"]
            assert {row["market_id"] for row in page["items"]} == {"old-A", "old-B"}
            recalculated = client.get("/api/v1/performance/reference-capital?capital_usdc=250")
            recalculated.raise_for_status()
            capital = recalculated.json()["data"]
            assert capital["capital_usdc"] == 250 and capital["sample_size"] == 2

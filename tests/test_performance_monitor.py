"""The performance monitor: gates over the sizes the pipeline promises.

The streaming design (contract §14) makes size properties structural -
aggregates are KB-scale regardless of history, detail lives in bounded
parts, a page is a page. These tests run the monitor's checks against real
published trees (small synthetic ones, plus oversized fakes) so a regression
to the pre-§14 shape - one file growing with the history, the clamp gone -
fails a gate instead of a Dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR = REPO_ROOT / "scripts" / "performance_monitor.py"


def _load_monitor():
    spec = importlib.util.spec_from_file_location("wq_performance_monitor", MONITOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _publish_tree(work: Path, rows: int = 60) -> Path:
    """A real published tree from the production streaming engine."""
    from datetime import UTC, datetime, timedelta

    from weatherquant.performance.streaming import build_performance_streaming

    parts_dir = work / "export" / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    clock = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    lines = []
    for i in range(rows):
        clock += timedelta(seconds=7)
        lines.append(
            json.dumps(
                {
                    "decision_id": f"dec-{i:08d}",
                    "decision_time_utc": clock.isoformat(),
                    "market_id": f"m-{i % 4}",
                    "target_date": "2026-10-16",
                    "model_name": "calibrated",
                    "direction": "buy_yes",
                    "mode": "LIVE",
                    "state": "ACCEPT",
                    "net_edge": 0.05,
                    "market_price": 0.6,
                    "model_probability": 0.65,
                    "available_depth_usd": 500.0,
                    "station": "KLGA",
                    "bracket_label": "81°F",
                }
            )
        )
    (parts_dir / "part-000000001.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )
    from datetime import datetime as dt

    build_performance_streaming(
        export=work / "export",
        out_dir=work / "performance",
        state_db=work / "state.duckdb",
        dataset="fixture",
        now=dt(2026, 10, 26, 12, 0, tzinfo=UTC),
    )
    return work / "performance"


def test_a_real_published_tree_passes_every_gate(tmp_path: Path) -> None:
    monitor = _load_monitor()
    performance = _publish_tree(tmp_path)

    generation, generation_id = monitor._generation_of(performance)
    assert generation is not None and generation_id

    profile = monitor.check_snapshot_profile(generation, max_small_mb=5, max_part_mb=64)
    assert profile["failures"] == [], profile["failures"]
    assert "summary.json" in profile["files"]
    assert "opportunities/" in profile["files"], "detail must be recognised as sharded"

    generations = monitor.check_generations(performance, max_total_mb=1024)
    assert generations["count"] == 1 and generations["failures"] == []

    code = subprocess.run(
        [sys.executable, str(MONITOR), "--performance", str(performance)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert code.returncode == 0, code.stdout + code.stderr
    assert "RESULT: PASSED" in code.stdout


def test_an_aggregate_growing_with_history_fails_the_small_file_gate(tmp_path: Path) -> None:
    """The pre-§14 shape: summary.json at 131 MB. The gate must catch it."""
    monitor = _load_monitor()
    performance = _publish_tree(tmp_path)
    generation, _ = monitor._generation_of(performance)
    (generation / "summary.json").write_bytes(b"x" * (6 * 1024 * 1024))

    profile = monitor.check_snapshot_profile(generation, max_small_mb=5, max_part_mb=64)
    assert profile["failures"], "a 6 MiB aggregate must trip the 5 MiB gate"
    assert "growing with the history" in profile["failures"][0]

    code = subprocess.run(
        [sys.executable, str(MONITOR), "--performance", str(performance)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
    )
    assert code.returncode == 1
    assert "RESULT: FAILED" in code.stdout


def test_the_generations_tree_gate_demands_a_prune(tmp_path: Path) -> None:
    monitor = _load_monitor()
    performance = _publish_tree(tmp_path)
    generations_dir = performance / "generations"
    current, _ = monitor._generation_of(performance)
    # Ten stale copies of the current generation: the retention problem in
    # miniature, without 2.2 GB/day of real publishing.
    for i in range(10):
        stale = generations_dir / f"20261001T0000{i:02d}Z-aaaaaaaaaaaa"
        stale.mkdir()
        for item in current.iterdir():
            if item.is_file():
                (stale / item.name).write_bytes(item.read_bytes())

    generations = monitor.check_generations(performance, max_total_mb=0.05)
    assert generations["failures"], "a tree past the size gate must demand a prune"
    assert "prune_performance_generations.py" in generations["failures"][0]


def test_the_limit_clamp_gate_over_a_stand_in_transport(tmp_path: Path) -> None:
    """A clamp that stops clamping fails the monitor without a socket."""
    import httpx

    monitor = _load_monitor()

    def handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params.get("limit", "50"))
        served = min(limit, 200)
        return httpx.Response(
            200,
            json={"data": {"items": [{"i": n} for n in range(served)]}},
        )

    report = monitor.probe_dashboard(
        "http://dashboard.test",
        token=None,
        probes=2,
        max_p95_ms=1000.0,
        max_response_mb=32.0,
        transport=httpx.MockTransport(handler),
    )
    assert report["failures"] == [], report["failures"]
    assert report["limit_clamp"]["served_items"] == 200

    def runaway_handler(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params.get("limit", "50"))
        return httpx.Response(
            200,
            json={"data": {"items": [{"i": n} for n in range(limit)]}},
        )

    broken = monitor.probe_dashboard(
        "http://dashboard.test",
        token=None,
        probes=1,
        max_p95_ms=1000.0,
        max_response_mb=32.0,
        transport=httpx.MockTransport(runaway_handler),
    )
    assert any("clamp" in failure for failure in broken["failures"]), broken["failures"]


def test_an_unreachable_dashboard_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    monitor = _load_monitor()
    report = monitor.probe_dashboard(
        "http://127.0.0.1:9",
        token=None,
        probes=1,
        max_p95_ms=1000.0,
        max_response_mb=32.0,
    )
    assert report["failures"], "unreachable must be reported"

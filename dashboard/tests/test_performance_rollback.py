"""Rollback: which generation is current, and what a reader can still read.

A publish can be wrong in a way that is not detectable from inside it - a
number that looks surprising, a generation built from the wrong export - and
the recovery has to be answerable without deleting anything. Two things make
that possible, and both are tested here:

* the pointer can be moved back, so the Dashboard serves a generation that is
  already known good;
* the *format* of a generation is knowable, so an operator can tell whether the
  running Dashboard can read it - because this round changed the snapshot
  shape, and a Dashboard cannot be rolled back on its own.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.performance.snapshot import (
    build_performance,
    publish_performance,
    resolve_generation,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from pin_performance_generation import (
    BOUNDED,
    LEGACY,
    UNKNOWN,
    current_pointer,
    list_generations,
)
from pin_performance_generation import (
    main as pin_main,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _rows(count: int, *, market_prefix: str = "m") -> list[dict]:
    return [
        {
            "market_id": f"{market_prefix}-{index}",
            "target_date": "2026-10-14",
            "model_name": "calibrated",
            "direction": "buy_yes",
            "mode": "LIVE",
            "state": "ACCEPT",
            "decided_at_utc": (NOW.replace(hour=index % 12, minute=index % 60)).isoformat(),
            "net_edge_usd": 0.05,
            "market_price": 0.6,
            "model_probability": 0.65,
            "available_depth_usd": 500.0,
            "station": "KLGA",
            "bracket_label": "81°F",
        }
        for index in range(count)
    ]


def _publish(root: Path, count: int, *, market_prefix: str = "m") -> str:
    publish_performance(
        build_performance(decisions=_rows(count, market_prefix=market_prefix), now=NOW, dataset="fixture"),
        root,
    )
    pointer = current_pointer(root)
    assert pointer is not None
    return pointer


def _client(root: Path) -> TestClient:
    return TestClient(
        create_app(
            DashboardSettings(
                collector_data_dir=root.parent / "collector",
                shadow_data_dir=root.parent / "shadow",
                performance_data_dir=root,
                rate_limit_per_minute=0,
            )
        )
    )


# ---------------------------------------------------------------------------
# moving the pointer
# ---------------------------------------------------------------------------


def test_generations_are_listed_with_their_format_and_completeness(tmp_path) -> None:
    root = tmp_path / "performance"
    first = _publish(root, 3)
    second = _publish(root, 6)

    listed = {info.generation_id: info for info in list_generations(root)}
    assert set(listed) == {first, second}
    assert all(info.complete for info in listed.values())
    assert all(info.format == BOUNDED for info in listed.values())
    assert current_pointer(root) == second, "the newest publish is current"


def test_pinning_back_moves_the_pointer_and_keeps_everything_else(tmp_path) -> None:
    """The recovery action is one pointer, not a deletion."""
    root = tmp_path / "performance"
    first = _publish(root, 3)
    second = _publish(root, 6)
    state_before = sorted(path.name for path in (root / "generations").iterdir())

    assert pin_main(["--performance", str(root), "--generation", first, "--apply"]) == 0

    assert current_pointer(root) == first
    # The generation that was current is still there, untouched, and so is
    # every other file. Nothing was destroyed to roll back.
    assert sorted(path.name for path in (root / "generations").iterdir()) == state_before
    assert (root / "generations" / second / "summary.json").exists()


def test_a_dry_run_does_not_move_the_pointer(tmp_path) -> None:
    root = tmp_path / "performance"
    first = _publish(root, 3)
    second = _publish(root, 6)

    assert pin_main(["--performance", str(root), "--generation", first]) == 0
    assert current_pointer(root) == second, "dry run must not write"


def test_an_incomplete_generation_is_refused(tmp_path) -> None:
    root = tmp_path / "performance"
    first = _publish(root, 3)
    second = _publish(root, 6)
    (root / "generations" / first / "edge.json").unlink()

    assert pin_main(["--performance", str(root), "--generation", first, "--apply"]) == 2
    assert current_pointer(root) == second, "a refusal must leave the pointer alone"


def test_an_unknown_format_is_refused(tmp_path) -> None:
    """A generation no Dashboard can be matched to is not a rollback target."""
    root = tmp_path / "performance"
    _publish(root, 3)
    second = _publish(root, 6)
    target = root / "generations" / second
    payload = json.loads((target / "opportunities.json").read_text())
    payload["data"]["episodes"] = {"something": "else"}
    (target / "opportunities.json").write_text(json.dumps(payload))

    assert next(g for g in list_generations(root) if g.generation_id == second).format == UNKNOWN
    assert pin_main(["--performance", str(root), "--generation", second, "--apply"]) == 2


# ---------------------------------------------------------------------------
# the drill: serving a generation after the pointer moved
# ---------------------------------------------------------------------------


def test_after_a_pin_the_api_serves_the_pinned_generation_whole(tmp_path) -> None:
    """Every file, and every part, comes from the generation the pointer names."""
    root = tmp_path / "performance"
    first = _publish(root, 5, market_prefix="old")
    second = _publish(root, 40, market_prefix="new")

    with _client(root) as client:
        before = client.get("/api/v1/performance/opportunities?limit=200").json()
        assert before["data"]["generation_id"] == second
        assert all(row["market_id"].startswith("new") for row in before["data"]["items"])

        assert pin_main(["--performance", str(root), "--generation", first, "--apply"]) == 0

        after = client.get("/api/v1/performance/opportunities?limit=200").json()
        assert after["data"]["generation_id"] == first
        assert len(after["data"]["items"]) == 5
        assert all(row["market_id"].startswith("old") for row in after["data"]["items"])
        summary = client.get("/api/v1/performance/summary").json()["data"]
        assert summary["generation_id"] == first
        assert summary["data"]["opportunity_episodes"] == 5

        # A cursor minted against the new generation is refused by the old one,
        # rather than being re-pointed at rows it never described.
        cursor = before["data"]["next_cursor"]
        if cursor is not None:
            refused = client.get(f"/api/v1/performance/opportunities?cursor={cursor}")
            assert refused.status_code == 409


# ---------------------------------------------------------------------------
# why the image and the snapshots roll back together
# ---------------------------------------------------------------------------


def _old_dashboard_reader(payload: dict) -> list[dict]:
    """The reader this round replaced, quoted verbatim from the old endpoint.

    ``dashboard/performance.py`` used to do exactly these two things: take
    the inline array and slice it, and read the capital results list. Kept
    here because the incompatibility it creates is the reason a rollback is a
    *pair* of actions, and an assertion is a better record than a sentence.
    """
    rows = list(payload["data"].get("episodes") or [])
    page = rows[0:2]
    for row in page:
        row["episode_id"]  # the old slice fed straight into a row dict
    return page


def test_a_new_snapshot_breaks_the_old_reader(tmp_path) -> None:
    """So the Dashboard image and the snapshots have to move together."""
    root = tmp_path / "performance"
    _publish(root, 6)
    generation = resolve_generation(root)
    assert generation.directory is not None
    payload = json.loads((generation.directory / "opportunities.json").read_text())

    # ``list()`` of a descriptor yields its keys, and a key is not a row.
    with pytest.raises(TypeError):
        _old_dashboard_reader(payload)

    summary = json.loads((generation.directory / "summary.json").read_text())
    capital = summary["data"]["reference_capital"]
    assert "results" not in capital, "the old reader expects a list that is gone"


def test_the_new_reader_still_reads_a_legacy_snapshot(tmp_path) -> None:
    """The upgrade direction is covered: an old generation stays readable.

    That is what makes the *first* deploy safe - the new Dashboard can read the
    generation the old one published, before the new publisher has run.
    """
    root = tmp_path / "performance"
    _publish(root, 6)
    generation = resolve_generation(root)
    assert generation.directory is not None
    target = generation.directory / "opportunities.json"

    # Rewrite the detail the way the old publisher wrote it: inline, and with
    # the capital block the old shape carried.
    payload = json.loads(target.read_text())
    descriptor = payload["data"]["episodes"]
    rows: list[dict] = []
    for part in descriptor["partitions"]:
        rows.extend(
            json.loads((generation.directory / descriptor["directory"] / part["file"]).read_text())
        )
    payload["data"]["episodes"] = rows
    payload["data"]["total"] = len(rows)
    target.write_text(json.dumps(payload))
    summary_path = generation.directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    capital = summary["data"]["reference_capital"]
    capital["results"] = capital.pop("results_sample")
    summary_path.write_text(json.dumps(summary))

    assert next(
        g for g in list_generations(root) if g.directory == generation.directory
    ).format == LEGACY
    with _client(root) as client:
        page = client.get("/api/v1/performance/opportunities?limit=2").json()["data"]
        assert len(page["items"]) == 2
        capital_out = client.get("/api/v1/performance/reference-capital").json()["data"]
        # The summary's own capital block is passed through, whatever shape it
        # has; only the on-demand simulation is ours.
        assert capital_out["scope"] == "window"


@pytest.mark.parametrize("damage", ["part", "manifest_id"])
def test_pin_refuses_a_generation_that_does_not_match_its_manifest(tmp_path, damage):
    root = tmp_path / "performance"
    target = _publish(root, 4)
    current = _publish(root, 6)
    directory = root / "generations" / target
    if damage == "part":
        next((directory / "opportunities").glob("part-*.json")).unlink()
    else:
        path = directory / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["generation_id"] = current
        path.write_text(json.dumps(manifest))
    assert pin_main(["--performance", str(root), "--generation", target, "--apply"]) == 2
    assert current_pointer(root) == current

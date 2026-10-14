"""Performance snapshots: one computation, one published answer.

The property worth guarding is that nothing downstream ever recomputes a
figure. So these tests check the envelope carries the scope and the
warnings with the number - because a total that travels without either is
a total that will one day be repeated as a return.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from weatherquant.performance.snapshot import (
    SNAPSHOT_FILES,
    build_performance,
    publish_performance,
    resolve_generation,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _decision(i: int, *, edge=0.05, state="ACCEPT", model="calibrated", target=None, market=None):
    return {
        "market_id": market or f"m-{i % 4}",
        "target_date": target or f"2026-10-{14 + (i % 5):02d}",
        "model_name": model,
        "direction": "buy_yes",
        "mode": "LIVE",
        "state": state,
        "decided_at_utc": NOW.isoformat(),
        "net_edge_usd": edge,
        "market_price": 0.6,
        "model_probability": 0.65,
        "available_depth_usd": 500.0,
        "station": "KLGA",
        "bracket_label": "81°F",
        "market_question": "Will it be 81F?",
    }


def _decisions(n=30):
    return [_decision(i, edge=0.03 + (i % 7) * 0.01) for i in range(n)]


def _settlements(payouts=None, *, verified=True):
    """Settlement answers keyed like the index expects."""
    out = []
    for i in range(4):
        payout = 1.0 if (payouts or {}).get(f"m-{i}", 1.0) == 1.0 else 0.0
        out.append(
            {
                "market_id": f"m-{i}",
                "target_date": f"2026-10-{14 + (i % 5):02d}",
                "settlement_status": "RESOLVED",
                "resolved_outcome": payout,
                "verification_status": "VERIFIED" if verified else "UNVERIFIED",
            }
        )
    return out


# --- the envelope is the contract --------------------------------------------


def test_all_six_snapshots_are_built() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)

    assert set(payloads) == set(SNAPSHOT_FILES)


def test_every_payload_carries_the_envelope_fields() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)

    for name, payload in payloads.items():
        assert payload["performance_schema_version"] == 1, name
        assert payload["generated_at_utc"], name
        assert payload["source"] == "weatherquant-research", name
        assert payload["as_of_utc"], name
        assert payload["dataset"] == "live", name
        assert payload["stale_after_seconds"] > 0, name
        assert payload["freshness"]["state"] == "fresh", name
        assert isinstance(payload["statistical_scope"], list) and payload["statistical_scope"], name
        assert isinstance(payload["warnings"], list), name
        assert "data" in payload, name


def test_the_edge_total_travels_with_a_warning_that_it_is_not_profit() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)
    summary = payloads["summary.json"]

    assert any("not profit" in w for w in summary["warnings"])
    assert any("no orders" in w for w in summary["warnings"])


def test_sample_size_is_reported_not_left_to_the_reader() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)

    assert payloads["edge.json"]["sample_size"] > 0
    assert payloads["opportunities.json"]["sample_size"] > 0


# --- the numbers --------------------------------------------------------------

def test_observation_and_opportunity_sums_are_both_present_and_different() -> None:
    """Repeated evaluations make the first larger than the second by design."""
    payloads = build_performance(decisions=_decisions(30), now=NOW)
    summary = payloads["summary.json"]["data"]

    assert summary["observation_edge_sum"] is not None
    assert summary["unique_opportunity_edge_sum"] is not None
    assert summary["accept_observations"] >= summary["opportunity_episodes"]


def test_the_win_rate_is_none_until_there_are_enough_samples() -> None:
    """Four resolved markets is not a rate the contract lets us state."""
    payloads = build_performance(decisions=_decisions(), settlements=_settlements(), now=NOW)
    summary = payloads["summary.json"]["data"]

    assert summary["realized_directional_win_rate"] is None
    assert summary["insufficient_reason"] is not None


def test_unsettled_data_yields_an_explicit_refusal_not_a_zero() -> None:
    payloads = build_performance(decisions=_decisions(), settlements=[], now=NOW)
    summary = payloads["summary.json"]["data"]

    assert summary["realized_directional_win_rate"] is None
    assert summary["counts"]["UNAVAILABLE"] >= 0
    assert summary["verified_opportunities"] == 0


def test_reference_capital_is_declared_hypothetical() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)
    capital = payloads["summary.json"]["data"]["reference_capital"]

    assert capital["capital_usdc"] == 100.0
    assert capital["statistical_scope"] == ["reference_capital_hypothetical"]
    assert any("not a funded account" in w for w in capital["warnings"])


def test_without_books_the_portfolio_admits_it_cannot_backtest() -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)
    capital = payloads["summary.json"]["data"]["reference_capital"]

    assert capital["backtestable"] is False
    assert capital["not_backtestable_reasons"]


# --- settlements --------------------------------------------------------------

def test_verified_and_unverified_are_kept_apart() -> None:
    payloads = build_performance(
        decisions=_decisions(), settlements=_settlements(verified=False), now=NOW
    )
    win_rate = payloads["win-rate.json"]["data"]["overall"]

    assert win_rate["valid_samples"] == 0, "an unverified settlement must not be scored"
    assert win_rate["counts"]["UNVERIFIABLE"] > 0


def test_settlements_are_indexed_by_market_and_target_date() -> None:
    payloads = build_performance(decisions=_decisions(), settlements=_settlements(), now=NOW)
    data = payloads["settlements.json"]["data"]

    assert data["answers"], "the index must produce entries"
    assert all("status" in a for a in data["answers"])


# --- publication ---------------------------------------------------------------


def _published(out: Path, name: str) -> Path:
    """Follow the generation pointer, the way the dashboard does.

    Snapshots now live under `generations/<id>/`; a test that read a flat
    path would be asserting the layout that was deliberately removed.
    """
    generation_dir, manifest = resolve_generation(out)
    assert generation_dir is not None, "nothing was published"
    assert manifest is not None, "the published generation has no manifest"
    return generation_dir / name


def test_publish_writes_every_declared_file(tmp_path: Path) -> None:
    payloads = build_performance(decisions=_decisions(), now=NOW)
    written = publish_performance(payloads, tmp_path)

    assert len(written) == len(SNAPSHOT_FILES)
    for name in SNAPSHOT_FILES:
        assert _published(tmp_path, name).exists(), name


def test_a_published_file_parses_as_one_document(tmp_path: Path) -> None:
    """Atomic write means a reader sees the whole file or none of it."""
    payloads = build_performance(decisions=_decisions(), now=NOW)
    publish_performance(payloads, tmp_path)

    for name in SNAPSHOT_FILES:
        parsed = json.loads(_published(tmp_path, name).read_text(encoding="utf-8"))
        assert parsed["data"] is not None, name


def test_publishing_an_empty_dataset_still_produces_a_valid_document(tmp_path: Path) -> None:
    """No data is a state the system must express, not one to invent around."""
    payloads = build_performance(decisions=[], settlements=[], now=NOW)
    written = publish_performance(payloads, tmp_path)

    assert len(written) == len(SNAPSHOT_FILES)
    summary = json.loads(_published(tmp_path, "summary.json").read_text(encoding="utf-8"))
    assert summary["data"]["observation_edge_sum"] is None
    assert summary["data"]["opportunity_episodes"] == 0
    assert summary["data"]["realized_directional_win_rate"] is None


def test_supplied_episodes_are_used_as_given() -> None:
    """A caller that already built episodes must not get a second definition."""
    from weatherquant.performance.episodes import build_episodes

    episodes, _ = build_episodes(_decisions())
    payloads = build_performance(decisions=_decisions(), episodes=episodes, now=NOW)

    assert payloads["summary.json"]["data"]["opportunity_episodes"] == len(episodes)


# --- generation consistency --------------------------------------------------


def test_every_file_carries_one_generation_id(tmp_path: Path) -> None:
    """A batch must be identifiable, not merely co-located.

    Co-location is not consistency: six files in one directory can still be
    three from one run and three from the last. The id is what a reader
    compares.
    """
    payloads = build_performance(decisions=_decisions(), now=NOW)
    publish_performance(payloads, tmp_path)

    generation_dir, manifest = resolve_generation(tmp_path)
    ids = set()
    for name in SNAPSHOT_FILES:
        payload = json.loads((generation_dir / name).read_text(encoding="utf-8"))
        ids.add(payload["generation_id"])

    assert len(ids) == 1, f"a published batch carries more than one generation: {ids}"
    assert manifest["generation_id"] in ids


def test_the_manifest_records_what_the_batch_was(tmp_path: Path) -> None:
    """Provenance travels with the batch, not with the directory."""
    payloads = build_performance(decisions=_decisions(), now=NOW, dataset="fixture")
    publish_performance(
        payloads,
        tmp_path,
        watermark=("2026-10-14T08:00:00+00:00", "id-0042"),
        coverage={"rows": 30},
    )

    _, manifest = resolve_generation(tmp_path)

    assert manifest["dataset"] == "fixture"
    assert manifest["source"] == "weatherquant-research"
    assert manifest["watermark"] == ["2026-10-14T08:00:00+00:00", "id-0042"]
    assert manifest["coverage"] == {"rows": 30}
    assert manifest["published_at_utc"]
    assert set(manifest["files"]) == set(SNAPSHOT_FILES)


def test_a_second_publish_replaces_the_pointer_not_the_files(tmp_path: Path) -> None:
    """The previous generation stays intact - that is the rollback story."""
    publish_performance(build_performance(decisions=_decisions(n=10), now=NOW), tmp_path)
    first, first_manifest = resolve_generation(tmp_path)

    publish_performance(build_performance(decisions=_decisions(n=20), now=NOW), tmp_path)
    second, second_manifest = resolve_generation(tmp_path)

    assert first != second, "a new publish must select a new generation"
    assert first.exists(), "the previous generation was destroyed"
    assert first_manifest["generation_id"] != second_manifest["generation_id"]


def test_a_republish_of_the_same_data_keeps_the_same_id(tmp_path: Path) -> None:
    """The id names the batch, not the clock that wrote it."""
    payloads = build_performance(decisions=_decisions(), now=NOW)
    publish_performance(payloads, tmp_path, generation_id="fixed-id")
    _, manifest = resolve_generation(tmp_path)

    assert manifest["generation_id"] == "fixed-id"
    assert (tmp_path / "generations" / "fixed-id" / "summary.json").exists()


def test_a_pointer_to_an_incomplete_generation_is_refused(tmp_path: Path) -> None:
    """A publish that died before its manifest must not become readable."""
    payloads = build_performance(decisions=_decisions(), now=NOW)
    publish_performance(payloads, tmp_path)

    generation_dir, manifest = resolve_generation(tmp_path)
    assert manifest is not None
    (generation_dir / "manifest.json").unlink()

    resolved_dir, resolved_manifest = resolve_generation(tmp_path)
    assert resolved_manifest is None
    assert resolved_dir is None, (
        "a generation without a manifest is an incomplete publish, not an "
        "absent backfill"
    )


def test_an_unparseable_pointer_yields_nothing_rather_than_a_crash(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "current.json").write_text("{ not json", encoding="utf-8")

    assert resolve_generation(tmp_path) == (None, None)


def test_a_file_from_another_generation_is_refused_by_the_source(tmp_path: Path) -> None:
    """Read-time check: the file must belong to the batch the pointer names."""
    # Only the performance directory matters here; the collector and shadow
    # trees may be empty, because the assertion is about one snapshot's
    # provenance and nothing else.
    root = tmp_path / "tree"
    (root / "collector").mkdir(parents=True, exist_ok=True)
    (root / "shadow").mkdir(parents=True, exist_ok=True)
    payloads = build_performance(decisions=_decisions(), now=NOW)
    publish_performance(payloads, root / "performance", generation_id="gen-a")
    # Tamper: leave a file that claims an older batch inside the current one.
    directory, manifest = resolve_generation(root / "performance")
    stale = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    stale["generation_id"] = "gen-000"
    (directory / "summary.json").write_text(json.dumps(stale), encoding="utf-8")
    assert manifest["generation_id"] == "gen-a"

    from datetime import UTC, datetime

    from weatherquant.dashboard.config import DashboardSettings
    from weatherquant.dashboard.sources import SnapshotSources

    settings = DashboardSettings(
        collector_data_dir=root / "collector",
        shadow_data_dir=root / "shadow",
        performance_data_dir=root / "performance",
    )
    sources = SnapshotSources(settings)
    source = sources.performance_snapshot(
        "performance-summary", "summary.json", datetime.now(tz=UTC)
    )

    assert source.payload is None
    assert "refusing to mix generations" in (source.reason or "")

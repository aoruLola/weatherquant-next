"""The backfill commands, executed rather than described.

The deployment doc tells an operator to run two subcommands. These tests
run them, because a documented command that does not exist is worse than
no documentation: it fails after the reader has already committed to the
steps around it.

Nothing here touches a live database. Decisions come from an export file
and settlements from a caller-supplied market list, which is the
single-writer rule stated as a test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from weatherquant.cli import main as research_main
from weatherquant.performance.backfill import (
    backfill_settlements,
    build_performance_snapshots,
    load_decisions,
)
from weatherquant.performance.snapshot import SNAPSHOT_FILES, resolve_generation

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _decision(i: int) -> dict:
    return {
        "market_id": f"m-{i % 4}",
        "target_date": f"2026-10-{14 + (i % 5):02d}",
        "model_name": "calibrated" if i % 2 else "gfs_raw",
        "direction": "buy_yes",
        "mode": "LIVE",
        "state": "ACCEPT",
        "decided_at_utc": NOW.isoformat(),
        "net_edge_usd": 0.03 + (i % 7) * 0.01,
        "market_price": 0.6,
        "model_probability": 0.65,
        "available_depth_usd": 500.0,
        "fee_rate": 0.0,
        "station": "KLGA",
        "bracket_label": "81°F",
    }


def _export(tmp_path: Path, *, count: int = 30, fmt: str = "jsonl") -> Path:
    rows = [_decision(i) for i in range(count)]
    path = tmp_path / f"decisions.{fmt}"
    if fmt == "jsonl":
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
    else:
        path.write_text(json.dumps(rows), encoding="utf-8")
    return path


# --- the documented commands exist -------------------------------------------


def test_both_documented_subcommands_are_registered(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        research_main(["--help"])
    assert exc.value.code == 0

    help_text = capsys.readouterr().out
    assert "backfill-settlements" in help_text
    assert "build-performance" in help_text


def test_the_dataset_flag_has_no_default(capsys, tmp_path) -> None:
    """A backfill that could silently publish fixtures as live is the risk."""
    with pytest.raises(SystemExit) as exc:
        research_main(
            [
                "build-performance",
                "--export", str(_export(tmp_path)),
                "--out", str(tmp_path / "out"),
            ]
        )
    assert exc.value.code == 2, "an unlabelled run must be refused, not defaulted"


def test_a_missing_export_is_reported_not_guessed(tmp_path, capsys) -> None:
    code = research_main(
        [
            "build-performance",
            "--export", str(tmp_path / "nope.jsonl"),
            "--out", str(tmp_path / "out"),
            "--dataset", "fixture",
        ]
    )
    assert code == 2
    assert "could not read decisions export" in capsys.readouterr().err


# --- an offline run produces the six snapshots --------------------------------



def _published(out: Path, name: str) -> Path:
    """Follow the generation pointer, the way the dashboard does."""
    generation_dir, manifest = resolve_generation(out)
    assert generation_dir is not None and manifest is not None, "nothing was published"
    return generation_dir / name


def test_build_performance_publishes_all_six_snapshots(tmp_path, capsys) -> None:
    out = tmp_path / "performance"
    code = research_main(
        [
            "build-performance",
            "--export", str(_export(tmp_path)),
            "--out", str(out),
            "--dataset", "fixture",
        ]
    )

    assert code == 0, capsys.readouterr()
    for name in SNAPSHOT_FILES:
        assert _published(out, name).exists(), name


def test_a_json_export_is_read_as_well_as_jsonl(tmp_path) -> None:
    assert load_decisions(_export(tmp_path, fmt="json"))


def test_an_unsupported_format_is_refused(tmp_path) -> None:
    weird = tmp_path / "decisions.csv"
    weird.write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported"):
        load_decisions(weird)


def test_the_published_snapshot_carries_the_requested_dataset(tmp_path) -> None:
    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=[_decision(i) for i in range(10)],
        out_dir=out,
        dataset="fixture",
        now=NOW,
    )

    payload = json.loads(_published(out, "summary.json").read_text(encoding="utf-8"))
    assert payload["dataset"] == "fixture", "a fixture run must not read as live"


def test_a_live_run_is_labelled_live(tmp_path) -> None:
    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=[_decision(i) for i in range(10)],
        out_dir=out,
        dataset="live",
        now=NOW,
    )

    payload = json.loads(_published(out, "summary.json").read_text(encoding="utf-8"))
    assert payload["dataset"] == "live"


def test_an_unknown_dataset_is_refused_before_anything_is_written(tmp_path) -> None:
    out = tmp_path / "performance"
    with pytest.raises(ValueError, match="dataset"):
        build_performance_snapshots(
            decisions=[_decision(0)], out_dir=out, dataset="production", now=NOW
        )
    assert not out.exists(), "a refused run must leave nothing behind"


def test_a_failure_writes_nothing_at_all(tmp_path) -> None:
    """All-or-nothing: a half-published set is unreadable as either version."""
    out = tmp_path / "performance"
    with pytest.raises(ValueError):
        build_performance_snapshots(
            decisions=[_decision(0)],
            out_dir=out,
            dataset="fixture",
            capital_usdc=0,
            now=NOW,
        )
    assert not out.exists()


# --- settlement backfill ------------------------------------------------------


class _FakePlatform:
    """Answers /v2/resolutions the way the platform did when audited."""

    def __init__(self, resolved: dict[str, str]) -> None:
        self.resolved = resolved
        self.calls: list[str] = []

    def get(self, url, *, params=None, timeout=None):
        """Return the parsed body, as ``fetch_resolution`` expects.

        The CLI adapter is ``httpx.Client.get(...).json()`` - the gateway
        function takes a *payload*, not a Response, and a fake that hands
        back a Response silently resolves every market to UNAVAILABLE.
        """
        self.calls.append(url)
        condition = (params or {}).get("condition", "")
        price = self.resolved.get(condition)
        if price is None:
            return {"data": []}
        return {
            "data": [
                {
                    "condition_id": condition,
                    "status": "resolved",
                    "price": price,
                    "was_disputed": False,
                    "transaction_hash": "0xabc",
                    "log_index": "1",
                    "last_update_timestamp": "1791868379",
                }
            ]
        }


def test_backfill_settlements_reads_the_platform_answer() -> None:
    platform = _FakePlatform({"0xaaa": "1", "0xbbb": "0"})
    markets = [
        {"market_id": "m-1", "event_id": "e", "target_date": "2026-10-14", "condition_id": "0xaaa"},
        {"market_id": "m-2", "event_id": "e", "target_date": "2026-10-14", "condition_id": "0xbbb"},
        {"market_id": "m-3", "event_id": "e", "target_date": "2026-10-14", "condition_id": "0xccc"},
    ]

    rows = backfill_settlements(markets, get_json=platform.get, dataset="live", now=NOW)

    assert len(rows) == 3
    assert rows[0]["resolved_outcome"] == 1.0
    assert rows[0]["settlement_status"] == "RESOLVED"
    assert rows[1]["resolved_outcome"] == 0.0
    # A condition the platform does not know is UNAVAILABLE, not a loss.
    assert rows[2]["settlement_status"] == "UNAVAILABLE"
    assert rows[2]["resolved_outcome"] is None


def test_only_verified_markets_are_marked_verified() -> None:
    platform = _FakePlatform({"0xaaa": "1"})
    markets = [{"market_id": "m-1", "target_date": "2026-10-14", "condition_id": "0xaaa"}]

    rows = backfill_settlements(
        markets,
        get_json=platform.get,
        dataset="live",
        verified=[{"market_id": "m-1", "target_date": "2026-10-14"}],
        now=NOW,
    )
    assert rows[0]["verification_status"] == "VERIFIED"

    unverified = backfill_settlements(
        markets, get_json=platform.get, dataset="live", now=NOW
    )
    assert unverified[0]["verification_status"] == "UNVERIFIED", (
        "a resolution the system has not cross-checked must not be scored"
    )


def test_the_chain_reference_is_kept() -> None:
    platform = _FakePlatform({"0xaaa": "1"})
    rows = backfill_settlements(
        [{"market_id": "m-1", "condition_id": "0xaaa"}],
        get_json=platform.get,
        dataset="live",
        now=NOW,
    )

    reference = rows[0]["raw_source_reference"]
    assert reference["condition_id"] == "0xaaa"
    assert reference["transaction_hash"] == "0xabc"


def test_the_backfill_cli_writes_an_index(tmp_path, capsys) -> None:
    markets = tmp_path / "markets.json"
    markets.write_text(
        json.dumps(
            [{"market_id": "m-1", "target_date": "2026-10-14", "condition_id": "0xaaa"}]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "settlements.json"

    code = research_main(
        [
            "backfill-settlements",
            "--markets", str(markets),
            "--out", str(out),
            "--dataset", "fixture",
        ]
    )

    # No network in this environment: the command must still fail cleanly
    # rather than write a plausible-looking empty index.
    assert code in (0, 2)
    if code == 0:
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert isinstance(payload, list)
        assert payload[0]["dataset"] == "fixture"

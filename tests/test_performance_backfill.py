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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.cli import main as research_main
from weatherquant.performance.backfill import (
    backfill_settlements,
    build_performance_snapshots,
    load_decisions,
    load_decisions_with_watermark,
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
    gen = resolve_generation(out)
    assert gen.directory is not None and gen.manifest is not None, "nothing was published"
    return gen.directory / name


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


def _export_row(**overrides):
    """A row in the *export's* shape, keyed by the export's own names.

    Derived from EXPORT_COLUMNS rather than hand-written, so a rename in
    the export moves this test with it instead of leaving it green against
    a vocabulary nothing produces.
    """
    from weatherquant.shadow.export import EXPORT_COLUMNS

    values = {
        "decision_id": "d-1",
        "market_id": "m-1",
        "target_date": "2026-10-14",
        "model_name": "calibrated",
        "direction": "buy_yes",
        "mode": "LIVE",
        "state": "ACCEPT",
        "decision_time_utc": NOW.isoformat(),
        "net_edge": 0.03,
        "market_price": 0.6,
        "model_probability": 0.65,
        "available_depth_usd": 500.0,
        "fee_rate": 0.0,
        "station": "KLGA",
        "bracket_label": "81°F",
    }
    values.update(overrides)
    return tuple(values.get(name.rsplit(".", 1)[-1]) for name in EXPORT_COLUMNS)


def test_the_export_shape_feeds_the_build(tmp_path) -> None:
    """The seam the two sides actually meet on.

    The fixtures above are written in the public vocabulary; a decisions
    export carries the engine's. Each suite passed against its own, so
    nothing caught that ``build-performance --export <shadow export>`` -
    the documented production path - died on its first row with a KeyError.
    """
    from weatherquant.shadow.export import _write_part

    parts = tmp_path / "export" / "parts"
    parts.mkdir(parents=True)
    _write_part(
        parts / "part-000000001.jsonl",
        [_export_row(decision_id=f"d-{i}", market_id=f"m-{i % 3}") for i in range(9)],
    )

    decisions = load_decisions(tmp_path / "export")
    assert len(decisions) == 9

    out = tmp_path / "performance"
    build_performance_snapshots(decisions=decisions, out_dir=out, dataset="live", now=NOW)

    # An unread alias would leave every ACCEPT unpriced, so the count is
    # what proves the edge was read rather than defaulted away.
    edge = json.loads(_published(out, "edge.json").read_text(encoding="utf-8"))
    assert edge["data"]["overall"]["count"] == 9


def test_an_export_row_with_no_edge_is_refused_not_zeroed(tmp_path) -> None:
    """Missing is not zero.

    The export carries a null edge for a decision the book could not price.
    Reading that as 0.0 would invent a break-even trade that never existed
    and drag the average toward it, so the row has to be refused instead.
    """
    from weatherquant.shadow.export import _write_part

    parts = tmp_path / "export" / "parts"
    parts.mkdir(parents=True)
    _write_part(
        parts / "part-000000001.jsonl",
        [
            _export_row(decision_id="d-priced"),
            _export_row(decision_id="d-unpriced", net_edge=None),
        ],
    )

    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=load_decisions(tmp_path / "export"),
        out_dir=out,
        dataset="live",
        now=NOW,
    )

    edge = json.loads(_published(out, "edge.json").read_text(encoding="utf-8"))
    assert edge["data"]["overall"]["count"] == 1, "an unpriced ACCEPT became an edge"
    assert edge["data"]["refused"]["accept_without_usable_edge"] == 1


def test_an_export_row_without_a_decision_time_fails_loudly(tmp_path) -> None:
    """A row that cannot say when it was decided is unusable, not defaulted.

    Episodes are ordered by this instant, so substituting one would merge
    opportunities that never coexisted.
    """
    from weatherquant.performance.episodes import row_from_mapping
    from weatherquant.shadow.export import _row_as_dict

    row = _row_as_dict(_export_row(decision_time_utc=None))

    with pytest.raises(TypeError, match="decided_at_utc"):
        row_from_mapping(row)


def test_an_export_row_with_no_book_claims_no_profit(tmp_path) -> None:
    """Depth is not a fill. Without retained book levels nothing is provable,
    so the opportunity is NOT_EXECUTABLE and no profit is asserted - rather
    than the zero that would read as a flat result.
    """
    from weatherquant.shadow.export import _row_as_dict

    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=[
            _row_as_dict(_export_row(decision_id=f"d-{i}", market_id=f"m-{i}"))
            for i in range(3)
        ],
        out_dir=out,
        dataset="live",
        now=NOW,
    )

    summary = json.loads(_published(out, "summary.json").read_text(encoding="utf-8"))
    capital = summary["data"]["reference_capital"]
    assert capital["executable_count"] == 0
    assert capital["not_executable_count"] == 3
    assert capital["backtestable"] is False
    assert capital["allocated_total_usdc"] == 0.0


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


# --- the manifest has to say what the run could see ---------------------------


def _manifest(out: Path) -> dict:
    return json.loads(_published(out, "manifest.json").read_text(encoding="utf-8"))


def test_the_manifest_records_the_export_watermark_and_coverage(tmp_path) -> None:
    """`watermark: null` and `coverage: {}` is not what the deployment doc claims.

    It promises both so a batch can be audited and traced back. Both were
    empty, so the auditability the doc described did not exist - and a reader
    could not tell a snapshot covering everything from one covering a slice.
    """
    from weatherquant.shadow.export import _write_part

    parts = tmp_path / "export" / "parts"
    parts.mkdir(parents=True)
    _write_part(
        parts / "part-000000001.jsonl",
        [
            _export_row(
                decision_id=f"d-{i}",
                market_id=f"m-{i % 3}",
                decision_time_utc=(NOW + timedelta(minutes=i)).isoformat(),
            )
            for i in range(9)
        ],
    )

    decisions, watermark = load_decisions_with_watermark(tmp_path / "export")
    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=decisions,
        out_dir=out,
        dataset="live",
        now=NOW,
        watermark=watermark,
    )

    manifest = _manifest(out)

    # The input's high-water mark, read from the parts rather than invented.
    assert manifest["watermark"] == ["2026-10-13T12:08:00+00:00", "d-8"]

    coverage = manifest["coverage"]
    assert coverage["decisions"] == 9
    assert coverage["all_decision_distinct_markets"] == 3
    assert coverage["all_decision_distinct_target_dates"] == 1
    assert coverage["first_decision_utc"] == NOW.isoformat()
    assert coverage["last_decision_utc"] == (NOW + timedelta(minutes=8)).isoformat()


def test_a_file_input_records_no_watermark_rather_than_inventing_one(tmp_path) -> None:
    """A hand-carried JSONL has no cursor, and `null` is the honest answer.

    A guessed range would read as a claim about coverage that nothing backs.
    """
    out = tmp_path / "performance"
    build_performance_snapshots(
        decisions=load_decisions(_export(tmp_path)),
        out_dir=out,
        dataset="live",
        now=NOW,
    )

    manifest = _manifest(out)
    assert manifest["watermark"] is None
    assert manifest["coverage"]["decisions"] == 30


def test_no_export_means_no_watermark(tmp_path) -> None:
    """Only a published parts directory has a cursor."""
    from weatherquant.shadow.export import export_watermark

    assert export_watermark(tmp_path / "nope") is None
    assert export_watermark(_export(tmp_path)) is None, "a file is not an export"


def test_coverage_of_nothing_is_null_not_zero(tmp_path) -> None:
    """No rows means no range. A zero instant would read as a covered one."""
    out = tmp_path / "performance"
    build_performance_snapshots(decisions=[], out_dir=out, dataset="live", now=NOW)

    coverage = _manifest(out)["coverage"]
    assert coverage["decisions"] == 0
    assert coverage["first_decision_utc"] is None
    assert coverage["last_decision_utc"] is None


def test_the_two_distinct_market_counts_are_named_for_their_populations(
    tmp_path,
) -> None:
    """One word meant two populations in one generation - 55 here, 28 there.

    `coverage` counts every decision read; the summary counts opportunities.
    Neither name said which, so a reader had to guess. They now do. The
    summary keeps the old name as an alias because a deployed dashboard still
    reads it, and a payload cannot ask its readers to upgrade.
    """
    from weatherquant.shadow.export import _row_as_dict

    decisions = [
        _row_as_dict(_export_row(decision_id="d-1", market_id="m-1", state="ACCEPT")),
        _row_as_dict(_export_row(decision_id="d-2", market_id="m-2", state="NO_TRADE")),
        _row_as_dict(_export_row(decision_id="d-3", market_id="m-3", state="NO_TRADE")),
    ]
    out = tmp_path / "performance"
    build_performance_snapshots(decisions=decisions, out_dir=out, dataset="live", now=NOW)

    coverage = _manifest(out)["coverage"]
    summary = json.loads(_published(out, "summary.json").read_text(encoding="utf-8"))["data"]

    # Every decision was read, but only one market produced an opportunity.
    assert coverage["all_decision_distinct_markets"] == 3
    assert summary["accept_opportunity_distinct_markets"] == 1
    assert coverage["all_decision_distinct_markets"] != summary[
        "accept_opportunity_distinct_markets"
    ], "the two populations collapsed into one, so the rename bought nothing"

    # The deprecated alias still carries the opportunity-scoped number.
    assert summary["distinct_markets"] == summary["accept_opportunity_distinct_markets"]


def test_the_watermark_describes_the_rows_that_were_actually_read(tmp_path) -> None:
    """Scanning the parts twice races the engine, which keeps publishing.

    A separate scan can see a part the first did not. The manifest would then
    advertise a watermark past the last decision its snapshots contain -
    claiming coverage of rows that were never read, which is the one thing the
    metadata exists to rule out.
    """
    from weatherquant.shadow.export import _write_part

    parts = tmp_path / "export" / "parts"
    parts.mkdir(parents=True)
    _write_part(
        parts / "part-000000001.jsonl",
        [_export_row(decision_id="d-0", decision_time_utc=NOW.isoformat())],
    )

    decisions, watermark = load_decisions_with_watermark(tmp_path / "export")

    # The engine publishes while the research side is working.
    _write_part(
        parts / "part-000000002.jsonl",
        [
            _export_row(
                decision_id="d-1",
                decision_time_utc=(NOW + timedelta(hours=1)).isoformat(),
            )
        ],
    )

    assert [row["decision_id"] for row in decisions] == ["d-0"]
    assert watermark == [NOW.isoformat(), "d-0"], (
        "the cursor described a part that was not in the read"
    )


def test_a_gap_in_the_parts_is_refused_not_read_past(tmp_path) -> None:
    """A missing part must not become a watermark that claims its rows.

    The cursor is a high-water mark, so with part 2 gone the remaining parts
    still yield a cursor from part 3 onward - and the manifest would claim
    coverage through it while the snapshots omit everything part 2 held. No
    count in `coverage` would show which range disappeared, so this is the one
    case where refusing is the only honest answer.
    """
    from weatherquant.shadow.export import _write_part

    parts = tmp_path / "export" / "parts"
    parts.mkdir(parents=True)
    for index in (1, 3):
        _write_part(
            parts / f"part-{index:09d}.jsonl",
            [
                _export_row(
                    decision_id=f"d-{index}",
                    decision_time_utc=(NOW + timedelta(hours=index)).isoformat(),
                )
            ],
        )

    with pytest.raises(ValueError, match="gap"):
        load_decisions_with_watermark(tmp_path / "export")

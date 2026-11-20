"""Live validation report: exact counts, funnel joins, and read-only safety.

Every assertion is a number the report claims, built from a fixture whose
rows are inserted by hand - not by driving the engines - so the test pins the
report's arithmetic rather than the engines' behaviour. The two safety
properties are proven the same way: hashes before/after, and a refused open of
a locked file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from weatherquant.storage.paper import PaperDatabase
from weatherquant.storage.shadow import ShadowDatabase

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_validation_report.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("wq_live_validation_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_mod = _load_report_module()

D1 = "d1" * 16
D2 = "d2" * 16
D3 = "d3" * 16
D4 = "d4" * 16
D5 = "d5" * 16
D6 = "d6" * 16
D7 = "d7" * 16
D8 = "d8" * 16

DAY = datetime(2026, 10, 26, tzinfo=UTC)
NEXT_DAY = datetime(2026, 10, 27, tzinfo=UTC)

DECISION_COLUMNS = (
    "decision_id, run_id, cycle_id, market_id, event_id, target_date, station, direction, "
    "state, primary_reason, snapshot_id, decision_time_utc, snapshot_age_ms, "
    "available_depth_usd, net_edge, proposed_shares, estimated_fill_shares, fee_rate, "
    "mode, created_at_utc, export_seq"
)


def _decision(
    decision_id: str,
    *,
    state: str,
    mode: str,
    when: datetime,
    target_date: str,
    market: str = "mkt-1",
    snapshot: str | None = None,
    age: int | None = None,
    depth: float | None = None,
    net_edge: float | None = None,
    fee: float | None = 0.05,
    proposed: float = 100.0,
    fill: float = 100.0,
    export_seq: int | None = None,
) -> tuple:
    return (
        decision_id,
        "run-1",
        f"cycle-{decision_id[:2]}",
        market,
        "evt-1",
        target_date,
        "KNYC",
        "buy_yes",
        state,
        None,
        snapshot,
        when,
        age,
        depth,
        net_edge,
        proposed,
        fill,
        fee,
        mode,
        when,
        export_seq,
    )


def _build_shadow(path: Path) -> None:
    with ShadowDatabase(path) as db:
        db.executemany(
            f"INSERT INTO shadow_decisions ({DECISION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _decision(
                    D1,
                    state="ACCEPT",
                    mode="LIVE",
                    when=DAY.replace(hour=10, minute=5),
                    target_date="2026-10-26",
                    snapshot="s1",
                    age=20_000,
                    depth=100.0,
                    net_edge=0.03,
                    export_seq=1,
                ),
                _decision(
                    D2,
                    state="REJECT",
                    mode="LIVE",
                    when=DAY.replace(hour=10, minute=15),
                    target_date="2026-10-26",
                    snapshot="s2",
                    age=95_000,
                    depth=100.0,
                    net_edge=-0.005,
                    export_seq=2,
                ),
                _decision(
                    D3,
                    state="REJECT",
                    mode="LIVE",
                    when=DAY.replace(hour=11, minute=10),
                    target_date="2026-10-27",
                    snapshot="s3",
                    age=30_000,
                    depth=20.0,
                    net_edge=0.01,
                    fee=None,
                    fill=60.0,
                    export_seq=3,
                ),
                _decision(
                    D4,
                    state="REJECT",
                    mode="LIVE",
                    when=DAY.replace(hour=11, minute=20),
                    target_date="2026-10-27",
                    market="mkt-2",
                    snapshot="s4",
                    age=40_000,
                    depth=50.0,
                    net_edge=-0.01,
                    export_seq=4,
                ),
                _decision(
                    D5,
                    state="ACCEPT",
                    mode="REPLAY",
                    when=DAY.replace(hour=12, minute=0),
                    target_date="2026-10-27",
                    snapshot="s5",
                    age=15_000,
                    depth=80.0,
                    net_edge=0.05,
                    export_seq=5,
                ),
                _decision(
                    D6,
                    state="ACCEPT",
                    mode="LIVE",
                    when=NEXT_DAY.replace(hour=9, minute=0),
                    target_date="2026-10-27",
                    market="mkt-2",
                    snapshot="s6",
                    age=10_000,
                    depth=60.0,
                    net_edge=0.04,
                    export_seq=6,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO market_snapshots ("
            "snapshot_id, market_id, token_id, book_id, book_source, venue_ts_origin, "
            "venue_ts_utc, collector_received_at_utc, shadow_received_at_utc, decision_time_utc, "
            "latency_ms, levels_bid, levels_ask, defects, quality, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "s1",
                    "mkt-1",
                    "tok-1",
                    "b1",
                    "ws",
                    "venue",
                    DAY.replace(hour=10, minute=4),
                    DAY.replace(hour=10, minute=4, second=1),
                    DAY.replace(hour=10, minute=4, second=1),
                    DAY.replace(hour=10, minute=5),
                    1_000,
                    3,
                    3,
                    "",
                    "ok",
                    DAY,
                ),
                (
                    "s2",
                    "mkt-1",
                    "tok-1",
                    "b2",
                    "ws",
                    "venue",
                    DAY.replace(hour=10, minute=14),
                    DAY.replace(hour=10, minute=14, second=1),
                    DAY.replace(hour=10, minute=14, second=1),
                    DAY.replace(hour=10, minute=15),
                    2_000,
                    3,
                    3,
                    "",
                    "ok",
                    DAY,
                ),
                (
                    "s3",
                    "mkt-1",
                    "tok-1",
                    "b3",
                    "ws",
                    "venue",
                    DAY.replace(hour=11, minute=9),
                    DAY.replace(hour=11, minute=9, second=1),
                    DAY.replace(hour=11, minute=9, second=1),
                    DAY.replace(hour=11, minute=10),
                    5_200,
                    3,
                    3,
                    "",
                    "ok",
                    DAY,
                ),
                (
                    "s4",
                    "mkt-2",
                    "tok-2",
                    "b4",
                    "rest",
                    "venue",
                    DAY.replace(hour=11, minute=19),
                    DAY.replace(hour=11, minute=19, second=1),
                    DAY.replace(hour=11, minute=19, second=1),
                    DAY.replace(hour=11, minute=20),
                    -2_100,
                    3,
                    3,
                    "",
                    "ok",
                    DAY,
                ),
                (
                    "s5",
                    "mkt-2",
                    "tok-2",
                    "b5",
                    "rest",
                    "venue",
                    DAY.replace(hour=11, minute=59),
                    DAY.replace(hour=11, minute=59, second=1),
                    DAY.replace(hour=11, minute=59, second=1),
                    DAY.replace(hour=12),
                    1_000,
                    3,
                    3,
                    "",
                    "ok",
                    DAY,
                ),
                (
                    "s6",
                    "mkt-2",
                    "tok-2",
                    "b6",
                    "ws",
                    "venue",
                    NEXT_DAY.replace(hour=8, minute=59),
                    NEXT_DAY.replace(hour=8, minute=59, second=1),
                    NEXT_DAY.replace(hour=8, minute=59, second=1),
                    NEXT_DAY.replace(hour=9),
                    500,
                    3,
                    3,
                    "",
                    "ok",
                    NEXT_DAY,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO rejection_reasons (decision_id, ordinal, reason, is_primary, detail) VALUES (?, ?, ?, ?, ?)",
            [
                (D2, 1, "STALE_ORDERBOOK", True, "book 95s old"),
                (D2, 2, "INSUFFICIENT_LIQUIDITY", False, "thin"),
                (D3, 1, "INSUFFICIENT_LIQUIDITY", True, "only 20 USD"),
                (D3, 2, "UNKNOWN_FEE", False, "no schedule"),
                (D4, 1, "NEGATIVE_NET_EDGE", True, "negative"),
            ],
        )
        db.executemany(
            "INSERT INTO data_quality_events (event_id, run_id, ts_utc, kind, severity, detail) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("q1", "run-1", DAY.replace(hour=9), "archive_gap", "warning", "gap"),
                ("q2", "run-1", DAY.replace(hour=10), "archive_gap", "warning", "gap"),
                ("q3", "run-1", DAY.replace(hour=11), "stale_book", "info", "late frame"),
            ],
        )


def _build_paper(path: Path) -> None:
    with PaperDatabase(path) as db:
        db.executemany(
            "INSERT INTO paper_signals ("
            "decision_id, run_id, cycle_id, market_id, direction, state, mode, "
            "decision_time_utc, tradeable, outcome, refusal_reason, refusal_detail, "
            "consumed_at_utc, event_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    D1,
                    "run-1",
                    "cycle-1",
                    "mkt-1",
                    "buy_yes",
                    "ACCEPT",
                    "LIVE",
                    DAY.replace(hour=10, minute=5),
                    True,
                    "FILLED",
                    None,
                    None,
                    DAY.replace(hour=10, minute=6),
                    "{}",
                ),
                (
                    D6,
                    "run-1",
                    "cycle-1",
                    "mkt-2",
                    "buy_yes",
                    "ACCEPT",
                    "LIVE",
                    NEXT_DAY.replace(hour=9),
                    True,
                    "ACCEPTED",
                    None,
                    None,
                    NEXT_DAY.replace(hour=9, minute=1),
                    "{}",
                ),
                (
                    D7,
                    "run-1",
                    "cycle-1",
                    "mkt-3",
                    "buy_yes",
                    "ACCEPT",
                    "LIVE",
                    DAY.replace(hour=13),
                    True,
                    "REFUSED",
                    "INSUFFICIENT_CAPACITY",
                    "book too thin",
                    DAY.replace(hour=13, minute=1),
                    "{}",
                ),
                (
                    D8,
                    "run-1",
                    "cycle-1",
                    "mkt-3",
                    "buy_yes",
                    "ACCEPT",
                    "REPLAY",
                    DAY.replace(hour=14),
                    False,
                    "REFUSED",
                    "NOT_LIVE",
                    "replay row",
                    DAY.replace(hour=14, minute=1),
                    "{}",
                ),
            ],
        )
        db.execute(
            "INSERT INTO paper_fills ("
            "decision_id, market_id, token_id, direction, side, book_id, book_source, "
            "book_venue_ts_utc, book_collector_received_at_utc, book_age_ms, requested_shares, "
            "filled_shares, avg_price_usd_per_share, best_price_usd_per_share, "
            "slippage_usd_per_share, fill_cost_usd, fees_usd, execution_buffer_usd, "
            "total_cost_usd, net_proceeds_usd, levels_consumed, complete, consumed_levels_json, "
            "decision_time_utc, filled_at_utc, fee_rate, thresholds_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                D1,
                "mkt-1",
                "tok-1",
                "buy_yes",
                "buy",
                "b1",
                "ws",
                DAY.replace(hour=10, minute=4),
                DAY.replace(hour=10, minute=4, second=1),
                60_000,
                100.0,
                100.0,
                0.55,
                0.55,
                0.0,
                55.0,
                0.5,
                0.2,
                55.7,
                None,
                1,
                True,
                '[{"price_usd_per_share":0.55,"size_shares_taken":100.0}]',
                DAY.replace(hour=10, minute=5),
                DAY.replace(hour=10, minute=6),
                0.05,
                "v2",
            ],
        )
        db.execute(
            "INSERT INTO paper_settlements ("
            "settlement_id, market_id, revision, status, yes_payout, settled_at_utc, "
            "evidence_json, applied, applied_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "settle-1",
                "mkt-1",
                1,
                "RESOLVED",
                1.0,
                NEXT_DAY,
                "{}",
                True,
                NEXT_DAY,
            ],
        )
        db.execute(
            "INSERT INTO paper_quality_events (event_id, ts_utc, kind, severity, detail) VALUES (?, ?, ?, ?, ?)",
            ["pq1", DAY.replace(hour=15), "malformed_line", "warning", "bad json"],
        )


@pytest.fixture()
def snapshots(tmp_path: Path) -> tuple[Path, Path]:
    """A migrated shadow/paper pair, copied the way an operator would copy them."""
    shadow = tmp_path / "shadow.duckdb"
    paper = tmp_path / "paper.duckdb"
    _build_shadow(shadow)
    _build_paper(paper)
    shadow_copy = tmp_path / "shadow-copy.duckdb"
    paper_copy = tmp_path / "paper-copy.duckdb"
    shutil.copy2(shadow, shadow_copy)
    shutil.copy2(paper, paper_copy)
    return shadow_copy, paper_copy


def _digest(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_decisions_and_reject_histogram(snapshots):
    shadow, paper = snapshots
    report = report_mod.build_report(shadow_db=shadow, paper_db=paper)

    decisions = report["decisions"]
    assert decisions["total"] == 5  # the REPLAY row is excluded by --mode LIVE
    assert decisions["accept"] == 2
    assert decisions["reject"] == 3
    assert decisions["not_executable"] == 0

    by_target = {row["bucket"]: row for row in decisions["by_target_date"]}
    assert by_target["2026-10-26"] == {
        "bucket": "2026-10-26",
        "total": 2,
        "accept": 1,
        "reject": 1,
        "not_executable": 0,
    }
    assert by_target["2026-10-27"]["total"] == 3
    assert by_target["2026-10-27"]["accept"] == 1

    by_hour = {(row["bucket"], row["total"]) for row in decisions["by_hour"]}
    assert ("2026-10-26 10:00:00+00:00", 2) in by_hour
    assert ("2026-10-26 11:00:00+00:00", 2) in by_hour
    assert ("2026-10-27 09:00:00+00:00", 1) in by_hour

    reasons = report["reject_reasons"]
    assert reasons["rejections_with_primary"] == 3
    assert {reason: entry["count"] for reason, entry in reasons["primary"].items()} == {
        "STALE_ORDERBOOK": 1,
        "INSUFFICIENT_LIQUIDITY": 1,
        "NEGATIVE_NET_EDGE": 1,
    }
    assert all(entry["pct"] == pytest.approx(33.33) for entry in reasons["primary"].values())
    assert reasons["all"]["INSUFFICIENT_LIQUIDITY"] == 2
    assert reasons["all"]["UNKNOWN_FEE"] == 1
    top = reasons["top_per_market_target"]
    assert any(row["market_id"] == "mkt-1" and row["reason"] == "STALE_ORDERBOOK" for row in top)


def test_gate_margins(snapshots):
    shadow, paper = snapshots
    gates = report_mod.build_report(shadow_db=shadow, paper_db=paper)["gates"]
    # The version and every threshold come from the shipped config; a v4 bump
    # must not require editing this test.
    thresholds = report_mod.load_thresholds(ROOT / "configs" / "shadow-thresholds.json")

    assert gates["thresholds_version"] == thresholds.version
    assert gates["thresholds_source"] == str(ROOT / "configs" / "shadow-thresholds.json")
    assert gates["book_age"]["threshold"] == thresholds.max_snapshot_age_ms
    assert gates["book_age"]["n"] == 5
    assert gates["book_age"]["p50"] == 30_000
    assert gates["book_age"]["rejects_within_10pct"] == 1  # 95s vs the 90s gateway
    assert gates["book_age"]["rejects_within_25pct"] == 1
    # D2 (95s) is also INSUFFICIENT_LIQUIDITY, so loosening book age alone would
    # not have accepted it; D3/D4 never failed book age at all.
    assert gates["book_age"]["gate_rejects"] == 1
    assert gates["book_age"]["would_pass_at"]["60000"] == 0
    assert gates["book_age"]["would_pass_at"]["180000"] == 0

    # v3 turned the dollar floor off (0); the venue minimum is the real floor.
    assert gates["depth"]["threshold"] == thresholds.min_depth_usd
    assert gates["depth"]["rejects_within_10pct"] == 0
    assert gates["depth"]["rejects_within_25pct"] == 0
    # Both depth rejects (D2, D3) carry a second reason.
    assert gates["depth"]["gate_rejects"] == 2
    assert gates["depth"]["would_pass_at"]["10"] == 0
    assert gates["depth"]["would_pass_at"]["100"] == 0
    assert gates["depth"]["would_fail_at"]["10"] == 0
    assert gates["depth"]["would_fail_at"]["100"] == 1  # D6 accepted at 60 USD

    assert gates["net_edge"]["threshold"] == thresholds.min_net_edge_usd_per_share
    assert gates["net_edge"]["rejects_within_10pct"] == 0
    assert gates["net_edge"]["gate_rejects"] == 1  # D4
    assert gates["net_edge"]["would_pass_at"]["0"] == 0  # D4's edge is negative

    # v3 measures negative skew (-latency); 2.1s against the 2s tolerance.
    assert gates["clock_skew"]["threshold"] == thresholds.max_clock_skew_ms
    # D4 is 2.1s skewed but was rejected for edge, not TIME_MISMATCH.
    assert gates["clock_skew"]["gate_rejects"] == 0
    assert gates["clock_skew"]["rejects_within_10pct"] == 0
    assert gates["clock_skew"]["rejects_within_25pct"] == 0
    assert gates["clock_skew"]["would_pass_at"]["2500"] == 0
    assert gates["clock_skew"]["would_pass_at"]["1000"] == 0

    assert gates["fee"]["decisions"] == 5
    assert gates["fee"]["fee_known"] == 4
    assert gates["fee"]["fee_missing"] == 1

    # v3 removes require_complete_fill: a sized-down partial is not a refusal.
    assert gates["size"]["threshold"] == thresholds.min_order_size_shares
    assert gates["size"]["below_min_order_rejects"] == 0


def _set_reasons(shadow: Path, reasons: dict[str, list[str]]) -> None:
    con = duckdb.connect(str(shadow))
    try:
        for decision_id, codes in reasons.items():
            con.execute("DELETE FROM rejection_reasons WHERE decision_id = ?", [decision_id])
            for ordinal, code in enumerate(codes, start=1):
                con.execute(
                    "INSERT INTO rejection_reasons (decision_id, ordinal, reason, is_primary, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [decision_id, ordinal, code, ordinal == 1, "test"],
                )
    finally:
        con.close()


def test_would_pass_counts_only_rejects_blocked_by_that_gate_alone(snapshots):
    shadow, paper = snapshots
    # Each LIVE reject now fails exactly one gate.
    _set_reasons(shadow, {D2: ["STALE_ORDERBOOK"], D3: ["INSUFFICIENT_LIQUIDITY"], D4: ["TIME_MISMATCH"]})
    gates = report_mod.build_report(shadow_db=shadow, paper_db=paper)["gates"]

    assert gates["book_age"]["gate_rejects"] == 1
    assert gates["book_age"]["would_pass_at"]["60000"] == 0  # 95s still too old
    assert gates["book_age"]["would_pass_at"]["120000"] == 1  # D2 only, not D3/D4
    assert gates["depth"]["gate_rejects"] == 1
    assert gates["depth"]["would_pass_at"]["10"] == 1  # D3 (20 USD) only
    assert gates["depth"]["would_pass_at"]["25"] == 0
    assert gates["clock_skew"]["gate_rejects"] == 1
    assert gates["clock_skew"]["rejects_within_10pct"] == 1  # 2.1s vs 2s
    assert gates["clock_skew"]["would_pass_at"]["2500"] == 1  # D4 only
    assert gates["clock_skew"]["would_pass_at"]["1000"] == 0
    assert gates["net_edge"]["gate_rejects"] == 0
    assert gates["net_edge"]["would_pass_at"]["0"] == 0

    # Below the edge threshold but positive: a zero floor would have taken it.
    _set_reasons(shadow, {D4: ["EDGE_BELOW_THRESHOLD"]})
    con = duckdb.connect(str(shadow))
    try:
        con.execute("UPDATE shadow_decisions SET net_edge = 0.005 WHERE decision_id = ?", [D4])
    finally:
        con.close()
    gates = report_mod.build_report(shadow_db=shadow, paper_db=paper)["gates"]
    assert gates["net_edge"]["gate_rejects"] == 1
    assert gates["net_edge"]["would_pass_at"]["0"] == 1  # not D3's 0.01, which failed liquidity
    assert gates["net_edge"]["would_pass_at"]["0.01"] == 0
    assert gates["clock_skew"]["would_pass_at"]["2500"] == 0


def test_fill_refusals_stay_in_the_signal_to_fill_denominator(snapshots):
    shadow, paper = snapshots
    # The fill engine refused D6: the outcome flips from ACCEPTED to REFUSED.
    con = duckdb.connect(str(paper))
    try:
        con.execute(
            "UPDATE paper_signals SET outcome = 'REFUSED', refusal_reason = 'INSUFFICIENT_CAPACITY' "
            "WHERE decision_id = ?",
            [D6],
        )
    finally:
        con.close()
    funnel = report_mod.build_report(shadow_db=shadow, paper_db=paper)["funnel"]

    assert funnel["paper_signals"] == 2
    assert funnel["paper_accepted"] == 2
    assert funnel["paper_refused"] == 1
    assert funnel["paper_fill_refused"] == 1
    assert funnel["paper_filled"] == 1
    assert funnel["conversion_pct"]["signal_to_fill"] == 50.0  # not 1/1 = 100%


def test_funnel_counts_and_reasons(snapshots):
    shadow, paper = snapshots
    funnel = report_mod.build_report(shadow_db=shadow, paper_db=paper)["funnel"]

    assert funnel["shadow_live_accept"] == 2
    assert funnel["exported"] == 2
    assert funnel["paper_signals"] == 2
    assert funnel["paper_accepted"] == 2
    assert funnel["paper_refused"] == 0
    assert funnel["paper_fill_refused"] == 0
    assert funnel["paper_filled"] == 1
    assert funnel["settled"] == 1
    assert funnel["conversion_pct"] == {
        "accept_to_export": 100.0,
        "export_to_signal": 100.0,
        "signal_to_fill": 50.0,
        "fill_to_settled": 100.0,
    }
    assert funnel["no_fill_reasons"] == {"INSUFFICIENT_CAPACITY": 1}
    assert funnel["top_no_fill_reasons"] == [("INSUFFICIENT_CAPACITY", 1)]
    # Both refusals are counted paper-wide, including the one not in this accept set.
    assert funnel["paper_refusal_reasons"] == {"INSUFFICIENT_CAPACITY": 1, "NOT_LIVE": 1}


def test_freshness_and_window(snapshots):
    shadow, paper = snapshots
    report = report_mod.build_report(
        shadow_db=shadow,
        paper_db=paper,
        since=datetime(2026, 10, 26, 11, 0, tzinfo=UTC),
        until=datetime(2026, 10, 27, 23, 0, tzinfo=UTC),
    )
    assert report["decisions"]["total"] == 3
    assert report["decisions"]["accept"] == 1
    assert report["decisions"]["reject"] == 2

    freshness = report["freshness"]
    # Recency is measured over the whole file, not the window.
    assert freshness["latest_decision_time_utc"] == "2026-10-27T09:00:00+00:00"
    assert freshness["latest_fill_time_utc"] == "2026-10-26T10:06:00+00:00"
    assert freshness["data_quality_events"] == {"stale_book": 1}
    assert freshness["paper_quality_events"] == {"malformed_line": 1}


def test_replay_mode_selects_the_replay_rows(snapshots):
    shadow, paper = snapshots
    report = report_mod.build_report(shadow_db=shadow, paper_db=paper, mode="REPLAY")
    assert report["decisions"]["total"] == 1
    assert report["decisions"]["accept"] == 1
    assert report["funnel"]["shadow_live_accept"] == 1


def test_missing_columns_and_tables_are_skipped(snapshots, tmp_path):
    _, paper = snapshots
    # A snapshot from before export_seq / fee_rate / rejection_reasons existed.
    older = tmp_path / "shadow-older.duckdb"
    con = duckdb.connect(str(older))
    con.execute(
        "CREATE TABLE shadow_decisions ("
        "decision_id VARCHAR PRIMARY KEY, state VARCHAR, mode VARCHAR, decision_time_utc TIMESTAMPTZ, "
        "snapshot_id VARCHAR, snapshot_age_ms BIGINT, available_depth_usd DOUBLE, net_edge DOUBLE, "
        "proposed_shares DOUBLE, estimated_fill_shares DOUBLE, target_date DATE, market_id VARCHAR)"
    )
    con.execute("CREATE TABLE market_snapshots (snapshot_id VARCHAR PRIMARY KEY, latency_ms BIGINT)")
    con.executemany(
        "INSERT INTO shadow_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                D1,
                "ACCEPT",
                "LIVE",
                DAY.replace(hour=10, minute=5),
                "s1",
                20_000,
                100.0,
                0.03,
                100.0,
                100.0,
                "2026-10-26",
                "mkt-1",
            ),
            (
                D2,
                "ACCEPT",
                "LIVE",
                DAY.replace(hour=10, minute=15),
                "s2",
                30_000,
                100.0,
                0.03,
                100.0,
                100.0,
                "2026-10-26",
                "mkt-1",
            ),
        ],
    )
    con.executemany("INSERT INTO market_snapshots VALUES (?, ?)", [("s1", 1_000), ("s2", 2_000)])
    con.close()

    report = report_mod.build_report(shadow_db=older, paper_db=paper)
    assert report["funnel"]["exported"] == report_mod.NA_COLUMN
    assert report["funnel"]["conversion_pct"]["accept_to_export"] == report_mod.NA_COLUMN
    assert report["gates"]["fee"] == report_mod.NA_COLUMN
    assert report["reject_reasons"]["primary"] == report_mod.NA_TABLE
    assert report["decisions"]["total"] == 2
    # The missing inputs are named, not silently swallowed.
    skipped = "\n".join(report["skipped"])
    assert "funnel.exported" in skipped
    assert "gates.fee" in skipped


def test_without_paper_db_every_paper_metric_is_named(snapshots):
    shadow, _ = snapshots
    report = report_mod.build_report(shadow_db=shadow)
    funnel = report["funnel"]
    assert funnel["paper_signals"] == report_mod.NA_PAPER
    assert funnel["settled"] == report_mod.NA_PAPER
    assert report["freshness"]["latest_fill_time_utc"] == report_mod.NA_PAPER
    assert report["inputs"]["paper_db"] is None


def test_inputs_are_never_modified(snapshots):
    shadow, paper = snapshots
    before = {path: _digest(path) for path in (shadow, paper)}
    report_mod.build_report(shadow_db=shadow, paper_db=paper)
    after = {path: _digest(path) for path in (shadow, paper)}
    assert before == after


def test_locked_database_is_refused(snapshots, tmp_path):
    shadow, _ = snapshots
    locked = tmp_path / "locked.duckdb"
    shutil.copy2(shadow, locked)
    holder = duckdb.connect(str(locked))
    try:
        holder.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()
        with pytest.raises(report_mod.ReportError, match="locked"):
            report_mod.build_report(shadow_db=locked)
    finally:
        holder.close()


def test_wal_sibling_is_refused(snapshots, tmp_path):
    shadow, _ = snapshots
    guarded = tmp_path / "guarded.duckdb"
    shutil.copy2(shadow, guarded)
    (tmp_path / "guarded.duckdb.wal").write_bytes(b"")
    with pytest.raises(report_mod.ReportError, match="write-ahead log"):
        report_mod.build_report(shadow_db=guarded)


def test_cli_writes_json_and_markdown(snapshots, tmp_path):
    shadow, paper = snapshots
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"

    exit_code = report_mod.main(
        [
            "--shadow-db",
            str(shadow),
            "--paper-db",
            str(paper),
            "--json",
            str(json_out),
            "--md",
            str(md_out),
        ]
    )

    assert exit_code == 0
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["decisions"]["total"] == 5
    assert payload["funnel"]["paper_filled"] == 1

    markdown = md_out.read_text(encoding="utf-8")
    for heading in (
        "# Live validation report",
        "## 1. Decisions",
        "## 2. Reject reasons",
        "## 3. Gate margins",
        "## 4. Funnel",
        "## 5. Freshness and gaps",
    ):
        assert heading in markdown
    assert "STALE_ORDERBOOK" in markdown
    assert "INSUFFICIENT_CAPACITY" in markdown


def test_cli_reports_a_bad_path_without_a_traceback(tmp_path, capsys):
    assert report_mod.main(["--shadow-db", str(tmp_path / "absent.duckdb")]) == 2
    stderr = capsys.readouterr().err
    assert "database not found" in stderr

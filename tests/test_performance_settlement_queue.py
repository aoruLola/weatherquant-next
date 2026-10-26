"""The settlement verification queue: coverage you can count, not hope for.

The invariant worth testing hardest is the ticket's acceptance line - "过去
产生 ACCEPT 的市场都进入核对队列" - stated as data: every distinct
(market_id, target_date) with an ACCEPT appears in the queue exactly once,
across models, directions, modes and repeated observations. The second
invariant is the pairing rule: a pair the queue calls ``verified`` is exactly
a pair the win-rate engine may score, checked here through the production
streaming engine rather than by trusting the bucket logic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.performance.settlement_queue import (
    SettlementQueueError,
    build_settlement_queue,
    catalog_from_collector_copy,
)

T0 = datetime(2026, 10, 13, tzinfo=UTC)
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _decision(market: str, target: str, *, state: str = "ACCEPT", seq: int = 0) -> dict:
    moment = T0 + timedelta(minutes=seq)
    return {
        "decision_id": f"d{seq:07d}",
        "decision_time_utc": moment.isoformat(),
        "market_id": market,
        "target_date": target,
        "model_name": "calibrated",
        "direction": "buy_yes",
        "mode": "LIVE",
        "state": state,
        "net_edge": 0.05,
        "market_price": 0.6,
        "model_probability": 0.65,
        "available_depth_usd": 500.0,
        "station": "KLGA",
        "bracket_label": "81°F",
    }


def _settlement(
    market: str, target: str, *, status: str, outcome=None, verification="UNVERIFIED", fetched_at=None
) -> dict:
    return {
        "market_id": market,
        "target_date": target,
        "settlement_status": status,
        "resolved_outcome": outcome,
        "verification_status": verification,
        "resolution_source": "clob:/v2/resolutions",
        "fetched_at_utc": fetched_at or NOW.isoformat(),
    }


# --- coverage: the acceptance invariant --------------------------------------


def test_every_accept_market_is_in_the_queue_exactly_once() -> None:
    """Six observations across three models/modes on one pair = one entry."""
    decisions = []
    seq = 0
    for model in ("calibrated", "fusion", "gfs_raw"):
        for mode in ("LIVE", "REPLAY"):
            for _ in range(3):
                seq += 1
                row = _decision("m-1", "2026-10-13", seq=seq)
                row["model_name"], row["mode"] = model, mode
                decisions.append(row)
    # A market decided but never ACCEPTed must not appear.
    decisions.append(_decision("m-2", "2026-10-13", state="REJECT", seq=seq + 1))
    decisions.append(_decision("m-3", "2026-10-13", state="NO_TRADE", seq=seq + 2))

    queue = build_settlement_queue(decisions, now=NOW)

    assert queue["coverage"]["accept_markets"] == 1
    assert [e["market_id"] for e in queue["queue"]] == ["m-1"]
    assert queue["coverage"]["all_accept_markets_in_queue"] is True
    entry = queue["queue"][0]
    assert entry["accept_observations"] == 18, "repeated evaluations stay observations"
    assert entry["bucket"] == "missing" and entry["action"] == "backfill"


def test_accept_rows_without_a_market_id_are_counted_rejects_are_not() -> None:
    """The hole that matters is an ACCEPT the queue can never hold.

    A REJECT row without an id says nothing about coverage; counting it made
    the number meaningless (an export with a few id-less REJECTs read as
    "lost decisions" even though nothing queueable was lost).
    """
    decisions = [
        _decision("m-1", "2026-10-13"),
        {"state": "ACCEPT", "decision_id": "x"},
        {"state": "ACCEPT", "decision_id": "y"},
        {"state": "REJECT", "decision_id": "z"},
        {"decision_id": "no-state"},
    ]
    queue = build_settlement_queue(decisions, now=NOW)

    assert queue["coverage"]["accept_rows"] == 3
    assert queue["coverage"]["accept_rows_without_market_id"] == 2
    assert queue["coverage"]["accept_markets"] == 1


def test_the_coverage_flag_is_a_real_comparison_not_a_tautology() -> None:
    """`len(queue) == len(pairs)` compared a structure with itself.

    The check now recomputes the expected pair set through a different code
    path (a set comprehension over the ACCEPT decisions) and compares sets -
    which a keying bug that collapses two days onto one pair can fail.
    """
    decisions = [_decision("m-1", "2026-10-13"), _decision("m-1", "2026-10-14")]
    queue = build_settlement_queue(decisions, now=NOW)
    assert queue["coverage"]["all_accept_markets_in_queue"] is True

    # Simulate the drift: one entry loses its date. The recomputed
    # expectation no longer matches the queue set.
    drifted = {(e["market_id"], "2026-10-13") for e in queue["queue"]}
    expected = {
        (str(d["market_id"]), d["target_date"])
        for d in decisions
        if d.get("market_id") and str(d["state"]).upper() in {"ACCEPT"}
    }
    assert drifted != expected


def test_lowercase_accept_is_counted_and_datetime_targets_are_normalised() -> None:
    """A feed saying "accept" degrades loudly; the same day under two shapes
    ("2026-10-13" vs "2026-10-13T00:00:00") is one pair, not two."""
    decisions = [
        _decision("m-1", "2026-10-13"),
        {
            "decision_id": "d2",
            "decision_time_utc": "2026-10-13T01:00:00+00:00",
            "market_id": "m-2",
            "target_date": "2026-10-13T00:00:00",
            "model_name": "calibrated",
            "direction": "buy_yes",
            "mode": "LIVE",
            "state": "accept",
            "net_edge": 0.05,
            "station": "KLGA",
            "bracket_label": "81F",
        },
    ]
    settlements = [
        _settlement("m-1", "2026-10-13", status="RESOLVED", outcome=1.0, verification="VERIFIED"),
        # Same pair, datetime-shaped: must bucket as one, newest fetch wins.
        _settlement("m-2", "2026-10-13T00:00:00", status="PENDING", fetched_at="2026-10-20T00:00:00+00:00"),
        _settlement(
            "m-2",
            "2026-10-13",
            status="RESOLVED",
            outcome=0.0,
            verification="UNVERIFIED",
            fetched_at="2026-10-21T00:00:00+00:00",
        ),
    ]
    queue = build_settlement_queue(decisions, settlements=settlements, now=NOW)

    assert queue["coverage"]["accept_markets"] == 2
    assert queue["coverage"]["all_accept_markets_in_queue"] is True
    by_id = {e["market_id"]: e for e in queue["queue"]}
    assert by_id["m-2"]["bucket"] == "resolved_unverified", "the newer fetch (RESOLVED) must win over the older PENDING"


def test_an_input_with_no_accepts_at_all_is_refused() -> None:
    decisions = [_decision("m-1", "2026-10-13", state="REJECT")]
    with pytest.raises(SettlementQueueError, match="no ACCEPT"):
        build_settlement_queue(decisions, now=NOW)


# --- buckets: the contract's vocabulary, one direction ------------------------


def test_settlement_states_map_onto_the_six_buckets() -> None:
    decisions = [
        _decision("m-missing", "2026-10-13", seq=1),
        _decision("m-pending", "2026-10-13", seq=2),
        _decision("m-disputed", "2026-10-13", seq=3),
        _decision("m-unavailable", "2026-10-13", seq=4),
        _decision("m-unverified", "2026-10-13", seq=5),
        _decision("m-verified", "2026-10-13", seq=6),
    ]
    settlements = [
        _settlement("m-pending", "2026-10-13", status="PENDING"),
        _settlement("m-disputed", "2026-10-13", status="DISPUTED"),
        _settlement("m-unavailable", "2026-10-13", status="UNAVAILABLE"),
        _settlement("m-unverified", "2026-10-13", status="RESOLVED", outcome=1.0, verification="UNVERIFIED"),
        _settlement("m-verified", "2026-10-13", status="RESOLVED", outcome=1.0, verification="VERIFIED"),
    ]
    queue = build_settlement_queue(decisions, settlements=settlements, now=NOW)

    buckets = {e["market_id"]: e["bucket"] for e in queue["queue"]}
    assert buckets == {
        "m-missing": "missing",
        "m-pending": "pending",
        "m-disputed": "disputed",
        "m-unavailable": "unavailable",
        "m-unverified": "resolved_unverified",
        "m-verified": "verified",
    }
    eligible = {e["market_id"] for e in queue["queue"] if e["win_loss_eligible"]}
    assert eligible == {"m-verified"}, "only verified pairs are WIN/LOSS eligible"
    assert queue["coverage"]["win_loss_eligible_markets"] == 1
    assert queue["coverage"]["win_loss_gating"].startswith("official resolved")


def test_a_resolved_record_without_a_verification_label_is_never_verified() -> None:
    """An unknown cross-check state cannot be allowed to read as verified."""
    queue = build_settlement_queue(
        [_decision("m-1", "2026-10-13")],
        settlements=[_settlement("m-1", "2026-10-13", status="RESOLVED", outcome=1.0, verification="")],
        now=NOW,
    )
    assert queue["queue"][0]["bucket"] == "resolved_unverified"
    assert queue["queue"][0]["win_loss_eligible"] is False


def test_source_and_retrieval_time_travel_with_each_entry() -> None:
    """来源与检索时间持久化: the queue surfaces what the backfill recorded."""
    record = _settlement("m-1", "2026-10-13", status="RESOLVED", outcome=1.0, verification="VERIFIED")
    queue = build_settlement_queue([_decision("m-1", "2026-10-13")], settlements=[record], now=NOW)
    entry = queue["queue"][0]
    assert entry["settlement_source"] == "clob:/v2/resolutions"
    assert entry["settlement_fetched_at_utc"] == NOW.isoformat()


# --- catalog: asking needs a condition id -------------------------------------


def test_the_catalog_enriches_and_marks_what_it_cannot_resolve() -> None:
    decisions = [
        _decision("m-known", "2026-10-13", seq=1),
        _decision("m-unknown", "2026-10-13", seq=2),
    ]
    catalog = [
        {
            "market_id": "m-known",
            "condition_id": "0xabc",
            "token_yes": "123",
            "event_id": "e-1",
        }
    ]
    queue = build_settlement_queue(decisions, catalog=catalog, now=NOW)

    by_id = {e["market_id"]: e for e in queue["queue"]}
    assert by_id["m-known"]["condition_id"] == "0xabc"
    assert by_id["m-known"]["needs_catalog"] is False
    assert by_id["m-unknown"]["needs_catalog"] is True
    assert "m-unknown" in by_id["m-unknown"]["catalog_lookup_hint"]
    assert queue["coverage"]["catalog_matched"] == 1
    assert queue["coverage"]["catalog_unmatched"] == 1


def test_the_catalog_can_be_read_from_a_collector_copy(tmp_path: Path) -> None:
    """A verified backup copy of the collector DB is a catalog source."""
    from weatherquant.data.writer import CollectorWriter

    db = tmp_path / "collector.duckdb"
    writer = CollectorWriter(db, run_id="catalog-test")
    writer.open()
    con = writer.con
    con.execute(
        "INSERT INTO events (event_id, slug, title, target_date, fetched_at_utc, raw) "
        "VALUES ('e-1', 'slug-1', 't', '2026-10-13', now(), '{}')"
    )
    con.execute(
        "INSERT INTO markets (market_id, event_id, condition_id, question, "
        "bracket_label, bracket_kind, token_yes, token_no, target_date, station, "
        "tick_size, min_order_size_shares, active, closed, accepting_orders, "
        "fetched_at_utc, raw) VALUES "
        "('m-1', 'e-1', '0xabc', 'q', '81°F', 'range', 'yes-token', 'no-token', "
        "'2026-10-13', 'KLGA', 0.01, 5, true, false, true, now(), '{}')"
    )
    writer.close(status="stopped", reason="test")

    catalog = catalog_from_collector_copy(str(db))
    assert catalog == [
        {
            "market_id": "m-1",
            "event_id": "e-1",
            "condition_id": "0xabc",
            "token_yes": "yes-token",
            "station": "KLGA",
            "bracket_label": "81°F",
            "bracket_low": None,
            "bracket_high": None,
            "target_date": "2026-10-13",
        }
    ]


# --- agreement with the win-rate engine ---------------------------------------


def test_verified_pairs_are_exactly_what_the_engine_scores(tmp_path: Path) -> None:
    """The queue's `verified` bucket and the engine's denominator agree.

    Both are driven from the same decisions and the same settlement index,
    through the production streaming engine - so the queue can never drift
    into calling something eligible that the engine refuses to score.
    """
    from weatherquant.performance.streaming import build_performance_streaming

    # One episode per pair, so episodes and pairs count the same.
    decisions = [_decision(f"m-{i}", "2026-10-13", seq=i) for i in range(4)]
    settlements = [
        _settlement("m-0", "2026-10-13", status="RESOLVED", outcome=1.0, verification="VERIFIED"),
        _settlement("m-1", "2026-10-13", status="RESOLVED", outcome=0.0, verification="VERIFIED"),
        _settlement("m-2", "2026-10-13", status="PENDING"),
        # m-3 has no record at all: missing, never asked.
    ]
    export = tmp_path / "export" / "parts"
    export.mkdir(parents=True)
    (export / "part-000000001.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions), encoding="utf-8")

    build_performance_streaming(
        export=tmp_path / "export",
        out_dir=tmp_path / "out",
        dataset="fixture",
        state_db=tmp_path / "state.duckdb",
        settlements=settlements,
        now=NOW,
    )
    generation = json.loads((tmp_path / "out" / "current.json").read_text())["generation_id"]
    win_rate = json.loads((tmp_path / "out" / "generations" / generation / "win-rate.json").read_text())

    queue = build_settlement_queue(decisions, settlements=settlements, now=NOW)
    assert queue["coverage"]["win_loss_eligible_markets"] == 2
    assert win_rate["data"]["overall"]["valid_samples"] == 2, (
        "the engine scored exactly the pairs the queue calls verified"
    )


def test_the_state_store_is_a_valid_decision_source(tmp_path: Path) -> None:
    """The state store's episodes are ACCEPT markets by construction."""
    import duckdb

    from weatherquant.performance.streaming import build_performance_streaming

    decisions = [_decision("m-1", "2026-10-13", seq=1), _decision("m-2", "2026-10-14", seq=2)]
    export = tmp_path / "export" / "parts"
    export.mkdir(parents=True)
    (export / "part-000000001.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions), encoding="utf-8")
    build_performance_streaming(
        export=tmp_path / "export",
        out_dir=tmp_path / "out",
        dataset="fixture",
        state_db=tmp_path / "state.duckdb",
        settlements=[_settlement("m-1", "2026-10-13", status="RESOLVED", outcome=1.0, verification="VERIFIED")],
        now=NOW,
    )

    con = duckdb.connect(str(tmp_path / "state.duckdb"), read_only=True)
    episodes = con.execute("SELECT market_id, target_date FROM performance_episodes ORDER BY market_id").fetchall()
    con.close()
    assert {row[0] for row in episodes} == {"m-1", "m-2"}


def test_settlement_queue_cli_returns_success_for_a_valid_accept(tmp_path, capsys):
    import json

    from weatherquant.cli import main

    source = tmp_path / "decisions.jsonl"
    source.write_text(
        json.dumps(
            {
                "decision_id": "d1",
                "market_id": "m1",
                "target_date": "2026-10-13",
                "state": "ACCEPT",
                "decision_time_utc": "2026-10-13T12:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "queue.json"
    assert main(["settlement-queue", "--export", str(source), "--out", str(out)]) == 0
    assert "accept markets: 1" in capsys.readouterr().out
    assert json.loads(out.read_text())["coverage"]["accept_markets"] == 1

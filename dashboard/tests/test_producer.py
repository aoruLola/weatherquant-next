"""Contract tests for the shadow-side producer (the not-yet-wired half).

These build a miniature shadow schema in a throwaway DuckDB file, run the
producer's builders against it, and then feed the published snapshots *back
into the API* - so the contract is proven on both ends: what the producer
emits is exactly what the dashboard consumes.

The database here belongs to the test and is created after every other file is
closed; nothing in this suite ever touches a production database (the API-side
guard lives in test_api.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from helpers import build_tree, utcnow

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.producer import (
    SNAPSHOT_SCHEMA_VERSION,
    build_decisions_snapshot,
    build_forecasts_snapshot,
    build_markets_snapshot,
    publish_detail_snapshots,
)

SCHEMA = """
CREATE TABLE market_catalog (
    market_id VARCHAR PRIMARY KEY, event_id VARCHAR, question VARCHAR,
    bracket_label VARCHAR, bracket_low DOUBLE, bracket_high DOUBLE,
    target_date DATE, station VARCHAR, token_yes VARCHAR, token_no VARCHAR,
    active BOOLEAN, closed BOOLEAN, accepting_orders BOOLEAN
);
CREATE TABLE token_books (
    token_id VARCHAR PRIMARY KEY, bids_json VARCHAR, asks_json VARCHAR,
    venue_ts_utc TIMESTAMPTZ, shadow_received_at_utc TIMESTAMPTZ,
    quality VARCHAR, defects VARCHAR
);
CREATE TABLE shadow_decisions (
    decision_id VARCHAR PRIMARY KEY, decision_time_utc TIMESTAMPTZ,
    market_id VARCHAR, bracket_label VARCHAR, target_date DATE,
    direction VARCHAR, state VARCHAR, mode VARCHAR, primary_reason VARCHAR,
    model_name VARCHAR, model_version VARCHAR, model_probability DOUBLE,
    market_price DOUBLE, best_bid DOUBLE, best_ask DOUBLE, net_edge DOUBLE,
    snapshot_age_ms BIGINT, forecast_age_hours DOUBLE,
    available_depth_usd DOUBLE, thresholds_version VARCHAR, run_id VARCHAR
);
CREATE TABLE rejection_reasons (
    decision_id VARCHAR, ordinal INTEGER, reason VARCHAR, is_primary BOOLEAN
);
CREATE TABLE model_predictions (
    model_name VARCHAR, model_version VARCHAR, target_date DATE,
    event_id VARCHAR, probabilities_json VARCHAR, predicted_max_f DOUBLE,
    forecast_run_time_utc TIMESTAMPTZ, training_cutoff_utc TIMESTAMPTZ,
    forecast_received_at_utc TIMESTAMPTZ, station VARCHAR, bracket_label VARCHAR
);
"""


@pytest.fixture
def shadow_con(tmp_path: Path):
    path = tmp_path / "shadow-test.duckdb"
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    now = utcnow()
    target = (now + timedelta(days=1)).date()

    con.execute(
        "INSERT INTO market_catalog VALUES "
        "('m-81','event-1','Will it be 81F?','81°F',81,81,?,'KLGA','tok-yes','tok-no',TRUE,FALSE,TRUE),"
        "('m-80','event-1','Will it be 80 or below?','80°F or below',NULL,NULL,?,'KLGA','tok2-yes','tok2-no',TRUE,FALSE,TRUE)",
        [target, target],
    )
    con.execute(
        "INSERT INTO token_books VALUES "
        "('tok-yes', ?, ?, NULL, ?, 'ok', '')",
        [
            json.dumps([["0.61", "50"], ["0.62", "100"]]),
            json.dumps([["0.65", "80"], ["0.66", "40"]]),
            now,
        ],
    )
    con.executemany(
        "INSERT INTO shadow_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("d1", now - timedelta(seconds=30), "m-81", "81°F", target, "buy_yes", "ACCEPT", "LIVE", None, "calibrated", "v1", 0.41, 0.65, 0.62, 0.65, 0.03, 400, 3.5, 48.0, "v3", "run-1"),
            ("d2", now - timedelta(seconds=60), "m-81", "81°F", target, "buy_yes", "REJECT", "LIVE", "EDGE_BELOW_THRESHOLD", "calibrated", "v1", 0.41, 0.65, 0.62, 0.65, -0.04, 400, 3.5, 48.0, "v3", "run-1"),
            ("d3", now - timedelta(seconds=90), "m-80", "80°F or below", target, "sell_yes", "NO_TRADE", "LIVE", "NO_LIQUIDITY", "calibrated", "v1", 0.7, 0.13, 0.10, 0.13, 0.0, 400, 3.5, 4.0, "v3", "run-1"),
        ],
    )
    con.executemany(
        "INSERT INTO rejection_reasons VALUES (?,?,?,?)",
        [("d2", 0, "EDGE_BELOW_THRESHOLD", True), ("d2", 1, "INSUFFICIENT_LIQUIDITY", False)],
    )
    con.executemany(
        "INSERT INTO model_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("calibrated", "v1", target, "event-1", json.dumps({"81°F": 0.41, "80°F or below": 0.59}), 81.2, now - timedelta(hours=6), datetime(2026, 9, 1, tzinfo=UTC), now - timedelta(hours=6), "KLGA", "81°F"),
            ("gfs_raw", "v1", target, "event-1", json.dumps({"81°F": 0.38, "80°F or below": 0.62}), 80.9, now - timedelta(hours=6), datetime(2026, 9, 1, tzinfo=UTC), now - timedelta(hours=6), "KLGA", "81°F"),
        ],
    )
    yield con
    con.close()


def test_decisions_snapshot_envelope_and_rows(shadow_con) -> None:
    payload = build_decisions_snapshot(
        shadow_con, now=utcnow(), run_id="run-1", source_version="0.1.1"
    )
    assert payload["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert payload["kind"] == "recent-decisions"
    assert payload["source_service"] == "weatherquant-shadow"
    assert payload["source_run_id"] == "run-1"
    assert payload["generated_at_utc"]
    assert payload["stale_after_seconds"] > 0

    decisions = payload["data"]["decisions"]
    assert [d["decision_id"] for d in decisions] == ["d1", "d2", "d3"]  # newest first
    rejected = decisions[1]
    assert rejected["reasons"] == ["EDGE_BELOW_THRESHOLD", "INSUFFICIENT_LIQUIDITY"]
    assert rejected["market_question"] == "Will it be 81F?"
    assert decisions[0]["net_edge_usd"] == pytest.approx(0.03)
    assert decisions[0]["mode"] == "LIVE"


def test_markets_snapshot_carries_book_and_model_probability(shadow_con) -> None:
    now = utcnow()
    payload = build_markets_snapshot(
        shadow_con, now=now, run_id="run-1", source_version="0.1.1", primary_model="calibrated"
    )
    markets = {m["market_id"]: m for m in payload["data"]["markets"]}
    assert set(markets) == {"m-81", "m-80"}

    top = markets["m-81"]
    assert top["model_probability"] == pytest.approx(0.41)
    assert top["book"]["best_bid"] == pytest.approx(0.62)
    assert top["book"]["best_ask"] == pytest.approx(0.65)
    assert top["book"]["bid_depth_usd"] == pytest.approx(50 * 0.61 + 100 * 0.62, abs=1e-6)
    assert top["last_decision"]["state"] == "ACCEPT"
    assert top["tradeable"] is True


def test_forecasts_snapshot_is_windowed_and_point_in_time(shadow_con) -> None:
    payload = build_forecasts_snapshot(
        shadow_con, now=utcnow(), run_id="run-1", source_version="0.1.1", primary_model="calibrated"
    )
    distributions = payload["data"]["distributions"]
    assert {d["model"] for d in distributions} == {"calibrated", "gfs_raw"}
    for dist in distributions:
        assert dist["forecast_run_time_utc"]  # when it was issued...
        assert dist["training_cutoff_utc"]  # ...and what it could have known
        assert sum(b["probability"] for b in dist["buckets"]) == pytest.approx(1.0)


def test_publish_writes_all_three_files_atomically(tmp_path: Path, shadow_con) -> None:
    reports = tmp_path / "reports"
    published = publish_detail_snapshots(
        shadow_con, directory=reports, run_id="run-1", source_version="0.1.1"
    )
    names = sorted(p.name for p in published)
    assert names == ["forecast-snapshot.json", "market-snapshot.json", "recent-decisions.json"]
    for path in published:
        payload = json.loads(path.read_text(encoding="utf-8"))  # complete, parseable
        assert payload["schema_version"] == 1
        assert payload["data"] is not None
    # no temp files left behind
    assert sorted(p.name for p in reports.iterdir()) == sorted(names)


def test_api_consumes_producer_output_end_to_end(tmp_path: Path, shadow_con, settings_for) -> None:
    """Producer output -> files -> API: the two halves of the contract agree."""
    root = build_tree(tmp_path)
    publish_detail_snapshots(
        shadow_con, directory=root / "shadow" / "reports", run_id="run-1", source_version="0.1.1"
    )
    app = create_app(settings_for(root))
    with TestClient(app) as client:
        decisions = client.get("/api/v1/decisions").json()["data"]
        assert decisions["rows_status"] == "fresh"
        assert len(decisions["rows"]) == 3
        assert decisions["rows"][0]["state"] == "ACCEPT"
        assert decisions["rows"][1]["reasons"] == ["EDGE_BELOW_THRESHOLD", "INSUFFICIENT_LIQUIDITY"]

        markets = client.get("/api/v1/markets").json()["data"]
        assert markets["source"] == "market-snapshot"
        by_id = {m["market_id"]: m for m in markets["items"]}
        # The three fields the whole integration exists for: per-market model
        # probability, theoretical edge, and the last decision - each with its
        # own provenance so none of them can be mistaken for a fill.
        accepted = by_id["m-81"]
        assert accepted["model_probability"] == pytest.approx(0.41)
        assert accepted["net_edge_usd"] == pytest.approx(0.03)
        assert accepted["last_decision"]["state"] == "ACCEPT"
        assert accepted["last_decision"]["mode"] == "LIVE"
        assert accepted["model_source"] == "market-snapshot"
        assert accepted["book"]["best_ask"] == pytest.approx(0.65)  # quote beside the model view
        # each bracket gets ITS bucket's probability from the same prediction row
        assert by_id["m-80"]["model_probability"] == pytest.approx(0.59)

        # decision detail carries the numbers a reviewer would ask for
        row = decisions["rows"][0]
        assert row["model_probability"] == pytest.approx(0.41)
        assert row["net_edge_usd"] == pytest.approx(0.03)
        assert row["thresholds_version"] == "v3"
        assert row["model_version"] == "v1"
        assert row["snapshot_age_ms"] == 400

        # and the overview's Model vs Market table uses them together
        overview = client.get("/api/v1/overview").json()["data"]
        mvm = next(r for r in overview["model_vs_market"] if r["market_id"] == "m-81")
        assert mvm["model_probability"] == pytest.approx(0.41)
        assert mvm["net_edge_usd"] == pytest.approx(0.03)
        assert mvm["book"]["best_bid"] == pytest.approx(0.62)

        forecasts = client.get("/api/v1/forecasts").json()["data"]
        assert forecasts["distributions"], "forecast distributions must reach the API"
        assert forecasts["distributions_status"] in ("fresh", "stale")

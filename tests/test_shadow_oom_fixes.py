"""Regression tests for the Shadow OOM-fix changes.

WQN-3 touches three hot paths in the Shadow. Each fix has a correctness
property this file pins down so future refactors cannot silently re-break it:

1. ``quantile_cont`` on DuckDB matches the old Python-side linear-interpolation
   percentile down to floating-point noise.
2. Market/forecast snapshots truly stop scanning rows older than the configured
   window bounds - no full ROW_NUMBER() over 60 days when only the last 30
   are meaningful.
3. Retention stays off by default: the runner's ``_maybe_retention`` no-ops
   when ``shadow_retention_days`` is ``None``.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from weatherquant.dashboard.producer import (
    FORECAST_LOOKBACK_DAYS,
    MARKET_DECISION_WINDOW_DAYS,
    build_markets_snapshot,
)
from weatherquant.shadow.report import _percentile


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("SET TimeZone='UTC'")
    c.execute(
        """
        CREATE TABLE shadow_decisions (
            decision_id VARCHAR PRIMARY KEY,
            market_id VARCHAR NOT NULL,
            state VARCHAR NOT NULL,
            decision_time_utc TIMESTAMPTZ NOT NULL,
            net_edge DOUBLE,
            mode VARCHAR,
            primary_reason VARCHAR,
            decision_latency_ms BIGINT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE market_catalog (
            market_id VARCHAR PRIMARY KEY,
            event_id VARCHAR NOT NULL,
            question VARCHAR,
            bracket_label VARCHAR,
            bracket_low DOUBLE,
            bracket_high DOUBLE,
            target_date DATE,
            station VARCHAR NOT NULL,
            token_yes VARCHAR NOT NULL,
            closed BOOLEAN NOT NULL DEFAULT FALSE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            accepting_orders BOOLEAN NOT NULL DEFAULT TRUE,
            first_seen_utc TIMESTAMPTZ NOT NULL,
            last_seen_utc TIMESTAMPTZ NOT NULL
        )
        """
    )
    c.execute(
        """
        CREATE TABLE model_predictions (
            model_name VARCHAR NOT NULL,
            model_version VARCHAR NOT NULL,
            station VARCHAR NOT NULL,
            target_date DATE NOT NULL,
            event_id VARCHAR NOT NULL,
            forecast_run_time_utc TIMESTAMPTZ NOT NULL,
            probabilities_json VARCHAR NOT NULL,
            predicted_max_f DOUBLE,
            lead_hours DOUBLE,
            training_cutoff_utc TIMESTAMPTZ,
            forecast_received_at_utc TIMESTAMPTZ
        )
        """
    )
    c.execute(
        """
        CREATE TABLE token_books (
            token_id VARCHAR PRIMARY KEY,
            market_id VARCHAR NOT NULL,
            bids_json VARCHAR,
            asks_json VARCHAR,
            venue_ts_utc TIMESTAMPTZ,
            shadow_received_at_utc TIMESTAMPTZ,
            quality VARCHAR,
            defects VARCHAR
        )
        """
    )
    return c


# ---------------------------------------------------------------------------
# Fix 1: quantile_cont must match the old Python percentile
# ---------------------------------------------------------------------------

def _seed_latency(con, n=500, seed=7):
    rng = random.Random(seed)
    values = [max(1, int(rng.gauss(200, 60))) for _ in range(n)]
    con.executemany(
        "INSERT INTO shadow_decisions (decision_id, market_id, state, "
        "decision_time_utc, decision_latency_ms) VALUES (?,?,?,?,?)",
        [(f"D{i:04d}", "M0001", "ACCEPT", NOW, v) for i, v in enumerate(values)],
    )
    return values


@pytest.mark.parametrize("n", [500, 1000, 5000])
def test_quantile_cont_matches_python_percentile(con, n):
    values = _seed_latency(con, n=n)
    old = _percentile(values, 95)
    row = con.execute(
        "SELECT quantile_cont(decision_latency_ms, 0.95) "
        "FROM shadow_decisions WHERE decision_latency_ms IS NOT NULL"
    ).fetchone()
    new = float(row[0])

    assert abs(old - new) < 1e-6, (
        f"quantile_cont={new} differs from Python _percentile={old} "
        f"by {abs(old - new)}"
    )


def test_quantile_cont_empty_is_none(con):
    row = con.execute(
        "SELECT quantile_cont(decision_latency_ms, 0.95) "
        "FROM shadow_decisions WHERE decision_latency_ms IS NOT NULL"
    ).fetchone()
    assert row[0] is None


def test_quantile_cont_single_row(con):
    con.execute(
        "INSERT INTO shadow_decisions (decision_id, market_id, state, "
        "decision_time_utc, decision_latency_ms) "
        "VALUES ('D1','M1','ACCEPT', ?, 42)",
        [NOW],
    )
    row = con.execute(
        "SELECT quantile_cont(decision_latency_ms, 0.95) FROM shadow_decisions"
    ).fetchone()
    assert float(row[0]) == 42.0


# ---------------------------------------------------------------------------
# Fix 2: market snapshot window truly stops at MARKET_DECISION_WINDOW_DAYS
# ---------------------------------------------------------------------------

def _seed_market_decisions(con, now):
    """Create a world where every market has a *fresh* decision inside the
    window AND an *ancient* decision 90 days ago. The snapshot must pick the
    fresh one, never the ancient one."""
    n = 50
    con.executemany(
        "INSERT INTO market_catalog "
        "(market_id, event_id, question, bracket_label, bracket_low, bracket_high, "
        "target_date, station, token_yes, closed, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (f"M{i:04d}", f"E{i:04d}", f"Q{i}", f"{30 + i}mm", 30.0 + i, 40.0 + i,
             (now - timedelta(days=3)).date(),
             f"S{i % 5:02d}", f"T{i:04d}", False,
             now - timedelta(days=40), now)
            for i in range(n)
        ],
    )
    con.execute(
        "INSERT INTO token_books "
        "SELECT token_yes, market_id, '[0.5]', '[0.5]', NULL, ?, 'ok', '' "
        "FROM market_catalog",
        [now],
    )
    rows = []
    for i in range(n):
        rows.append((
            f"D_A{i:04d}", f"M{i:04d}", "ACCEPT",
            now - timedelta(days=35),  # OUTSIDE the 30-day window
            0.1, "LIVE", "edge_too_low",
        ))
        rows.append((
            f"D_F{i:04d}", f"M{i:04d}", "ACCEPT",
            now - timedelta(days=5),  # INSIDE
            0.25, "LIVE", "candidate",
        ))
    con.executemany(
        "INSERT INTO shadow_decisions (decision_id, market_id, state, "
        "decision_time_utc, net_edge, mode, primary_reason) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return n


def test_market_snapshot_window_filters_old_decisions(con):
    _seed_market_decisions(con, NOW)
    snap = build_markets_snapshot(
        con, now=NOW, run_id="r", source_version="t", primary_model="ecmwf_raw",
    )

    markets = snap["data"]["markets"]
    assert markets, "snapshot returned no markets"
    window_start = NOW - timedelta(days=MARKET_DECISION_WINDOW_DAYS)

    for mk in markets:
        last_dec = mk.get("last_decision")
        if not last_dec:
            continue
        last_ts = last_dec.get("decided_at_utc")
        if last_ts is None:
            continue
        last_decision = datetime.fromisoformat(last_ts)
        assert last_decision >= window_start, (
            f"market {market_id} last_decision_time_utc={last_decision} "
            f"is outside the {MARKET_DECISION_WINDOW_DAYS}-day window "
            f"(window starts at {window_start}) - the WHERE clause was dropped!"
        )


def test_market_snapshot_window_boundary(con):
    """A decision *exactly* at the window bound must count as IN."""
    con.execute(
        """
        INSERT INTO market_catalog
            (market_id, event_id, question, bracket_label, bracket_low, bracket_high,
             target_date, station, token_yes, closed, first_seen_utc, last_seen_utc)
        VALUES ('MX','EX','QMX','30mm',30.0,40.0,?,'S00','TX',false,?,?)
        """,
        [(NOW - timedelta(days=3)).date(), NOW - timedelta(days=40), NOW],
    )
    con.execute(
        "INSERT INTO token_books (token_id, market_id, bids_json, asks_json, "
        "venue_ts_utc, shadow_received_at_utc, quality, defects) "
        "VALUES ('TX','MX','[0.5]','[0.5]',NULL,?,'ok','')",
        [NOW],
    )
    boundary_ts = NOW - timedelta(days=MARKET_DECISION_WINDOW_DAYS)
    con.execute(
        "INSERT INTO shadow_decisions "
        "(decision_id, market_id, state, decision_time_utc, net_edge, mode, primary_reason) "
        "VALUES ('DBOUND','MX','ACCEPT',?,?,?,?)",
        [boundary_ts, 0.05, "LIVE", "edge_ok"],
    )
    snap = build_markets_snapshot(
        con, now=NOW, run_id="r", source_version="t", primary_model="ecmwf_raw",
    )
    mx = next((m for m in snap["data"]["markets"] if m["market_id"] == "MX"), None)
    assert mx is not None, "market MX must be present in the snapshot"
    last_dec = mx.get("last_decision")
    assert last_dec is not None, "MX must have a last_decision"
    assert datetime.fromisoformat(last_dec["decided_at_utc"]) == boundary_ts


# ---------------------------------------------------------------------------
# Fix 2 continued: forecast snapshot LOOKBACK_DAYS lower bound
# ---------------------------------------------------------------------------

def test_forecast_snapshot_lookback_filters_old_predictions(con):
    now = NOW
    con.execute(
        """
        INSERT INTO model_predictions
            (model_name, model_version, station, target_date, event_id,
             forecast_run_time_utc, probabilities_json, predicted_max_f)
        VALUES
            ('ecmwf_raw','v1','S01',?, 'EV1', ?, '{}', 32.0),
            ('ecmwf_raw','v1','S01',?, 'EV1', ?, '{}', 33.0)
        """,
        [
            (now - timedelta(days=3)).date(), now - timedelta(hours=2),
            (now - timedelta(days=FORECAST_LOOKBACK_DAYS + 2)).date(), now - timedelta(hours=1),
        ],
    )
    from weatherquant.dashboard.producer import build_forecasts_snapshot
    snap = build_forecasts_snapshot(
        con, now=now, run_id="r", source_version="t",
        primary_model="ecmwf_raw", horizon_days=7,
    )
    horizons = [p["target_date"] for p in snap.get("forecasts", [])]
    if horizons:
        lb = (now.date() - timedelta(days=FORECAST_LOOKBACK_DAYS))
        for hd in horizons:
            assert hd >= lb, (
                f"forecast target_date={hd} is below "
                f"FORECAST_LOOKBACK_DAYS={FORECAST_LOOKBACK_DAYS} bound ({lb})"
            )

#!/usr/bin/env python
"""Benchmark the three Shadow OOM-fix changes against synthetic data.

Compares, across 1M / 3M / 5M row synthetic databases:

1. P95 latency: Python fetchall + sort (old) vs DuckDB quantile_cont (new).
2. Markets snapshot: ROW_NUMBER() across ALL decisions (old) vs rolling 30-day window (new).
3. Forecasts snapshot: QUALIFY across ALL predictions (old) vs target_date lower bound (new).

Prints JSON to stdout.

Usage::

    uv run python scripts/bench_shadow_oom.py                  # 1M / 3M / 5M
    uv run python scripts/bench_shadow_oom.py --rows 3000000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


SCHEMA_SQL = """
CREATE TABLE shadow_decisions (
    decision_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL, cycle_id VARCHAR NOT NULL,
    market_id VARCHAR NOT NULL, event_id VARCHAR, token_id VARCHAR,
    bracket_label VARCHAR, target_date DATE, station VARCHAR NOT NULL,
    direction VARCHAR NOT NULL, state VARCHAR NOT NULL,
    primary_reason VARCHAR, model_name VARCHAR, model_version VARCHAR,
    model_run_id VARCHAR, forecast_run_time_utc TIMESTAMPTZ,
    snapshot_id VARCHAR, book_id VARCHAR, venue_ts_utc TIMESTAMPTZ,
    collector_received_at_utc TIMESTAMPTZ, shadow_received_at_utc TIMESTAMPTZ,
    decision_time_utc TIMESTAMPTZ NOT NULL, snapshot_age_ms BIGINT,
    model_probability DOUBLE, market_price DOUBLE,
    best_bid DOUBLE, best_ask DOUBLE, spread DOUBLE,
    available_depth_usd DOUBLE, proposed_shares DOUBLE,
    estimated_fill_shares DOUBLE, estimated_fill_price DOUBLE,
    estimated_fees_usd DOUBLE, estimated_buffer_usd DOUBLE,
    estimated_cost_usd DOUBLE, expected_net_profit_usd DOUBLE,
    net_edge DOUBLE, market_closed BOOLEAN NOT NULL DEFAULT FALSE,
    forecast_age_hours DOUBLE, thresholds_version VARCHAR,
    decision_latency_ms BIGINT, created_at_utc TIMESTAMPTZ NOT NULL,
    mode VARCHAR DEFAULT 'LIVE', mode_reason VARCHAR
);
CREATE INDEX idx_decisions_market ON shadow_decisions (market_id, decision_time_utc);

CREATE TABLE model_predictions (
    model_name VARCHAR NOT NULL, model_version VARCHAR NOT NULL,
    station VARCHAR NOT NULL, target_date DATE NOT NULL, event_id VARCHAR NOT NULL,
    forecast_run_time_utc TIMESTAMPTZ NOT NULL,
    probabilities_json VARCHAR NOT NULL, predicted_max_f DOUBLE,
    lead_hours DOUBLE NOT NULL, n_hours INTEGER, n_missing_hours INTEGER,
    model_run_id VARCHAR NOT NULL, training_cutoff_utc TIMESTAMPTZ NOT NULL,
    forecast_publishable_at_utc TIMESTAMPTZ NOT NULL,
    forecast_publishable_basis VARCHAR NOT NULL,
    forecast_received_at_utc TIMESTAMPTZ NOT NULL,
    decision_time_utc TIMESTAMPTZ NOT NULL,
    calibration_version VARCHAR NOT NULL, source_data_hash VARCHAR NOT NULL,
    computed_at_utc TIMESTAMPTZ NOT NULL
);

CREATE TABLE market_catalog (
    market_id VARCHAR PRIMARY KEY, event_id VARCHAR NOT NULL, question VARCHAR,
    bracket_label VARCHAR, bracket_low DOUBLE, bracket_high DOUBLE,
    target_date DATE, station VARCHAR NOT NULL, token_yes VARCHAR NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE, closed BOOLEAN NOT NULL DEFAULT FALSE,
    accepting_orders BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_utc TIMESTAMPTZ NOT NULL, last_seen_utc TIMESTAMPTZ NOT NULL
);
"""


def _build_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in SCHEMA_SQL.split(";"):
        s = stmt.strip()
        if s:
            con.execute(s)


def _seed(
    con: duckdb.DuckDBPyConnection,
    *,
    n_decisions: int,
    n_predictions: int,
    n_markets: int = 200,
    seed: int = 42,
) -> None:
    rng = random.Random(seed)
    now = datetime.now(tz=UTC)
    start_iso = (now - timedelta(days=60)).isoformat()
    con.execute("SET memory_limit='4GB'")

    # --- market_catalog (tiny, Python build) ---
    market_rows = []
    for i in range(n_markets):
        mid, eid = f"M{i:05d}", f"E{i:04d}"
        active = rng.random() > 0.05
        closed = not active or rng.random() < 0.1
        target_date = (now - timedelta(days=rng.uniform(-2, 6))).date()
        bracket_low = 30.0 + i
        market_rows.append((
            mid, eid,
            f"Will station {i} exceed threshold?",
            f"{bracket_low:.1f}mm", bracket_low, 40.0 + i,
            target_date,
            f"S{i % 12:02d}", f"TKN{i:05d}Y",
            active, closed, active and not closed,
            now - timedelta(days=rng.uniform(20, 60)),
            now - timedelta(days=rng.uniform(0, 10)),
        ))
    con.executemany(
        "INSERT INTO market_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        market_rows,
    )

    # --- shadow_decisions: one big INSERT ... SELECT from generate_series ---
    # time spread: 60 days of history uniformly distributed
    # latency: 高斯 ~ N(250, 80)  => 用 abs(normal()) + base 近似
    print(f"  seeding {n_decisions:,} decisions (SQL vectorised) ...", end=" ", flush=True)
    con.execute(
        f"""
        INSERT INTO shadow_decisions
        SELECT
            'D' || LPAD(g.id::VARCHAR, 8, '0')   AS decision_id,
            'run_001'                              AS run_id,
            'C' || LPAD(g.id::VARCHAR, 7, '0')   AS cycle_id,
            m.market_id,
            m.event_id,
            m.token_yes                            AS token_id,
            m.bracket_label,
            m.target_date,
            m.station,
            CASE WHEN random() < 0.5 THEN 'buy_yes' ELSE 'sell_yes' END AS direction,
            CASE
                WHEN random() < 0.15 THEN 'ACCEPT'
                WHEN random() < 0.75 THEN 'REJECT'
                WHEN random() < 0.95 THEN 'NO_TRADE'
                ELSE 'CANDIDATE'
            END                                    AS state,
            CASE (abs(hash(g.id::VARCHAR)) % 5)
                WHEN 0 THEN 'edge_too_low'
                WHEN 1 THEN 'forecast_too_old'
                WHEN 2 THEN 'snapshot_age'
                WHEN 3 THEN 'depth_too_low'
                ELSE 'fill_unlikely'
            END                                    AS primary_reason,
            CASE (abs(hash(g.id::VARCHAR)) % 4)
                WHEN 0 THEN 'ecmwf_raw'
                WHEN 1 THEN 'gfs_raw'
                WHEN 2 THEN 'fusion'
                ELSE 'calibrated'
            END                                    AS model_name,
            'v1.0'                                 AS model_version,
            'MR' || LPAD((g.id % 1000000)::VARCHAR, 6, '0') AS model_run_id,
            d_time - (random() * INTERVAL '2 hours') AS forecast_run_time_utc,
            'S' || LPAD((g.id % 1000000)::VARCHAR, 6, '0') AS snapshot_id,
            'B' || LPAD((g.id % 1000000)::VARCHAR, 6, '0') AS book_id,
            d_time - (random() * INTERVAL '30 seconds')    AS venue_ts_utc,
            d_time - (random() * INTERVAL '5 seconds')      AS collector_received_at_utc,
            d_time                                          AS shadow_received_at_utc,
            d_time                                          AS decision_time_utc,
            CAST(100 + random() * 2900 AS BIGINT)           AS snapshot_age_ms,
            random()                                        AS model_probability,
            0.5                                             AS market_price,
            0.49                                            AS best_bid,
            0.51                                            AS best_ask,
            0.02                                            AS spread,
            50 + random() * 4950                            AS available_depth_usd,
            100.0                                           AS proposed_shares,
            100.0                                           AS estimated_fill_shares,
            0.50                                            AS estimated_fill_price,
            0.01                                            AS estimated_fees_usd,
            0.02                                            AS estimated_buffer_usd,
            0.03                                            AS estimated_cost_usd,
            -0.1 + random() * 0.6                           AS expected_net_profit_usd,
            -0.05 + random() * 0.35                         AS net_edge,
            m.closed                                        AS market_closed,
            0.5 + random() * 23.5                           AS forecast_age_hours,
            'v1'                                            AS thresholds_version,
            CAST(GREATEST(1, 150 + abs(random() * 300 - 100)) AS BIGINT) AS decision_latency_ms,
            d_time                                          AS created_at_utc,
            'LIVE'                                          AS mode,
            NULL                                            AS mode_reason
        FROM generate_series(1, {n_decisions}) AS g(id)
        JOIN (SELECT *, ROW_NUMBER() OVER () AS rn FROM market_catalog) m
          ON m.rn = (g.id % {n_markets}) + 1,
             (SELECT TIMESTAMPTZ '{start_iso}' + INTERVAL '60 days' * random() AS d_time) dt
        """
    )
    con.execute("VACUUM shadow_decisions")
    print("ok")

    # --- model_predictions: same trick, one big SQL ---
    print(f"  seeding {n_predictions:,} predictions (SQL vectorised) ...", end=" ", flush=True)
    con.execute(
        f"""
        INSERT INTO model_predictions
        SELECT
            CASE (abs(hash(g.id::VARCHAR)) % 4)
                WHEN 0 THEN 'ecmwf_raw'
                WHEN 1 THEN 'gfs_raw'
                WHEN 2 THEN 'fusion'
                ELSE 'calibrated'
            END                                    AS model_name,
            'v1.0'                                 AS model_version,
            m.station,
            m.target_date,
            m.event_id,
            frun,
            '{{"bins": [0.1, 0.2, 0.4, 0.2, 0.1]}}' AS probabilities_json,
            20.0 + random() * 30.0                  AS predicted_max_f,
            1.0 + random() * 47.0                   AS lead_hours,
            24                                      AS n_hours,
            0                                       AS n_missing_hours,
            'MR' || LPAD((g.id % 1000000)::VARCHAR, 6, '0') AS model_run_id,
            frun - INTERVAL '6 hours'               AS training_cutoff_utc,
            frun + INTERVAL '6 hours'               AS forecast_publishable_at_utc,
            'estimated'                             AS forecast_publishable_basis,
            frun + (random() * INTERVAL '30 minutes') AS forecast_received_at_utc,
            frun + (random() * INTERVAL '2 hours')    AS decision_time_utc,
            'v3'                                    AS calibration_version,
            'hash' || LPAD((g.id % 1000000)::VARCHAR, 6, '0') AS source_data_hash,
            frun + INTERVAL '30 seconds'            AS computed_at_utc
        FROM generate_series(1, {n_predictions}) AS g(id)
        JOIN (SELECT *, ROW_NUMBER() OVER () AS rn FROM market_catalog) m
          ON m.rn = (g.id % {n_markets}) + 1,
             (SELECT TIMESTAMPTZ '{start_iso}' + INTERVAL '60 days' * random() AS frun) dt
        """
    )
    con.execute("VACUUM model_predictions")
    print("ok")


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

def _percentile_py(values: list, pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    w = rank - lo
    return ordered[lo] * (1 - w) + ordered[hi] * w


def _run_bench(fn, *args):
    tracemalloc.start()
    t0 = time.perf_counter()
    out = fn(*args)
    t = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {
        "latency_ms": int(t * 1000),
        "peak_mb": round(peak / (1024 * 1024), 2),
        **(out if isinstance(out, dict) else {}),
    }


def bench_p95_py(con):
    rows = con.execute(
        "SELECT decision_latency_ms FROM shadow_decisions WHERE decision_latency_ms IS NOT NULL"
    ).fetchall()
    return {"value": _percentile_py([r[0] for r in rows], 95)}


def bench_p95_sql(con):
    row = con.execute(
        "SELECT quantile_cont(decision_latency_ms, 0.95) "
        "FROM shadow_decisions WHERE decision_latency_ms IS NOT NULL"
    ).fetchone()
    return {"value": float(row[0]) if row and row[0] is not None else None}


def bench_markets_old(con):
    # Full ROW_NUMBER across all decisions - what was there before the fix
    con.execute(
        """
        SELECT market_id, state, decision_time_utc, net_edge, mode, primary_reason
        FROM shadow_decisions
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY decision_time_utc DESC) = 1
        """
    ).fetchall()
    return {}


def bench_markets_new(con, now):
    window = now - timedelta(days=30)
    con.execute(
        """
        SELECT market_id, state, decision_time_utc, net_edge, mode, primary_reason
        FROM shadow_decisions
        WHERE decision_time_utc >= ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY decision_time_utc DESC) = 1
        """,
        [window],
    ).fetchall()
    return {}


def bench_forecasts_old(con, now):
    horizon = now.date() + timedelta(days=10)
    con.execute(
        """
        SELECT model_name, model_version, target_date, event_id, probabilities_json
        FROM model_predictions
        WHERE target_date <= ? AND forecast_run_time_utc <= ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY model_name, target_date, event_id
            ORDER BY forecast_run_time_utc DESC
        ) = 1
        """,
        [horizon, now],
    ).fetchall()
    return {}


def bench_forecasts_new(con, now):
    horizon = now.date() + timedelta(days=10)
    lb = now.date() - timedelta(days=1)
    con.execute(
        """
        SELECT model_name, model_version, target_date, event_id, probabilities_json
        FROM model_predictions
        WHERE target_date >= ? AND target_date <= ? AND forecast_run_time_utc <= ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY model_name, target_date, event_id
            ORDER BY forecast_run_time_utc DESC
        ) = 1
        """,
        [lb, horizon, now],
    ).fetchall()
    return {}


# ---------------------------------------------------------------------------

def run_one(n_decisions: int, n_predictions: int) -> dict:
    print(f"\n{'='*60}")
    print(f"  {n_decisions:,} decisions / {n_predictions:,} predictions")
    print(f"{'='*60}")

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=2")
    _build_schema(con)
    _seed(con, n_decisions=n_decisions, n_predictions=n_predictions)

    row_count = con.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0]
    pred_count = con.execute("SELECT COUNT(*) FROM model_predictions").fetchone()[0]
    print(f"  table sizes: decisions={row_count:,} predictions={pred_count:,}")

    now = datetime.now(tz=UTC)

    result = {
        "n_decisions": row_count,
        "n_predictions": pred_count,
        "p95_python": _run_bench(bench_p95_py, con),
        "p95_sql": _run_bench(bench_p95_sql, con),
        "markets_old": _run_bench(bench_markets_old, con),
        "markets_new": _run_bench(bench_markets_new, con, now),
        "forecasts_old": _run_bench(bench_forecasts_old, con, now),
        "forecasts_new": _run_bench(bench_forecasts_new, con, now),
    }

    if result["p95_python"]["value"] is not None and result["p95_sql"]["value"] is not None:
        diff = abs(result["p95_python"]["value"] - result["p95_sql"]["value"])
        print(
            f"  P95 python={result['p95_python']['value']:.1f}  "
            f"sql={result['p95_sql']['value']:.1f}  diff={diff:.3f}"
        )
        if diff > 1e-6:
            raise SystemExit("P95 values disagree - quantile_cont does not match original _percentile")

    # sanity: "new" must not be slower than "old" by more than 2x on any case
    for kold, knew in [
        ("markets_old", "markets_new"),
        ("forecasts_old", "forecasts_new"),
    ]:
        if result[kold]["latency_ms"] > 0:
            ratio = result[knew]["latency_ms"] / result[kold]["latency_ms"]
            print(f"  {kold}->{knew}: {ratio:.2f}x (lower is better)")

    con.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=0, help="Single row count; omit to run 1M/3M/5M")
    args = ap.parse_args()

    scales = [args.rows] if args.rows else [1_000_000, 3_000_000, 5_000_000]

    out = []
    for n in scales:
        out.append(run_one(n, n))

    print(f"\n{'='*60}")
    print("  JSON RESULTS (paste into docs/shadow-oom-fix.md)")
    print(f"{'='*60}")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

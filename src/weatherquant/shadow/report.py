"""The machine-readable run report.

Three separations the specification asks for, enforced in how the numbers are
labelled rather than in how they are computed:

1. **Prediction quality** - how well probabilities track outcomes. The shadow
   cannot answer this: it has no settlement. It reports coverage and refusals
   and says so, rather than filling the gap with something that looks like
   accuracy.
2. **Theoretical edge** - the model's own expectation after costs. This is an
   expectation *under the model*, not a profit, and every field derived from it
   is labelled as such.
3. **Liquidity vs opportunity** - how much depth existed, and separately how
   many evaluations passed every gate. A large theoretical edge with no depth
   is not an opportunity, and reporting them as one number would hide that.

There is no equity curve, and ``equity_curve`` is explicitly ``null``: nothing
has been bought, so nothing can have gained. Producing a curve from
hypothetical fills would be the single most misleading artefact this module
could emit.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from weatherquant.shadow.publish import atomic_write_json

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "build_report",
    "format_report",
    "load_report_snapshot",
    "publish_report_snapshot",
    "snapshot_staleness",
]


def build_report(con, *, now: datetime | None = None, horizon_days: int = 7) -> dict[str, Any]:
    """Build the full report from an open shadow database."""
    now = now or datetime.now(tz=UTC)

    runs = _one(con, "SELECT run_id, started_at_utc, stopped_at_utc, status FROM shadow_runs ORDER BY started_at_utc DESC LIMIT 1")
    current_run = None
    if runs:
        run_id, started, stopped, status = runs
        uptime = (now - started).total_seconds() if status == "running" and started else None
        current_run = {
            "run_id": run_id,
            "started_at_utc": _iso(started),
            "stopped_at_utc": _iso(stopped),
            "status": status,
            "uptime_seconds": uptime,
        }

    # How many runs this database file has ever held. Every decision aggregate
    # below counts rows across all of them - nothing in this module filters by
    # run_id and nothing anywhere deletes them - so this number is what stops
    # the totals from being read as "since this process started".
    runs_in_database = int(_one(con, "SELECT COUNT(*) FROM shadow_runs") or 1)

    counts = {
        row[0]: _table_count(con, row[0])
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
    }
    decision_counts = {
        row[0]: row[1]
        for row in con.execute("SELECT state, COUNT(*) FROM shadow_decisions GROUP BY 1").fetchall()
    }
    total_decisions = sum(decision_counts.values())

    reasons = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT reason, COUNT(*) FROM rejection_reasons WHERE is_primary GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    }
    all_reasons = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT reason, COUNT(*) FROM rejection_reasons GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    }

    by_mode = _by_mode(con)
    edge = _edge_stats(con)
    latency = _percentile(
        [
            r[0]
            for r in con.execute(
                "SELECT decision_latency_ms FROM shadow_decisions "
                "WHERE decision_latency_ms IS NOT NULL"
            ).fetchall()
        ],
        95,
    )

    return {
        "generated_at_utc": now.isoformat(),
        "run": current_run,
        "tables": {k: v for k, v in sorted(counts.items()) if v},
        "liveness": _liveness(con, now),
        "decisions": {
            "evaluated": total_decisions,
            "by_state": {k: v for k, v in sorted(decision_counts.items())},
            "candidates_generated": edge["priced_decisions"],
            "accepted": decision_counts.get("ACCEPT", 0),
            "rejected": decision_counts.get("REJECT", 0),
            "no_trade": decision_counts.get("NO_TRADE", 0),
            "data_unavailable": decision_counts.get("DATA_UNAVAILABLE", 0),
            "interrupted": decision_counts.get("CANDIDATE", 0),
            "by_mode": by_mode,
            "live_accepted": by_mode.get("LIVE", {}).get("accepted", 0),
            "replay_accepted": by_mode.get("REPLAY", {}).get("accepted", 0),
            "primary_reasons": reasons,
            "all_reasons": all_reasons,
            "p95_decision_latency_ms": latency,
            "by_model": _by_model(con),
            "accept_scope": _accept_scope(con, runs_in_database=runs_in_database),
            # Scope of every number in this block. It is *not* the current
            # process: nothing here filters on run_id and nothing deletes
            # decisions, so a restart adds to these totals instead of
            # resetting them. `cycles` in the heartbeat is the one counter
            # that does start at zero, which is exactly how the two got
            # mistaken for each other.
            "window": (
                f"cumulative over this shadow database's lifetime "
                f"({runs_in_database} run(s) recorded); a restart does not reset it"
            ),
            "runs_in_database": runs_in_database,
        },
        "theoretical_edge": edge,
        "prediction_quality": _prediction_quality(con, now),
        "liquidity": _liquidity(con),
        "coverage": _coverage(con, horizon_days=horizon_days),
        "data_quality": _data_quality(con),
        "warnings": _warnings(decision_counts, by_mode),
        "equity_curve": None,
        "equity_curve_note": (
            "null on purpose: shadow mode places no orders and holds no position, "
            "so there is no realised return to plot and a hypothetical one would be misleading"
        ),
    }


# --- sections --------------------------------------------------------------


def _liveness(con, now: datetime) -> dict[str, Any]:
    last_book = _one(con, "SELECT MAX(shadow_received_at_utc) FROM token_books")
    last_venue = _one(con, "SELECT MAX(venue_ts_utc) FROM token_books")
    last_forecast = _one(con, "SELECT MAX(received_at_utc) FROM forecast_runs WHERE status = 'ok'")
    last_run_time = _one(con, "SELECT MAX(run_time_utc) FROM forecast_runs WHERE status = 'ok'")
    last_archive = _one(con, "SELECT MAX(last_received_at_utc) FROM ingest_checkpoints")
    return {
        "collector_last_seen_utc": _iso(last_archive),
        "collector_silence_seconds": _age(last_archive, now),
        "latest_orderbook_read_utc": _iso(last_book),
        "latest_orderbook_read_age_seconds": _age(last_book, now),
        "latest_orderbook_venue_utc": _iso(last_venue),
        "latest_orderbook_venue_age_seconds": _age(last_venue, now),
        "latest_forecast_received_utc": _iso(last_forecast),
        "latest_forecast_age_seconds": _age(last_forecast, now),
        "latest_forecast_run_time_utc": _iso(last_run_time),
        "latest_forecast_run_age_seconds": _age(last_run_time, now),
    }


def _by_mode(con) -> dict[str, dict[str, Any]]:
    """Counts split by LIVE / REPLAY, so historical output can never be read
    as contemporary signal.

    A replay decision is a legitimate observation *about the archive*; it is
    not a decision anyone could have made at that instant, and pooling it with
    a live one is precisely how a shadow log starts asserting something that
    never happened.
    """
    out: dict[str, dict[str, Any]] = {}
    for mode, state, n in con.execute(
        "SELECT COALESCE(mode, 'UNKNOWN'), state, COUNT(*) FROM shadow_decisions GROUP BY 1, 2"
    ).fetchall():
        entry = out.setdefault(str(mode), {"evaluated": 0, "by_state": {}, "accepted": 0})
        entry["evaluated"] += int(n)
        entry["by_state"][str(state)] = int(n)
        if state == "ACCEPT":
            entry["accepted"] += int(n)
    return dict(sorted(out.items()))


def _by_model(con) -> dict[str, dict[str, Any]]:
    """State counts per model, so a decision total can never be read as if
    every model contributed the same number of judgements."""
    out: dict[str, dict[str, Any]] = {}
    for model, state, n in con.execute(
        "SELECT COALESCE(model_name, 'unknown'), state, COUNT(*) "
        "FROM shadow_decisions GROUP BY 1, 2"
    ).fetchall():
        out.setdefault(str(model), {})[str(state)] = int(n)
    return dict(sorted(out.items()))


def _accept_scope(con, *, runs_in_database: int) -> dict[str, Any]:
    """How many *independent* observations an ACCEPT total actually contains.

    The engine re-evaluates every open market on every cycle, so a single
    opportunity that stays open for an hour is recorded dozens of times. The
    raw ACCEPT count is a number of **observations**, not of opportunities -
    reporting it alone is how a shadow log starts to sound like a track
    record. Everything here is counted to make that distinction checkable.

    Token is the decided market's ``token_yes``: a buy_yes and a sell_yes on
    the same bracket are two reads of the same book, not two instruments.

    Scope: the whole database, not this process. ``shadow_decisions`` is never
    filtered by ``run_id`` and never deleted, so a restart **adds** to these
    numbers. ``scope`` / ``runs_in_database`` travel with the payload so a
    reader never has to guess, and a zeroed ``cycles`` counter in the heartbeat
    can never be mistaken for these totals falling.
    """
    obs, markets, pairs, target_dates = con.execute(
        """
        SELECT COUNT(*),
               COUNT(DISTINCT market_id),
               COUNT(DISTINCT market_id || '|' || COALESCE(direction, '')),
               COUNT(DISTINCT target_date)
        FROM shadow_decisions WHERE state = 'ACCEPT'
        """
    ).fetchone()
    tokens = _one(
        con,
        "SELECT COUNT(DISTINCT c.token_yes) FROM shadow_decisions d "
        "JOIN market_catalog c ON c.market_id = d.market_id "
        "WHERE d.state = 'ACCEPT' AND c.token_yes IS NOT NULL",
    )
    model_count = _one(con, "SELECT COUNT(DISTINCT model_name) FROM shadow_decisions") or 0
    observations = int(obs or 0)
    distinct_pairs = int(pairs or 0)
    return {
        "accepted_observations": observations,
        "accepted_distinct_markets": int(markets or 0),
        "accepted_distinct_tokens": int(tokens or 0),
        "accepted_distinct_target_dates": int(target_dates or 0),
        "accepted_distinct_market_directions": distinct_pairs,
        "accepted_repeats": max(observations - distinct_pairs, 0),
        "models_with_decisions": int(model_count),
        "scope": "shadow_database_lifetime",
        "runs_in_database": int(runs_in_database),
        "window": (
            f"cumulative over this shadow database's lifetime "
            f"({runs_in_database} run(s) recorded); a restart does not reset it"
        ),
        "note": (
            "an ACCEPT is one evaluation of one market in one cycle, not one "
            "opportunity: the same bracket is counted again on every later "
            "cycle in which it still passes, so accepted_observations >= "
            "accepted_distinct_markets by construction"
        ),
    }


def _edge_stats(con) -> dict[str, Any]:
    row = _one(
        con,
        "SELECT COUNT(*), AVG(net_edge), MIN(net_edge), MAX(net_edge), "
        "SUM(CASE WHEN net_edge > 0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN net_edge >= 0 THEN 1 ELSE 0 END) "
        "FROM shadow_decisions WHERE net_edge IS NOT NULL",
    )
    priced, avg, lo, hi, positive, non_negative = row or (0, None, None, None, 0, 0)
    accepted = _one(con, "SELECT AVG(net_edge) FROM shadow_decisions WHERE state = 'ACCEPT'")
    return {
        "note": (
            "expectation under the model's own probabilities, after fill, fee and "
            "buffer - not a realised return and not a forecast of one"
        ),
        "priced_decisions": priced,
        "mean_usd_per_share": avg,
        "min_usd_per_share": lo,
        "max_usd_per_share": hi,
        "positive_count": positive,
        "non_negative_count": non_negative,
        "mean_of_accepted_usd_per_share": accepted,
    }


def _prediction_quality(con, now: datetime) -> dict[str, Any]:
    by_model = {
        row[0]: {"predictions": row[1], "events": row[2], "latest_run": _iso(row[3])}
        for row in con.execute(
            "SELECT model_name, COUNT(*), COUNT(DISTINCT event_id), MAX(forecast_run_time_utc) "
            "FROM model_predictions GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    refusals = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT kind, COUNT(*) FROM data_quality_events WHERE kind = 'prediction_refused' GROUP BY 1"
        ).fetchall()
    }
    return {
        "by_model": by_model,
        "refused": refusals,
        "note": (
            "Brier / log-loss cannot be computed here: they need the settlement, "
            "which has not happened yet at decision time. Prediction *quality* is "
            "scored retrospectively by the research pipeline, not by the shadow"
        ),
        "scored": False,
    }


def _liquidity(con) -> dict[str, Any]:
    row = _one(
        con,
        "SELECT COUNT(*), AVG(spread), AVG(ask_depth_usd), AVG(bid_depth_usd) "
        "FROM market_snapshots WHERE spread IS NOT NULL",
    ) or (0, None, None, None)
    snapshots, spread, ask_depth, bid_depth = row
    insufficient = _one(
        con,
        "SELECT COUNT(*) FROM rejection_reasons WHERE reason = 'INSUFFICIENT_LIQUIDITY'",
    ) or 0
    return {
        "snapshots": snapshots,
        "mean_spread_usd": spread,
        "mean_ask_depth_usd": ask_depth,
        "mean_bid_depth_usd": bid_depth,
        "insufficient_liquidity_rejections": insufficient,
        "note": "depth actually present on the books at the decision instant",
    }


def _coverage(con, *, horizon_days: int) -> dict[str, Any]:
    open_targets = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT target_date FROM market_catalog "
            "WHERE active AND accepting_orders AND NOT closed AND target_date IS NOT NULL "
            "ORDER BY target_date"
        ).fetchall()
    ]
    models = [r[0] for r in con.execute("SELECT DISTINCT model_name FROM model_versions").fetchall()]
    needed = len(open_targets) * len(models)
    # A forecast pair counts only when both its model and target date belong
    # to the *same* scope as the denominator. Historic predictions must not
    # turn a 16/12 mismatch into a reported 100% via min().
    pairs = con.execute(
        """
        SELECT p.model_name, p.target_date,
               BOOL_OR(
                   p.n_hours > 0 AND p.n_missing_hours = 0
                   AND p.forecast_publishable_at_utc <= p.decision_time_utc
                   AND p.training_cutoff_utc <= p.decision_time_utc
               ) AS valid_at_decision
        FROM model_predictions p
        JOIN (SELECT DISTINCT target_date FROM market_catalog
              WHERE active AND accepting_orders AND NOT closed
                AND target_date IS NOT NULL) d ON p.target_date = d.target_date
        JOIN (SELECT DISTINCT model_name FROM model_versions) m
             ON p.model_name = m.model_name
        GROUP BY p.model_name, p.target_date
        """
    ).fetchall()
    have = len(pairs)
    valid = sum(1 for _model, _date, flag in pairs if flag)
    historical_pairs = _one(
        con,
        "SELECT COUNT(*) FROM (SELECT DISTINCT model_name, target_date "
        "FROM model_predictions) historical",
    ) or 0

    token_rows = con.execute(
        "SELECT market_id, token_yes FROM market_catalog WHERE active AND accepting_orders AND NOT closed"
    ).fetchall()
    with_book = 0
    for _market_id, token in token_rows:
        if token and _one(con, "SELECT token_id FROM token_books WHERE token_id = ?", [token]):
            with_book += 1
    # How many tokens have ever had a book recorded. This used to filter on
    # `shadow_received_at_utc >= now`, which can only ever match a row stamped
    # in the future - so the field read 0 forever while every book on screen
    # was seconds old. "Total" is what the name says, so that is what it is.
    books_total = _one(con, "SELECT COUNT(*) FROM token_books") or 0
    return {
        "open_target_dates": [str(d) for d in open_targets],
        "models_tracked": sorted(models),
        "forecast_needed": needed,
        "forecast_available": have,
        "forecast_coverage": (have / needed) if needed else None,
        "forecast_historical_pairs": int(historical_pairs),
        "forecast_valid_available": valid,
        "forecast_valid_coverage": (valid / needed) if needed else None,
        "forecast_valid_definition": (
            "at least one prediction per active model/target-date pair with "
            "n_hours > 0, n_missing_hours = 0, publishable_at <= decision_time "
            "and training_cutoff <= decision_time; historical validity, not "
            "a guarantee of current forecast freshness"
        ),
        "open_markets": len(token_rows),
        "open_markets_with_a_book": with_book,
        "orderbook_coverage": (with_book / len(token_rows)) if token_rows else None,
        "books_total": books_total,
        "note": (
            "forecast_available is the number of model/target-date pairs "
            "recorded for currently open target dates; forecast_historical_pairs "
            "includes every historical target date. No open dates -> N/A."
        ),
    }


def _data_quality(con) -> dict[str, Any]:
    by_kind = {
        row[0]: {"count": row[1], "severity": row[2]}
        for row in con.execute(
            "SELECT kind, COUNT(*), MIN(severity) FROM data_quality_events GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    }
    gap_kinds = {
        "archive_gap",
        "archive_file_truncated",
        "archive_file_missing",
        "archive_line_malformed",
        "archive_line_oversized",
        "archive_file_unreadable",
    }
    gaps = sum(v["count"] for k, v in by_kind.items() if k in gap_kinds)
    errors = sum(v["count"] for v in by_kind.values() if v["severity"] == "error")
    return {
        "by_kind": by_kind,
        "data_gaps": gaps,
        "errors": errors,
        "note": "recorded, never silently skipped",
    }


def _warnings(decision_counts: dict, by_mode: dict | None = None) -> list[str]:
    warnings: list[str] = []
    if decision_counts.get("CANDIDATE"):
        warnings.append(
            f"{decision_counts['CANDIDATE']} decision(s) are still CANDIDATE: an evaluation "
            "started and never finished, which means a cycle was interrupted"
        )
    if decision_counts.get("ACCEPT"):
        warnings.append(
            f"{decision_counts['ACCEPT']} ACCEPT signal(s) were recorded. In shadow mode this "
            "is an observation only - no order was or will be placed, and an ACCEPT is not "
            "evidence that the trade would have profited"
        )
    replay = (by_mode or {}).get("REPLAY", {})
    if replay.get("accepted"):
        warnings.append(
            f"{replay['accepted']} ACCEPT signal(s) are REPLAY, not LIVE: they were "
            "evaluated against a historical archive at the instant that archive "
            "represents. They are NOT contemporary signals and must not be counted "
            "with the live ones"
        )
    unknown = (by_mode or {}).get("UNKNOWN")
    if unknown:
        warnings.append(
            f"{unknown.get('evaluated', 0)} decision(s) have mode=UNKNOWN, which means "
            "they were written before the mode was recorded. Treat them as neither "
            "live nor replay until re-run"
        )
    return warnings


def _table_count(con, table: str) -> int:
    # Table name comes from information_schema, not from input; quoted anyway.
    row = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0]) if row else 0


# --- helpers ---------------------------------------------------------------


def _one(con, sql: str, params: list | None = None):
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row and len(row) == 1 else (row if row else None)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _age(value, now: datetime) -> float | None:
    if value is None:
        return None
    return round((now - value).total_seconds(), 3)


def _percentile(values: list, pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def format_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of the same dict the JSON mode emits."""
    lines: list[str] = []
    run = report.get("run") or {}
    lines.append(f"shadow report  generated {report['generated_at_utc']}")
    if run:
        uptime = run.get("uptime_seconds")
        lines.append(
            f"  run {run['run_id'][:12]}  status={run['status']}"
            + (f"  uptime={uptime/3600:.1f}h" if uptime else "")
        )

    live = report.get("liveness", {})
    lines.append("\n[liveness]")
    lines.append(f"  collector last seen      {_fmt_age(live.get('collector_silence_seconds'))}")
    lines.append(f"  latest book (venue)      {_fmt_age(live.get('latest_orderbook_venue_age_seconds'))}")
    lines.append(f"  latest forecast run      {_fmt_age(live.get('latest_forecast_run_age_seconds'))}")

    dec = report.get("decisions", {})
    lines.append("\n[decisions]")
    lines.append(f"  evaluated                {dec.get('evaluated', 0)}")
    by_mode = dec.get("by_mode") or {}
    if by_mode:
        lines.append(
            "  by mode                  "
            + ", ".join(f"{name}={entry.get('evaluated', 0)}" for name, entry in by_mode.items())
        )
        lines.append(
            f"  ACCEPT live / replay     {dec.get('live_accepted', 0)} / "
            f"{dec.get('replay_accepted', 0)}"
        )
    lines.append(f"  candidates (priced)      {dec.get('candidates_generated', 0)}")
    lines.append(f"  ACCEPT                   {dec.get('accepted', 0)}")
    lines.append(f"  REJECT                   {dec.get('rejected', 0)}")
    lines.append(f"  NO_TRADE                 {dec.get('no_trade', 0)}")
    lines.append(f"  DATA_UNAVAILABLE         {dec.get('data_unavailable', 0)}")
    if dec.get("replay_accepted"):
        lines.append(
            "  NOTE                     some ACCEPT rows are REPLAY: see warnings, "
            "they are not contemporary signals"
        )
    if dec.get("interrupted"):
        lines.append(f"  CANDIDATE (interrupted)  {dec['interrupted']}")
    p95 = dec.get("p95_decision_latency_ms")
    lines.append(f"  P95 decision latency     {f'{p95:.1f} ms' if p95 is not None else 'n/a'}")

    if dec.get("primary_reasons"):
        lines.append("\n  primary reasons:")
        for reason, n in dec["primary_reasons"].items():
            lines.append(f"    {reason:28} {n}")

    edge = report.get("theoretical_edge", {})
    lines.append("\n[theoretical edge - expectation under the model, NOT realised profit]")
    lines.append(f"  priced                   {edge.get('priced_decisions', 0)}")
    lines.append(f"  mean USD/share           {_fmt(edge.get('mean_usd_per_share'))}")
    lines.append(f"  positive                 {edge.get('positive_count', 0)}")
    lines.append(f"  mean of ACCEPT           {_fmt(edge.get('mean_of_accepted_usd_per_share'))}")

    pred = report.get("prediction_quality", {})
    lines.append("\n[prediction coverage - quality needs settlement, scored by research]")
    for model, stats in (pred.get("by_model") or {}).items():
        lines.append(f"  {model:20} {stats['predictions']:6} predictions  latest {stats['latest_run']}")

    liq = report.get("liquidity", {})
    lines.append("\n[liquidity]")
    lines.append(f"  snapshots                {liq.get('snapshots', 0)}")
    lines.append(f"  mean spread USD          {_fmt(liq.get('mean_spread_usd'))}")
    lines.append(f"  mean ask depth USD       {_fmt(liq.get('mean_ask_depth_usd'))}")
    lines.append(f"  insufficient liquidity   {liq.get('insufficient_liquidity_rejections', 0)}")

    cov = report.get("coverage", {})
    lines.append("\n[coverage]")
    lines.append(f"  forecast                 {_pct(cov.get('forecast_coverage'))}")
    lines.append(f"  order book               {_pct(cov.get('orderbook_coverage'))}")

    dq = report.get("data_quality", {})
    lines.append("\n[data quality]")
    lines.append(f"  data gaps                {dq.get('data_gaps', 0)}")
    lines.append(f"  errors                   {dq.get('errors', 0)}")
    for kind, stats in list((dq.get("by_kind") or {}).items())[:8]:
        lines.append(f"    {kind:28} {stats['count']}")

    if report.get("warnings"):
        lines.append("\n[warnings]")
        for warning in report["warnings"]:
            lines.append(f"  ! {warning}")

    lines.append(f"\n[equity curve] {_NO_EQUITY}")
    return "\n".join(lines)


_NO_EQUITY = (
    "none - shadow mode places no orders, so there is no realised return to show. "
    "See theoretical_edge above for what the model believed."
)


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _fmt(value) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


def dumps(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)


# --- the published snapshot ------------------------------------------------
#
# The engine holds an exclusive lease on shadow.duckdb for its whole life, and
# DuckDB refuses a second process the file even read-only. So `report` reads a
# snapshot this module writes from the connection that is already open - and
# `--offline` still opens the database directly for the case where nothing is
# running and there is no snapshot to read.

SNAPSHOT_SCHEMA_VERSION = 1


def publish_report_snapshot(
    path: str | Path,
    report: dict[str, Any],
    *,
    stale_after_seconds: float,
    source: str = "shadow-main",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute-once, publish atomically. Returns what was written.

    The report is built by the caller from the *existing* connection, so this
    function never touches DuckDB - it only lays the freshness fields over the
    top and swaps the file into place.

    ``generated_at_utc`` comes from the report itself, so the timestamp a
    reader sees is when the numbers were computed, not when the file happened
    to be written.
    """
    now = now or datetime.now(tz=UTC)
    generated = report.get("generated_at_utc")
    if not generated:
        generated = now.isoformat()
        report = {**report, "generated_at_utc": generated}
    try:
        generated_at = datetime.fromisoformat(str(generated))
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
    except ValueError:
        generated_at = now

    payload = {
        **report,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "source": source,
        "stale_after_seconds": stale_after_seconds,
        "expires_at_utc": (generated_at + timedelta(seconds=stale_after_seconds)).isoformat(),
    }
    atomic_write_json(path, payload)
    return payload


def load_report_snapshot(path: str | Path) -> dict[str, Any] | None:
    """Read a published snapshot, or ``None`` when there is nothing to read.

    ``None`` covers absent, unreadable and unparseable alike: the caller's
    question is "is there a snapshot?", and distinguishing the three would
    only change which fallback message it prints.
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "decisions" not in payload:
        return None
    return payload


def snapshot_staleness(
    payload: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, float | None, float | None]:
    """``(stale, age_seconds, seconds_past_expiry)`` for a loaded snapshot.

    All three are ``None``-tolerant: a snapshot without a parseable
    ``generated_at_utc`` is reported as stale with an unknown age rather than
    as fresh - a timestamp we cannot read is not evidence of recency.
    """
    now = now or datetime.now(tz=UTC)
    try:
        generated = datetime.fromisoformat(str(payload.get("generated_at_utc")))
    except (TypeError, ValueError):
        return True, None, None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)

    age = (now - generated).total_seconds()
    window = payload.get("stale_after_seconds")
    try:
        window_f = float(window)
    except (TypeError, ValueError):
        window_f = 300.0
    past = age - window_f
    return past > 0, age, (past if past > 0 else 0.0)

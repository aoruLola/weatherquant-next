#!/usr/bin/env python3
"""One read-only answer to "why were trades refused, and did accepts fill?".

The first supervised live run has three processes writing three different
files: the shadow decides, the paper account consumes the shadow's export and
prices fills. Reading any one of them in isolation answers half a question -
the shadow's report says "rejected", the paper ledger says "no fill", and
neither says whether those are the *same* signals or two unrelated stories.

This script joins the two databases by the shadow's deterministic
``decision_id`` and prints one funnel:

    Shadow LIVE ACCEPT -> exported -> paper signal -> paper fill -> settled

plus the reject-reason histogram and the gate margins that produced it. It is
a measurement tool. It changes nothing, it opens every input
``read_only=True``, and it refuses to touch a file the engines still hold.

Safety rules, because this runs against production snapshots:

* **Never the live file.** DuckDB takes an exclusive lease for the writer's
  whole life; a read-only open of that lease fails. The failure is caught and
  re-raised with the instruction to copy the file first, rather than a raw
  "Could not set lock". A leftover ``.wal`` beside the database is refused for
  the same reason: a clean snapshot has none.
* **Never a write.** The connections are read-only; there is no DDL, no
  checkpoint, no temp table. The tests hash the input files before and after.
* **Schema-tolerant.** Older snapshots lack a table or a column (``export_seq``,
  ``fee_rate``, ``paper_settlements``...). A missing input skips that metric
  and says ``n/a (column missing)`` instead of crashing the whole report. The
  point of the tool is to run on whatever the first live run produced.

Thresholds are read from ``configs/shadow-thresholds.json`` - the same file
the engine was configured with - and are only ever compared against, never
changed. "Would pass at X" figures are explicitly informational.

Usage::

    python scripts/live_validation_report.py \\
        --shadow-db /tmp/shadow.duckdb --paper-db /tmp/paper.duckdb \\
        --since 2026-11-20T00:00:00Z --json out.json --md out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb

from weatherquant.shadow.config import Thresholds, load_thresholds

__all__ = ["ReportError", "build_report", "format_markdown", "main", "render_json"]

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_THRESHOLDS_PATH = REPO_ROOT / "configs" / "shadow-thresholds.json"

#: How a skipped metric is spelled. Deliberately one kind of string, so a
#: reader (or a test) can tell "the answer is zero" from "the data to answer
#: is not here".
NA_COLUMN = "n/a (column missing)"
NA_TABLE = "n/a (table missing)"
NA_PAPER = "n/a (no --paper-db)"
NA_THRESHOLDS = "n/a (thresholds missing)"

#: Candidate thresholds shown as "would pass at X" - informational only. Each
#: list spans stricter and looser than the shipped gate, so an operator can see
#: whether a refusal is a knife-edge or a mile.
CANDIDATE_AGES_MS = (30_000, 60_000, 120_000, 180_000)
CANDIDATE_DEPTHS_USD = (10.0, 25.0, 50.0, 100.0)
CANDIDATE_EDGES = (0.0, 0.01, 0.015, 0.02)
CANDIDATE_LATENCIES_MS = (1_000, 2_500, 5_000, 15_000)

#: Paper outcomes split by which side of the engine wrote them. A consumption
#: refusal means "this row was never a trade"; a fill refusal means "the trade
#: was proposed and evidence/pricing said no". They answer different questions,
#: so the funnel reports them as different steps.
CONSUMPTION_REFUSALS = frozenset(
    {
        "NOT_LIVE",
        "NOT_ACCEPT",
        "STALE_SIGNAL",
        "MARKET_NOT_OPEN",
        "DUPLICATE_SIGNAL",
        "ENGINE_DISABLED",
    }
)

#: Explicitly "the contract accepted this" outcomes in ``paper_signals``.
ACCEPTED_OUTCOMES = ("ACCEPTED", "FILLED", "PENDING")


class ReportError(RuntimeError):
    """An input cannot be read safely, or the arguments do not make sense."""


# --- opening ---------------------------------------------------------------


def _open_read_only(path: Path, label: str) -> duckdb.DuckDBPyConnection:
    """Open ``path`` read-only, refusing anything the engines may still hold.

    DuckDB's lock error is cryptic ("Could not set lock on file ... Conflicting
    lock is held") and, inside one process, "different configuration than
    existing connections". Both mean the same operator action: copy the file
    off the NAS with a snapshot/backup mechanism, do not point this at the live
    volume. A sibling ``.wal`` means the file was not shut down cleanly (or is
    still open), so it is refused before DuckDB gets a chance to replay it.
    """
    if not path.exists():
        raise ReportError(f"{label} database not found: {path}")
    wal = path.with_name(path.name + ".wal")
    if wal.exists():
        raise ReportError(
            f"{label} database at {path} has a pending write-ahead log ({wal.name}); "
            "it is live or was not shut down cleanly. Take a backup/snapshot copy "
            "instead of opening this file"
        )
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        message = str(exc)
        lowered = message.lower()
        if any(token in lowered for token in ("lock", "different configuration", "read-only", "read only")):
            raise ReportError(
                f"{label} database at {path} is locked by a running engine ({message}). "
                f"Copy or snapshot it off the volume, then point --{label}-db at the copy"
            ) from exc
        raise ReportError(f"cannot open {label} database at {path}: {message}") from exc
    con.execute("SET TimeZone='UTC'")
    return con


class Schema:
    """Table/column presence, so every query can be guarded before it runs."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        rows = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'main'"
        ).fetchall()
        self.columns: dict[str, set[str]] = {}
        for table, column in rows:
            self.columns.setdefault(str(table), set()).add(str(column))

    def has_table(self, table: str) -> bool:
        return table in self.columns

    def has_columns(self, table: str, *columns: str) -> bool:
        held = self.columns.get(table)
        return held is not None and all(column in held for column in columns)


class Filter:
    """Ordered WHERE conditions and their parameters, kept in step.

    Built once per table and extended with additional conditions through
    :meth:`sql` / :meth:`bind`; keeping the two together is what prevents the
    classic parameter-order bug where a ``state`` predicate silently binds the
    ``since`` timestamp.
    """

    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, value: Any) -> Filter:
        self.clauses.append(clause)
        self.params.append(value)
        return self

    def sql(self, *extra: str) -> str:
        parts = [*self.clauses, *extra]
        return f" WHERE {' AND '.join(parts)}" if parts else ""

    def bind(self, *extra: Any) -> list[Any]:
        return [*self.params, *extra]


def _table_filter(
    schema: Schema,
    table: str,
    *,
    mode: str | None,
    since: datetime | None,
    until: datetime | None,
    prefix: str = "",
) -> Filter:
    """Mode + decision-time window, applied only where those columns exist."""
    built = Filter()
    if mode is not None and schema.has_columns(table, "mode"):
        built.add(f"{prefix}mode = ?", mode)
    if schema.has_columns(table, "decision_time_utc"):
        if since is not None:
            built.add(f"{prefix}decision_time_utc >= ?", since)
        if until is not None:
            built.add(f"{prefix}decision_time_utc <= ?", until)
    return built


def _load_thresholds(path: Path) -> tuple[Thresholds | None, str]:
    try:
        return load_thresholds(path), str(path)
    except (FileNotFoundError, ValueError) as exc:
        return None, f"{path} ({exc})"


def _scalar(con, sql: str, params: list[Any] | None = None) -> Any:
    row = con.execute(sql, params or []).fetchone()
    return None if row is None else row[0]


def _rows(con, sql: str, params: list[Any] | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def _pct(part: Any, whole: Any) -> float | str:
    if not isinstance(part, int) or not isinstance(whole, int) or whole <= 0:
        return "n/a"
    return round(100.0 * part / whole, 2)


def _number_key(value: float) -> str:
    """Stable JSON key for a candidate threshold: 100.0 -> "100", 0.01 stays."""
    return str(int(value)) if float(value).is_integer() else str(value)


# --- sections --------------------------------------------------------------


def _decisions(con, schema: Schema, flt: Filter) -> dict[str, Any]:
    if not schema.has_table("shadow_decisions"):
        return {"total": NA_TABLE, "accept": NA_TABLE, "reject": NA_TABLE, "not_executable": NA_TABLE}
    if not schema.has_columns("shadow_decisions", "state", "decision_time_utc"):
        return {"total": NA_COLUMN, "accept": NA_COLUMN, "reject": NA_COLUMN, "not_executable": NA_COLUMN}

    by_state = {
        str(row[0]): int(row[1])
        for row in _rows(
            con, f"SELECT state, COUNT(*) FROM shadow_decisions{flt.sql()} GROUP BY 1 ORDER BY 1", flt.bind()
        )
    }

    def group(expression: str) -> list[dict[str, Any]]:
        rows = _rows(
            con,
            "SELECT "
            f"{expression} AS bucket, COUNT(*), "
            "SUM(CASE WHEN state = 'ACCEPT' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN state = 'REJECT' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN state = 'NOT_EXECUTABLE' THEN 1 ELSE 0 END) "
            f"FROM shadow_decisions{flt.sql()} GROUP BY 1 ORDER BY 1",
            flt.bind(),
        )
        return [
            {
                "bucket": str(row[0]),
                "total": int(row[1]),
                "accept": int(row[2] or 0),
                "reject": int(row[3] or 0),
                "not_executable": int(row[4] or 0),
            }
            for row in rows
        ]

    return {
        "total": sum(by_state.values()),
        "accept": by_state.get("ACCEPT", 0),
        "reject": by_state.get("REJECT", 0),
        "not_executable": by_state.get("NOT_EXECUTABLE", 0),
        "by_state": by_state,
        "by_hour": group("date_trunc('hour', decision_time_utc)"),
        "by_target_date": group("CAST(target_date AS VARCHAR)")
        if schema.has_columns("shadow_decisions", "target_date")
        else NA_COLUMN,
    }


def _reject_reasons(con, schema: Schema, flt: Filter) -> dict[str, Any]:
    if not schema.has_table("shadow_decisions") or not schema.has_table("rejection_reasons"):
        return {"primary": NA_TABLE, "all": NA_TABLE, "top_per_market_target": NA_TABLE}
    if not schema.has_columns("rejection_reasons", "decision_id", "reason", "is_primary"):
        return {"primary": NA_COLUMN, "all": NA_COLUMN, "top_per_market_target": NA_COLUMN}

    join = f" FROM rejection_reasons rr JOIN shadow_decisions d ON d.decision_id = rr.decision_id{flt.sql()}"

    def histogram(primary_only: bool) -> dict[str, int]:
        extra = " AND rr.is_primary" if primary_only else ""
        rows = _rows(con, f"SELECT rr.reason, COUNT(*){join}{extra} GROUP BY 1 ORDER BY 2 DESC, 1", flt.bind())
        return {str(row[0]): int(row[1]) for row in rows}

    primary_counts = histogram(True)
    all_counts = histogram(False)
    primary_total = sum(primary_counts.values())
    primary = {
        reason: {"count": count, "pct": round(100.0 * count / primary_total, 2)}
        for reason, count in primary_counts.items()
    }

    top_per_group: list[dict[str, Any]] = []
    if schema.has_columns("shadow_decisions", "market_id", "target_date"):
        rows = _rows(
            con,
            "SELECT d.market_id, CAST(d.target_date AS VARCHAR), rr.reason, COUNT(*) AS n"
            f"{join} AND rr.is_primary GROUP BY 1, 2, 3 ORDER BY 1, 2, 4 DESC, 3",
            flt.bind(),
        )
        seen: dict[tuple[str, str], int] = {}
        for market, target, reason, count in rows:
            key = (str(market), str(target))
            if seen.get(key, 0) >= 3:
                continue
            seen[key] = seen.get(key, 0) + 1
            top_per_group.append(
                {"market_id": str(market), "target_date": str(target), "reason": str(reason), "count": int(count)}
            )
            if len(top_per_group) >= 50:
                break

    return {
        "rejections_with_primary": primary_total,
        "primary": primary,
        "all": all_counts,
        "top_per_market_target": top_per_group,
    }


def _percentiles(con, table: str, column: str, flt: Filter, *, join: str = "") -> dict[str, Any]:
    rows = _rows(
        con,
        f"SELECT quantile_cont({column}, 0.5), quantile_cont({column}, 0.9), "
        f"quantile_cont({column}, 0.99), COUNT(*) FROM {table}{join}{flt.sql(f'{column} IS NOT NULL')}",
        flt.bind(),
    )
    if not rows or int(rows[0][3]) == 0:
        return {"n": 0, "p50": None, "p90": None, "p99": None}
    p50, p90, p99, count = rows[0]
    return {
        "n": int(count),
        "p50": None if p50 is None else round(float(p50), 4),
        "p90": None if p90 is None else round(float(p90), 4),
        "p99": None if p99 is None else round(float(p99), 4),
    }


def _gate(
    con,
    *,
    table: str,
    column: str,
    flt: Filter,
    threshold: float | None,
    threshold_name: str,
    lower_is_better: bool,
    candidates: tuple[float, ...],
    join: str = "",
    reject_qualifier: str = "",
) -> dict[str, Any]:
    """Distribution + near-miss + informational counts for one gate."""
    distribution = _percentiles(con, table, column, flt, join=join)
    result: dict[str, Any] = {
        "threshold": threshold,
        "threshold_name": threshold_name,
        "direction": "lower_is_better" if lower_is_better else "higher_is_better",
        **distribution,
        "rejects_within_10pct": None,
        "rejects_within_25pct": None,
        "would_pass_at": None,
    }
    if threshold is None:
        result["note"] = NA_THRESHOLDS
        return result

    reject = f"{reject_qualifier}state = 'REJECT'"
    if lower_is_better:
        near_10 = (threshold, threshold * 1.10)
        near_25 = (threshold, threshold * 1.25)
        pass_clause = f"{column} <= ?"
    else:
        near_10 = (threshold * 0.90, threshold)
        near_25 = (threshold * 0.75, threshold)
        pass_clause = f"{column} >= ?"

    for key, (low, high) in (("rejects_within_10pct", near_10), ("rejects_within_25pct", near_25)):
        result[key] = int(
            _scalar(
                con,
                f"SELECT COUNT(*) FROM {table}{join}{flt.sql(f'{column} BETWEEN ? AND ?', reject)}",
                flt.bind(low, high),
            )
            or 0
        )

    result["would_pass_at"] = {}
    for candidate in candidates:
        result["would_pass_at"][_number_key(candidate)] = int(
            _scalar(
                con,
                f"SELECT COUNT(*) FROM {table}{join}{flt.sql(f'{column} IS NOT NULL AND {pass_clause}', reject)}",
                flt.bind(candidate),
            )
            or 0
        )
    return result


def _gates(con, schema: Schema, where: Filter, thresholds: Thresholds | None, join_where: Filter) -> dict[str, Any]:
    if not schema.has_table("shadow_decisions"):
        return {"note": NA_TABLE}
    gates: dict[str, Any] = {
        "thresholds_version": None if thresholds is None else thresholds.version,
        "thresholds_source": None if thresholds is None else str(DEFAULT_THRESHOLDS_PATH),
    }

    def simple(
        name: str,
        column: str,
        threshold: float | None,
        threshold_name: str,
        lower: bool,
        candidates: tuple[float, ...],
    ) -> None:
        if not schema.has_columns("shadow_decisions", column):
            gates[name] = NA_COLUMN
            return
        gates[name] = _gate(
            con,
            table="shadow_decisions",
            column=column,
            flt=where,
            threshold=threshold,
            threshold_name=threshold_name,
            lower_is_better=lower,
            candidates=candidates,
        )

    simple(
        "book_age",
        "snapshot_age_ms",
        None if thresholds is None else thresholds.max_snapshot_age_ms,
        "max_snapshot_age_ms",
        True,
        CANDIDATE_AGES_MS,
    )
    simple(
        "depth",
        "available_depth_usd",
        None if thresholds is None else thresholds.min_depth_usd,
        "min_depth_usd",
        False,
        CANDIDATE_DEPTHS_USD,
    )
    simple(
        "net_edge",
        "net_edge",
        None if thresholds is None else thresholds.min_net_edge_usd_per_share,
        "min_net_edge_usd_per_share",
        False,
        CANDIDATE_EDGES,
    )

    # Clock skew lives on the snapshot, not the decision: latency_ms is
    # shadow-received minus the venue's own timestamp. v3 only punishes the
    # negative direction, so the measured skew is -latency_ms and the gate is
    # max_clock_skew_ms; positive latency is staleness, handled by book age.
    if (
        schema.has_table("market_snapshots")
        and schema.has_columns("market_snapshots", "latency_ms", "snapshot_id")
        and schema.has_columns("shadow_decisions", "snapshot_id")
    ):
        gates["clock_skew"] = _gate(
            con,
            table="market_snapshots s",
            column="-s.latency_ms",
            flt=join_where,
            threshold=None if thresholds is None else thresholds.max_clock_skew_ms,
            threshold_name="max_clock_skew_ms",
            lower_is_better=True,
            candidates=CANDIDATE_LATENCIES_MS,
            join=" JOIN shadow_decisions d ON d.snapshot_id = s.snapshot_id",
            reject_qualifier="d.",
        )
    else:
        gates["clock_skew"] = NA_COLUMN if schema.has_table("market_snapshots") else NA_TABLE

    # The fee gate refuses an *unknown* schedule, so there is no magnitude to
    # compare - the observable is presence.
    if schema.has_columns("shadow_decisions", "fee_rate"):
        gates["fee"] = {
            "threshold": None,
            "threshold_name": "fee schedule known",
            **_percentiles(con, "shadow_decisions", "fee_rate", where),
            "decisions": int(_scalar(con, f"SELECT COUNT(*) FROM shadow_decisions{where.sql()}", where.bind()) or 0),
            "fee_known": int(
                _scalar(
                    con,
                    f"SELECT COUNT(*) FROM shadow_decisions{where.sql('fee_rate IS NOT NULL')}",
                    where.bind(),
                )
                or 0
            ),
            "note": "the gate refuses an unknown schedule; presence, not magnitude",
        }
        gates["fee"]["fee_missing"] = gates["fee"]["decisions"] - gates["fee"]["fee_known"]
    else:
        gates["fee"] = NA_COLUMN

    # Size: v3 sizes a fill down instead of refusing a partial one, so the only
    # size gate left is the venue minimum. A REJECT whose estimated fill is
    # below it could never have been placed.
    if schema.has_columns("shadow_decisions", "estimated_fill_shares"):
        size: dict[str, Any] = {
            "threshold": None if thresholds is None else thresholds.min_order_size_shares,
            "threshold_name": "min_order_size_shares",
            "below_min_order_rejects": None,
        }
        if thresholds is not None:
            size["below_min_order_rejects"] = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM shadow_decisions"
                    + where.sql(
                        "state = 'REJECT'",
                        "estimated_fill_shares IS NOT NULL",
                        "estimated_fill_shares < ?",
                    ),
                    where.bind(thresholds.min_order_size_shares),
                )
                or 0
            )
        else:
            size["note"] = NA_THRESHOLDS
        gates["size"] = size
    else:
        gates["size"] = NA_COLUMN

    gates["note"] = "would_pass_at figures are informational; no threshold is changed by this report"
    return gates


def _shadow_accepts(con, schema: Schema, flt: Filter) -> tuple[set[str], set[str] | None]:
    """(ACCEPT ids in scope, those the export has picked up or None if unknown)."""
    if not schema.has_table("shadow_decisions") or not schema.has_columns("shadow_decisions", "decision_id", "state"):
        return set(), None
    accept_sql = flt.sql("state = 'ACCEPT'")
    if not schema.has_columns("shadow_decisions", "export_seq"):
        rows = _rows(con, f"SELECT decision_id FROM shadow_decisions{accept_sql}", flt.bind())
        return {str(row[0]) for row in rows}, None
    rows = _rows(con, f"SELECT decision_id, export_seq FROM shadow_decisions{accept_sql}", flt.bind())
    return {str(row[0]) for row in rows}, {str(row[0]) for row in rows if row[1] is not None}


def _paper_stage(con, schema: Schema, since, until) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "signals": 0,
        "accepted": 0,
        "refused": 0,
        "refusal_reasons": {},
        "no_fill_reasons": {},
        "signal_ids": set(),
        "accepted_ids": set(),
        "refused_ids": set(),
        "filled_ids": set(),
        "settled_ids": set(),
    }
    if not schema.has_table("paper_signals"):
        stage["note"] = NA_TABLE
        return stage
    if not schema.has_columns("paper_signals", "decision_id", "outcome"):
        stage["note"] = NA_COLUMN
        return stage

    signal_filter = _table_filter(schema, "paper_signals", mode=None, since=since, until=until)
    reason_column = schema.has_columns("paper_signals", "refusal_reason")
    select = "decision_id, outcome" + (", refusal_reason" if reason_column else "")
    rows = _rows(con, f"SELECT {select} FROM paper_signals{signal_filter.sql()}", signal_filter.bind())
    for row in rows:
        decision_id = str(row[0])
        outcome = str(row[1])
        stage["signal_ids"].add(decision_id)
        if outcome in ACCEPTED_OUTCOMES:
            stage["accepted"] += 1
            stage["accepted_ids"].add(decision_id)
        elif outcome == "REFUSED":
            stage["refused"] += 1
            stage["refused_ids"].add(decision_id)
            reason = str(row[2] or "UNKNOWN") if reason_column else "UNKNOWN"
            stage["refusal_reasons"][reason] = stage["refusal_reasons"].get(reason, 0) + 1
            if reason not in CONSUMPTION_REFUSALS:
                stage["no_fill_reasons"][reason] = stage["no_fill_reasons"].get(reason, 0) + 1

    if not schema.has_table("paper_fills") or not schema.has_columns("paper_fills", "decision_id", "market_id"):
        stage["filled"] = NA_TABLE if not schema.has_table("paper_fills") else NA_COLUMN
        stage["settled"] = NA_TABLE
        return stage

    fill_filter = _table_filter(schema, "paper_fills", mode=None, since=since, until=until)
    fills = _rows(
        con,
        f"SELECT decision_id, market_id FROM paper_fills{fill_filter.sql()}",
        fill_filter.bind(),
    )
    stage["filled_ids"] = {str(row[0]) for row in fills}
    stage["filled"] = len(fills)

    if not schema.has_table("paper_settlements") or not schema.has_columns("paper_settlements", "market_id"):
        stage["settled"] = NA_TABLE if not schema.has_table("paper_settlements") else NA_COLUMN
        return stage
    if schema.has_columns("paper_settlements", "applied"):
        settled = _rows(con, "SELECT DISTINCT market_id FROM paper_settlements WHERE applied")
    elif schema.has_columns("paper_settlements", "status"):
        settled = _rows(con, "SELECT DISTINCT market_id FROM paper_settlements WHERE status = 'RESOLVED'")
    else:
        stage["settled"] = NA_COLUMN
        return stage
    settled_markets = {str(row[0]) for row in settled}
    settled_fills = [row for row in fills if str(row[1]) in settled_markets]
    stage["settled"] = len(settled_fills)
    stage["settled_ids"] = {str(row[0]) for row in settled_fills}
    return stage


def _funnel(shadow_accepts: set[str], exported: set[str] | None, paper: dict[str, Any] | None) -> dict[str, Any]:
    n_accept = len(shadow_accepts)
    n_exported = None if exported is None else len(shadow_accepts & exported)
    funnel: dict[str, Any] = {
        "shadow_live_accept": n_accept,
        "exported": NA_COLUMN if n_exported is None else n_exported,
        "paper_signals": None,
        "paper_accepted": None,
        "paper_refused": None,
        "paper_filled": None,
        "settled": None,
        "conversion_pct": {},
        "paper_refusal_reasons": None,
        "no_fill_reasons": None,
        "top_no_fill_reasons": None,
    }

    if paper is None or "note" in paper:
        for key in ("paper_signals", "paper_accepted", "paper_refused", "paper_filled", "settled"):
            funnel[key] = NA_PAPER
        funnel["note"] = NA_PAPER if paper is None else paper["note"]
        funnel["conversion_pct"] = {
            "accept_to_export": _pct(n_exported, n_accept) if n_exported is not None else NA_COLUMN,
            "export_to_signal": NA_PAPER,
            "signal_to_fill": NA_PAPER,
            "fill_to_settled": NA_PAPER,
        }
        return funnel

    signal_ids = shadow_accepts & paper["signal_ids"]
    filled_ids = shadow_accepts & paper["filled_ids"]
    funnel["paper_signals"] = len(signal_ids)
    funnel["paper_accepted"] = len(signal_ids & paper["accepted_ids"])
    funnel["paper_refused"] = len(signal_ids & paper["refused_ids"])
    funnel["paper_filled"] = len(filled_ids)
    funnel["settled"] = len(filled_ids & paper["settled_ids"])
    funnel["paper_refusal_reasons"] = paper["refusal_reasons"]
    funnel["no_fill_reasons"] = paper["no_fill_reasons"]
    funnel["top_no_fill_reasons"] = sorted(paper["no_fill_reasons"].items(), key=lambda item: (-item[1], item[0]))[:5]

    entry = n_exported if n_exported is not None else n_accept
    funnel["conversion_pct"] = {
        "accept_to_export": _pct(n_exported, n_accept) if n_exported is not None else NA_COLUMN,
        "export_to_signal": _pct(funnel["paper_signals"], entry),
        "signal_to_fill": _pct(funnel["paper_filled"], funnel["paper_accepted"]),
        "fill_to_settled": _pct(funnel["settled"], funnel["paper_filled"]),
    }
    return funnel


def _freshness(con, schema: Schema, *, mode: str, since, until) -> dict[str, Any]:
    """Recency ignores --since/--until: it answers "how current is this file?"."""
    latest_decision: Any = NA_TABLE
    latest_accept: Any = NA_TABLE
    if schema.has_table("shadow_decisions") and schema.has_columns("shadow_decisions", "decision_time_utc"):
        mode_filter = _table_filter(schema, "shadow_decisions", mode=mode, since=None, until=None)
        latest_decision = _timestamp(
            _scalar(
                con,
                f"SELECT MAX(decision_time_utc) FROM shadow_decisions{mode_filter.sql()}",
                mode_filter.bind(),
            )
        )
        accept_sql = mode_filter.sql("state = 'ACCEPT'")
        latest_accept = _timestamp(
            _scalar(
                con,
                f"SELECT MAX(decision_time_utc) FROM shadow_decisions{accept_sql}",
                mode_filter.bind(),
            )
        )

    quality: Any = NA_TABLE
    if schema.has_table("data_quality_events") and schema.has_columns("data_quality_events", "kind", "ts_utc"):
        window = Filter()
        if since is not None:
            window.add("ts_utc >= ?", since)
        if until is not None:
            window.add("ts_utc <= ?", until)
        quality = {
            str(row[0]): int(row[1])
            for row in _rows(
                con,
                f"SELECT kind, COUNT(*) FROM data_quality_events{window.sql()} GROUP BY 1 ORDER BY 2 DESC",
                window.bind(),
            )
        }

    return {
        "latest_decision_time_utc": latest_decision,
        "latest_accept_time_utc": latest_accept,
        "data_quality_events": quality,
        "note": "recency is measured over the whole file; --since/--until apply to the counts",
    }


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


# --- assembly --------------------------------------------------------------


def build_report(
    *,
    shadow_db: str | Path,
    paper_db: str | Path | None = None,
    mode: str = "LIVE",
    since: datetime | None = None,
    until: datetime | None = None,
    thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
) -> dict[str, Any]:
    """Read both snapshots (never writing) and return the report as a dict."""
    if mode not in {"LIVE", "REPLAY"}:
        raise ReportError(f"mode must be LIVE or REPLAY, got {mode!r}")
    if since is not None and until is not None and since > until:
        raise ReportError("--since is after --until")

    shadow_path = Path(shadow_db)
    thresholds, thresholds_source = _load_thresholds(Path(thresholds_path))
    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "inputs": {
            "shadow_db": str(shadow_path),
            "paper_db": None if paper_db is None else str(paper_db),
            "mode": mode,
            "since": _timestamp(since),
            "until": _timestamp(until),
            "thresholds": thresholds_source,
        },
    }

    with closing(_open_read_only(shadow_path, "shadow")) as con:
        schema = Schema(con)
        flt = _table_filter(schema, "shadow_decisions", mode=mode, since=since, until=until)
        join_flt = _table_filter(schema, "shadow_decisions", mode=mode, since=since, until=until, prefix="d.")
        report["decisions"] = _decisions(con, schema, flt)
        report["reject_reasons"] = _reject_reasons(con, schema, flt)
        report["gates"] = _gates(con, schema, flt, thresholds, join_flt)
        report["freshness"] = _freshness(con, schema, mode=mode, since=since, until=until)
        accepts, exported = _shadow_accepts(con, schema, flt)

    paper: dict[str, Any] | None = None
    if paper_db is not None:
        with closing(_open_read_only(Path(paper_db), "paper")) as con:
            schema = Schema(con)
            paper = _paper_stage(con, schema, since, until)
            fill_time = (
                _timestamp(_scalar(con, "SELECT MAX(filled_at_utc) FROM paper_fills"))
                if schema.has_columns("paper_fills", "filled_at_utc")
                else NA_TABLE
            )
            report["freshness"]["latest_fill_time_utc"] = fill_time
            if schema.has_table("paper_quality_events") and schema.has_columns(
                "paper_quality_events", "kind", "ts_utc"
            ):
                window = Filter()
                if since is not None:
                    window.add("ts_utc >= ?", since)
                if until is not None:
                    window.add("ts_utc <= ?", until)
                report["freshness"]["paper_quality_events"] = {
                    str(row[0]): int(row[1])
                    for row in _rows(
                        con,
                        f"SELECT kind, COUNT(*) FROM paper_quality_events{window.sql()} GROUP BY 1 ORDER BY 2 DESC",
                        window.bind(),
                    )
                }
            else:
                report["freshness"]["paper_quality_events"] = NA_TABLE
    else:
        report["freshness"]["latest_fill_time_utc"] = NA_PAPER
        report["freshness"]["paper_quality_events"] = NA_PAPER

    report["funnel"] = _funnel(accepts, exported, paper)
    report["skipped"] = _skipped(report)
    return report


def _skipped(report: dict[str, Any]) -> list[str]:
    skipped: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, str) and value.startswith("n/a"):
            skipped.append(f"{path}: {value}")
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    for section in ("decisions", "reject_reasons", "gates", "funnel", "freshness"):
        walk(report.get(section), section)
    return skipped


# --- rendering -------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return lines


def format_markdown(report: dict[str, Any]) -> str:
    inputs = report["inputs"]
    lines: list[str] = [
        "# Live validation report",
        "",
        f"Generated {report['generated_at_utc']} from `{inputs['shadow_db']}`"
        + (f" and `{inputs['paper_db']}`" if inputs["paper_db"] else " (no paper database)"),
        "",
        f"Mode `{inputs['mode']}`"
        + (f", since `{inputs['since']}`" if inputs["since"] else "")
        + (f", until `{inputs['until']}`" if inputs["until"] else "")
        + ".",
        "Read-only measurement: no threshold is changed by this report.",
        "",
        "## 1. Decisions",
        "",
    ]

    decisions = report["decisions"]
    lines += _md_table(
        ["metric", "count"],
        [
            ["total", decisions["total"]],
            ["ACCEPT", decisions["accept"]],
            ["REJECT", decisions["reject"]],
            ["NOT_EXECUTABLE", decisions["not_executable"]],
        ],
    )
    if isinstance(decisions.get("by_state"), dict) and decisions["by_state"]:
        lines += ["", "Every state present in this snapshot:", ""]
        lines += _md_table(["state", "count"], [[state, count] for state, count in decisions["by_state"].items()])

    def grouped(title: str, key: str, label: str) -> None:
        lines.extend(["", title, ""])
        value = decisions[key]
        if isinstance(value, list):
            lines.extend(
                _md_table(
                    [label, "total", "accept", "reject", "not_executable"],
                    [
                        [row["bucket"], row["total"], row["accept"], row["reject"], row["not_executable"]]
                        for row in value
                    ],
                )
            )
        else:
            lines.append(_cell(value))

    grouped("### By hour", "by_hour", "hour (UTC)")
    grouped("### By target date", "by_target_date", "target date")

    lines += ["", "## 2. Reject reasons", ""]
    reasons = report["reject_reasons"]
    if isinstance(reasons["primary"], dict):
        lines.append(f"Primary reasons on {_cell(reasons['rejections_with_primary'])} rejected decision(s):")
        lines += [""]
        lines += _md_table(
            ["primary reason", "count", "% of rejections"],
            [[reason, entry["count"], entry["pct"]] for reason, entry in reasons["primary"].items()],
        )
        lines += ["", "All recorded reasons (a decision may carry several):", ""]
        lines += _md_table(["reason", "count"], [[reason, count] for reason, count in reasons["all"].items()])
        lines += ["", "Top primary reasons per market / target date:", ""]
        lines += _md_table(
            ["market", "target date", "reason", "count"],
            [
                [row["market_id"], row["target_date"], row["reason"], row["count"]]
                for row in reasons["top_per_market_target"]
            ],
        )
    else:
        lines.append(_cell(reasons["primary"]))

    lines += ["", "## 3. Gate margins", ""]
    gates = report["gates"]
    if gates.get("note") == NA_TABLE:
        lines.append(NA_TABLE)
    else:
        rows = []
        for name in ("book_age", "depth", "net_edge", "clock_skew", "fee", "size"):
            gate = gates.get(name)
            if isinstance(gate, str):
                rows.append([name, gate, "-", "-", "-", "-", "-"])
                continue
            if name == "fee":
                rows.append(
                    [
                        name,
                        gate["threshold_name"],
                        gate.get("p50"),
                        gate.get("p90"),
                        gate.get("p99"),
                        f"known {gate['fee_known']} / {gate['decisions']}",
                        "-",
                    ]
                )
                continue
            if name == "size":
                rows.append(
                    [
                        name,
                        gate["threshold_name"],
                        gate["threshold"],
                        "-",
                        "-",
                        "-",
                        f"below minimum {gate['below_min_order_rejects']}",
                    ]
                )
                continue
            rows.append(
                [
                    f"{name} — {gate['threshold_name']}",
                    gate["threshold"],
                    gate.get("p50"),
                    gate.get("p90"),
                    gate.get("p99"),
                    f"10%: {gate.get('rejects_within_10pct')}, 25%: {gate.get('rejects_within_25pct')}",
                    ", ".join(f"{key}->{value}" for key, value in (gate.get("would_pass_at") or {}).items()) or "-",
                ]
            )
        lines += _md_table(
            ["gate", "threshold", "p50", "p90", "p99", "rejects near threshold", "would pass at (rejects)"],
            rows,
        )
        lines += ["", f"Threshold set: `{gates.get('thresholds_version')}`. Figures are informational only.", ""]

    lines += ["", "## 4. Funnel", ""]
    funnel = report["funnel"]
    conversion = funnel["conversion_pct"]
    lines += _md_table(
        ["stage", "count", "conversion %"],
        [
            ["Shadow LIVE ACCEPT", funnel["shadow_live_accept"], "-"],
            ["exported", funnel["exported"], conversion.get("accept_to_export")],
            ["paper signals", funnel["paper_signals"], conversion.get("export_to_signal")],
            ["paper accepted", funnel["paper_accepted"], "-"],
            ["paper refused", funnel["paper_refused"], "-"],
            ["paper filled", funnel["paper_filled"], conversion.get("signal_to_fill")],
            ["settled", funnel["settled"], conversion.get("fill_to_settled")],
        ],
    )
    if funnel.get("note"):
        lines += ["", funnel["note"]]
    if isinstance(funnel["no_fill_reasons"], dict) and funnel["no_fill_reasons"]:
        lines += ["", "Top no-fill reasons:", ""]
        lines += _md_table(
            ["reason", "count"],
            [[reason, count] for reason, count in (funnel["top_no_fill_reasons"] or [])],
        )
    if isinstance(funnel["paper_refusal_reasons"], dict) and funnel["paper_refusal_reasons"]:
        lines += ["", "All paper refusal reasons:", ""]
        lines += _md_table(
            ["reason", "count"],
            [[reason, count] for reason, count in funnel["paper_refusal_reasons"].items()],
        )

    lines += ["", "## 5. Freshness and gaps", ""]
    freshness = report["freshness"]
    lines += _md_table(
        ["metric", "value"],
        [
            ["latest decision (UTC)", freshness.get("latest_decision_time_utc")],
            ["latest ACCEPT (UTC)", freshness.get("latest_accept_time_utc")],
            ["latest fill (UTC)", freshness.get("latest_fill_time_utc")],
        ],
    )
    for label, key in (
        ("shadow data-quality events", "data_quality_events"),
        ("paper quality events", "paper_quality_events"),
    ):
        value = freshness.get(key)
        lines += ["", f"{label}:", ""]
        if isinstance(value, dict):
            lines += (
                _md_table(["type", "count"], [[kind, count] for kind, count in value.items()])
                if value
                else ["none recorded"]
            )
        else:
            lines.append(_cell(value))

    if report["skipped"]:
        lines += ["", "## Skipped metrics", ""]
        lines += [f"- {entry}" for entry in report["skipped"]]

    return "\n".join(lines) + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, default=str)


# --- CLI -------------------------------------------------------------------


def _parse_instant(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReportError(f"{name} must be an ISO-8601 instant, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only live validation report (shadow + paper).")
    parser.add_argument("--shadow-db", required=True, help="a COPY/backup of shadow.duckdb")
    parser.add_argument("--paper-db", help="a COPY/backup of paper.duckdb")
    parser.add_argument("--since", help="UTC ISO lower bound on decision_time_utc")
    parser.add_argument("--until", help="UTC ISO upper bound on decision_time_utc")
    parser.add_argument("--mode", default="LIVE", choices=["LIVE", "REPLAY"])
    parser.add_argument("--json", dest="json_path", help="write the report as JSON here")
    parser.add_argument("--md", dest="md_path", help="write the report as Markdown here")
    args = parser.parse_args(argv)

    try:
        report = build_report(
            shadow_db=args.shadow_db,
            paper_db=args.paper_db,
            mode=args.mode,
            since=_parse_instant(args.since, "--since") if args.since else None,
            until=_parse_instant(args.until, "--until") if args.until else None,
        )
    except ReportError as exc:
        print(f"live validation report: {exc}", file=sys.stderr)
        return 2

    markdown = format_markdown(report)
    if args.json_path:
        Path(args.json_path).write_text(render_json(report) + "\n", encoding="utf-8")
    if args.md_path:
        Path(args.md_path).write_text(markdown, encoding="utf-8")
    if not args.json_path and not args.md_path:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

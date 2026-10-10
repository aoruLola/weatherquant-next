"""Read-only access to the legacy database and an import *planner*.

Scope for P0: this module **reads and classifies**. It opens the legacy file
read-only, walks the tables that carry research value, applies the rules from
:mod:`weatherquant.legacy.rules`, and reports what an import would do.

It deliberately does **not** transform or write rows yet. Row-level mapping
means reconciling differing column names and JSON encodings (the legacy order
book stores levels as ``{"price", "size"}`` where the new schema uses
``{"price_usd_per_share", "size_shares"}``), which is a P1 task - see
``docs/roadmap.md``. Building it here as an untested stub would be worse than
not having it: an import that half-works is how provenance gets lost.

Nothing in this module ever writes to the legacy file. The only write API
present is :func:`record_import_run`, which writes a manifest row into the
**new** database.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from weatherquant.legacy.rules import LEGACY_TABLES, ValueTier, VerificationStatus, classify
from weatherquant.storage import connect_legacy_read_only

__all__ = ["ImportPlan", "LegacySource", "TablePlan", "plan_import", "record_import_run"]


class LegacySource:
    """Read-only handle on a previous project's DuckDB file.

    Guarantees:
      * the underlying connection is opened with ``read_only=True``;
      * no method on this class issues DDL or DML;
      * the handle is a context manager so a caller cannot leak an open file
        lock on the legacy database.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._con = connect_legacy_read_only(self.path)

    def __enter__(self) -> LegacySource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    def fingerprint(self) -> dict[str, Any]:
        """Identity of the exact file we read, for the import manifest."""
        stat = self.path.stat()
        return {
            "source_db": str(self.path.resolve()),
            "source_db_bytes": stat.st_size,
            "source_db_mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        }

    def tables(self) -> list[str]:
        rows = self._con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
        return [name for (name,) in rows]

    def count(self, table: str) -> int:
        self._require_known(table)
        return int(self._con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        """Yield every row of ``table`` as a plain dict.

        Streaming rather than ``fetchall()`` so that a 30k-row table does not
        have to be materialised, and so classification can run as it goes.
        """
        self._require_known(table)
        cursor = self._con.execute(f'SELECT * FROM "{table}"')
        columns = [d[0] for d in cursor.description]
        while True:
            batch = cursor.fetchmany(2000)
            if not batch:
                break
            for values in batch:
                yield dict(zip(columns, values, strict=True))

    def _require_known(self, table: str) -> None:
        if table not in LEGACY_TABLES:
            raise KeyError(f"table {table!r} is not in the legacy inventory")
        if table not in self.tables():
            raise KeyError(f"table {table!r} does not exist in {self.path}")


@dataclass(frozen=True, slots=True)
class TablePlan:
    """What an import would do with one legacy table."""

    legacy_table: str
    target_table: str | None
    tier: ValueTier
    present: bool
    rows: int = 0
    verified: int = 0
    pending: int = 0
    reasons: tuple[tuple[str, int], ...] = ()

    @property
    def migrates(self) -> bool:
        return self.target_table is not None and self.present

    @property
    def pending_fraction(self) -> float:
        return self.pending / self.rows if self.rows else 0.0


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """A complete, write-free description of what an import would do."""

    source: dict[str, Any]
    tables: tuple[TablePlan, ...]
    generated_at_utc: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )

    @property
    def migrating_tables(self) -> tuple[TablePlan, ...]:
        return tuple(t for t in self.tables if t.migrates)

    @property
    def skipped_tables(self) -> tuple[TablePlan, ...]:
        return tuple(t for t in self.tables if not t.migrates)

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.migrating_tables)

    @property
    def total_verified(self) -> int:
        return sum(t.verified for t in self.migrating_tables)

    @property
    def total_pending(self) -> int:
        return sum(t.pending for t in self.migrating_tables)

    @property
    def tables_json(self) -> str:
        payload = {
            t.legacy_table: {
                "target_table": t.target_table,
                "tier": str(t.tier),
                "present": t.present,
                "rows": t.rows,
                "verified": t.verified,
                "pending": t.pending,
                "reasons": dict(t.reasons),
            }
            for t in self.tables
        }
        return json.dumps(payload, sort_keys=True)

    def summary(self) -> str:
        lines = [
            f"legacy source: {self.source.get('source_db')}",
            f"  {self.total_rows} rows across {len(self.migrating_tables)} migratable tables "
            f"({self.total_verified} verified, {self.total_pending} pending)",
            "  migrating:",
        ]
        for t in self.migrating_tables:
            lines.append(
                f"    {t.legacy_table:<20} -> {t.target_table:<24} {t.rows:>7} rows, "
                f"{t.pending:>6} pending"
            )
        skipped = [t for t in self.skipped_tables if t.present]
        if skipped:
            lines.append("  not migrated:")
            for t in skipped:
                lines.append(
                    f"    {t.legacy_table:<20} ({t.tier}) - {LEGACY_TABLES[t.legacy_table].note}"
                )
        missing = [t for t in self.skipped_tables if not t.present]
        if missing:
            lines.append(f"  absent from source: {', '.join(t.legacy_table for t in missing)}")
        return "\n".join(lines)


def plan_import(source: LegacySource) -> ImportPlan:
    """Read the legacy database and classify every migratable row.

    Reads only. Writes nothing anywhere - not to the legacy file, not to a new
    database. Pure enough to run against production data on the first try.
    """
    present = set(source.tables())

    # Forecast rows inherit their run's verdict, so run statuses are loaded
    # first and joined in memory rather than re-queried per row.
    run_status: dict[tuple[str, datetime], str] = {}
    if "forecast_runs" in present:
        for row in source.iter_rows("forecast_runs"):
            key = (str(row["model"]), row["run_time"])
            run_status[key] = str(row.get("status") or "")

    plans: list[TablePlan] = []
    for name, spec in LEGACY_TABLES.items():
        is_present = name in present
        if not is_present or spec.target_table is None:
            plans.append(
                TablePlan(
                    legacy_table=name,
                    target_table=spec.target_table,
                    tier=spec.tier,
                    present=is_present,
                )
            )
            continue

        counters: Counter[str] = Counter()
        rows = verified = pending = 0
        for row in source.iter_rows(name):
            rows += 1
            parent = None
            if name in ("forecast_hourly", "forecast_daily"):
                parent = run_status.get((str(row["model"]), row["run_time"]))
            status, reason = classify(name, row, run_status=parent)
            if status is VerificationStatus.VERIFIED:
                verified += 1
            else:
                pending += 1
                counters[reason or "unspecified"] += 1

        plans.append(
            TablePlan(
                legacy_table=name,
                target_table=spec.target_table,
                tier=spec.tier,
                present=True,
                rows=rows,
                verified=verified,
                pending=pending,
                reasons=tuple(sorted(counters.items(), key=lambda kv: (-kv[1], kv[0]))),
            )
        )

    # Stable order: research tables first, then the ones left behind.
    plans.sort(key=lambda p: (p.target_table is None, p.legacy_table))
    return ImportPlan(source=source.fingerprint(), tables=tuple(plans))


def record_import_run(
    target_con: duckdb.DuckDBPyConnection,
    plan: ImportPlan,
    *,
    dry_run: bool = True,
    status: str = "completed",
    error: str | None = None,
) -> str:
    """Write an import-manifest row into the **new** database.

    Never touches the legacy file. A dry run is recorded too: "we inspected the
    legacy database on this date and saw this" is research state in its own
    right, and it is what makes a later real import auditable.
    """
    run_id = uuid.uuid4().hex
    finished = datetime.now(tz=UTC)
    started = plan.generated_at_utc
    target_con.execute(
        """
        INSERT INTO legacy_import_runs
            (run_id, source_db, source_db_bytes, source_db_mtime,
             started_at_utc, finished_at_utc, dry_run, status, error, tables_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            run_id,
            plan.source.get("source_db", ""),
            plan.source.get("source_db_bytes"),
            plan.source.get("source_db_mtime"),
            started,
            finished,
            dry_run,
            status,
            error,
            plan.tables_json,
        ],
    )
    return run_id


def source_digest(path: str | Path) -> str:
    """SHA-256 of the legacy file, for proving which bytes were read.

    Computed lazily and only for small/medium files at import time; the plan
    records size and mtime instead so planning stays cheap on a 100 MB file.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

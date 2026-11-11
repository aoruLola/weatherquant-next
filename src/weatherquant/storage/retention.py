"""Retention / archival for the shadow DuckDB database.

Default is **off**. Nothing here ever deletes a row unless an operator
explicitly sets ``shadow_retention_days > 0`` and an archive directory.

Why so conservative? DuckDB has no soft-delete and no undo; once a row is
gone the research team cannot re-read it, and the dashboard's historical
view would silently develop holes. The default therefore leaves everything
in place forever. When an operator does opt in we insist on two steps in
order: archive first, delete second.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

__all__ = ["apply_retention", "RetentionResult"]


class RetentionResult:
    def __init__(self) -> None:
        self.archived_decisions = 0
        self.archived_predictions = 0
        self.deleted_decisions = 0
        self.deleted_predictions = 0
        self.ran = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "ran": self.ran,
            "archived_decisions": self.archived_decisions,
            "archived_predictions": self.archived_predictions,
            "deleted_decisions": self.deleted_decisions,
            "deleted_predictions": self.deleted_predictions,
        }


def apply_retention(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int | None,
    archive_dir: Path | None,
    now: datetime | None = None,
) -> RetentionResult:
    """Archive rows older than ``days``, then delete them.

    Returns a :class:`RetentionResult` even when the call is a no-op, so
    callers can log a stable shape.

    Parameters
    ----------
    con:
        Open DuckDB connection (the shadow's own). Must be a writer.
    days:
        Age cutoff in days. ``None`` or ``<= 0`` means retention is disabled
        and nothing is touched.
    archive_dir:
        Directory to write Parquet files into before deletion. Required when
        ``days > 0``.
    now:
        Reference time; defaults to ``datetime.now(timezone.utc)``.
    """
    result = RetentionResult()
    if days is None or days <= 0:
        log.debug("retention disabled (days=%s); nothing to do", days)
        return result
    if archive_dir is None:
        raise ValueError(
            "shadow_retention_archive_dir must be set when shadow_retention_days > 0; "
            "refusing to delete without an archive"
        )
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    ts = now.strftime("%Y%m%dT%H%M%SZ")
    result.ran = True

    decisions_out = archive_dir / f"shadow_decisions_{ts}.parquet"
    predictions_out = archive_dir / f"model_predictions_{ts}.parquet"

    try:
        n_dec = con.execute(
            """
            SELECT count(*) FROM shadow_decisions WHERE decision_time_utc < ?
            """,
            [cutoff],
        ).fetchone()[0]
        n_pred = con.execute(
            """
            SELECT count(*) FROM model_predictions WHERE forecast_run_time_utc < ?
            """,
            [cutoff],
        ).fetchone()[0]

        if n_dec:
            con.execute(
                "COPY (SELECT * FROM shadow_decisions WHERE decision_time_utc < ?) TO ? (FORMAT PARQUET)",
                [cutoff, str(decisions_out)],
            )
            con.execute("DELETE FROM shadow_decisions WHERE decision_time_utc < ?", [cutoff])
            con.execute("VACUUM shadow_decisions")
            result.archived_decisions = n_dec
            result.deleted_decisions = n_dec
            log.info("retention: archived and deleted %d shadow_decisions -> %s", n_dec, decisions_out)

        if n_pred:
            con.execute(
                "COPY (SELECT * FROM model_predictions WHERE forecast_run_time_utc < ?) TO ? (FORMAT PARQUET)",
                [cutoff, str(predictions_out)],
            )
            con.execute("DELETE FROM model_predictions WHERE forecast_run_time_utc < ?", [cutoff])
            con.execute("VACUUM model_predictions")
            result.archived_predictions = n_pred
            result.deleted_predictions = n_pred
            log.info("retention: archived and deleted %d model_predictions -> %s", n_pred, predictions_out)
    except Exception:
        log.exception("retention failed before deletion completed; DB is untouched, partial parquet may exist")
        # Remove a partial parquet if COPY failed mid-stream — the file would
        # be corrupt and worse than having no archive at all.
        for p in (decisions_out, predictions_out):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise

    if not n_dec and not n_pred:
        log.debug("retention: no rows older than %s days; nothing to archive", days)
    return result

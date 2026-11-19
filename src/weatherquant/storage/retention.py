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

import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

__all__ = ["RetentionResult", "apply_retention"]


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
    exported_through_seq: int | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    """Archive eligible rows older than ``days``, then delete them atomically.

    Decisions are eligible only after the durable decision-export cursor has
    passed their ``export_seq``. Predictions do not wait for the export
    cursor, but a prediction is kept for as long as any retained decision
    still points at its forecast run (same model, version, station, event
    and run): the alignment audit and the dashboard trace every decision to
    its prediction, and a decision just inside the window routinely uses a
    run initialised hours before the cutoff. Rejection reasons always follow
    the exact same decision set, keeping report aggregates inside the
    retained window. Every archive is fsynced before the first delete.

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
    exported_through_seq:
        Confirmed v2 decision-export cursor. ``None`` retains every decision
        and rejection reason; NULL sequences are always retained.
    now:
        Reference time; defaults to ``datetime.now(UTC)``.
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
        now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    ts = now.strftime("%Y%m%dT%H%M%SZ")
    result.ran = True

    decisions_out = archive_dir / f"shadow_decisions_{ts}.parquet"
    reasons_out = archive_dir / f"rejection_reasons_{ts}.parquet"
    predictions_out = archive_dir / f"model_predictions_{ts}.parquet"
    outputs = (decisions_out, reasons_out, predictions_out)
    # An archive of a committed earlier pass may be the only copy of its rows.
    # Never overwrite one, and never let this pass's failure cleanup unlink it.
    existing = [path for path in outputs if path.exists()]
    if existing:
        raise FileExistsError(
            f"retention archive already exists, refusing to overwrite: {existing[0]}"
        )
    decision_params = [cutoff, exported_through_seq]
    # Evaluated after the decision deletes, so a run referenced only by
    # decisions retired in this same transaction becomes eligible.
    prediction_where = """
        WHERE p.forecast_run_time_utc < ?
          AND NOT EXISTS (
              SELECT 1 FROM shadow_decisions d
              WHERE d.model_name = p.model_name
                AND d.model_version = p.model_version
                AND d.station = p.station
                AND d.event_id = p.event_id
                AND d.forecast_run_time_utc = p.forecast_run_time_utc
          )
    """
    n_dec = 0
    n_reasons = 0

    def copy_to(query: str, params: list, path: Path) -> None:
        # DuckDB refuses to bind a COPY target filename as a parameter, but the
        # caller controls this path. Quote it literally so a quote in the path
        # cannot escape the statement.
        target = str(path).replace("'", "''")
        con.execute(f"{query} TO '{target}' (FORMAT PARQUET)", params)
        _fsync_file(path)

    try:
        con.execute("BEGIN TRANSACTION")
        if exported_through_seq is not None:
            n_dec = con.execute(
                """
                SELECT count(*) FROM shadow_decisions
                WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                """,
                decision_params,
            ).fetchone()[0]
            n_reasons = con.execute(
                """
                SELECT count(*) FROM rejection_reasons
                WHERE decision_id IN (
                    SELECT decision_id FROM shadow_decisions
                    WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                )
                """,
                decision_params,
            ).fetchone()[0]
        else:
            log.info("retention: no confirmed v2 export cursor; retaining decisions and rejection reasons")

        # Every archive is written and fsynced before the delete it covers.
        # The surrounding transaction covers all deletes: a later failure
        # rolls the whole database change back before these uncommitted
        # archives are removed.
        if n_dec:
            copy_to(
                """
                COPY (
                    SELECT * FROM shadow_decisions
                    WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                )
                """,
                decision_params,
                decisions_out,
            )
        if n_reasons:
            copy_to(
                """
                COPY (
                    SELECT * FROM rejection_reasons
                    WHERE decision_id IN (
                        SELECT decision_id FROM shadow_decisions
                        WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                    )
                )
                """,
                decision_params,
                reasons_out,
            )
        if n_dec or n_reasons:
            _fsync_dir(archive_dir)

        if n_reasons:
            con.execute(
                """
                DELETE FROM rejection_reasons
                WHERE decision_id IN (
                    SELECT decision_id FROM shadow_decisions
                    WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                )
                """,
                decision_params,
            )
            con.execute("VACUUM rejection_reasons")
        if n_dec:
            con.execute(
                """
                DELETE FROM shadow_decisions
                WHERE decision_time_utc < ? AND export_seq IS NOT NULL AND export_seq <= ?
                """,
                decision_params,
            )
            con.execute("VACUUM shadow_decisions")

        n_pred = con.execute(
            f"SELECT count(*) FROM model_predictions p {prediction_where}", [cutoff]
        ).fetchone()[0]
        if n_pred:
            copy_to(
                f"COPY (SELECT * FROM model_predictions p {prediction_where})",
                [cutoff],
                predictions_out,
            )
            _fsync_dir(archive_dir)
            con.execute(f"DELETE FROM model_predictions p {prediction_where}", [cutoff])
            con.execute("VACUUM model_predictions")
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        # No deletion committed, so none of these files is the sole copy of a
        # retained row. Remove complete and partial output from the failed run.
        for path in outputs:
            with contextlib.suppress(OSError):
                path.unlink()
        log.exception("retention failed; database deletions rolled back and uncommitted archives removed")
        raise

    result.archived_decisions = n_dec
    result.deleted_decisions = n_dec
    result.archived_predictions = n_pred
    result.deleted_predictions = n_pred
    if n_dec:
        log.info("retention: archived and deleted %d shadow_decisions -> %s", n_dec, decisions_out)
    if n_reasons:
        log.info("retention: archived and deleted %d rejection_reasons -> %s", n_reasons, reasons_out)
    if n_pred:
        log.info("retention: archived and deleted %d model_predictions -> %s", n_pred, predictions_out)
    if not n_dec and not n_pred:
        log.debug("retention: no eligible rows older than %s days; nothing to archive", days)
    return result


def _fsync_file(path: Path) -> None:
    """Force an archive's bytes to disk before any row it holds is deleted.

    DuckDB's COPY closes the file but does not promise it reached the disk;
    the database COMMIT is durable, so without this a power cut after the
    commit could lose the only copy of the deleted rows.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path) -> None:
    """Make the archives' directory entries durable as well as their bytes."""
    with contextlib.suppress(OSError):  # not every platform can open a directory
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

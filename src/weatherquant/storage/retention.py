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
    passed their ``export_seq``. Predictions have no such dependency and are
    retained independently. Rejection reasons always follow the exact same
    decision set, keeping report aggregates inside the retained window.

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
    decision_params = [cutoff, exported_through_seq]
    n_dec = 0
    n_reasons = 0

    def copy_to(query: str, params: list, path: Path) -> None:
        # DuckDB refuses to bind a COPY target filename as a parameter, but the
        # caller controls this path. Quote it literally so a quote in the path
        # cannot escape the statement.
        target = str(path).replace("'", "''")
        con.execute(f"{query} TO '{target}' (FORMAT PARQUET)", params)

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

        n_pred = con.execute(
            """
            SELECT count(*) FROM model_predictions WHERE forecast_run_time_utc < ?
            """,
            [cutoff],
        ).fetchone()[0]

        # Publish every archive before deleting anything. The surrounding
        # transaction also covers the deletes: a later failure rolls the whole
        # database change back before these uncommitted archives are removed.
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
        if n_pred:
            copy_to(
                "COPY (SELECT * FROM model_predictions WHERE forecast_run_time_utc < ?)",
                [cutoff],
                predictions_out,
            )

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
        if n_pred:
            con.execute("DELETE FROM model_predictions WHERE forecast_run_time_utc < ?", [cutoff])
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

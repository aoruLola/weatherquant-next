#!/usr/bin/env python3
"""Backup-recovery drill: prove a backup can actually come back, on a copy.

A backup that has never been restored is a hope, not a capability - and the
2026-10-17 incident proved the rest of the pipeline around backups can fail
too: a backup hung for three hours, was abandoned as ``.incomplete-…`` (3.6 GB
of it on disk), and the service degraded fifteen minutes later. This drill
runs the whole lifecycle against **copies** in a scratch directory, never
against the source database, which is opened read-only once for the
recovery-point comparison and never written.

The stages, each timed and each judged on measured results:

=========================  ==============================================
restore                    Copy the source database to a scratch dir and
                           verify the recovery point: schema version,
                           per-table row counts, per-table content hash
                           and the decision watermark - restored must
                           equal source, or the drill fails.
backup_cycle               On the restored copy: write at production
                           flush cadence, run ``Writer.backup()``, then
                           restore *that* backup into a third directory
                           and re-verify the recovery point. Backup →
                           restore → verify, end to end.
clean_shutdown             Graceful close must fold the WAL: after a
                           clean ``close()``, no non-empty ``.wal`` file
                           may remain.
wal_recovery               SIGKILL a budgeted writer mid-write (the
                           state an OOM kill or a power cut leaves),
                           reopen, and require the committed prefix
                           intact plus a checkpoint that folds the WAL.
slow_io                    Re-run the restore inside a cgroup with a
                           throttled ``io.max`` - the kernel's own
                           rate limiter, the same mechanism a slow NAS
                           array would exercise. Needs root; skipped
                           honestly (recorded, never faked) otherwise.
incomplete_files           Plant ``.incomplete-*`` files in a backups
                           directory and require that backup discovery
                           never offers one as a restore candidate.
=========================  ==============================================

Every run appends one JSON line to the ledger (``--ledger``, default
``<work>/recovery-drills.jsonl``): stages with statuses and durations, the
recovery-point facts, and an overall verdict. That ledger is the DuckDB/WAL
lifecycle record WQN-22 asks for.

Usage::

    uv run python scripts/backup_recovery_drill.py --work /tmp/wq-drill
    uv run python scripts/backup_recovery_drill.py --work /tmp/wq-drill \\
        --source /path/to/copy-of-production.duckdb --ledger /mnt/user/.../drills.jsonl

``--source`` must be a COPY of a production file; the drill never opens a
live database (the writer holds its lease), and restoring anything back into
production is a separately authorised act.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DRILL_VERSION = "recovery-drill-1"

#: Rows per synthetic flush batch and how many batches the synthesized source
#: gets. Small on purpose: the drill is about correctness under restore, not
#: throughput; a source of a few thousand rows exercises every code path.
SYNTH_BATCHES = 8
SYNTH_ROWS_PER_BATCH = 500


@dataclass
class Stage:
    name: str
    status: str = "pass"           # pass | fail | skip | not_applicable
    duration_ms: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "detail": self.detail,
        }


@dataclass
class DrillReport:
    drill_id: str
    started_at: datetime
    stages: list[Stage] = field(default_factory=list)
    recovery_point: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    verdict: str = "PENDING"

    @property
    def ended_at(self) -> datetime:
        return datetime.now(tz=UTC)

    def stage(self, name: str) -> Stage:
        for stage in self.stages:
            if stage.name == name:
                return stage
        stage = Stage(name=name)
        self.stages.append(stage)
        return stage

    def as_dict(self) -> dict:
        return {
            "drill_id": self.drill_id,
            "tool_version": DRILL_VERSION,
            "started_at_utc": self.started_at.isoformat(),
            "ended_at_utc": self.ended_at.isoformat(),
            "source": self.source,
            "recovery_point": self.recovery_point,
            "stages": [s.as_dict() for s in self.stages],
            "verdict": self.verdict,
        }


# ---------------------------------------------------------------------------
# database helpers (shared by parent and child modes)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_names(con) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name != 'schema_migrations' "
        "ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _table_digest(con, table: str) -> tuple[int, int]:
    """(row count, deterministic content sum) for one table.

    ``hash()`` over every column, summed - order-independent, so the same rows
    in a restored file produce the same number. The value is only comparable
    within one duckdb version, which is exactly the comparison a restore makes;
    the ledger records the version so nobody reuses it across one.
    """

    columns = [
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
            [table],
        ).fetchall()
    ]
    projection = ", ".join(f'"{c}"' for c in columns)
    count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    digest = 0
    if count:
        digest = int(con.execute(
            f'SELECT sum(hash({projection}))::HUGEINT FROM "{table}"'
        ).fetchone()[0])
    return int(count), int(digest)


def _decision_watermark(con) -> dict:
    """The recovery point's logical watermark, from whichever table exists.

    A shadow database answers with its latest decision time; a collector
    database with its latest price point or book fetch. Saying which table
    produced it is part of the answer - a watermark without its source is a
    number nobody can audit.
    """
    for table, column, label in (
        ("shadow_decisions", "decision_time_utc", "latest_decision_utc"),
        ("price_history", "ts", "latest_price_utc"),
        ("order_books", "fetched_at_utc", "latest_book_utc"),
    ):
        try:
            row = con.execute(f'SELECT max("{column}") FROM "{table}"').fetchone()
        except Exception:
            continue
        if row and row[0] is not None:
            return {"source": f"{table}.{column}", "value": str(row[0]), "name": label}
    return {"source": None, "value": None, "name": "none"}


def _recovery_point(db_path: Path) -> dict:
    """Everything the drill means by 'this backup is recoverable to here'."""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    # Pin the session to UTC, as every project connection does: an unpinned
    # session renders TIMESTAMPTZ in the host's zone, and a watermark whose
    # text moves between machines is a worse audit fact than a stable one.
    con.execute("SET TimeZone='UTC'")
    try:
        tables: dict[str, dict] = {}
        for table in _table_names(con):
            count, digest = _table_digest(con, table)
            tables[table] = {"rows": count, "digest": digest}
        # The schema version is part of the recovery point: a restored copy on
        # an older migration ledger than its source would open fine and still
        # not be the database that was backed up.
        try:
            schema_version = con.execute(
                "SELECT max(version) FROM schema_migrations"
            ).fetchone()[0]
        except Exception:
            schema_version = None
        return {
            "tables": tables,
            "schema_version": int(schema_version) if schema_version is not None else None,
            "duckdb_version": duckdb.__version__,
            "watermark": _decision_watermark(con),
        }
    finally:
        con.close()


def _recovery_points_agree(source: dict, restored: dict) -> list[str]:
    failures = []
    if source.get("schema_version") != restored.get("schema_version"):
        failures.append("schema migration version differs after restore")
    for table, facts in source.get("tables", {}).items():
        got = restored.get("tables", {}).get(table)
        if got is None:
            failures.append(f"table {table} missing after restore")
            continue
        if got["rows"] != facts["rows"]:
            failures.append(f"table {table}: rows {got['rows']} != {facts['rows']}")
        if got["digest"] != facts["digest"]:
            failures.append(f"table {table}: content digest differs")
    for table in set(restored.get("tables", {})) - set(source.get("tables", {})):
        failures.append(f"table {table} appeared after restore")
    if source.get("watermark") != restored.get("watermark"):
        failures.append(
            f"watermark differs: {source.get('watermark')} != {restored.get('watermark')}"
        )
    return failures


# ---------------------------------------------------------------------------
# source synthesis: a real database written through the real writer
# ---------------------------------------------------------------------------


def _synthetic_books(batch: int, rows: int, run_id: str, base: datetime) -> list[tuple]:
    stamp = base.replace(second=batch % 60, microsecond=0)
    out = []
    for i in range(rows):
        n = batch * rows + i
        content = f"drill-{n:016x}"
        out.append(
            (
                content, "DRILL-M", f"DRILL-T-{i % 50:03d}",
                '[[0.55,"500"]]', '[[0.57,"450"]]', 0.55, 0.57, 0.02,
                500.0, 450.0, "", stamp, stamp, "rest", "venue", content, "ok", run_id,
            )
        )
    return out


def _synthetic_prices(batch: int, rows: int, run_id: str, base: datetime) -> list[tuple]:
    stamp = base.replace(second=batch % 60, microsecond=0)
    return [
        ("DRILL-M", f"DRILL-T-{(batch * rows + i) % 50:03d}", stamp, 0.56, "rest",
         stamp, "venue", run_id)
        for i in range(rows)
    ]


def synthesize_source(db_path: Path, *, batches: int = SYNTH_BATCHES) -> dict:
    from weatherquant.data.writer import CollectorWriter

    run_id = f"drill-{uuid.uuid4().hex[:8]}"
    writer = CollectorWriter(
        db_path, run_id=run_id, flush_rows=500, flush_seconds=5.0, checkpoint_seconds=300.0
    )
    writer.open()
    writer.start_run(
        app_version="recovery-drill", git_sha=None, image_tag="drill",
        hostname=os.uname().nodename, pid=os.getpid(),
    )
    base = datetime.now(tz=UTC)
    try:
        for batch in range(batches):
            for row in _synthetic_books(batch, SYNTH_ROWS_PER_BATCH, run_id, base):
                writer.stage_book(row)
            for row in _synthetic_prices(batch, SYNTH_ROWS_PER_BATCH, run_id, base):
                writer.stage_price(row)
            writer.flush(force=True)
        writer.close(status="stopped", reason="recovery-drill source")
    except BaseException:
        if writer.is_open:
            writer.close(status="aborted", reason="recovery-drill exception")
        raise
    return {"run_id": run_id, "batches": batches}


# ---------------------------------------------------------------------------
# child modes
# ---------------------------------------------------------------------------


def child_restore(source: Path, dest: Path, chunk_mb: int = 4) -> None:
    """Copy with per-chunk fsync - what a throttled device can actually feel."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunk = chunk_mb * 1024 * 1024
    with source.open("rb") as src, dest.open("wb") as dst:
        while True:
            data = src.read(chunk)
            if not data:
                break
            dst.write(data)
            dst.flush()
            os.fsync(dst.fileno())


def child_wal_writer(db_path: Path, rows_per_batch: int) -> None:
    """Commit journalled batches until killed - the WAL-recovery subject."""
    os.environ.setdefault("WQ_DUCKDB_MEMORY_LIMIT", "256MB")
    os.environ.setdefault("WQ_DUCKDB_TEMP_DIR", str(db_path.parent / "spill"))
    from weatherquant.storage import connect

    con = connect(db_path)
    con.execute("SET checkpoint_threshold='1TB'")
    con.execute(
        "CREATE TABLE IF NOT EXISTS drill_wal (batch INTEGER, payload VARCHAR)"
    )
    journal = db_path.parent / "drill-wal-journal.txt"
    batch = 0
    while True:
        batch += 1
        con.execute("BEGIN TRANSACTION")
        try:
            con.executemany(
                "INSERT INTO drill_wal (batch, payload) VALUES (?, ?)",
                [(batch, f"{batch}:{i}" * 8) for i in range(rows_per_batch)],
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(f"{batch}\n")
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_restore(report: DrillReport, work: Path, source: Path) -> tuple[Path, dict, dict]:
    stage = report.stage("restore")
    started = time.monotonic()
    try:
        dest = work / "restored" / "weatherquant.duckdb"
        if dest.parent.exists():
            shutil.rmtree(dest.parent)
        child_restore(source, dest)
        copy_s = time.monotonic() - started

        verify_started = time.monotonic()
        source_point = _recovery_point(source)
        restored_point = _recovery_point(dest)
        failures = _recovery_points_agree(source_point, restored_point)
        if failures:
            stage.status = "fail"
            stage.detail = "; ".join(failures)
            raise RuntimeError(stage.detail)
        stage.detail = (
            f"copy {copy_s:.1f}s, verify {time.monotonic() - verify_started:.1f}s, "
            f"{len(source_point['tables'])} tables, "
            f"watermark={source_point['watermark']['value']}"
        )
        report.recovery_point = source_point
        return dest, source_point, restored_point
    finally:
        stage.duration_ms = (time.monotonic() - started) * 1000.0


def _is_collector_shaped(db_path: Path) -> bool:
    """Does this database speak the collector's schema?

    ``backup_cycle`` and ``clean_shutdown`` exercise the collector's writer,
    whose migration ledger is checksum-checked against its own history - the
    migrator (rightly) refuses to open a shadow database with collector
    migrations. Shape is decided by tables, not by path, so a renamed file
    cannot fool it.
    """
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        names = set(_table_names(con))
    finally:
        con.close()
    return {"markets", "order_books"} <= names and "shadow_decisions" not in names


def stage_backup_cycle(report: DrillReport, work: Path, restored: Path) -> None:
    """Backup → restore → verify, the full chain, on the restored copy."""
    stage = report.stage("backup_cycle")
    started = time.monotonic()
    db_path = restored
    if not _is_collector_shaped(db_path):
        stage.status = "not_applicable"
        stage.detail = (
            "source is not a collector database; the collector's writer and "
            "self-backup path do not apply (the WAL-recovery stage covers the "
            "write path this source actually has)"
        )
        stage.duration_ms = (time.monotonic() - started) * 1000.0
        return
    try:
        from weatherquant.data.writer import CollectorWriter

        run_id = f"drill-cycle-{uuid.uuid4().hex[:8]}"
        writer = CollectorWriter(
            db_path, run_id=run_id, flush_rows=500, flush_seconds=5.0,
            checkpoint_seconds=300.0,
        )
        writer.open()
        writer.start_run(
            app_version="recovery-drill", git_sha=None, image_tag="drill",
            hostname=os.uname().nodename, pid=os.getpid(),
        )
        base = datetime.now(tz=UTC)
        backup_report = None
        try:
            for batch in range(2):
                for row in _synthetic_books(100 + batch, 500, run_id, base):
                    writer.stage_book(row)
                for row in _synthetic_prices(100 + batch, 500, run_id, base):
                    writer.stage_price(row)
                writer.flush(force=True)
            backup_report = writer.backup(work / "backups")
            writer.close(status="stopped", reason="recovery-drill cycle")
        except BaseException:
            if writer.is_open:
                writer.close(status="aborted", reason="recovery-drill exception")
            raise

        if backup_report is None or not backup_report.ok:
            stage.status = "fail"
            stage.detail = f"backup failed verification: {backup_report and backup_report.error}"
            return
        published = backup_report.destination
        if published.name.startswith(".incomplete"):
            stage.status = "fail"
            stage.detail = f"a backup published under an incomplete name: {published.name}"
            return

        # Restore the published backup into a third directory and re-verify.
        chain_dest = work / "restored-from-backup" / "weatherquant.duckdb"
        if chain_dest.parent.exists():
            shutil.rmtree(chain_dest.parent)
        child_restore(published, chain_dest)
        chained_point = _recovery_point(chain_dest)
        # The chain starts from the *restored* copy, which already carries the
        # cycle's extra rows - so the chain's recovery point is compared to a
        # fresh reading of the restored copy, not to the original source.
        expected_point = _recovery_point(restored)
        # `collector_runs`/`collector_events` legitimately move AFTER a backup
        # snapshot: close() records the run's exit status and stats into them.
        # A backup cannot contain a graceful shutdown that had not happened
        # yet, so these two are excluded from the chain comparison by name.
        excluded = {"collector_runs", "collector_events"}
        for point in (expected_point, chained_point):
            point["tables"] = {
                t: facts for t, facts in point["tables"].items() if t not in excluded
            }
        failures = _recovery_points_agree(expected_point, chained_point)
        if failures:
            stage.status = "fail"
            stage.detail = "; ".join(failures)
            return
        stage.status = "pass"
        stage.detail = (
            f"backup verified={backup_report.ok} bytes={backup_report.bytes_written or 0}; "
            f"chain restore verified ({len(expected_point['tables'])} tables; "
            f"{', '.join(sorted(excluded))} excluded - they advance at close)"
        )
    except Exception as exc:
        stage.status = "fail"
        stage.detail = str(exc)
    finally:
        stage.duration_ms = (time.monotonic() - started) * 1000.0


def stage_clean_shutdown(report: DrillReport, work: Path, restored: Path) -> None:
    """A graceful close must leave no WAL behind."""
    stage = report.stage("clean_shutdown")
    started = time.monotonic()
    if not _is_collector_shaped(restored):
        stage.status = "not_applicable"
        stage.detail = (
            "source is not a collector database; the collector's close path "
            "does not apply - a shadow database's own writer owns its shutdown"
        )
        stage.duration_ms = (time.monotonic() - started) * 1000.0
        return
    try:
        from weatherquant.data.writer import CollectorWriter

        db_path = work / "clean-shutdown" / "weatherquant.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Start from the restored copy so the stage runs against real data.
        child_restore(restored, db_path)
        writer = CollectorWriter(
            db_path, run_id=f"drill-clean-{uuid.uuid4().hex[:8]}",
            flush_rows=500, flush_seconds=5.0, checkpoint_seconds=300.0,
        )
        writer.open()
        base = datetime.now(tz=UTC)
        for row in _synthetic_books(900, 500, writer.run_id, base):
            writer.stage_book(row)
        writer.flush(force=True)
        wal = db_path.parent / "weatherquant.duckdb.wal"
        wal_during = wal.stat().st_size if wal.exists() else 0
        writer.close(status="stopped", reason="recovery-drill clean shutdown")
        wal_after = wal.stat().st_size if wal.exists() else 0
        stage.duration_ms = (time.monotonic() - started) * 1000.0
        if wal_during > 0 and wal_after == 0:
            stage.detail = f"wal {wal_during} bytes folded away by graceful close"
        elif wal_during == 0:
            stage.detail = "no WAL at close (checkpoint had already folded it)"
        else:
            stage.status = "fail"
            stage.detail = f"wal still {wal_after} bytes after a clean close"
    except Exception as exc:
        stage.status = "fail"
        stage.detail = str(exc)
        stage.duration_ms = (time.monotonic() - started) * 1000.0


def stage_wal_recovery(report: DrillReport, work: Path, restored: Path) -> None:
    stage = report.stage("wal_recovery")
    started = time.monotonic()
    db_path = work / "wal-recovery" / "weatherquant.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    journal = db_path.parent / "drill-wal-journal.txt"
    if journal.exists():
        journal.unlink()
    child_restore(restored, db_path)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--child-wal", str(db_path)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if journal.exists() and sum(1 for _ in journal.open()) >= 6:
                break
            if proc.poll() is not None:
                tail = ""
                if proc.stdout is not None:
                    tail = "".join(proc.stdout.readlines()[-8:])
                raise RuntimeError(f"wal child exited before committing enough batches: {tail}")
            time.sleep(0.2)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)

        journalled = sum(1 for line in journal.read_text().splitlines() if line.strip())
        wal = db_path.parent / "weatherquant.duckdb.wal"
        wal_bytes = wal.stat().st_size if wal.exists() else 0

        from weatherquant.storage import connect

        con = connect(db_path)  # reopen replays the WAL
        try:
            max_batch, rows = con.execute(
                "SELECT MAX(batch), COUNT(*) FROM drill_wal"
            ).fetchone()
            con.execute("CHECKPOINT")
        finally:
            con.close()
        wal_after = wal.stat().st_size if wal.exists() else 0
        stage.duration_ms = (time.monotonic() - started) * 1000.0

        failures = []
        if rows != max_batch * 1000:
            failures.append(f"rows {rows} != {max_batch} batches x 1000: torn transaction")
        if not journalled <= max_batch <= journalled + 1:
            failures.append("committed prefix moved backwards")
        if wal_bytes == 0:
            failures.append("no WAL existed at kill time; the stage proves nothing")
        if wal_after >= wal_bytes and wal_bytes > 0:
            failures.append("WAL did not fold on checkpoint")
        if failures:
            stage.status = "fail"
            stage.detail = "; ".join(failures)
        else:
            stage.detail = (
                f"kill left {journalled} journalled batches, max_batch={max_batch}, "
                f"wal {wal_bytes} -> {wal_after} bytes"
            )
    except Exception as exc:
        stage.status = "fail"
        stage.detail = str(exc)
        stage.duration_ms = (time.monotonic() - started) * 1000.0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def _device_of(path: Path) -> str | None:
    """``major:minor`` of the device holding ``path``, for io.max."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"


def stage_slow_io(report: DrillReport, work: Path, source: Path) -> None:
    """Re-restore under a kernel-enforced io.max, on a copy, timed.

    The io controller is the kernel's own rate limiter - the same mechanism
    that makes a busy NAS array slow, applied here to prove the restore path
    degrades in duration and not in correctness. Root is required; anything
    else is recorded as a skip, never silently passed.
    """
    stage = report.stage("slow_io")
    started = time.monotonic()
    cgroup = Path("/sys/fs/cgroup/wq-recovery-drill")
    try:
        if os.geteuid() != 0:
            stage.status = "skip"
            stage.detail = "requires root to create a cgroup with io.max"
            return
        if "io" not in Path("/sys/fs/cgroup/cgroup.controllers").read_text().split():
            stage.status = "skip"
            stage.detail = "io controller not available on this kernel"
            return
        device = _device_of(work)
        if device is None:
            stage.status = "skip"
            stage.detail = "could not resolve the work directory's device"
            return

        shutil.rmtree(cgroup, ignore_errors=True)
        cgroup.mkdir(parents=True)
        # 4 MiB/s both ways: slow enough to feel, fast enough for the drill
        # to finish in minutes even against a 100 MB source.
        (cgroup / "io.max").write_text(f"{device} rbps=4194304 wbps=4194304")

        dest = work / "restored-slow" / "weatherquant.duckdb"
        if dest.parent.exists():
            shutil.rmtree(dest.parent)
        proc = subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()),
                "--child-restore", str(source), str(dest),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (cgroup / "cgroup.procs").write_text(str(proc.pid))
        proc.wait(timeout=900)

        verify_started = time.monotonic()
        expected = _recovery_point(source)
        got = _recovery_point(dest)
        failures = _recovery_points_agree(expected, got)
        stage.duration_ms = (time.monotonic() - started) * 1000.0
        if failures:
            stage.status = "fail"
            stage.detail = "; ".join(failures)
        else:
            stage.detail = (
                f"restore under 4MiB/s io.max completed in {stage.duration_ms / 1000:.1f}s, "
                f"recovery point verified ({len(expected['tables'])} tables, "
                f"verify {time.monotonic() - verify_started:.1f}s)"
            )
    except Exception as exc:
        stage.status = "fail"
        stage.detail = str(exc)
        stage.duration_ms = (time.monotonic() - started) * 1000.0
    finally:
        shutil.rmtree(cgroup, ignore_errors=True)


def stage_incomplete_files(report: DrillReport, work: Path, restored: Path) -> None:
    """``.incomplete-*`` files are never restore candidates, and are reported."""
    stage = report.stage("incomplete_files")
    started = time.monotonic()
    try:
        backups = work / "backups-incomplete"
        backups.mkdir(parents=True, exist_ok=True)
        planted = []
        for name, content in (
            (".incomplete-20261017T000017Z-0f671df2.duckdb", b"abandoned-bytes"),
            (".incomplete-halfwritten.duckdb", b"\x00" * 128),
        ):
            path = backups / name
            path.write_bytes(content)
            planted.append(path)
        # A real-looking published file, so discovery has a genuine candidate.
        good = backups / "weatherquant-20261026T000000Z-deadbeef.duckdb"
        shutil.copyfile(restored, good)

        candidates = discover_backups(backups)
        stage.duration_ms = (time.monotonic() - started) * 1000.0
        if [c.name for c in candidates] != [good.name]:
            stage.status = "fail"
            stage.detail = f"discovery offered: {[c.name for c in candidates]}"
            return
        leftovers = report_incomplete_files(backups)
        if [item["name"] for item in leftovers] != sorted(p.name for p in planted):
            stage.status = "fail"
            stage.detail = f"leftover report wrong: {leftovers}"
            return
        stage.detail = (
            f"discovery ignored {len(planted)} .incomplete files "
            f"({sum(item['bytes'] for item in leftovers)} bytes reported for cleanup)"
        )
    except Exception as exc:
        stage.status = "fail"
        stage.detail = str(exc)
        stage.duration_ms = (time.monotonic() - started) * 1000.0


# ---------------------------------------------------------------------------
# discovery / ledger
# ---------------------------------------------------------------------------


def discover_backups(backups_dir: Path) -> list[Path]:
    """Published backups only. A file still named ``.incomplete-`` is by
    definition one whose verification never finished - offering it as a
    restore candidate would turn the 2026-10-17 leftovers into a restore
    source."""
    if not backups_dir.is_dir():
        return []
    return sorted(
        p for p in backups_dir.glob("weatherquant-*.duckdb")
        if not p.name.startswith(".incomplete")
    )


def report_incomplete_files(backups_dir: Path) -> list[dict]:
    """What the incident class of files looks like right now, for cleanup."""
    if not backups_dir.is_dir():
        return []
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(backups_dir.glob(".incomplete-*"))
    ]


def append_ledger(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, help="scratch directory (never a production path)")
    parser.add_argument("--source", type=Path, default=None,
                        help="a COPY of a database to drill against; default: synthesize one")
    parser.add_argument("--source-is-copy", action="store_true",
                        help="confirm --source is a file copy, not a database a writer "
                             "may have open - a byte-copy of a live DuckDB is torn, and "
                             "the drill refuses to turn that into a 'recovery'")
    parser.add_argument("--ledger", type=Path, default=None,
                        help="JSONL lifecycle ledger to append to (default <work>/recovery-drills.jsonl)")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--child-restore", type=Path, nargs=2, metavar=("SRC", "DEST"), help=argparse.SUPPRESS)
    parser.add_argument("--child-wal", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child_restore is not None:
        child_restore(args.child_restore[0], args.child_restore[1])
        return 0
    if args.child_wal is not None:
        child_wal_writer(args.child_wal, rows_per_batch=1000)
        return 0

    if args.work is None:
        parser.error("--work is required (child modes excepted)")
    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ledger = (args.ledger.resolve() if args.ledger else work / "recovery-drills.jsonl")

    report = DrillReport(
        drill_id=f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        started_at=datetime.now(tz=UTC),
    )
    print(f"recovery drill {report.drill_id} over {work}")

    source: Path | None = None
    try:
        if args.source is not None:
            source = args.source.resolve()
            if not source.exists():
                raise FileNotFoundError(f"source not found: {source}")
            if not source.is_relative_to(work) and not args.source_is_copy:
                print(
                    "refusing: --source outside --work needs --source-is-copy. The drill "
                    "reads its source byte-for-byte, and a byte-copy of a database a "
                    "writer still has open is a torn file - the guard is what keeps a "
                    "live production file from becoming a 'recovery' source.",
                    file=sys.stderr,
                )
                return 2
            report.source = {"origin": str(source), "synthesized": False}
        else:
            started = time.monotonic()
            source = work / "source" / "weatherquant.duckdb"
            source.parent.mkdir(parents=True, exist_ok=True)
            facts = synthesize_source(source)
            report.source = {
                "origin": str(source),
                "synthesized": True,
                **facts,
                "synthesis_s": round(time.monotonic() - started, 1),
            }

        report.source["sha256"] = _sha256_file(source)
        report.source["bytes"] = source.stat().st_size

        restored, _source_point, _restored_point = stage_restore(report, work, source)
        stage_backup_cycle(report, work, restored)
        stage_clean_shutdown(report, work, restored)
        stage_wal_recovery(report, work, restored)
        stage_slow_io(report, work, source)
        stage_incomplete_files(report, work, restored)
    except Exception as exc:
        stage = report.stage("drill")
        stage.status = "fail"
        stage.detail = str(exc)

    failures = [s for s in report.stages if s.status == "fail"]
    report.verdict = "FAILED" if failures else "PASSED"

    print("\n" + "=" * 72)
    print("RECOVERY DRILL REPORT")
    print("=" * 72)
    for stage in report.stages:
        print(f"  [{stage.status:>4}] {stage.name:<18} {stage.duration_ms:9.0f} ms  {stage.detail}")
    print(f"\n  source: {report.source.get('bytes', 0) / 1024 / 1024:.1f} MiB, "
          f"sha256={report.source.get('sha256', '')[:16]}…")
    watermark = report.recovery_point.get("watermark", {})
    print(f"  recovery point: {len(report.recovery_point.get('tables', {}))} tables, "
          f"watermark {watermark.get('name')}={watermark.get('value')}")
    print(f"  ledger: {ledger}")

    entry = report.as_dict()
    append_ledger(ledger, entry)
    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAILED")
        return 1
    print("RESULT: PASSED")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"work kept at {work}")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())

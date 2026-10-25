"""The production memory gate: what stands between this collector and another
host-wide stall.

PR #14 bounded the engine. That alone does not close WQN-3: ``memory_limit``
is not a process cap (DuckDB spills above it, and Python/native allocations
live outside it), so the container needs its own ceiling, the spill needs a
real filesystem, and the kill - which is now *designed for* rather than
feared - has to leave a database that recovers.

These tests pin each layer:

* the compose file carries the 6 GB container limit, no swap, and the
  environment that points spill at the NAS array;
* a writable connection materialises its temp directory (a mistyped path
  must fail at startup, not hours in, mid-transaction);
* a SIGKILL mid-write - the exact state a container OOM kill leaves - loses
  no committed transaction, and the WAL replays;
* the daily backup completes and verifies under a tight engine budget.
"""

from __future__ import annotations

import re
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker" / "compose.unraid.yaml"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# compose: the container's own ceiling
# ---------------------------------------------------------------------------


def test_compose_caps_the_container_at_6gb() -> None:
    text = _compose_text()
    assert re_search(r"^  mem_limit:\s*6g\s*$", text), (
        "the collector container has no memory ceiling: the engine budget "
        "bounds DuckDB, not the process, and the host cannot survive another "
        "unbounded container"
    )
    assert re_search(r"^  memswap_limit:\s*6g\s*$", text), (
        "memswap_limit must equal mem_limit: a container that pages to swap "
        "is not capped, it is only slowed - and this NAS has no swap to give"
    )


def re_search(pattern: str, text: str) -> re.Match[str] | None:
    return re.search(pattern, text, re.MULTILINE)


def test_compose_points_spill_at_a_real_filesystem() -> None:
    text = _compose_text()
    match = re_search(r"WQ_DUCKDB_TEMP_DIR:\s*(\S+)", text)
    assert match, "no spill directory configured: DuckDB would spill into RAM"
    spill = match.group(1)
    assert spill.startswith("/data"), (
        f"spill directory {spill!r} must live under /data (the NAS array); "
        "a tmpfs spill is itself a memory allocation and defeats the budget"
    )


def test_compose_carries_the_engine_budget_environment() -> None:
    text = _compose_text()
    assert re_search(r"WQ_DUCKDB_MEMORY_LIMIT:\s*3GB", text), (
        "the engine budget must be explicit in the deployment, not left to "
        "whatever the image defaults to"
    )
    assert re_search(r"WQ_DUCKDB_THREADS:\s*\"4\"", text), (
        "the thread pool must be pinned: one thread per core starves the "
        "services this container shares the host with"
    )


def test_compose_budget_stays_well_under_the_container_cap() -> None:
    """3GB engine + Python + native allocations must fit inside 6GB with room."""
    text = _compose_text()
    limit = re_search(r"WQ_DUCKDB_MEMORY_LIMIT:\s*(\d+)(GB|MB)", text)
    cap = re_search(r"mem_limit:\s*(\d+)(g|m)", text)
    assert limit and cap
    engine_gb = float(limit.group(1)) * (1 if limit.group(2) == "GB" else 1 / 1024)
    cap_gb = float(cap.group(1)) * (1 if cap.group(2) == "g" else 1 / 1024)
    assert engine_gb * 2 <= cap_gb, (
        "engine budget leaves no room for spill, Python and native allocations"
    )


# ---------------------------------------------------------------------------
# connect: the spill target exists before it is needed
# ---------------------------------------------------------------------------


def test_a_configured_temp_directory_is_created_at_open(tmp_path: Path) -> None:
    from weatherquant.storage.db import connect

    spill = tmp_path / "deeply" / "nested" / "spill"
    con = connect(tmp_path / "t.duckdb", temp_directory=str(spill))
    try:
        assert spill.is_dir(), "spill directory must exist from open(), not mid-query"
    finally:
        con.close()


def test_an_impossible_temp_directory_fails_at_open_not_mid_query(tmp_path: Path) -> None:
    from weatherquant.storage.db import connect

    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    with pytest.raises(ValueError, match="temp_directory"):
        connect(tmp_path / "t.duckdb", temp_directory=str(blocker / "spill"))


# ---------------------------------------------------------------------------
# SIGKILL mid-write: the state a container OOM kill leaves behind
# ---------------------------------------------------------------------------

_CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    data = Path(sys.argv[1])
    os.environ["WQ_DUCKDB_TEMP_DIR"] = str(data / "spill")
    from weatherquant.storage import connect
    con = connect(data / "weatherquant.duckdb")
    # Keep every commit in the WAL: the kill must leave un-checkpointed
    # commits behind, or the replay check proves nothing.
    con.execute("SET checkpoint_threshold='1TB'")
    con.execute("CREATE TABLE IF NOT EXISTS batches (batch INTEGER, payload VARCHAR)")
    journal = open(data / "journal.txt", "a")
    batch = 0
    while True:
        batch += 1
        # One flush = one transaction, exactly as CollectorWriter.flush does:
        # the kill may land mid-batch, and the whole batch must then roll back.
        con.execute("BEGIN TRANSACTION")
        try:
            con.executemany(
                "INSERT INTO batches (batch, payload) VALUES (?, ?)",
                [(batch, "x" * 200)] * 500,
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        journal.write(f"{batch}\\n")
        journal.flush()
        os.fsync(journal.fileno())
    """
)


def _spawn_writer_child(data_dir: Path) -> subprocess.Popen:
    child = data_dir / "writer_child.py"
    child.write_text(_CHILD, encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(child), str(data_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _journal_batches(data_dir: Path) -> int:
    journal = data_dir / "journal.txt"
    if not journal.exists():
        return 0
    return sum(1 for line in journal.read_text().splitlines() if line.strip())


def test_a_sigkill_mid_write_loses_no_committed_transaction(tmp_path: Path) -> None:
    from weatherquant.storage import connect

    data = tmp_path / "data"
    data.mkdir()
    proc = _spawn_writer_child(data)

    deadline = time.monotonic() + 60.0
    while _journal_batches(data) < 8 and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _journal_batches(data) >= 8, "child never committed; cannot test recovery"

    proc.send_signal(signal.SIGKILL)  # what the kernel OOM killer delivers
    proc.wait(timeout=30)

    wal = data / "weatherquant.duckdb.wal"
    wal_bytes = wal.stat().st_size if wal.exists() else 0

    con = connect(data / "weatherquant.duckdb")  # reopen replays the WAL
    try:
        max_batch, rows = con.execute("SELECT MAX(batch), COUNT(*) FROM batches").fetchone()
    finally:
        con.close()

    journalled = _journal_batches(data)
    assert max_batch == rows // 500, "committed batches are not contiguous"
    assert journalled <= max_batch <= journalled + 1, (
        "the committed prefix moved backwards across a hard kill"
    )

    # The replay folded in: a forced checkpoint must shrink the WAL away.
    con = connect(data / "weatherquant.duckdb")
    try:
        con.execute("CHECKPOINT")
    finally:
        con.close()
    wal_after = wal.stat().st_size if wal.exists() else 0
    assert wal_bytes > 0, "no WAL existed at kill time; the test proves nothing"
    assert wal_after < wal_bytes, "WAL did not shrink: the kill's commits were not replayed"


# ---------------------------------------------------------------------------
# the daily backup under a tight engine budget
# ---------------------------------------------------------------------------

_BACKUP_CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from datetime import UTC, datetime, timedelta
    data = Path(sys.argv[1])
    os.environ["WQ_DUCKDB_MEMORY_LIMIT"] = "512MB"
    os.environ["WQ_DUCKDB_TEMP_DIR"] = str(data / "spill")
    from weatherquant.data.writer import CollectorWriter

    writer = CollectorWriter(
        data / "weatherquant.duckdb", run_id="budget-backup-test",
        flush_rows=500, flush_seconds=5.0, checkpoint_seconds=300.0,
    )
    writer.open()
    writer.start_run(
        app_version="test", git_sha=None, image_tag="test",
        hostname="test", pid=os.getpid(),
    )
    now = datetime.now(tz=UTC)
    for batch in range(60):
        stamp = now - timedelta(seconds=batch)
        for i in range(500):
            n = batch * 500 + i
            content = f"budget-{n:012x}"
            writer.stage_book((
                content, "BUDGET-M", f"TOKEN-{n % 100:03d}",
                '[[0.55,"500"]]', '[[0.57,"450"]]', 0.55, 0.57, 0.02,
                500.0, 450.0, "", stamp, stamp, "rest", "venue", content, "ok",
                "budget-backup-test",
            ))
        writer.flush(force=True)
    report = writer.backup(data / "backups")
    writer.close(status="stopped", reason="test done")
    print(f"BACKUP verified={report.ok} bytes={report.bytes_written or 0} "
          f"error={report.error}")
    """
)


def test_the_daily_backup_verifies_under_a_tight_budget(tmp_path: Path) -> None:
    """PR #14's open question: does the backup - a distinct high-water mark -
    still work when the engine only has a fraction of its old headroom?

    512 MB is ~1/6 of the production 3GB budget and ~1/24 of the unbounded
    default this fleet ran with. The measured floor on this path sits between
    256 MB (inserts fail: DuckDB cannot spill a sub-row-group append, so
    small allocations fail outright) and 384 MB (passes) - a floor an
    operator tuning WQ_DUCKDB_MEMORY_LIMIT needs to know about, recorded in
    docs/memory-acceptance.md.
    """
    data = tmp_path / "data"
    data.mkdir()
    child = data / "backup_child.py"
    child.write_text(_BACKUP_CHILD, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(child), str(data)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"backup child failed:\n{result.stdout}\n{result.stderr}"
    assert "verified=True" in result.stdout, result.stdout

    backups = list((data / "backups").glob("weatherquant-*.duckdb"))
    assert len(backups) == 1, "a verified backup must be published under backups/"
    assert not list((data / "backups").glob(".incomplete-*")), (
        "an .incomplete file was left behind: a failed backup that looks finished"
    )
    # The spill directory was used, and it is on disk by construction (tmp).
    assert (data / "spill").is_dir()

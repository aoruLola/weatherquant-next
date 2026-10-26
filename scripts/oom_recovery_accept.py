#!/usr/bin/env python3
"""Container-level OOM acceptance for the collector's DuckDB writer.

Why this proves what it proves
------------------------------
Docker's ``mem_limit`` **is** cgroup v2 ``memory.max``: when a container's
memory usage crosses the limit, the kernel's OOM killer sends the process a
SIGKILL - no cleanup, no atexit, no DuckDB shutdown, exactly the state a
power cut leaves. Creating a cgroup directly reproduces that mechanism on any
Linux machine with cgroup v2, whether or not Docker is installed, which is
why this script does not require Docker.

What it demonstrates, in order:

1. **The kill.** A writer process inside a cgroup with ``memory.max`` and
   ``memory.swap.max=0`` grows until the kernel OOM-kills it. DuckDB's own
   budget (``WQ_DUCKDB_MEMORY_LIMIT``) bounds the *engine* and spills to disk
   when exceeded - which is precisely why a process cap is still needed:
   Python and native allocations live outside the budget.
2. **No torn transaction.** The writer commits numbered batches and journals
   each batch id to a plain file (fsync'd) *after* the DuckDB commit. After
   the kill, the committed batches must be contiguous and the journal must
   agree with the database within one batch (the kill can land between
   commit and journal write - that is the honest boundary, not corruption).
3. **WAL replay.** The child disables automatic checkpointing, so every
   commit lives only in the ``.wal`` file at kill time. Reopening the
   database replays that WAL; the row count must be exactly the committed
   count. A forced checkpoint afterwards must fold the WAL away.
4. **Restart continuation.** A fresh writer process (what ``restart:
   unless-stopped`` does) appends the next batch ids with no duplicates and
   no gap.

Usage::

    sudo -E uv run python scripts/oom_recovery_accept.py \\
        --memory-max 1GB --data /tmp/wq-oom-accept

Root is required to create a cgroup. Everything runs under ``--data``; the
directory is chowned back to the invoking user at the end.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CGROUP_PATH = Path("/sys/fs/cgroup/wq-oom-accept")

#: Under this ceiling the interpreter plus the duckdb import can be OOM-killed
#: before the writer even opens the database, which would prove nothing.
MIN_MEMORY_BYTES = 512 * 1024 * 1024


def _parse_size(text: str) -> int:
    """'1GB' / '512MB' / '268435456' -> bytes."""
    units = {"TB": 1024**4, "GB": 1024**3, "MB": 1024**2, "KB": 1024, "B": 1}
    cleaned = text.strip().upper()
    for suffix, factor in units.items():
        if cleaned.endswith(suffix):
            return int(float(cleaned[: -len(suffix)]) * factor)
    return int(float(cleaned))


def _cgroup_counter(name: str, key: str) -> int:
    """Read a counter from a cgroup control file.

    ``memory.events`` is ``key value`` lines; ``memory.peak`` is a bare
    integer. The ``key`` is matched when present and ignored when the file is
    a single number.
    """
    try:
        text = (CGROUP_PATH / name).read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    return 0


# ---------------------------------------------------------------------------
# child mode: the process that gets OOM-killed
# ---------------------------------------------------------------------------


def run_child(data_dir: Path, row_bytes: int, rows_per_batch: int) -> int:
    """Write committed, journalled batches until killed.

    The engine budget is forced *below* the cgroup cap on purpose: it makes
    DuckDB spill to ``WQ_DUCKDB_TEMP_DIR`` instead of growing past its own
    budget, so the kill is caused by the allocations that live outside the
    engine budget - the exact gap the container limit exists to close.
    """
    os.environ.setdefault("WQ_DUCKDB_MEMORY_LIMIT", "128MB")
    os.environ.setdefault("WQ_DUCKDB_TEMP_DIR", str(data_dir / "spill"))

    # Imported after the env is set so the budget applies.
    from weatherquant.storage import connect

    db_path = data_dir / "weatherquant.duckdb"
    journal = data_dir / "oom-journal.txt"
    con = connect(db_path)
    # Every commit stays in the WAL, so the kill is guaranteed to leave
    # un-checkpointed commits behind and the replay check has teeth.
    con.execute("SET checkpoint_threshold='1TB'")
    con.execute(
        "CREATE TABLE IF NOT EXISTS oom_batches ("
        "batch_id INTEGER, seq INTEGER, payload VARCHAR)"
    )

    start_id = int(con.execute("SELECT COALESCE(MAX(batch_id), 0) FROM oom_batches").fetchone()[0])
    print(f"child: starting at batch {start_id + 1}", flush=True)

    blob = "x" * row_bytes
    held: list[tuple[int, int, str]] = []  # never freed: this is what overflows
    batch_id = start_id
    while True:
        batch_id += 1
        # Every row carries a UNIQUE payload string. A shared blob object
        # would make `held` a list of small tuples pointing at one string,
        # and the heap would grow by megabytes instead of gigabytes - the
        # cgroup would never overflow and nothing would be demonstrated.
        rows = [(batch_id, i, f"{blob}{batch_id}:{i}") for i in range(rows_per_batch)]
        # One batch = one transaction, exactly as CollectorWriter.flush does:
        # a kill mid-batch must roll the whole batch back, never leave half.
        con.execute("BEGIN TRANSACTION")
        try:
            con.executemany(
                "INSERT INTO oom_batches (batch_id, seq, payload) VALUES (?, ?, ?)",
                rows,
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        # The batch stays referenced after the commit. This is the point of
        # the child: DuckDB's own budget is 128MB, so the growth that kills
        # the process comes from the allocations OUTSIDE the engine budget -
        # exactly the gap the container limit exists to close.
        held.extend(rows)
        del rows
        # Journalled strictly after the commit, fsync'd so the kill cannot
        # lose what the database already accepted.
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(f"{batch_id}\n")
            fh.flush()
            os.fsync(fh.fileno())
        if batch_id % 5 == 0:
            print(f"child: committed batch {batch_id}", flush=True)
    return 0  # pragma: no cover - the kernel ends this loop, not us


# ---------------------------------------------------------------------------
# parent mode: cgroup harness + recovery verification
# ---------------------------------------------------------------------------


def _spawn_child(data_dir: Path, row_bytes: int, rows_per_batch: int) -> subprocess.Popen:
    env = dict(os.environ)
    # The child's engine budget is its own decision, never inherited: a 1GB
    # parent budget would let the engine - not the Python heap - fill the
    # cgroup, and the kill would stop demonstrating the right thing.
    env["WQ_DUCKDB_MEMORY_LIMIT"] = "128MB"
    env["WQ_DUCKDB_TEMP_DIR"] = str(data_dir / "spill")
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--data",
            str(data_dir),
            "--child",
            str(data_dir),
            "--row-bytes",
            str(row_bytes),
            "--rows-per-batch",
            str(rows_per_batch),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _dump_tail(proc: subprocess.Popen, limit: int = 15) -> None:
    """Print the child's last log lines - the diagnostics when it dies early."""
    if proc.stdout is None:
        return
    try:
        lines = proc.stdout.readlines()
    except OSError:
        return
    for line in lines[-limit:]:
        print(f"    child: {line.rstrip()}")


def _wait_until_gone(proc: subprocess.Popen, timeout_s: float) -> tuple[bool, str]:
    """Wait for the child to die. Returns (died_on_its_own, note)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True, f"process gone with return code {proc.returncode}"
        time.sleep(0.5)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
        return False, "kernel did not OOM in time; SIGKILLed by the harness"
    return True, f"process gone with return code {proc.returncode}"


def _db_state(db_path: Path) -> tuple[int, int, int]:
    """(max batch_id, row count, journalled batch count).

    Opened **writable, under a budget** - which is what a restarted collector
    does, and not an aesthetic choice: a read-only connection carries no
    engine budget by design, and replaying a multi-gigabyte WAL through one
    grew to 7.8 GB here and pushed this 15 GB host into a *global* OOM. The
    harness must never become the outage it is trying to catch.
    """
    from weatherquant.storage import connect

    con = connect(db_path)
    try:
        max_id = int(con.execute("SELECT COALESCE(MAX(batch_id), 0) FROM oom_batches").fetchone()[0])
        rows = int(con.execute("SELECT COUNT(*) FROM oom_batches").fetchone()[0])
    finally:
        con.close()
    journal = db_path.parent / "oom-journal.txt"
    journalled = 0
    if journal.exists():
        journalled = sum(1 for line in journal.read_text().splitlines() if line.strip())
    return max_id, rows, journalled


def _chown_tree(root: Path, uid: int | None, gid: int | None) -> None:
    if not uid:
        return
    for path in [root, *root.rglob("*")]:
        with contextlib.suppress(OSError):
            os.chown(path, uid, gid or -1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-max", default="1GB", help="cgroup memory.max (default 1GB)")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1200.0, help="kill deadline in seconds")
    parser.add_argument("--row-bytes", type=int, default=1024)
    parser.add_argument("--rows-per-batch", type=int, default=20000)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child is not None:
        return run_child(args.child, args.row_bytes, args.rows_per_batch)

    if os.geteuid() != 0:
        print(
            "root is required to create a cgroup. Re-run with sudo, e.g.\n"
            "  sudo -E uv run python scripts/oom_recovery_accept.py "
            f"--memory-max {args.memory_max} --data {args.data}",
            file=sys.stderr,
        )
        return 2

    # The parent's own recovery connections run under a budget: replaying a
    # multi-GB WAL through the unbounded engine default is how the harness
    # OOMs the very host it is auditing. Line buffering, because a killed
    # harness takes an 8KB stdout buffer - and its evidence - with it.
    os.environ.setdefault("WQ_DUCKDB_MEMORY_LIMIT", "1GB")
    sys.stdout.reconfigure(line_buffering=True)

    data_dir = args.data.resolve()
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    limit = _parse_size(args.memory_max)
    if limit < MIN_MEMORY_BYTES:
        print(
            f"--memory-max {args.memory_max} is below the {MIN_MEMORY_BYTES // (1024 * 1024)}MB "
            "floor: the interpreter would be killed during import, before it "
            "writes anything, which would prove nothing.",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    print(f"oom acceptance: memory.max={args.memory_max} over {data_dir}")

    CGROUP_PATH.mkdir(parents=True, exist_ok=True)
    (CGROUP_PATH / "memory.max").write_text(str(limit))
    (CGROUP_PATH / "memory.swap.max").write_text("0")

    invoker_uid = int(os.environ.get("SUDO_UID", "0")) or None
    invoker_gid = int(os.environ.get("SUDO_GID", "0")) or None

    try:
        # --- the kill ------------------------------------------------------
        proc = _spawn_child(data_dir, args.row_bytes, args.rows_per_batch)
        CGROUP_PATH.joinpath("cgroup.procs").write_text(str(proc.pid))
        died, note = _wait_until_gone(proc, args.timeout)
        if not died or proc.returncode not in (-9, 137):
            # Anything other than a SIGKILL-shaped exit means the child never
            # got to the point of being OOM-killed; show what it said.
            _dump_tail(proc)
        oom_kills = _cgroup_counter("memory.events", "oom_kill")
        peak = _cgroup_counter("memory.peak", "peak") if (CGROUP_PATH / "memory.peak").exists() else 0
        wal = data_dir / "weatherquant.duckdb.wal"
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        print(
            f"  kill:          {note}; kernel oom_kill={oom_kills}; "
            f"peak={peak / 1024 / 1024:.0f} MiB; wal={wal_bytes / 1024:.0f} KiB"
        )
        if not died:
            failures.append("the child had to be SIGKILLed by the harness, not the kernel")
        if oom_kills < 1:
            failures.append("memory.events shows no kernel oom_kill")

        # --- recovery: reopen and verify the committed prefix --------------
        max_id, rows, journalled = _db_state(data_dir / "weatherquant.duckdb")
        print(f"  recovered:     max batch={max_id}, rows={rows}, journalled={journalled}")
        if rows != max_id * args.rows_per_batch:
            failures.append(
                f"row count {rows} does not equal {max_id} committed batches x "
                f"{args.rows_per_batch} rows: a committed transaction was lost or torn"
            )
        if not journalled <= max_id <= journalled + 1:
            failures.append(
                f"journal ({journalled}) and database ({max_id}) disagree by more than "
                "one batch: the committed prefix moved backwards"
            )

        # --- WAL replay: checkpoint must fold the WAL away -----------------
        from weatherquant.storage import connect

        con = connect(data_dir / "weatherquant.duckdb")
        con.execute("CHECKPOINT")
        con.close()
        wal_after = wal.stat().st_size if wal.exists() else 0
        print(
            f"  wal replay:    {wal_bytes / 1024:.0f} KiB -> {wal_after / 1024:.0f} KiB "
            "after checkpoint"
        )
        if wal_bytes > 0 and wal_after >= wal_bytes:
            failures.append("WAL did not shrink on checkpoint: replay did not fold it in")

        # --- restart continuation ------------------------------------------
        child = _spawn_child(data_dir, args.row_bytes, args.rows_per_batch)
        time.sleep(12.0)  # long enough to import and commit several batches
        child.send_signal(signal.SIGTERM)  # what `docker stop` delivers
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
        max_after, rows_after, _ = _db_state(data_dir / "weatherquant.duckdb")
        gained = max_after - max_id
        print(f"  restart:       batches {max_id} -> {max_after} (+{gained}), rows={rows_after}")
        if gained < 1:
            failures.append("the restarted writer committed no new batches")
        if rows_after != max_after * args.rows_per_batch:
            failures.append("restarted writer left a non-contiguous row set")
    finally:
        shutil.rmtree(CGROUP_PATH, ignore_errors=True)
        _chown_tree(data_dir, invoker_uid, invoker_gid)

    print("\n" + "=" * 64)
    if failures:
        print("RESULT: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASSED")
    print("  kernel OOM kill, committed-prefix integrity, WAL replay and")
    print("  restart continuation all verified against the real engine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

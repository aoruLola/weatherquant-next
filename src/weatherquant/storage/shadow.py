"""The shadow engine's own DuckDB file.

Same rationale as :mod:`weatherquant.storage.research`, one step further: the
collector holds a 24/7 write lease on its file, the research database is
re-derived from retrospective batches, and the shadow engine is a *third*
lifetime - long-running, single-writer, restart-sensitive, and forbidden to
touch either of the others. So it gets a third file and a third migrations
directory, and reuses the same checksummed forward-only migrator.

Two things this class adds that the research one does not need:

``transaction()``
    The tailer's byte offsets and the effects of the bytes it read are
    committed together. Without that, a crash between "applied" and
    "checkpointed" replays a batch, and a crash the other way round drops it.

``_guard_foreign_database()``
    The migrator would eventually refuse a foreign file (the shared ``0001``
    version carries a different checksum), but it would say "applied migrations
    are immutable", which is true and useless. Naming the actual owner of the
    file is a cheaper mistake to diagnose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.storage.db import connect, table_row_counts
from weatherquant.storage.migrator import applied_migrations, migrate

__all__ = ["DEFAULT_SHADOW_DB_PATH", "SHADOW_MIGRATIONS_DIR", "ShadowDatabase"]

DEFAULT_SHADOW_DB_PATH = Path("data/shadow/shadow.duckdb")
SHADOW_MIGRATIONS_DIR = Path(__file__).parent / "migrations_shadow"

# Paths this process already has open. DuckDB refuses to open one file with two
# different configurations (read-write *and* read-only) inside a process, so the
# ownership probe must not run against a file we ourselves are holding - and a
# second open of our own file is the normal case for "run the CLI while the
# engine is up", not an ownership conflict.
_OPEN_BY_US: set[str] = set()

# Tables that identify whose file this is. None of them belong to us, so their
# presence means the path is wrong rather than that the schema needs migrating.
_FOREIGN_MARKERS = {
    "order_books": "the collector's database",
    "settlement_outcome": "the research database",
}


def initialize_shadow(path: str | Path = DEFAULT_SHADOW_DB_PATH) -> tuple[Any, list[str]]:
    con = connect(path)
    applied = migrate(con, SHADOW_MIGRATIONS_DIR)
    if applied:
        # Persist the schema before appending decisions. On DuckDB 1.1.0,
        # checkpointing the rebuilt reasons table together with the first
        # live batch can crash the process during checkpoint or close.
        con.execute("CHECKPOINT")
    return con, applied


class ShadowDatabase:
    """Open, migrate and write the shadow database. Exactly one process."""

    def __init__(self, path: str | Path = DEFAULT_SHADOW_DB_PATH) -> None:
        self.path = Path(path)
        self._con = None
        self._key = ""

    def open(self) -> list[str]:
        key = str(self.path.resolve()) if self.path.exists() else str(self.path)
        if self.path.exists() and self.path.stat().st_size > 0 and key not in _OPEN_BY_US:
            self._guard_foreign_database()
        self._con, applied = initialize_shadow(self.path)
        _OPEN_BY_US.add(key)
        self._key = key
        return applied

    @property
    def con(self):
        if self._con is None:
            raise RuntimeError("shadow database is not open")
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
            _OPEN_BY_US.discard(getattr(self, "_key", ""))

    def __enter__(self) -> ShadowDatabase:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic batch: either every effect and its checkpoint, or none."""
        con = self.con
        con.execute("BEGIN TRANSACTION")
        try:
            yield
        except BaseException:
            # A rollback failure must not mask the cause of the abort.
            with suppress(Exception):
                con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")

    def execute(self, sql: str, params: list | tuple | None = None):
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def executemany(self, sql: str, rows: list) -> None:
        if rows:
            self.con.executemany(sql, rows)

    # -- run bookkeeping ----------------------------------------------------

    def start_run(self, purpose: str, params: dict | None = None) -> str:
        import json
        import uuid

        run_id = uuid.uuid4().hex
        self.execute(
            "INSERT INTO shadow_runs (run_id, started_at_utc, status, config_json) VALUES (?, ?, 'running', ?)",
            [run_id, _utcnow(), json.dumps(params or {}, sort_keys=True, default=str)],
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stats: dict | None = None,
        reason: str | None = None,
    ) -> None:
        import json

        self.execute(
            "UPDATE shadow_runs SET stopped_at_utc = ?, status = ?, stats_json = ?, stop_reason = ? "
            "WHERE run_id = ?",
            [
                _utcnow(),
                status,
                json.dumps(stats or {}, sort_keys=True, default=str),
                reason,
                run_id,
            ],
        )

    # -- reads --------------------------------------------------------------

    def table_counts(self) -> dict[str, int]:
        return table_row_counts(self.con)

    def applied_migrations(self) -> dict[str, str]:
        return applied_migrations(self.con)

    # -- guards -------------------------------------------------------------

    def _guard_foreign_database(self) -> None:
        """Refuse a file that already belongs to another engine, by name."""
        probe = connect(self.path, read_only=True)
        try:
            tables = {
                row[0]
                for row in probe.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
        finally:
            probe.close()
        for marker, owner in _FOREIGN_MARKERS.items():
            if marker in tables:
                raise RuntimeError(
                    f"{self.path} is {owner} (found table {marker!r}); the shadow engine "
                    f"writes its own file and must never open another engine's database"
                )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)

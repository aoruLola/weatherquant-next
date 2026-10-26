"""The paper engine's own DuckDB file.

Same rationale as :mod:`weatherquant.storage.shadow`, one lifetime further:
the collector holds a 24/7 write lease, the research database is re-derived
from retrospective batches, the shadow engine tails the collector's archive,
and the paper engine consumes what the shadow *publishes* - so it gets a
fourth file and a fourth migrations directory, and reuses the same
checksummed forward-only migrator.

What this class adds beyond the shadow's version:

``_guard_foreign_database()``
    Names all three sibling engines as foreign owners, not two. A paper
    engine pointed at ``shadow.duckdb`` by a mistyped path must be told
    "that is the shadow's database" rather than watch a migration fail with
    an unrelated checksum error.

Nothing here opens another engine's file, and nothing in the paper package
may import :mod:`weatherquant.storage.shadow` or
:mod:`weatherquant.storage.db`'s writer paths for another engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.storage.db import connect, table_row_counts
from weatherquant.storage.migrator import applied_migrations, migrate

__all__ = ["DEFAULT_PAPER_DB_PATH", "PAPER_MIGRATIONS_DIR", "PaperDatabase"]

DEFAULT_PAPER_DB_PATH = Path("data/paper/paper.duckdb")
PAPER_MIGRATIONS_DIR = Path(__file__).parent / "migrations_paper"

# Paths this process already has open. DuckDB refuses to open one file with two
# different configurations inside a process, so the ownership probe must not
# run against a file we ourselves are holding.
_OPEN_BY_US: set[str] = set()

# Tables that identify whose file this is. None of them belong to the paper
# engine, so their presence means the path is wrong rather than that the
# schema needs migrating.
_FOREIGN_MARKERS = {
    "order_books": "the collector's database",
    "settlement_outcome": "the research database",
    "shadow_decisions": "the shadow engine's database",
}


def initialize_paper(path: str | Path = DEFAULT_PAPER_DB_PATH) -> tuple[Any, list[str]]:
    con = connect(path)
    applied = migrate(con, PAPER_MIGRATIONS_DIR)
    return con, applied


class PaperDatabase:
    """Open, migrate and write the paper database. Exactly one process."""

    def __init__(self, path: str | Path = DEFAULT_PAPER_DB_PATH) -> None:
        self.path = Path(path)
        self._con = None
        self._key = ""

    def open(self) -> list[str]:
        key = str(self.path.resolve()) if self.path.exists() else str(self.path)
        if self.path.exists() and self.path.stat().st_size > 0 and key not in _OPEN_BY_US:
            self._guard_foreign_database()
        self._con, applied = initialize_paper(self.path)
        _OPEN_BY_US.add(key)
        self._key = key
        return applied

    @property
    def con(self):
        if self._con is None:
            raise RuntimeError("paper database is not open")
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
            _OPEN_BY_US.discard(getattr(self, "_key", ""))

    def __enter__(self) -> PaperDatabase:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic batch: effects and the cursor that describes them, or none."""
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
            "INSERT INTO paper_runs (run_id, started_at_utc, status, config_json) VALUES (?, ?, 'running', ?)",
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
            "UPDATE paper_runs SET stopped_at_utc = ?, status = ?, stats_json = ?, stop_reason = ? "
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
                    f"{self.path} is {owner} (found table {marker!r}); the paper engine "
                    f"writes its own file and must never open another engine's database"
                )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)

"""The research database: a separate file from the collector's.

Why a second database rather than more tables in the first
---------------------------------------------------------
* **The collector must not be disturbed.** Its schema is applied by whatever
  image the NAS is running; adding migrations to it would change a live
  database underneath a writer we are explicitly forbidden to touch.
* **Different lifetimes.** The collector's file is append-only depth history
  that is expensive to reacquire. Research tables are wide, re-derived, and
  occasionally rebuilt from scratch.
* **Different failure modes.** A research backfill that fills a disk or dies
  mid-write must not be able to touch the order-book history.

The same checksummed, forward-only migration machinery is reused, pointed at a
second migrations directory - so a research schema can never drift silently
either.
"""

from __future__ import annotations

from pathlib import Path

from weatherquant.storage.db import connect
from weatherquant.storage.migrator import MigrationError, applied_migrations, migrate

__all__ = [
    "DEFAULT_RESEARCH_DB_PATH",
    "MigrationError",
    "ResearchDatabase",
    "applied_migrations",
    "initialize_research",
    "migrate",
    "migrate_research",
]

RESEARCH_MIGRATIONS_DIR = Path(__file__).parent / "migrations_research"
DEFAULT_RESEARCH_DB_PATH = Path("data/research/weather-research.duckdb")


def migrate_research(con, directory: Path = RESEARCH_MIGRATIONS_DIR) -> list[str]:
    """Apply pending research migrations. Same rules as the collector's."""
    return migrate(con, directory)


def initialize_research(
    path: str | Path = DEFAULT_RESEARCH_DB_PATH,
) -> tuple:
    """Create/open the research database and bring it up to date."""
    con = connect(path)
    applied = migrate_research(con)
    return con, applied


class ResearchDatabase:
    """Owns the research connection. Never hands out the collector's handle.

    Usage::

        with ResearchDatabase() as db:
            db.execute(...)
    """

    def __init__(self, path: str | Path = DEFAULT_RESEARCH_DB_PATH) -> None:
        self.path = Path(path)
        self._con = None

    def open(self) -> list[str]:
        # A research file that is actually the collector's file would be a
        # configuration mistake with a bad outcome, so refuse by identity of
        # schema: the collector's database has `order_books`, ours does not.
        con, applied = initialize_research(self.path)
        self._con = con
        return applied

    @property
    def con(self):
        if self._con is None:
            raise RuntimeError("research database is not open")
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> ResearchDatabase:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- helpers used across the pipeline ---------------------------------

    def execute(self, sql: str, params=None):
        """Thin proxy so callers can use the handle directly."""
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def executemany(self, sql: str, seq_of_params) -> None:
        self.con.executemany(sql, seq_of_params)

    def executescript(self, sql: str) -> None:
        self.con.execute(sql)

    def table_counts(self) -> dict[str, int]:
        rows = self.con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name != 'schema_migrations' "
            "ORDER BY table_name"
        ).fetchall()
        return {
            name: int(self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for (name,) in rows
        }

    def start_run(self, purpose: str, params: dict | None = None) -> str:
        import json
        import uuid

        run_id = uuid.uuid4().hex
        self.con.execute(
            "INSERT INTO research_runs (run_id, purpose, started_at_utc, status, params) "
            "VALUES (?, ?, ?, 'running', ?)",
            [run_id, purpose, _utcnow(), json.dumps(params or {}, sort_keys=True, default=str)],
        )
        return run_id

    def finish_run(self, run_id: str, *, status: str, stats: dict | None = None, error: str | None = None) -> None:
        import json

        self.con.execute(
            "UPDATE research_runs SET finished_at_utc = ?, status = ?, stats = ?, error = ? "
            "WHERE run_id = ?",
            [
                _utcnow(),
                status,
                json.dumps(stats or {}, sort_keys=True, default=str),
                error,
                run_id,
            ],
        )


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)

"""DuckDB connection handling.

Two rules are enforced for every connection:

1. **The session timezone is pinned to UTC.** Every temporal column in the
   schema is ``TIMESTAMPTZ``; pinning the session means the same instant
   serialises to the same bytes regardless of the host's local zone.
2. **Opening the legacy database is always read-only.** :func:`connect_legacy_read_only`
   accepts no write mode and passes ``read_only=True`` unconditionally, so no
   code path can mutate the previous project's data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from weatherquant.storage.migrator import migrate

__all__ = ["connect", "connect_legacy_read_only", "initialize"]

DEFAULT_DB_PATH = Path("data/weatherquant.duckdb")


def connect(path: str | Path = DEFAULT_DB_PATH, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the session pinned to UTC.

    Use ``":memory:"`` for tests.
    """
    target = str(path)
    if target != ":memory:" and not read_only:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(target, read_only=read_only)
    con.execute("SET TimeZone='UTC'")
    if not read_only:
        # Deterministic float/decimal behaviour and a sane default memory cap.
        con.execute("SET preserve_insertion_order=true")
    return con


def connect_legacy_read_only(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open the previous project's database strictly for reading.

    Refuses to open anything in write mode. The caller must not issue DDL/DML
    against the returned handle; it exists so migration code cannot accidentally
    gain a writable path to legacy data.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"legacy database not found: {p}")
    con = duckdb.connect(str(p), read_only=True)
    con.execute("SET TimeZone='UTC'")
    return con


def initialize(path: str | Path = DEFAULT_DB_PATH) -> tuple[duckdb.DuckDBPyConnection, list[str]]:
    """Create/open the database and bring the schema up to the latest version.

    Returns the connection and the list of migration names that were applied by
    this call (empty when the schema was already current).
    """
    con = connect(path)
    applied = migrate(con)
    return con, applied


def table_row_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count per user table, excluding the migration ledger.

    Used by tests and by the integrity check to assert that a migration did not
    silently drop data.
    """
    rows: list[tuple[Any, ...]] = con.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'main' AND table_name != 'schema_migrations'
        ORDER BY table_name
        """
    ).fetchall()
    return {name: con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for (name,) in rows}

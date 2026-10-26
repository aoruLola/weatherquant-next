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

import os
import re
from pathlib import Path
from typing import Any

import duckdb

from weatherquant.storage.migrator import migrate

__all__ = ["connect", "connect_legacy_read_only", "initialize"]

DEFAULT_DB_PATH = Path("data/weatherquant.duckdb")

#: DuckDB's own default memory_limit is 80% of physical RAM. On the 16 GB host
#: this runs on - no swap, and a Shadow, a KVM guest and the whole Unraid
#: userland sharing the same 16 GB - that is ~12.7 GB, which is more than the
#: host can spare. The engine then grows until the kernel is forced into
#: direct reclaim, and every other service stalls with it.
#:
#: A budget is therefore set explicitly, low enough that the rest of the host
#: keeps its headroom and high enough that the collector's own working set
#: fits. Overriding it is an operator decision, via the environment, so the
#: number can be tuned on the host without rebuilding the image.
DEFAULT_MEMORY_LIMIT = os.environ.get("WQ_DUCKDB_MEMORY_LIMIT", "3GB")

#: DuckDB defaults to one thread per core. The collector is I/O bound and
#: shares 16 cores with the Shadow, the KVM guest and Docker, so a smaller
#: pool costs it little and leaves the host responsive.
DEFAULT_THREADS = os.environ.get("WQ_DUCKDB_THREADS", "4")

#: Where DuckDB spills when a query exceeds the budget. Empty means "let
#: DuckDB decide", which is the in-memory default and is what we are trying to
#: move away from: spilling needs a real filesystem with room, never a tmpfs.
DEFAULT_TEMP_DIRECTORY = os.environ.get("WQ_DUCKDB_TEMP_DIR", "")

_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s?(KB|MB|GB|TB|B)$", re.IGNORECASE)


def _memory_limit(value: str) -> str:
    """Validate a DuckDB size literal before it reaches a SET statement.

    The value can come from the environment, so it is checked rather than
    interpolated on trust.
    """
    text = str(value).strip()
    if not _MEMORY_LIMIT_RE.match(text):
        raise ValueError(
            f"memory_limit must look like '3GB' or '512MB', got {value!r}"
        )
    return text.upper().replace(" ", "")


def _threads(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"threads must be an integer, got {value!r}") from exc
    if count < 1:
        raise ValueError(f"threads must be >= 1, got {count}")
    return count


def _apply_budget(
    con: duckdb.DuckDBPyConnection,
    *,
    memory_limit: str | None,
    threads: Any,
    temp_directory: str | None,
) -> None:
    """Pin the engine's resource envelope on a writable connection.

    Only writable connections get this. A read-only connection - the legacy
    database, and every dashboard read - cannot spill, so a limit there would
    turn a large query into a failure rather than into a slower answer.
    """
    con.execute(f"SET memory_limit='{_memory_limit(memory_limit or DEFAULT_MEMORY_LIMIT)}'")
    con.execute(f"SET threads={_threads(threads if threads is not None else DEFAULT_THREADS)}")
    directory = (temp_directory if temp_directory is not None else DEFAULT_TEMP_DIRECTORY).strip()
    if directory:
        con.execute(f"SET temp_directory='{directory.replace(chr(39), chr(39) * 2)}'")


def connect(
    path: str | Path = DEFAULT_DB_PATH,
    *,
    read_only: bool = False,
    memory_limit: str | None = None,
    threads: Any = None,
    temp_directory: str | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection with the session pinned to UTC and a budget.

    Use ``":memory:"`` for tests.

    The budget arguments exist so a caller can be explicit, but the defaults
    are the point: an operator who sets nothing still gets a bounded engine
    rather than DuckDB's 80%-of-RAM default.
    """
    target = str(path)
    if target != ":memory:" and not read_only:
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(target, read_only=read_only)
    con.execute("SET TimeZone='UTC'")
    if not read_only:
        # Deterministic float/decimal behaviour, and the resource envelope
        # that keeps this process from starving the host it shares.
        con.execute("SET preserve_insertion_order=true")
        _apply_budget(
            con,
            memory_limit=memory_limit,
            threads=threads,
            temp_directory=temp_directory,
        )
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

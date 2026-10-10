"""DuckDB connection management, schema and versioned migrations."""

from weatherquant.storage.db import connect, connect_legacy_read_only, initialize
from weatherquant.storage.migrator import MigrationError, applied_migrations, migrate

__all__ = [
    "MigrationError",
    "applied_migrations",
    "connect",
    "connect_legacy_read_only",
    "initialize",
    "migrate",
]

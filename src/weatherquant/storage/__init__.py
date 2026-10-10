"""DuckDB connection management, schema and versioned migrations."""

from weatherquant.storage.db import connect, connect_legacy_read_only, initialize
from weatherquant.storage.migrator import MigrationError, applied_migrations, migrate
from weatherquant.storage.research import (
    DEFAULT_RESEARCH_DB_PATH,
    ResearchDatabase,
    migrate_research,
)

__all__ = [
    "DEFAULT_RESEARCH_DB_PATH",
    "MigrationError",
    "ResearchDatabase",
    "applied_migrations",
    "connect",
    "connect_legacy_read_only",
    "initialize",
    "migrate",
    "migrate_research",
]

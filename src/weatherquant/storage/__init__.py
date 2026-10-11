"""DuckDB connection management, schema and versioned migrations."""

from weatherquant.storage.db import connect, connect_legacy_read_only, initialize
from weatherquant.storage.migrator import MigrationError, applied_migrations, migrate
from weatherquant.storage.research import (
    DEFAULT_RESEARCH_DB_PATH,
    ResearchDatabase,
    migrate_research,
)
from weatherquant.storage.shadow import (
    DEFAULT_SHADOW_DB_PATH,
    SHADOW_MIGRATIONS_DIR,
    ShadowDatabase,
    initialize_shadow,
)

__all__ = [
    "DEFAULT_RESEARCH_DB_PATH",
    "DEFAULT_SHADOW_DB_PATH",
    "SHADOW_MIGRATIONS_DIR",
    "MigrationError",
    "ResearchDatabase",
    "ShadowDatabase",
    "applied_migrations",
    "connect",
    "connect_legacy_read_only",
    "initialize",
    "initialize_shadow",
    "migrate",
    "migrate_research",
]

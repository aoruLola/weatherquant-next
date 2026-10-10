"""Forward-only, checksummed SQL migrations.

Design
------
* Migrations are plain ``.sql`` files in ``storage/migrations/``, named
  ``NNNN_snake_case_description.sql`` and applied in lexicographic order.
* Each applied migration is recorded in ``schema_migrations`` together with a
  SHA-256 of its content. Re-running is a no-op; a *changed* file with an
  already-recorded version is a hard error, because silently diverging schemas
  are exactly how a reproducible research database stops being reproducible.
* Every migration runs inside a transaction. A failure leaves no partial schema.
* There are no down-migrations. Reproducibility comes from re-running the
  pipeline from raw data, not from editing history.

The ledger table is created outside the migration set (bootstrap) so that the
first migration can rely on it existing.
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass
from pathlib import Path

import duckdb

__all__ = ["MigrationError", "MigrationSpec", "applied_migrations", "available_migrations", "migrate"]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    checksum    VARCHAR NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class MigrationError(RuntimeError):
    """A migration could not be applied, or the recorded state is inconsistent."""


@dataclass(frozen=True, slots=True)
class MigrationSpec:
    version: str
    name: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def available_migrations(directory: Path = MIGRATIONS_DIR) -> list[MigrationSpec]:
    """Load every migration file, validated and ordered by version."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    specs: list[MigrationSpec] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        if "_" not in stem:
            raise MigrationError(f"migration filename must be NNNN_name.sql, got {path.name!r}")
        version, _, name = stem.partition("_")
        if not version.isdigit():
            raise MigrationError(f"migration version must be numeric, got {version!r} in {path.name!r}")
        if version in seen:
            raise MigrationError(f"duplicate migration version {version}")
        seen.add(version)
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"migration {path.name} is empty")
        specs.append(MigrationSpec(version=version, name=name, path=path, sql=sql))

    if not specs:
        raise MigrationError(f"no .sql migrations found in {directory}")
    versions = [s.version for s in specs]
    if versions != sorted(versions, key=int):
        raise MigrationError(f"migration versions are not contiguous/sorted: {versions}")
    return specs


def applied_migrations(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Map of applied version -> checksum, from the ledger table."""
    rows = con.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    return {version: checksum for version, checksum in rows}


def migrate(con: duckdb.DuckDBPyConnection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the names applied by this call."""
    con.execute(_BOOTSTRAP)
    specs = available_migrations(directory)
    applied = applied_migrations(con)
    done: list[str] = []

    for spec in specs:
        existing = applied.get(spec.version)
        if existing is not None:
            if existing != spec.checksum:
                raise MigrationError(
                    f"migration {spec.version}_{spec.name} was already applied with checksum "
                    f"{existing[:12]} but its content now hashes to {spec.checksum[:12]}. "
                    "Applied migrations are immutable - add a new migration instead of editing history."
                )
            continue

        try:
            con.execute("BEGIN TRANSACTION")
            con.execute(spec.sql)
            con.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                [spec.version, spec.name, spec.checksum],
            )
            con.execute("COMMIT")
        except Exception as exc:
            with contextlib.suppress(duckdb.Error):
                con.execute("ROLLBACK")
            raise MigrationError(f"migration {spec.version}_{spec.name} failed: {exc}") from exc

        done.append(f"{spec.version}_{spec.name}")

    return done

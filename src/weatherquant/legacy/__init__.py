"""Read-only import of data captured by the previous WeatherQuant project.

Contract (spec §四)
-------------------
* The legacy database is opened **read-only** and never written, modified,
  cleaned or vacuumed. :func:`weatherquant.storage.connect_legacy_read_only`
  accepts no write mode.
* Every migrated row keeps its **source** (which legacy table, which file) and
  its **original capture time** (the legacy ``fetched_at`` / ``computed_at``
  columns, never the import time).
* Data that fails a reliability rule is tagged
  ``pending_verification`` - it is carried over, but never auto-trusted.
* The old probability model, signal logic and paper ledger are **not**
  inherited. Only raw/reference data moves across; see :data:`LEGACY_TABLES`
  for exactly which tables qualify.
"""

from weatherquant.legacy.importer import ImportPlan, LegacySource, plan_import
from weatherquant.legacy.rules import (
    LEGACY_TABLES,
    MigratableTable,
    ValueTier,
    VerificationStatus,
    classify,
)

__all__ = [
    "LEGACY_TABLES",
    "ImportPlan",
    "LegacySource",
    "MigratableTable",
    "ValueTier",
    "VerificationStatus",
    "classify",
    "plan_import",
]

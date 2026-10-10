"""Upstream data collection: clients, raw archive, persistence and health.

Nothing in this package places orders or authenticates against the venue. Every
client here is read-only public API surface.
"""

from weatherquant.data.archive import ArchiveRecord, RawArchive, extract_venue_ts
from weatherquant.data.heartbeat import (
    CollectionStats,
    HealthStatus,
    HealthVerdict,
    Heartbeat,
    HeartbeatWriter,
    evaluate_health,
    read_heartbeat,
)
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.data.ratelimit import RateLimitError, RateLimitRegistry, SlidingWindowLimiter
from weatherquant.data.writer import (
    BackupReport,
    CollectorWriter,
    IntegrityReport,
    ParquetExportReport,
    book_content_hash,
    canonical_levels_json,
    next_event_id,
)

__all__ = [
    "ArchiveRecord",
    "BackupReport",
    "CollectionStats",
    "CollectorWriter",
    "HealthStatus",
    "HealthVerdict",
    "Heartbeat",
    "HeartbeatWriter",
    "IntegrityReport",
    "LockError",
    "ParquetExportReport",
    "RateLimitError",
    "RateLimitRegistry",
    "RawArchive",
    "SingleInstanceLock",
    "SlidingWindowLimiter",
    "book_content_hash",
    "canonical_levels_json",
    "evaluate_health",
    "extract_venue_ts",
    "next_event_id",
    "read_heartbeat",
]

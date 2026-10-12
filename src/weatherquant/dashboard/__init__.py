"""WeatherQuant dashboard: read-only web UI over published snapshots.

Three rules the whole package is built around:

1. **Never open a DuckDB file.** Collector and Shadow each hold an exclusive
   lease on theirs for their whole life; a second process is refused even
   read-only. Everything here reads small JSON files the engines publish
   atomically, plus a *bounded* tail of the raw JSONL archive.
2. **Never invent data.** A missing producer lands as an explicit
   ``unavailable`` state with a reason, never as a plausible-looking default.
3. **Freshness is independent of health.** The dashboard answering 200 says
   nothing about whether the collector is collecting; every payload carries
   its own freshness verdict.
"""

from __future__ import annotations

__all__ = ["API_SCHEMA_VERSION"]

#: Version of the HTTP API contract (response envelopes, field names).
#: Bumped only for breaking changes; additive fields do not bump it.
API_SCHEMA_VERSION = 1

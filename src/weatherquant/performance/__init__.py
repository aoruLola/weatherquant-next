"""Performance analytics: edge, opportunity episodes, settlement, win rate.

The whole package reads the shadow's decision history and the research
settlement tables. It never opens a database the shadow engine holds a lease
on, never writes to the collector's archive, and never places an order - the
numbers here are model expectations and verified settlements, nothing else.

Two engines read that history, and they publish the same six snapshots:

* ``build_performance`` holds the whole history in memory. It is the reference
  implementation, and it is the thing the streaming engine is checked against.
* ``streaming.build_performance_streaming`` reads the export one part at a time
  and keeps its state in a DuckDB file of its own
  (``docs/performance-streaming-audit.md``). That file is research state, not
  the engine's database, and it is derived data: deleting it costs a rebuild,
  nothing else.

Single source of truth: docs/performance-data-contract.md. If this package
and that document disagree, one of them is wrong and the document wins.
"""

from __future__ import annotations

__all__ = ["PERFORMANCE_SCHEMA_VERSION"]

#: Version of the performance contract (snapshot files, API fields, MCP
#: envelopes). Bumped only for breaking changes; additive fields do not bump it.
PERFORMANCE_SCHEMA_VERSION = 1

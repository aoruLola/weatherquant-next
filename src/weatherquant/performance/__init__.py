"""Performance analytics: edge, opportunity episodes, settlement, win rate.

The whole package reads the shadow's decision history and the research
settlement tables. It never opens a database the shadow engine holds a lease
on, never writes to the collector's archive, and never places an order - the
numbers here are model expectations and verified settlements, nothing else.

Single source of truth: docs/performance-data-contract.md. If this package
and that document disagree, one of them is wrong and the document wins.
"""

from __future__ import annotations

__all__ = ["PERFORMANCE_SCHEMA_VERSION"]

#: Version of the performance contract (snapshot files, API fields, MCP
#: envelopes). Bumped only for breaking changes; additive fields do not bump it.
PERFORMANCE_SCHEMA_VERSION = 1

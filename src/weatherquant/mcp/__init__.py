"""WeatherQuant MCP gateway: a read-only window for AI clients.

The gateway does one thing and refuses several. It calls the dashboard's
own REST API and relays what comes back with the contract's envelope
attached. It does not open a database, does not read a file, does not
accept SQL, does not place an order, and will not fetch an arbitrary URL.

Those refusals are the point. An MCP server is a program an outside model
can drive, so every capability it offers is a capability a confused or
hostile prompt can aim at anything. Narrowing the surface to "these thirteen
reads against one configured host" is what makes it safe to connect.

Contract: docs/performance-data-contract.md sections 8 and 12.
"""

from __future__ import annotations

__all__ = ["GATEWAY_VERSION"]

#: Version of this gateway, reported in every tool response so a client can
#: tell which build answered it.
GATEWAY_VERSION = "0.1.4"

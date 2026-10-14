"""Gateway entrypoint: stdio for a local agent, HTTP for a remote client.

Two transports, one server. ``stdio`` is what Claude Code, Codex and a
local agent use - the process is the client's child, so no socket is
exposed and no inbound token is needed. ``streamable-http`` is what a
hosted client uses, and it binds to a host the operator chooses.

Two addresses are configured here and they are not the same thing:

``--host``                     where we **listen**
``WQ_MCP_DASHBOARD_URL``       where we **send**

The first used to be validated as if it were the second. A container
configured with ``0.0.0.0`` therefore refused to start, because the
"no plaintext outbound" rule was applied to a bind address that never
leaves the container. They are validated separately now: listening on
``0.0.0.0`` is normal inside Docker, and the port mapping decides what
the outside world reaches.

Authentication on HTTP is enforced in front of routing
(:mod:`weatherquant.mcp.http_app`), so a caller without the bearer never
reaches a tool. The inbound bearer and the dashboard credential are two
different settings - neither defaults to the other, and neither is ever
forwarded to the other's owner.

Environment:

``WQ_MCP_DASHBOARD_URL``            where the dashboard lives (required)
``WQ_MCP_TOKEN``                    **inbound** bearer token for MCP clients
``WQ_MCP_DASHBOARD_TOKEN``          **outbound** credential for the dashboard
``WQ_MCP_PLAINTEXT_NETWORKS``       CIDRs reachable over http, comma separated
``WQ_MCP_ALLOWED_ORIGINS``          comma-separated browser origins
``WQ_MCP_ALLOWED_HOSTS``            Host header values, for DNS-rebinding checks
``WQ_MCP_HOST``                     bind address, default 127.0.0.1
``WQ_MCP_PORT``                     bind port, default 8788
``WQ_MCP_RATE``                     calls per minute per caller, default 60
``WQ_MCP_TRANSPORT``                stdio | streamable-http, default stdio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from weatherquant.mcp.client import DashboardClient, _is_loopback
from weatherquant.mcp.security import RequestGuard
from weatherquant.mcp.server import Gateway, build_server

__all__ = ["build_gateway", "main"]

log = logging.getLogger("weatherquant.mcp")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788


def build_gateway(
    *,
    base_url: str | None = None,
    dashboard_token: str | None = None,
    inbound_token: str | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    rate_limit_per_minute: int = 60,
    max_concurrent: int = 4,
    timeout_seconds: float = 20.0,
    plaintext_networks: Sequence[str] | str = (),
) -> Gateway:
    """Assemble the gateway from configuration, validating as it goes.

    ``inbound_token`` guards callers talking to us; ``dashboard_token`` is
    what we present when talking to the dashboard. They are separate
    parameters on purpose: passing one where the other belongs would either
    leak our upstream credential to every caller or forward an attacker's
    bearer to the dashboard.
    """
    url = base_url or os.environ.get("WQ_MCP_DASHBOARD_URL", "")
    if not url:
        raise SystemExit(
            "WQ_MCP_DASHBOARD_URL is required: the gateway only ever talks to "
            "one configured dashboard, never to a URL a caller supplies"
        )
    outbound = (
        dashboard_token
        if dashboard_token is not None
        else os.environ.get("WQ_MCP_DASHBOARD_TOKEN") or None
    )
    inbound = inbound_token if inbound_token is not None else os.environ.get("WQ_MCP_TOKEN") or None
    networks = plaintext_networks or os.environ.get("WQ_MCP_PLAINTEXT_NETWORKS", "")

    client = DashboardClient(
        url,
        timeout_seconds=timeout_seconds,
        token=outbound,
        allow_plaintext_networks=networks,
    )
    guard = RequestGuard(
        token=inbound,
        allowed_origins=allowed_origins,
        rate_limit_per_minute=rate_limit_per_minute,
        max_concurrent=max_concurrent,
    )
    return Gateway(client, guard)


def _split(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _allowed_hosts(host: str, port: int) -> list[str]:
    """Host header values a client may legitimately send.

    A bound address of ``0.0.0.0`` is never a Host header - a client
    connects to a real name or address. All three forms are accepted so a
    health probe and a browser agree about who we are.
    """
    return sorted({f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weatherquant-mcp")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("WQ_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("WQ_MCP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WQ_MCP_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    origins = _split(os.environ.get("WQ_MCP_ALLOWED_ORIGINS"))
    rate = int(os.environ.get("WQ_MCP_RATE", "60"))
    gateway = build_gateway(allowed_origins=frozenset(origins), rate_limit_per_minute=rate)
    server = build_server(gateway)

    if args.transport == "stdio":
        server.run("stdio")
        return 0

    # Listen on what the operator asked for. A non-loopback bind needs a
    # token: an unauthenticated gateway on a LAN address is an audience
    # nobody chose. Nothing here treats the bind address as a destination.
    if not _is_loopback(args.host):
        gateway.guard.require_token()
        log.warning(
            "binding %s:%d - the port mapping decides what reaches this "
            "gateway. Put an authenticating TLS reverse proxy in front of it; "
            "this process does not terminate TLS.",
            args.host,
            args.port,
        )

    from weatherquant.mcp.http_app import run_http

    run_http(
        server,
        guard=gateway.guard,
        host=args.host,
        port=args.port,
        allowed_hosts=_allowed_hosts(args.host, args.port),
        allowed_origins=origins,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

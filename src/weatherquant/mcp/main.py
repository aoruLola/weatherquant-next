"""Gateway entrypoint: stdio for a local agent, HTTP for a remote client.

Two transports, one server. ``stdio`` is what Claude Code, Codex and a
local agent use - the process is the client's child, so no socket is
exposed and no token is needed. ``streamable-http`` is what a hosted client
uses, and it binds to a host the operator chooses.

The binding rule is the whole safety story in one place: a non-loopback
address requires a token, and a plaintext non-loopback address is refused
by the client before a request can be made. An unauthenticated gateway on
a LAN address is how research data acquires an audience nobody chose.

Environment:

``WQ_MCP_DASHBOARD_URL``   where the dashboard lives (required)
``WQ_MCP_TOKEN``           bearer token; required off loopback
``WQ_MCP_ALLOWED_ORIGINS`` comma-separated browser origins
``WQ_MCP_HOST``            bind address, default 127.0.0.1
``WQ_MCP_PORT``            bind port, default 8788
``WQ_MCP_RATE``            calls per minute per client, default 60
``WQ_MCP_TRANSPORT``       stdio | streamable-http, default stdio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from weatherquant.mcp.client import DashboardClient
from weatherquant.mcp.security import RequestGuard
from weatherquant.mcp.server import Gateway, build_server

__all__ = ["build_gateway", "main"]

log = logging.getLogger("weatherquant.mcp")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788


def build_gateway(
    *,
    base_url: str | None = None,
    token: str | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    rate_limit_per_minute: int = 60,
    max_concurrent: int = 4,
    timeout_seconds: float = 20.0,
) -> Gateway:
    """Assemble the gateway from configuration, validating as it goes."""
    url = base_url or os.environ.get("WQ_MCP_DASHBOARD_URL", "")
    if not url:
        raise SystemExit(
            "WQ_MCP_DASHBOARD_URL is required: the gateway only ever talks to "
            "one configured dashboard, never to a URL a caller supplies"
        )
    secret = token if token is not None else os.environ.get("WQ_MCP_TOKEN") or None
    client = DashboardClient(url, timeout_seconds=timeout_seconds, token=secret)
    guard = RequestGuard(
        token=secret,
        allowed_origins=allowed_origins,
        rate_limit_per_minute=rate_limit_per_minute,
        max_concurrent=max_concurrent,
    )
    return Gateway(client, guard)


def _split_origins(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _is_loopback_host(host: str) -> bool:
    from weatherquant.mcp.client import _is_loopback

    return _is_loopback(host)


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

    origins = _split_origins(os.environ.get("WQ_MCP_ALLOWED_ORIGINS"))
    rate = int(os.environ.get("WQ_MCP_RATE", "60"))
    gateway = build_gateway(allowed_origins=origins, rate_limit_per_minute=rate)
    server = build_server(gateway)

    if args.transport == "stdio":
        server.run("stdio")
        return 0

    # HTTP: refuse to expose an unauthenticated gateway beyond loopback.
    if not _is_loopback_host(args.host):
        gateway.guard.require_token()
        from weatherquant.mcp.client import _validate_base_url

        # Same rule as the client: research data does not travel in clear text
        # once it leaves this machine.
        _validate_base_url(f"http://{args.host}")
        log.warning(
            "binding %s:%d - ensure an authenticating TLS reverse proxy sits in "
            "front of this; the gateway itself does not terminate TLS",
            args.host,
            args.port,
        )

    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

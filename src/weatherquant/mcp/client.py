"""A read-only HTTP client bound to one configured host.

Three properties are enforced here rather than trusted:

**One base URL.** Every request is built from the configured origin plus a
path from a fixed allowlist. The caller never supplies a URL, so a prompt
that asks the model to "just fetch this" has nowhere to put it. A base URL
that is not https is refused outright unless it is loopback - a gateway
that will happily post research data over plaintext is a gateway that
eventually does.

**Bounded output.** Responses are size- and time-limited. An unbounded
read against an endpoint that can return a large page turns one tool call
into a denial of service against the caller's own context window.

**No credentials in logs.** Errors carry the status and the path, never the
authorization header - a gateway whose audit log contains its own bearer
token has protected nothing.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

__all__ = [
    "ALLOWED_PATHS",
    "ALLOWED_PATH_PATTERNS",
    "ClientError",
    "DashboardClient",
    "TimeoutError",
    "resolve_path",
]

#: The only paths this gateway will ever request. Adding one is a decision,
#: not an accident, because it has to be written down here first.
ALLOWED_PATHS = frozenset(
    {
        "/api/v1/overview",
        "/api/v1/collector/health",
        "/api/v1/shadow/health",
        "/api/v1/shadow/report",
        "/api/v1/markets",
        "/api/v1/forecasts",
        "/api/v1/decisions",
        "/api/v1/performance/summary",
        "/api/v1/performance/edge",
        "/api/v1/performance/win-rate",
        "/api/v1/performance/opportunities",
        "/api/v1/performance/settlements",
        "/api/v1/performance/models",
        "/api/v1/performance/reference-capital",
        "/healthz",
    }
)

#: Routes with one interpolated segment. An exact set cannot express
#: ``/api/v1/markets/<id>`` - matching it by substring would also admit
#: ``/api/v1/markets/../shadow/report``, so the pattern is anchored and the
#: segment is restricted to characters that cannot form a traversal.
#:
#: Dots are excluded deliberately: a market id is an integer, and ``..`` is
#: the whole reason a naive prefix check is unsafe.
ALLOWED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/v1/markets/[A-Za-z0-9_-]{1,64}$"),
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Resolver is injectable so the network-boundary check is testable without
#: touching real DNS - and so a test can prove a name resolving outside the
#: trusted networks is refused.
Resolver = Callable[[str], list[str]]


def _dns_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos})


def resolve_path(path: str) -> str:
    """Return ``path`` if the gateway is allowed to request it.

    Two shapes are accepted: an exact member of the allowlist, or a pattern
    from ``ALLOWED_PATH_PATTERNS``. Anything containing a percent sign, a
    query, a fragment or a dot is refused before matching - those are the
    characters that turn an anchored pattern into a bypass, and none of them
    appear in any path this gateway legitimately builds.
    """
    if not path:
        raise ValueError("path must not be empty")
    if any(ch in path for ch in ("%", "?", "#")):
        raise ValueError(
            f"path {path!r} may not contain percent-encoding, a query or a fragment"
        )
    if path in ALLOWED_PATHS:
        return path
    for pattern in ALLOWED_PATH_PATTERNS:
        if pattern.match(path):
            return path
    raise ValueError(
        f"path {path!r} is not in the gateway's allowlist; add it to "
        "ALLOWED_PATHS or ALLOWED_PATH_PATTERNS deliberately, not here"
    )


class ClientError(Exception):
    """The dashboard could not be reached or answered with an error."""


class TimeoutError(ClientError):
    """The dashboard did not answer within the budget."""


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_networks(raw: Sequence[str] | str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    text = raw if isinstance(raw, str) else ",".join(raw)
    networks = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid plaintext network {part!r}: {exc}") from exc
    return tuple(networks)


def _validate_base_url(
    base_url: str,
    *,
    allow_plaintext_networks: Sequence[str] | str = (),
    resolver: Resolver | None = None,
) -> str:
    """Validate the *upstream target* the gateway will call.

    This is a destination, not a bind address. A listener on ``0.0.0.0`` is
    a normal way to accept connections inside a container; validating it as
    if it were a URL we intended to fetch from is a category error that
    stops the shipped image from starting.

    Plaintext to a non-loopback host is allowed only when the operator has
    explicitly declared the internal networks where it may happen, *and*
    the name resolves inside one of them. Declaring nothing means refusing
    everything - a Docker network is a convenience, not a security boundary,
    so trust is granted per CIDR rather than assumed from the topology.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme {parts.scheme!r} in base URL")
    if not parts.hostname:
        raise ValueError("base URL must include a host")
    if parts.path and parts.path not in {"", "/"}:
        raise ValueError(
            "base URL must be an origin, not a path - the gateway appends "
            "its own allowlisted paths"
        )
    if parts.scheme == "https" or _is_loopback(parts.hostname):
        return base_url.rstrip("/")

    networks = _parse_networks(allow_plaintext_networks)
    if not networks:
        raise ValueError(
            "refusing a plaintext base URL that is not loopback: performance "
            "and settlement data would cross the network unencrypted. Set "
            "WQ_MCP_PLAINTEXT_NETWORKS to the internal CIDRs this gateway "
            "may reach over http (for example 172.16.0.0/12), or use https."
        )

    resolve = resolver or _dns_resolver
    addresses = resolve(parts.hostname)
    if not addresses:
        raise ValueError(
            f"could not resolve {parts.hostname!r} to check it against the "
            "configured plaintext networks"
        )

    candidates: list[ipaddress._BaseAddress] = []
    for address in addresses:
        try:
            candidates.append(ipaddress.ip_address(address.split("%")[0]))
        except ValueError:
            continue
    if not candidates:
        raise ValueError(f"resolved {parts.hostname!r} to no usable address")

    for candidate in candidates:
        for network in networks:
            if candidate in network:
                return base_url.rstrip("/")

    raise ValueError(
        f"{parts.hostname!r} resolved to {', '.join(map(str, candidates))}, which is "
        f"outside the configured plaintext networks "
        f"({', '.join(str(n) for n in networks)}); refusing to send data there in clear text"
    )


class DashboardClient:
    """Calls the dashboard. Owns one httpx client and no opinions about data."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        token: str | None = None,
        user_agent: str = "weatherquant-mcp/0.1.4",
        allow_plaintext_networks: Sequence[str] | str = (),
        resolver: Resolver | None = None,
    ) -> None:
        self.base_url = _validate_base_url(
            base_url,
            allow_plaintext_networks=allow_plaintext_networks,
            resolver=resolver,
        )
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if token:
            # This is the *outbound* credential - what we present to the
            # dashboard. It is a different secret from the bearer token an
            # inbound MCP client presents to us, and it is never copied from
            # one to the other. Never logged, never echoed in an error.
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,  # a redirect is how a host becomes another host
        )

    def __enter__(self) -> DashboardClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one allowlisted path and return its parsed body.

        Raises ``ClientError`` carrying the status and path only. The
        Authorization header is deliberately absent from every message: an
        audit trail is more useful than a token is secret, but not at the
        price of the token.
        """
        # Resolved here rather than trusted: the allowlist is the only thing
        # standing between a tool argument and an outbound request.
        resolve_path(path)
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"dashboard did not answer within {self.timeout_seconds:g}s: {path}") from exc
        except httpx.HTTPError as exc:
            raise ClientError(f"request to {path} failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise ClientError(f"dashboard returned {response.status_code} for {path}")

        body = response.content
        if len(body) > self.max_response_bytes:
            raise ClientError(
                f"response for {path} is {len(body)} bytes, over the "
                f"{self.max_response_bytes}-byte limit"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ClientError(f"dashboard returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise ClientError(f"dashboard returned a non-object body for {path}")
        return payload

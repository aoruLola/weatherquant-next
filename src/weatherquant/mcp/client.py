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
from typing import Any
from urllib.parse import urlsplit

import httpx

__all__ = [
    "ALLOWED_PATHS",
    "ClientError",
    "DashboardClient",
    "TimeoutError",
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

#: Refuse to send research data over plaintext, except to loopback where
#: there is no network to observe it.
_ALLOW_PLAINTEXT_LOOPBACK = True

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


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


def _validate_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme {parts.scheme!r} in base URL")
    if not parts.hostname:
        raise ValueError("base URL must include a host")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError(
            "refusing a plaintext base URL that is not loopback: performance "
            "and settlement data would cross the network unencrypted"
        )
    if parts.path and parts.path not in {"", "/"}:
        raise ValueError(
            "base URL must be an origin, not a path - the gateway appends "
            "its own allowlisted paths"
        )
    return base_url.rstrip("/")


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
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        if token:
            # Never logged, never echoed in an error, never returned.
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
        if path not in ALLOWED_PATHS:
            raise ValueError(
                f"path {path!r} is not in the gateway's allowlist; add it to "
                "weatherquant.mcp.client.ALLOWED_PATHS deliberately, not here"
            )
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

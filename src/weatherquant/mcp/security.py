"""The gateway's defences: what it checks before it answers anything.

An MCP server is reachable by a model that can be prompted by anyone who
can reach the socket. So every check here answers a question an attacker
gets to ask, and each one fails closed.

**Origin.** A browser will happily post a cross-origin request with the
user's cookie attached. Validating ``Origin`` against an allowlist stops a
malicious page from driving the gateway through a visitor's session.

**Token.** A shared secret, compared in constant time. Rotation is a
restart with a new value: there is no state to migrate and no old token
that keeps working after the env changes.

**Rate and concurrency.** One model can emit tool calls in a loop without
noticing. A small sliding window and a concurrency ceiling turn a runaway
loop into a rejected call rather than a saturated host.

**Audit.** Every call is recorded with its tool, outcome and duration.
The token never appears - not in the record, not in an error, not in a
traceback. A log that contains its own credential is not an audit trail,
it is a credential store.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AuthError",
    "OriginError",
    "RateLimitError",
    "RequestGuard",
    "audit_event",
]

log = logging.getLogger("weatherquant.mcp.audit")


class OriginError(Exception):
    """The request's Origin is not on the allowlist."""


class AuthError(Exception):
    """Missing or incorrect bearer token."""


class RateLimitError(Exception):
    """Too many calls from this client in the window."""


@dataclass(slots=True)
class _WindowState:
    stamps: deque[float] = field(default_factory=deque)
    in_flight: int = 0


class RequestGuard:
    """Checks every request before a tool runs.

    ``allowed_origins`` may be empty, which means "no browser origin is
    accepted" - correct for a stdio client and for a gateway reached only by
    server-side agents. A browser cannot send ``Origin: null`` usefully
    either, so that is refused too.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        allowed_origins: frozenset[str] = frozenset(),
        rate_limit_per_minute: int = 60,
        max_concurrent: int = 4,
    ) -> None:
        if rate_limit_per_minute <= 0:
            raise ValueError("rate_limit_per_minute must be positive")
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._token = token
        self._allowed_origins = frozenset(allowed_origins)
        self._rate = rate_limit_per_minute
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._state: dict[str, _WindowState] = {}

    def check_origin(self, origin: str | None) -> None:
        if origin is None or origin == "":
            return  # non-browser client
        if origin == "null":
            raise OriginError("opaque origins are not accepted")
        if origin not in self._allowed_origins:
            raise OriginError(f"origin {origin!r} is not on the allowlist")

    def check_token(self, presented: str | None) -> None:
        """Constant-time comparison; a missing configured token means open.

        An unset token is a local-development convenience, not a
        production mode. ``RequestGuard.require_token`` is what a deployment
        should call, and the gateway refuses to bind a non-loopback address
        without one.
        """
        if self._token is None:
            return
        if presented is None or presented == "":
            raise AuthError("missing bearer token")
        if not hmac.compare_digest(presented, self._token):
            raise AuthError("incorrect bearer token")

    def acquire(self, client_key: str) -> None:
        now = time.monotonic()
        with self._lock:
            state = self._state.setdefault(client_key, _WindowState())
            while state.stamps and now - state.stamps[0] > 60.0:
                state.stamps.popleft()
            if len(state.stamps) >= self._rate:
                raise RateLimitError(
                    f"rate limit of {self._rate} calls per minute exceeded"
                )
            if state.in_flight >= self._max_concurrent:
                raise RateLimitError(
                    f"concurrency limit of {self._max_concurrent} in-flight calls exceeded"
                )
            state.stamps.append(now)
            state.in_flight += 1

    def release(self, client_key: str) -> None:
        with self._lock:
            state = self._state.get(client_key)
            if state is not None and state.in_flight > 0:
                state.in_flight -= 1

    def require_token(self) -> None:
        if self._token is None:
            raise ValueError(
                "no token configured; a gateway reachable by anything other "
                "than loopback must be given one"
            )


def audit_event(
    *,
    tool: str,
    outcome: str,
    duration_ms: float,
    client: str,
    detail: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> None:
    """Record one call.

    ``arguments`` is included because a rejected query is the one worth
    reconstructing later, but callers must not pass anything secret in it.
    The token is never accepted as a parameter here, which is how it stays
    out of the log.
    """
    log.info(
        "mcp_call tool=%s outcome=%s duration_ms=%.1f client=%s detail=%s arguments=%s",
        tool,
        outcome,
        duration_ms,
        client,
        detail or "-",
        _redact(arguments or {}),
    )


#: Keys whose values are never written to a log, whatever a caller passes.
_SENSITIVE = frozenset({"token", "authorization", "api_key", "password", "secret"})


def _redact(arguments: dict[str, Any]) -> str:
    if not arguments:
        return "-"
    safe = {k: ("<redacted>" if k.lower() in _SENSITIVE else v) for k, v in arguments.items()}
    return str(safe)[:500]

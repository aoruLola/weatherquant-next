"""The HTTP transport, with authentication in front of it.

The gateway's guards were written long before this file, and for a while
nothing called them: a unit test could prove ``check_token`` rejected a bad
bearer while the running server accepted every request. That is the gap
this closes. Every inbound request passes through one middleware that
authenticates *before* Starlette has routed it, so a rejected caller never
reaches a tool, never reaches the dashboard, and never consumes a
rate-limit slot.

Three decisions worth stating:

**The inbound bearer is not the outbound credential.** A caller presenting
a token to us must not have it forwarded to the dashboard as though it were
the dashboard's own. They are separate settings, and neither defaults to
the other.

**Origin is checked here, not only by the SDK.** The MCP SDK validates
Host/Origin for DNS-rebinding protection, which is protocol plumbing. This
middleware runs the application's own ``RequestGuard`` as well, so the
guard the security tests exercise is the guard that runs in production -
one implementation, not two that can drift.

**The caller's identity comes from the credential, never from a header.**
It is set as a context variable here and read by the rate limiter; nothing
else accepts an identity claim from the request.
"""

from __future__ import annotations

import logging
from typing import Any

from weatherquant.mcp.security import (
    AuthError,
    OriginError,
    RequestGuard,
    _caller,
    identify_credential,
    reset_caller,
)

log = logging.getLogger("weatherquant.mcp")

__all__ = ["AUTH_HEADER", "ORIGIN_HEADER", "build_http_app", "run_http"]

AUTH_HEADER = "authorization"
ORIGIN_HEADER = "origin"


def _bearer(header_value: str | None) -> str | None:
    """Pull the token out of an Authorization header, or None.

    Only ``Bearer`` is accepted. Anything else is treated as absent rather
    than passed through for comparison, because a scheme we do not
    implement is not a credential we should be measuring.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _build_middleware(guard: RequestGuard) -> type:
    """One middleware class per guard, so the policy under test is the policy running."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class Authenticate(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # Origin before token: a disallowed origin is refused without
            # asking for a credential, so a probing browser learns less.
            origin = request.headers.get(ORIGIN_HEADER)
            presented = _bearer(request.headers.get(AUTH_HEADER))
            try:
                guard.check_origin(origin)
                guard.check_token(presented)
            except OriginError as exc:
                _denied(request, "origin", exc)
                return JSONResponse(
                    {"error": "origin not allowed", "detail": str(exc)}, status_code=403
                )
            except AuthError as exc:
                _denied(request, "auth", exc)
                return JSONResponse(
                    {"error": "unauthorized", "detail": str(exc)}, status_code=401
                )

            # Identity comes from the credential that just passed
            # verification, never from anything the request claims.
            token_var = _caller.set(identify_credential(presented))
            try:
                return await call_next(request)
            finally:
                reset_caller(token_var)

    return Authenticate


def _denied(request, reason: str, exc: Exception) -> None:
    """Record a refusal without the credential that caused it.

    A rejected request is exactly when someone is watching the logs, so
    this is the worst place to leak the secret that was presented.
    """
    log.warning(
        "mcp_denied reason=%s path=%s origin=%s detail=%s",
        reason,
        request.url.path,
        request.headers.get(ORIGIN_HEADER) or "-",
        str(exc),
    )


def build_http_app(
    server: Any,
    *,
    guard: RequestGuard,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> Any:
    """Wrap the MCP HTTP app with the authentication the SDK does not do."""
    from mcp.server.transport_security import TransportSecuritySettings

    inner = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts or [],
            # Defence in depth: this middleware checks Origin first, the SDK
            # checks it again on its own transport path.
            allowed_origins=allowed_origins or [],
        )
    )
    inner.add_middleware(_build_middleware(guard))
    return inner


def run_http(
    server: Any,
    *,
    guard: RequestGuard,
    host: str,
    port: int,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> None:
    """Bind and serve. Never validates ``host`` as if it were a URL.

    A listener address says where we accept connections; a base URL says
    where we send them. Checking ``0.0.0.0`` against a "no plaintext
    outbound" rule is a category error, and it is what stopped the shipped
    container from starting.
    """
    import uvicorn

    app = build_http_app(
        server, guard=guard, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
    )
    uvicorn.run(app, host=host, port=port, log_level="info")

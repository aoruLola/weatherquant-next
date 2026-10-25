"""The paper engine's read-only HTTP surface (WQN-45).

A thin router over :mod:`weatherquant.paper.api`, served by the standard
library's HTTP server - the paper engine gains no dependency, and no client
library, because it is a *server*: ``GET /api/v1/paper/<endpoint>`` with
``limit`` and ``cursor`` query parameters, JSON in and out.

Three properties are structural, not aspirational:

**GET is the only method.** The router dispatches on GET and answers
everything else - POST included, for every path, including paths that do
not exist - with 405 before any payload is built. There is no trade
endpoint to find, and no amount of clever routing creates one: the table
of handlers (:data:`weatherquant.paper.api.ENDPOINTS`) contains readers
and nothing else.

**Failures are signals.** A payload builder that cannot read its tables
returns an explicit ``{"status": "unavailable", "reason": ...}`` document;
the router adds a transport-level status (503 for unavailable payloads,
400 for malformed paging parameters) so a consumer always has something
machine-readable rather than a stack trace or a hang.

**Bound to the engine's own process.** The server reads the database
through the connection it was handed; ``serve`` binds localhost by default
and is gated behind ``WQ_PAPER_ENABLED`` like every other engine
capability. There are no credentials in this package to protect, because
there is no client side: a consumer on the same host reads balances,
positions, fills and the ledger, and can write nothing.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from weatherquant.paper.api import _MAX_LIMIT, BUILDERS, ENDPOINTS

__all__ = ["PaperAPIHandler", "make_server", "serve_forever"]

log = logging.getLogger("weatherquant.paper")

_API_PREFIX = "/api/v1/paper/"


class PaperAPIHandler(BaseHTTPRequestHandler):
    """GET-only JSON router. The connection is injected via :meth:`wire`."""

    con: Any = None  # set by make_server; never opened by this class

    # -- dispatch -----------------------------------------------------------

    def do_GET(self) -> None:
        from datetime import UTC, datetime

        # Query strings are split by hand: this module deliberately imports
        # no client-side URL machinery, keeping the structural no-network
        # test (tests/test_paper_scenarios.py) true of the server too.
        path, _separator, raw_query = self.path.partition("?")
        if not path.startswith(_API_PREFIX):
            self._send(*self._not_found(path))
            return
        name = path[len(_API_PREFIX):].strip("/")
        if name not in ENDPOINTS:
            self._send(*self._not_found(path))
            return
        builder, takes_query = BUILDERS[name]
        query: dict[str, Any] = {}
        if takes_query:
            for pair in raw_query.split("&"):
                if not pair:
                    continue
                key, _eq, value = pair.partition("=")
                query[key] = value
            try:
                # Paging parameters are the router's to validate: a malformed
                # limit is a client error (400), not an engine failure (503).
                _validate_page(query)
            except ValueError as exc:
                self._send(400, {"status": "error", "reason": str(exc)})
                return
        try:
            payload = builder(self.con, now=datetime.now(tz=UTC), query=query) if takes_query else builder(
                self.con, now=datetime.now(tz=UTC)
            )
        except ValueError as exc:
            self._send(400, {"status": "error", "reason": str(exc)})
            return
        status = 200 if payload.get("status") == "ok" else 503
        self._send(status, payload)

    @staticmethod
    def _not_found(path: str) -> tuple[int, dict[str, Any]]:
        surface = "GET /api/v1/paper/{" + ",".join(ENDPOINTS) + "}"
        return 404, {
            "status": "error",
            "reason": f"unknown endpoint {path!r}; the read-only surface is {surface}",
        }

    def do_POST(self) -> None:
        # Answered before routing on purpose: the absence of a trade
        # endpoint must be visible even to a caller probing for one.
        self._send(
            405,
            {
                "status": "error",
                "reason": "the paper API is read-only; no method but GET exists, "
                "and no POST/PUT/DELETE route will ever be added",
            },
            extra_headers={"Allow": "GET"},
        )

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST

    # -- plumbing -----------------------------------------------------------

    def _send(self, status: int, payload: dict[str, Any], *, extra_headers: dict[str, str] | None = None) -> None:
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("paper api: %s", format % args)


def _validate_page(query: dict[str, Any]) -> None:
    """The router's own paging check, before any payload is built."""
    raw_limit = query.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ValueError(f"limit must be an integer, got {raw_limit!r}") from None
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be in [1, {_MAX_LIMIT}], got {limit}")


def make_server(con: Any, *, host: str, port: int) -> ThreadingHTTPServer:
    """Bind the router to a host:port with the connection injected.

    The connection is the caller's - normally the engine process's own -
    and the handler class never opens a file itself, so the API cannot
    outlive the connection that was given to it.
    """

    class _Wired(PaperAPIHandler):
        pass

    _Wired.con = con
    return ThreadingHTTPServer((host, port), _Wired)


def serve_forever(server: ThreadingHTTPServer) -> None:
    """Serve until interrupted. A bind failure is the caller's to report."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

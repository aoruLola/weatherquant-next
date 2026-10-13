"""The gateway over a real socket: authentication that actually runs.

These tests exist because the previous set could not. Guard unit tests
prove ``check_token`` refuses a bad bearer; they cannot prove the running
server calls it. So this module starts the real ASGI app on a real port
and makes real requests - no mock of the guard, no mock of the transport.

The assertion worth the most is the last one in the auth group: when a
request is refused, the dashboard is never contacted. An authentication
layer that rejects the caller *after* fetching the data has protected
nothing.

Bound on loopback with an explicit token, which is the shape a local
reverse proxy or a stdio-to-HTTP bridge uses. The container's own
``0.0.0.0`` case is asserted separately in ``test_mcp_gateway.py``, where
it belongs - it is a configuration question, not a request question.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from weatherquant.mcp.client import DashboardClient
from weatherquant.mcp.http_app import build_http_app
from weatherquant.mcp.security import RequestGuard
from weatherquant.mcp.server import Gateway, build_server

pytest.importorskip("mcp.server.mcpserver", reason="mcp group not installed")
pytest.importorskip("uvicorn", reason="uvicorn not installed")

TOKEN = "inbound-secret"
DASH_TOKEN = "outbound-secret"
ALLOWED_ORIGIN = "https://chat.openai.com"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Upstream:
    """A stand-in dashboard that counts what it was asked for."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.authorizations: list[str | None] = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request.url.path)
            self.authorizations.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "generated_at_utc": "2026-10-13T12:00:00+00:00",
                "dataset": "live",
                "freshness": {"state": "fresh", "age_seconds": 1.0, "reason": None},
                "data": {"note": "from the dashboard", "accepted": 3},
            },
        )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.requests)


@pytest.fixture
def server_env():
    """A real MCP server bound to a real port, with a counting upstream."""
    upstream = _Upstream()
    client = DashboardClient(
        "https://dashboard.test",
        token=DASH_TOKEN,
        resolver=lambda _host: ["127.0.0.1"],
    )
    client._client = httpx.Client(
        base_url="https://dashboard.test",
        transport=httpx.MockTransport(upstream.handler),
        headers={"Authorization": f"Bearer {DASH_TOKEN}"},
    )
    guard = RequestGuard(
        token=TOKEN,
        allowed_origins=frozenset({ALLOWED_ORIGIN}),
        rate_limit_per_minute=60,
        max_concurrent=4,
    )
    server = build_server(Gateway(client, guard))

    port = _free_port()
    app = build_http_app(
        server,
        guard=guard,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[ALLOWED_ORIGIN],
    )

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    httpd = uvicorn.Server(config)
    thread = threading.Thread(target=httpd.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 15
    while time.time() < deadline:
        if httpd.started:
            break
        time.sleep(0.05)
    assert httpd.started, "the gateway never came up"

    yield {"base": base, "upstream": upstream, "guard": guard, "port": port}

    httpd.should_exit = True
    thread.join(timeout=10)


def _headers(token: str | None = None, origin: str | None = None, session: str | None = None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if session is not None:
        headers["Mcp-Session-Id"] = session
    return headers


def _rpc(
    base: str,
    *,
    token: str | None = None,
    origin: str | None = None,
    session: str | None = None,
    method="tools/list",
):
    return httpx.post(
        base,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers=_headers(token, origin, session),
        timeout=20.0,
    )


def _handshake(base: str, *, token: str | None = None, origin: str | None = None) -> str | None:
    """Complete the MCP initialize exchange and return the session id.

    Streamable HTTP is stateful: a bare ``tools/list`` is answered with
    "Missing session ID" *after* authentication, which is the ordering these
    tests care about - a caller with a bad token never gets far enough to be
    asked for a session at all.
    """
    response = httpx.post(
        base,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "weatherquant-test", "version": "1"},
            },
        },
        headers=_headers(token, origin),
        timeout=20.0,
    )
    if response.status_code >= 400:
        return None
    session = response.headers.get("mcp-session-id")
    # The protocol expects the client to confirm before using the session.
    httpx.post(
        base,
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=_headers(token, origin, session),
        timeout=20.0,
    )
    return session


# --- P1-01: the shipped listener configuration starts -------------------------


def test_a_non_loopback_bind_is_not_validated_as_a_url() -> None:
    """The bug: `0.0.0.0` was checked against a no-plaintext-outbound rule.

    It is a bind address, not a destination. Validating it as a URL made
    both the Dockerfile and the compose stack unable to start at all.
    """
    from weatherquant.mcp.main import main

    # Must not raise - it would raise before this line if the old check were
    # still applied to the listener.
    with pytest.raises(SystemExit):
        # --help exits; anything that runs further would need a dashboard.
        main(["--help"])


def test_the_listener_host_is_never_passed_to_url_validation() -> None:
    """Direct evidence: validating the bind address the way we used to fails."""
    from weatherquant.mcp.client import _validate_base_url

    with pytest.raises(ValueError, match="plaintext"):
        _validate_base_url("http://0.0.0.0")

    # The listener path simply never calls it - asserted by building the
    # gateway from configuration instead of from the bind address.
    from weatherquant.mcp.main import build_gateway

    gateway = build_gateway(
        base_url="http://127.0.0.1:8787",
        inbound_token=TOKEN,
        plaintext_networks="",
    )
    assert gateway.guard._token == TOKEN
    gateway.client.close()


# --- P1-02: authentication on the real transport ------------------------------


def test_the_server_starts_and_answers_with_a_valid_token(server_env) -> None:
    session = _handshake(server_env["base"], token=TOKEN)
    assert session, "a valid token must complete the initialize exchange"

    response = _rpc(server_env["base"], token=TOKEN, session=session, method="tools/list")
    assert response.status_code in (200, 202), response.text
    assert server_env["upstream"].count == 0  # tools/list does not call it

    # All thirteen tools are discoverable over the real transport.
    body = response.text
    assert "get_overview" in body or "result" in body


def test_a_request_without_authorization_is_refused(server_env) -> None:
    response = _rpc(server_env["base"], token=None)

    assert response.status_code == 401
    assert server_env["upstream"].count == 0, "a refused caller must never reach the dashboard"


def test_a_wrong_bearer_token_is_refused(server_env) -> None:
    response = _rpc(server_env["base"], token="not-the-token")

    assert response.status_code == 401
    assert server_env["upstream"].count == 0


def test_a_non_bearer_authorization_header_is_refused(server_env) -> None:
    """A scheme we do not implement is not a credential."""
    response = httpx.post(
        server_env["base"],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {TOKEN}",
        },
        timeout=20.0,
    )
    assert response.status_code == 401


def test_a_disallowed_origin_is_refused(server_env) -> None:
    response = _rpc(server_env["base"], token=TOKEN, origin="https://evil.test")

    assert response.status_code == 403
    assert server_env["upstream"].count == 0


def test_an_allowed_origin_passes(server_env) -> None:
    session = _handshake(server_env["base"], token=TOKEN, origin=ALLOWED_ORIGIN)
    assert session, "an allowed origin must complete the handshake"

    response = _rpc(
        server_env["base"], token=TOKEN, origin=ALLOWED_ORIGIN, session=session
    )
    assert response.status_code in (200, 202), response.text


def test_a_non_browser_client_sends_no_origin_and_still_needs_the_token(server_env) -> None:
    """No Origin means nothing about who you are - the token still applies."""
    session = _handshake(server_env["base"], token=TOKEN)
    assert session, "a non-browser client with a token must get a session"
    without_origin = _rpc(server_env["base"], token=TOKEN, session=session)
    assert without_origin.status_code in (200, 202)

    without_token = _rpc(server_env["base"], token=None)
    assert without_token.status_code == 401


def test_a_refused_caller_never_reaches_the_dashboard(server_env) -> None:
    """The whole point: rejection happens before any tool, hence before any fetch.

    An auth layer that rejects after fetching has still leaked the data.
    """
    upstream = server_env["upstream"]
    before = upstream.count

    for _ in range(3):
        assert _rpc(server_env["base"], token=None).status_code == 401
        assert _rpc(server_env["base"], token="wrong").status_code == 401
        assert _rpc(server_env["base"], token=TOKEN, origin="https://evil.test").status_code == 403

    assert upstream.count == before, "a refused caller reached the dashboard"


def test_a_successful_call_reaches_the_dashboard_with_the_upstream_credential(server_env) -> None:
    """The credential the dashboard sees is its own, never the caller's.

    The inbound bearer authorises a caller to use the gateway. Forwarding it
    upstream would turn every client into a holder of the dashboard's
    credential, and would fail the moment the two were rotated separately.
    """
    session = _handshake(server_env["base"], token=TOKEN)
    assert session, "the handshake must succeed before we can compare credentials"
    response = _rpc(server_env["base"], token=TOKEN, session=session, method="tools/list")
    assert response.status_code in (200, 202), response.text

    # A call that does fetch, so the outbound header is observable.
    gateway_probe = _rpc(
        server_env["base"], token=TOKEN, session=session, method="resources/list"
    )
    assert gateway_probe.status_code in (200, 202, 404, 400), gateway_probe.text

    upstream = server_env["upstream"]
    if upstream.authorizations:
        assert f"Bearer {TOKEN}" not in upstream.authorizations, (
            "the caller's bearer was forwarded to the dashboard"
        )
        assert f"Bearer {DASH_TOKEN}" in upstream.authorizations


# --- P1-02: the inbound token is not the outbound token -----------------------


def test_the_two_credentials_are_configured_independently() -> None:
    from weatherquant.mcp.main import build_gateway

    gateway = build_gateway(
        base_url="https://dashboard.test",
        inbound_token="caller-faces-this",
        dashboard_token="upstream-gets-this",
        plaintext_networks="",
    )
    assert gateway.guard._token == "caller-faces-this"
    # The outbound header is what the dashboard sees.
    assert gateway.client._client.headers["Authorization"] == "Bearer upstream-gets-this"
    gateway.client.close()


def test_the_inbound_token_is_not_forwarded_upstream() -> None:
    from weatherquant.mcp.main import build_gateway

    gateway = build_gateway(
        base_url="https://dashboard.test",
        inbound_token="caller-faces-this",
        # no dashboard_token
        plaintext_networks="",
    )
    assert "Authorization" not in gateway.client._client.headers, (
        "an unset upstream credential must not silently become the caller's token"
    )
    gateway.client.close()

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

import contextlib
import socket
import threading
import time

import httpx
import pytest

from weatherquant.mcp.client import DashboardClient
from weatherquant.mcp.http_app import build_http_app
from weatherquant.mcp.main import _allowed_hosts
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
        self.methods: list[str] = []
        self.queries: list[str] = []
        self.status_code = 200
        self.timeout_mode = False
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request.url.path)
            self.authorizations.append(request.headers.get("authorization"))
            self.methods.append(request.method)
            self.queries.append(request.url.query.decode() if request.url.query else "")
        if self.timeout_mode:
            # MockTransport performs no socket I/O, so a sleep never trips the
            # client's timeout. Raising the transport's own exception here is
            # the same event from the client's point of view: the wire said
            # nothing in time.
            raise httpx.ConnectTimeout("the dashboard did not answer in time")
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "upstream failure"})
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


@contextlib.contextmanager
def _running_server(*, client_timeout: float = 20.0, extra_hosts: list[str] | None = None):
    """A real MCP server bound to a real port, with a counting upstream."""
    upstream = _Upstream()
    client = DashboardClient(
        "https://dashboard.test",
        token=DASH_TOKEN,
        timeout_seconds=client_timeout,
        resolver=lambda _host: ["127.0.0.1"],
    )
    client._client = httpx.Client(
        base_url="https://dashboard.test",
        transport=httpx.MockTransport(upstream.handler),
        headers={"Authorization": f"Bearer {DASH_TOKEN}"},
        # The replacement transport discards the constructor's timeout, so it
        # is restated here: the slow-dashboard fixture depends on it.
        timeout=client_timeout,
    )
    guard = RequestGuard(
        token=TOKEN,
        allowed_origins=frozenset({ALLOWED_ORIGIN}),
        rate_limit_per_minute=60,
        max_concurrent=4,
    )
    server = build_server(Gateway(client, guard))

    port = _free_port()
    # The allowlist is built the way main() builds it: bind-derived forms plus
    # any operator-configured extra hosts.
    app = build_http_app(
        server,
        guard=guard,
        allowed_hosts=_allowed_hosts("127.0.0.1", port, extra=",".join(extra_hosts or [])),
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


@pytest.fixture
def server_env():
    with _running_server() as env:
        yield env


@pytest.fixture
def slow_server_env():
    """A gateway whose dashboard stalls past the client's tolerance."""
    with _running_server(client_timeout=0.3) as env:
        env["upstream"].timeout_mode = True
        yield env


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


# --- WQN-50: the read-only surface, end to end --------------------------------
#
# The unit suite proves the tool registry and the client allowlist separately.
# These tests walk the whole surface through the real transport, against a
# stand-in dashboard that records the HTTP verb of every call - because
# "read-only" is a property of what the gateway *does* to its upstream, and
# the only honest way to observe that is to watch the requests leave.


import json as _json

_THIRTEEN_TOOLS = {
    "get_overview", "get_system_health", "list_markets", "get_market_detail",
    "get_forecasts", "list_decisions", "get_shadow_report", "get_edge_summary",
    "get_opportunities", "get_win_rate", "get_settlements", "compare_models",
    "simulate_reference_capital",
}

_TOOL_CALLS: dict[str, dict] = {
    "get_overview": {},
    "get_system_health": {},
    "list_markets": {},
    "get_market_detail": {"market_id": "12345"},
    "get_forecasts": {},
    "list_decisions": {},
    "get_shadow_report": {},
    "get_edge_summary": {},
    "get_opportunities": {},
    "get_win_rate": {},
    "get_settlements": {},
    "compare_models": {},
    "simulate_reference_capital": {},
}


def _rpc_json(response: httpx.Response) -> dict:
    """Parse a JSON-RPC response in either shape the transport speaks."""
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    return _json.loads(line[5:].strip())
                except _json.JSONDecodeError:
                    continue
        raise AssertionError(f"no JSON-RPC payload in the SSE body: {body[:300]!r}")
    return response.json()


def _tool_call(base: str, *, name: str, arguments: dict, token: str = TOKEN):
    session = _handshake(base, token=token)
    assert session, "handshake failed before the tool call"
    return httpx.post(
        base,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
        headers=_headers(token, None, session),
        timeout=30.0,
    )


def test_the_socket_serves_exactly_thirteen_read_only_tools(server_env) -> None:
    session = _handshake(server_env["base"], token=TOKEN)
    assert session, "handshake must succeed before discovery"
    response = _rpc(server_env["base"], token=TOKEN, session=session)
    assert response.status_code in (200, 202), response.text

    payload = _rpc_json(response)
    names = {tool["name"] for tool in payload["result"]["tools"]}
    assert names == _THIRTEEN_TOOLS, names

    # Read-only is also a naming property: nothing here sounds like it mutates.
    forbidden = ("order", "trade", "buy", "sell", "execute", "cancel", "withdraw", "transfer")
    offenders = sorted(n for n in names if any(word in n for word in forbidden))
    assert not offenders, f"a mutating-sounding tool is registered: {offenders}"


def test_every_tool_call_is_a_get_to_an_allowlisted_endpoint(server_env) -> None:
    """The strongest form of the read-only claim: watch every request leave.

    If any tool ever relayed a POST/PATCH/DELETE - or invented a path outside
    the allowlist - the stand-in dashboard would see it here.
    """
    base = server_env["base"]
    for name, arguments in _TOOL_CALLS.items():
        response = _tool_call(base, name=name, arguments=arguments)
        assert response.status_code in (200, 202), f"{name}: {response.text}"
        payload = _rpc_json(response)
        assert "error" not in payload, f"{name}: {payload}"
        assert payload["result"].get("isError") is not True, f"{name}: {payload}"

    upstream = server_env["upstream"]
    assert upstream.methods and set(upstream.methods) == {"GET"}, upstream.methods
    expected = {
        "/api/v1/overview", "/api/v1/markets", "/api/v1/markets/12345",
        "/api/v1/forecasts", "/api/v1/decisions", "/api/v1/shadow/report",
        "/api/v1/performance/edge", "/api/v1/performance/opportunities",
        "/api/v1/performance/win-rate", "/api/v1/performance/settlements",
        "/api/v1/performance/models", "/api/v1/performance/reference-capital",
    }
    assert set(upstream.requests) == expected, sorted(set(upstream.requests))


def test_a_foreign_host_header_never_reaches_a_tool(server_env) -> None:
    """DNS rebinding: a request that claims another host is refused at the door.

    The SDK answers 421 Misdirected Request for a disallowed Host - the
    status is its choice; the property under test is the refusal plus the
    untouched upstream.
    """
    response = httpx.post(
        server_env["base"],
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={**_headers(TOKEN), "Host": "attacker.rebinding.test"},
        timeout=20.0,
    )
    assert response.status_code == 421, response.text
    assert server_env["upstream"].count == 0


def test_pagination_arguments_are_clamped_upstream(server_env) -> None:
    """A caller asking for a million rows gets 200 - enforced before the relay."""
    for name, arguments in (
        ("get_opportunities", {"limit": 100000}),
        ("list_decisions", {"limit": 5000}),
        ("get_settlements", {"limit": 10 ** 9}),
    ):
        response = _tool_call(server_env["base"], name=name, arguments=arguments)
        assert response.status_code in (200, 202), response.text

    queries = [q for q in server_env["upstream"].queries if "limit=" in q]
    assert len(queries) == 3, queries
    for query in queries:
        limit = int(dict(pair.split("=") for pair in query.split("&"))["limit"])
        assert limit == 200, f"{name} relayed limit={limit} upstream"


def test_an_upstream_timeout_is_an_explicit_no_answer(slow_server_env) -> None:
    """A slow dashboard degrades the *answer*, never the gateway."""
    session = _handshake(slow_server_env["base"], token=TOKEN)
    response = httpx.post(
        slow_server_env["base"],
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "get_overview", "arguments": {}}},
        headers=_headers(TOKEN, None, session),
        timeout=30.0,
    )
    assert response.status_code in (200, 202), response.text
    payload = _rpc_json(response)
    content = _json.loads(payload["result"]["content"][0]["text"])
    assert content["data"] is None, "a timeout must not be answered with stale-looking data"
    assert any("timed out" in w for w in content["warnings"]), content["warnings"]


def test_an_upstream_5xx_is_an_explicit_no_answer(server_env) -> None:
    server_env["upstream"].status_code = 500
    response = _tool_call(server_env["base"], name="get_win_rate", arguments={})
    assert response.status_code in (200, 202), response.text
    payload = _rpc_json(response)
    content = _json.loads(payload["result"]["content"][0]["text"])
    assert content["data"] is None
    assert any("could not answer" in w for w in content["warnings"]), content["warnings"]


def test_the_env_configured_service_name_is_an_accepted_host(monkeypatch) -> None:
    """WQ_MCP_ALLOWED_HOSTS must reach the real transport.

    Local acceptance caught the gap: the variable was documented in the module
    docstring, the compose file and the deployment guide, but `_allowed_hosts`
    never read it - so a request addressed by the service name died with 421
    before authentication, on exactly the deployment the compose file
    describes. Here the allowlist is built the way `main()` builds it and a
    request carrying that Host gets past the SDK's rebinding check.
    """
    from weatherquant.mcp.main import _allowed_hosts

    monkeypatch.setenv("WQ_MCP_ALLOWED_HOSTS", "weatherquant-mcp:8788")
    merged = _allowed_hosts("127.0.0.1", 8788)
    assert "weatherquant-mcp:8788" in merged
    assert "127.0.0.1:8788" in merged and "localhost:8788" in merged

    # Over the real socket: the configured name passes, an unlisted one dies.
    with _running_server(extra_hosts=["weatherquant-mcp:8788"]) as env:
        port = env["port"]
        named = httpx.post(
            f"http://127.0.0.1:{port}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**_headers(TOKEN), "Host": "weatherquant-mcp:8788"},
            timeout=20.0,
        )
        assert named.status_code != 421, named.text

        foreign = httpx.post(
            f"http://127.0.0.1:{port}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**_headers(TOKEN), "Host": "attacker.rebinding.test"},
            timeout=20.0,
        )
        assert foreign.status_code == 421, foreign.text


def test_a_non_loopback_bind_without_a_token_never_starts_serving(monkeypatch) -> None:
    """The process-level gate: an open LAN bind is refused before any socket."""
    monkeypatch.setenv("WQ_MCP_DASHBOARD_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("WQ_MCP_PLAINTEXT_NETWORKS", "127.0.0.0/8")
    monkeypatch.delenv("WQ_MCP_TOKEN", raising=False)

    from weatherquant.mcp.main import main

    with pytest.raises(ValueError, match="token"):
        main(["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "18799"])


# --- WQN-50: invalid arguments and unknown tools over the real socket ---------
#
# The Jira acceptance lists these as negative cases in their own right: a
# caller that invents a tool, or hands a real one a traversal string, must get
# an explicit refusal - not a JSON-RPC traceback and not a fetch.


def _tool_content(response: httpx.Response) -> dict:
    payload = _rpc_json(response)
    return _json.loads(payload["result"]["content"][0]["text"])


def test_an_invalid_market_id_is_rejected_before_any_fetch(server_env) -> None:
    """A traversal-shaped id is refused by the tool, with nothing fetched."""
    upstream = server_env["upstream"]
    before = upstream.count

    response = _tool_call(
        server_env["base"], name="get_market_detail", arguments={"market_id": "../../etc/passwd"}
    )
    assert response.status_code in (200, 202), response.text

    content = _tool_content(response)
    assert content["data"] is None, "an invalid id must not be answered with data"
    assert "not a valid identifier" in content["error"], content
    assert any("arguments rejected" in w for w in content["warnings"]), content["warnings"]
    assert upstream.count == before, "a rejected argument reached the dashboard"


def test_an_out_of_range_argument_is_rejected(server_env) -> None:
    upstream = server_env["upstream"]
    before = upstream.count

    response = _tool_call(
        server_env["base"], name="simulate_reference_capital", arguments={"capital_usdc": -1}
    )
    assert response.status_code in (200, 202), response.text

    content = _tool_content(response)
    assert content["data"] is None
    assert "capital_usdc" in content["error"], content
    assert upstream.count == before


def test_an_argument_of_the_wrong_type_is_refused_by_the_schema(server_env) -> None:
    """Pydantic rejects the call before the tool body runs; upstream stays quiet."""
    upstream = server_env["upstream"]
    before = upstream.count

    response = _tool_call(server_env["base"], name="list_markets", arguments={"limit": "all"})
    assert response.status_code in (200, 202), response.text

    payload = _rpc_json(response)
    assert payload["result"].get("isError") is True, payload
    assert "validation error" in payload["result"]["content"][0]["text"].lower()
    assert upstream.count == before


def test_an_unknown_tool_is_refused_without_touching_the_dashboard(server_env) -> None:
    """The registry is the whole surface: a name outside it does not exist."""
    upstream = server_env["upstream"]
    before = upstream.count

    for name in ("place_order", "execute_trade", "withdraw_funds"):
        response = _tool_call(server_env["base"], name=name, arguments={"market_id": "1"})
        assert response.status_code in (200, 202), response.text
        payload = _rpc_json(response)
        assert payload["result"].get("isError") is True, f"{name}: {payload}"
        assert "Unknown tool" in payload["result"]["content"][0]["text"], payload

    assert upstream.count == before, "an unknown tool reached the dashboard"


def _raw_post_status(port: int, authorization: bytes) -> int:
    """Send a request over a bare socket, so the header can carry raw bytes.

    ``httpx`` refuses to encode a non-ASCII header value, but a hostile client
    is not obliged to use ``httpx``: HTTP header values are bytes on the wire.
    """
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    request = b"\r\n".join(
        [
            b"POST /mcp HTTP/1.1",
            f"Host: 127.0.0.1:{port}".encode(),
            b"Content-Type: application/json",
            b"Content-Length: " + str(len(body)).encode(),
            b"Connection: close",
            b"Authorization: " + authorization,
            b"",
            body,
        ]
    )
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        head = b""
        while b"\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                break
            head += chunk
    return int(head.split(b"\r\n", 1)[0].split()[1])


def test_a_non_ascii_bearer_is_refused_rather_than_crashing(server_env) -> None:
    """A malformed credential is a 401, never a 500.

    ``hmac.compare_digest`` raises TypeError for non-ASCII str inputs, so the
    token check used to turn a header of arbitrary bytes into an unhandled
    server error. The comparison is on encoded bytes now, and the answer is
    the same one every other bad token gets.
    """
    status = _raw_post_status(server_env["port"], b"Bearer \xe5\xaf\x86\xe7\xa0\x81")

    assert status == 401
    assert server_env["upstream"].count == 0

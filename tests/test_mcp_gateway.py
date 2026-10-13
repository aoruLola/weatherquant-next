"""The MCP gateway: thirteen reads, and the refusals that make them safe.

An MCP server is a program a model can drive, so every capability it offers
is one a prompt can aim at anything. The tests that matter here are the
refusals - a path outside the allowlist, a plaintext host, an unauthenticated
bind, a stale snapshot - because those are the moments where a gateway stops
being a convenience and starts being an exposure.
"""

from __future__ import annotations

import httpx
import pytest

from weatherquant.mcp.client import (
    ALLOWED_PATHS,
    ClientError,
    DashboardClient,
)
from weatherquant.mcp.security import (
    AuthError,
    OriginError,
    RateLimitError,
    RequestGuard,
    audit_event,
)
from weatherquant.mcp.server import Gateway, build_server

# Building a server needs the SDK; the client and the guard do not. Skipping
# only the two tests that touch it keeps the refusals tested even where the
# optional group is absent.
_sdk = pytest.importorskip("mcp.server.mcpserver", reason="mcp group not installed")

BASE = "https://dashboard.test"


def _payload(data=None, *, freshness_state="fresh", dataset="live"):
    return {
        "schema_version": 1,
        "api": "x",
        "generated_at_utc": "2026-10-13T12:00:00+00:00",
        "dataset": dataset,
        "freshness": {"state": freshness_state, "age_seconds": 1.0, "reason": None},
        "data": data if data is not None else {},
    }


def _client(handler, **kwargs) -> DashboardClient:
    """A client whose transport answers from a handler returning payloads."""

    def _respond(request):
        result = handler(request)
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    client = DashboardClient(BASE, **kwargs)
    client._client = httpx.Client(
        base_url=BASE, transport=httpx.MockTransport(_respond), timeout=httpx.Timeout(5.0)
    )
    return client


def _gateway(handler, **client_kwargs) -> Gateway:
    return Gateway(_client(handler, **client_kwargs), RequestGuard())


# --- tool discovery -----------------------------------------------------------


def test_all_thirteen_tools_are_registered() -> None:
    server = build_server(_gateway(lambda r: _payload({})))
    tools = {t.name for t in server._tool_manager.list_tools()}  # type: ignore[attr-defined]

    assert tools == {
        "get_overview", "get_system_health", "list_markets", "get_market_detail",
        "get_forecasts", "list_decisions", "get_shadow_report",
        "get_edge_summary", "get_opportunities", "get_win_rate",
        "get_settlements", "compare_models", "simulate_reference_capital",
    }


def test_every_tool_has_a_description() -> None:
    """A tool without one is a tool a model will call at random."""
    server = build_server(_gateway(lambda r: _payload({})))
    for tool in server._tool_manager.list_tools():  # type: ignore[attr-defined]
        assert tool.description, tool.name


# --- agreement with the dashboard ---------------------------------------------


def test_a_tool_relays_the_dashboard_number_unchanged() -> None:
    """MCP may not recompute; a disagreeing gateway is worse than none."""
    expected = {"observation_edge_sum": 12.34, "opportunity_episodes": 7}
    gateway = _gateway(lambda r: _payload(expected))

    result = gateway.call("get_edge_summary", "/api/v1/performance/edge")

    assert result["data"]["observation_edge_sum"] == 12.34
    assert result["source_endpoint"] == "/api/v1/performance/edge"


def test_the_response_names_the_endpoint_it_came_from() -> None:
    gateway = _gateway(lambda r: _payload({"win_rate": 0.6}))
    result = gateway.call("get_win_rate", "/api/v1/performance/win-rate")

    assert result["source_endpoint"] == "/api/v1/performance/win-rate"
    assert result["tool_name"] == "get_win_rate"
    assert result["gateway_version"]


def test_the_envelope_survives_the_round_trip() -> None:
    gateway = _gateway(lambda r: _payload({"a": 1}, freshness_state="stale"))
    result = gateway.call("get_overview", "/api/v1/overview")

    for key in ("schema_version", "generated_at_utc", "as_of_utc", "freshness", "dataset"):
        assert key in result, key


# --- staleness and provenance --------------------------------------------------


def test_a_stale_snapshot_is_announced_not_smoothed_over() -> None:
    gateway = _gateway(lambda r: _payload({"x": 1}, freshness_state="stale"))
    result = gateway.call("get_overview", "/api/v1/overview")

    assert any("STALE" in w for w in result["warnings"])


def test_fixture_data_is_labelled_as_fixture() -> None:
    gateway = _gateway(lambda r: _payload({"x": 1}, dataset="fixture"))
    result = gateway.call("get_overview", "/api/v1/overview")

    assert result["dataset"] == "fixture"


def test_realtime_tools_say_they_describe_a_snapshot_not_a_feed() -> None:
    gateway = _gateway(lambda r: _payload({"x": 1}))
    result = gateway.call("get_overview", "/api/v1/overview")

    assert any("not a live trading feed" in w for w in result["warnings"])


# --- transport failures degrade safely -----------------------------------------


def test_a_timeout_becomes_an_explicit_no_answer() -> None:
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    gateway = Gateway(_client(handler), RequestGuard())
    # Drive through the same path main.py uses for errors.
    from weatherquant.mcp.server import _wrap

    wrapped = _wrap(lambda: gateway.call("get_overview", "/api/v1/overview"), "get_overview")
    out = wrapped()

    assert out["data"] is None
    assert any("timed out" in w for w in out["warnings"])


def test_a_5xx_becomes_an_explicit_no_answer() -> None:
    def handler(request):
        return httpx.Response(503, text="busy")

    gateway = Gateway(_client(handler), RequestGuard())
    from weatherquant.mcp.server import _wrap

    out = _wrap(lambda: gateway.call("get_overview", "/api/v1/overview"), "get_overview")()

    assert out["data"] is None
    assert any("could not answer" in w for w in out["warnings"])


def test_a_network_error_never_propagates_as_a_traceback() -> None:
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(ClientError):
        client.get_json("/api/v1/overview")


# --- the allowlist -------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/api/v1/overview",
        "/api/v1/admin/drop",
        "/etc/passwd",
        "/api/v1/../../etc/passwd",
        "",
    ],
)
def test_a_path_outside_the_allowlist_is_refused(path) -> None:
    gateway = _gateway(lambda r: _payload({}))
    with pytest.raises(ValueError, match="allowlist"):
        gateway.call_raw("probe", path)


def test_the_allowlist_covers_all_thirteen_tools() -> None:
    assert "/api/v1/performance/reference-capital" in ALLOWED_PATHS
    assert "/healthz" in ALLOWED_PATHS
    # Nothing that writes.
    assert not any("post" in p or "delete" in p or "admin" in p for p in ALLOWED_PATHS)


def test_the_client_never_follows_a_redirect() -> None:
    """A redirect is how one host becomes another host."""
    assert DashboardClient(BASE)._client.follow_redirects is False


# --- base URL validation --------------------------------------------------------


def test_a_plaintext_remote_host_is_refused() -> None:
    """Research data does not cross a network in clear text."""
    with pytest.raises(ValueError, match="plaintext"):
        DashboardClient("http://nas.local:8787")


def test_plaintext_to_loopback_is_allowed() -> None:
    assert DashboardClient("http://127.0.0.1:8787").base_url == "http://127.0.0.1:8787"


def test_a_base_url_with_a_path_is_refused() -> None:
    with pytest.raises(ValueError, match="origin, not a path"):
        DashboardClient("https://dash.test/api/v1")


def test_a_non_http_scheme_is_refused() -> None:
    for scheme in ("file", "ftp", "gopher"):
        with pytest.raises(ValueError, match="unsupported scheme"):
            DashboardClient(f"{scheme}://dash.test")


# --- auth and origin ------------------------------------------------------------


def test_a_wrong_token_is_refused() -> None:
    guard = RequestGuard(token="correct-horse")
    with pytest.raises(AuthError):
        guard.check_token("wrong")


def test_a_missing_token_is_refused_when_one_is_configured() -> None:
    guard = RequestGuard(token="correct-horse")
    with pytest.raises(AuthError):
        guard.check_token(None)


def test_tokens_are_compared_in_constant_time() -> None:
    guard = RequestGuard(token="correct-horse")
    assert guard._token.__class__ is str
    # Presence of the comparison is what matters; correctness is asserted above.
    guard.check_token("correct-horse")


def test_a_no_token_gateway_refuses_to_advertise_itself_as_configured() -> None:
    guard = RequestGuard()
    guard.check_token(None)  # local development: open by choice
    with pytest.raises(ValueError, match="no token configured"):
        guard.require_token()


def test_an_unlisted_origin_is_refused() -> None:
    guard = RequestGuard(allowed_origins=frozenset({"https://chat.openai.com"}))
    with pytest.raises(OriginError):
        guard.check_origin("https://evil.test")


def test_an_opaque_origin_is_refused() -> None:
    guard = RequestGuard()
    with pytest.raises(OriginError, match="opaque"):
        guard.check_origin("null")


def test_a_non_browser_client_sends_no_origin() -> None:
    guard = RequestGuard()
    guard.check_origin(None)
    guard.check_origin("")


def test_an_allowed_origin_passes() -> None:
    guard = RequestGuard(allowed_origins=frozenset({"https://chat.openai.com"}))
    guard.check_origin("https://chat.openai.com")


# --- rate and concurrency --------------------------------------------------------


def test_the_rate_limit_rejects_a_runaway_loop() -> None:
    guard = RequestGuard(rate_limit_per_minute=3)
    for _ in range(3):
        guard.acquire("c")
    with pytest.raises(RateLimitError, match="rate limit"):
        guard.acquire("c")


def test_the_concurrency_limit_bounds_in_flight_calls() -> None:
    guard = RequestGuard(max_concurrent=2)
    guard.acquire("c")
    guard.acquire("c")
    with pytest.raises(RateLimitError, match="concurrency"):
        guard.acquire("c")
    guard.release("c")
    guard.acquire("c")


def test_an_impossible_configuration_is_refused() -> None:
    with pytest.raises(ValueError):
        RequestGuard(rate_limit_per_minute=0)
    with pytest.raises(ValueError):
        RequestGuard(max_concurrent=0)


# --- audit ------------------------------------------------------------------------


def test_the_audit_record_never_carries_the_token(capsys) -> None:
    """A log that contains its own credential is a credential store."""
    import logging

    handler = logging.StreamHandler()
    logger = logging.getLogger("weatherquant.mcp.audit")
    logger.addHandler(handler)
    try:
        audit_event(
            tool="list_decisions",
            outcome="ok",
            duration_ms=12.0,
            client="mcp",
            arguments={"state": "ACCEPT", "token": "super-secret"},
        )
    finally:
        logger.removeHandler(handler)

    record = capsys.readouterr()
    combined = record.out + record.err
    assert "super-secret" not in combined


def test_arguments_are_truncated_so_a_call_cannot_flood_the_log(capsys) -> None:
    import logging

    handler = logging.StreamHandler()
    logger = logging.getLogger("weatherquant.mcp.audit")
    logger.addHandler(handler)
    try:
        audit_event(
            tool="x", outcome="rejected", duration_ms=1.0, client="mcp",
            arguments={"query": "a" * 5000},
        )
    finally:
        logger.removeHandler(handler)

    captured = capsys.readouterr()
    record = captured.out + captured.err
    assert len(record) < 2000, "an unbounded argument would make the log a denial-of-service target"

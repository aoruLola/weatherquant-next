"""The thirteen read-only tools, and the gateway that owns them.

Every tool does the same three things: translate natural-language intent
into a *bounded* dashboard query, relay the answer, and state where the
answer came from. None of them computes. The edge figures come from the
performance snapshots, the win rate comes from the performance snapshot,
and ``source_endpoint`` names the API path that produced each one - so a
client can always tell a relayed figure from an invented one, and the MCP
layer can never disagree with the dashboard about a number.

Descriptions are written for a model, not a person. Each one says when to
use it, what it will not answer, and what the caller must not conclude -
because the failure this design guards against is not a tool error, it is
a confident wrong sentence in a chat window.
"""

from __future__ import annotations

import re
from typing import Any

from weatherquant.mcp import GATEWAY_VERSION
from weatherquant.mcp.client import ClientError, DashboardClient, TimeoutError
from weatherquant.mcp.security import RequestGuard, audit_event, current_caller

__all__ = ["Gateway", "build_server", "market_path"]

#: Tools that answer about *now*. The distinction matters because a client
#: that asks "what is happening" and gets a week-old settlement will
#: describe it in the present tense.
_REALTIME_TOOLS = frozenset(
    {"get_overview", "get_system_health", "list_markets", "get_market_detail", "get_forecasts"}
)


class Gateway:
    """Holds the client and the guard; one instance per running server."""

    def __init__(self, client: DashboardClient, guard: RequestGuard) -> None:
        self.client = client
        self.guard = guard

    def call(self, tool: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch, stamp and return. One place, so every answer looks alike."""
        # Keyed by the authenticated caller, not by the tool. Keying by tool
        # would hand one caller a fresh quota per tool while making unrelated
        # callers contend for a single one.
        caller = current_caller()
        self.guard.acquire(caller)
        try:
            payload = self.client.get_json(path, params=params)
        finally:
            self.guard.release(caller)

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        freshness = payload.get("freshness") if isinstance(payload.get("freshness"), dict) else {}
        envelope = {
            "tool_name": tool,
            "source_endpoint": path,
            "schema_version": payload.get("schema_version"),
            "generated_at_utc": payload.get("generated_at_utc"),
            "as_of_utc": payload.get("generated_at_utc"),
            "freshness": freshness,
            "dataset": payload.get("dataset"),
            "mode": data.get("mode") if isinstance(data, dict) else None,
            "sample_size": _sample_size(data),
            "statistical_scope": _scope(data),
            "warnings": _warnings(data, freshness, tool),
            "gateway_version": GATEWAY_VERSION,
            "data": data,
        }
        return envelope

    def call_raw(self, tool: str, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch without unwrapping - for payloads whose data is a list."""
        caller = current_caller()
        self.guard.acquire(caller)
        try:
            payload = self.client.get_json(path, params=params)
        finally:
            self.guard.release(caller)
        return {
            "tool_name": tool,
            "source_endpoint": path,
            "schema_version": payload.get("schema_version"),
            "generated_at_utc": payload.get("generated_at_utc"),
            "as_of_utc": payload.get("generated_at_utc"),
            "freshness": payload.get("freshness") or {},
            "dataset": payload.get("dataset"),
            "gateway_version": GATEWAY_VERSION,
            "data": payload.get("data"),
        }


def _sample_size(data: Any) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in ("sample_size", "total", "total_rows", "opportunity_episodes", "evaluated"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def _scope(data: Any) -> list[str]:
    if isinstance(data, dict):
        scope = data.get("statistical_scope")
        if isinstance(scope, list):
            return [str(s) for s in scope]
    return []


def _warnings(data: Any, freshness: dict[str, Any], tool: str) -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        given = data.get("warnings")
        if isinstance(given, list):
            out.extend(str(w) for w in given)
        # A nested data block may carry its own scope and caveats.
        nested = data.get("data")
        if isinstance(nested, dict):
            nested_warnings = nested.get("warnings")
            if isinstance(nested_warnings, list):
                out.extend(str(w) for w in nested_warnings)
    state = str(freshness.get("state") or "")
    if state and state != "fresh":
        out.append(
            f"DATA IS {state.upper()}: "
            f"{freshness.get('reason') or 'the underlying snapshot is not current'}. "
            "Do not describe this as current."
        )
    if tool in _REALTIME_TOOLS:
        out.append("Figures describe the last published snapshot, not a live trading feed.")
    return out


#: One segment, no dots. A market id is an integer from the dashboard, and
#: refusing dots is what keeps ``..`` from becoming a traversal no matter
#: what the allowlist pattern later says.
_MARKET_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def market_path(market_id: str) -> str:
    """Build the market-detail path from a validated id.

    The id is checked here *and* again by ``resolve_path`` in the client.
    Two checks on one path is deliberate: this one produces a readable error
    for a bad argument, and that one is the only thing standing between a
    tool call and an outbound request - neither is allowed to be the single
    point of failure for the other's mistake.
    """
    candidate = str(market_id or "")
    if not _MARKET_ID.match(candidate):
        raise ValueError(
            f"market_id {market_id!r} is not a valid identifier: expected 1-64 "
            "characters from A-Z a-z 0-9 _ or -"
        )
    return f"/api/v1/markets/{candidate}"


def build_server(gateway: Gateway) -> Any:
    """Construct the MCP server with all thirteen tools registered."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="weatherquant-mcp",
        instructions=(
            "Read-only access to WeatherQuant, a weather-market research system. "
            "It collects Polymarket weather markets and runs model decisions in "
            "shadow mode: nothing is ever bought or sold, and no figure here is "
            "a realised return. When a response carries `warnings` or a non-fresh "
            "`freshness.state`, repeat that in the answer rather than smoothing "
            "it away. Theoretical edge is a model expectation; realised win rate "
            "exists only where settlement data proves it."
        ),
    )

    @server.tool(
        name="get_overview",
        title="运行总览",
        description=(
            "WeatherQuant 运行总览：Collector/Shadow 状态、活跃市场数、订阅 Token、"
            "最近决策计数与数据新鲜度。回答「系统现在健康吗」时先用这个。\n\n"
            "不会给出任何预测或交易建议。"
        ),
    )
    def get_overview() -> dict[str, Any]:
        return gateway.call("get_overview", "/api/v1/overview")

    @server.tool(
        name="get_system_health",
        title="系统健康",
        description=(
            "Collector 与 Shadow 的健康状态：REST/WS 连接、上游接触年龄、磁盘、"
            "数据库与归档大小、版本。\n\n"
            "适合回答「采集是否中断」「Shadow 是否还在跑」。"
        ),
    )
    def get_system_health() -> dict[str, Any]:
        overview = gateway.call("get_system_health", "/api/v1/overview")
        data = overview.get("data") or {}
        overview["data"] = {
            "collector": data.get("collector"),
            "shadow": data.get("shadow"),
            "system_health": data.get("system_health"),
        }
        return overview

    @server.tool(
        name="list_markets",
        title="查询当前市场",
        description=(
            "列出天气市场：题目、温度区间、目标日、盘口 bid/ask/spread/深度、"
            "新鲜度与是否可交易。\n\n"
            "可按 status(all|tradeable|active|closed)、目标日、盘口状态筛选。\n"
            "报价是盘口快照，不是可保证成交的价格。"
        ),
    )
    def list_markets(
        status: str = "tradeable",
        limit: int = 25,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"status": status, "limit": max(1, min(limit, 200))}
        if target_date:
            params["date"] = target_date
        return gateway.call_raw("list_markets", "/api/v1/markets", params=params)

    @server.tool(
        name="get_market_detail",
        title="市场详情",
        description=(
            "单个市场的完整详情：盘口层级、模型概率、理论净 Edge、最近决策。\n\n"
            "需要 market_id。找不到时明确返回 not found，不会用相似市场顶替。"
        ),
    )
    def get_market_detail(market_id: str) -> dict[str, Any]:
        return gateway.call_raw("get_market_detail", market_path(market_id))

    @server.tool(
        name="get_forecasts",
        title="读取天气预测",
        description=(
            "读取四类模型（calibrated/ecmwf_raw/fusion/gfs_raw）的预测与温度概率分布，"
            "含预测发布时间与 point-in-time 信息。\n\n"
            "严格按时间顺序：不会用发布时刻之后的信息。缺数据时显示不可用而非填值。"
        ),
    )
    def get_forecasts() -> dict[str, Any]:
        return gateway.call_raw("get_forecasts", "/api/v1/forecasts")

    @server.tool(
        name="list_decisions",
        title="查询 Shadow 决策",
        description=(
            "分页查询 Shadow 决策：状态、模式(LIVE/REPLAY)、模型、方向、"
            "模型概率、评估价格(盘口快照)、理论净 Edge、拒绝原因。\n\n"
            "回答「最近有哪些 ACCEPT」「为什么被拒绝」时用这个。\n"
            "ACCEPT 表示通过过滤条件，不代表盈利机会。REPLAY 不是当时的实时信号。"
        ),
    )
    def list_decisions(
        state: str | None = None,
        mode: str | None = None,
        model: str | None = None,
        limit: int = 25,
        cursor: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200)), "cursor": max(0, cursor)}
        if state:
            params["state"] = state
        if mode:
            params["mode"] = mode
        if model:
            params["model"] = model
        return gateway.call_raw("list_decisions", "/api/v1/decisions", params=params)

    @server.tool(
        name="get_shadow_report",
        title="Shadow 累计报表",
        description=(
            "Shadow 的累计统计：决策分布、理论 Edge、覆盖率、数据质量、"
            "LIVE/REPLAY 拆分、ACCEPT 独立度(accept_scope)。\n\n"
            "统计范围是整个 shadow 数据库，重启不清零——回答时必须说明这一点。"
        ),
    )
    def get_shadow_report() -> dict[str, Any]:
        return gateway.call_raw("get_shadow_report", "/api/v1/shadow/report")

    @server.tool(
        name="get_edge_summary",
        title="理论 Edge 汇总",
        description=(
            "理论 Edge 分布：观测口径(sum/mean/median/分位数)与去重后的独立机会口径，"
            "按模型、方向、目标日分组，含每日趋势与 Edge 分档。\n\n"
            "回答「当前最大理论 Edge 在哪」「累计理论 Edge 是多少」时用这个。\n"
            "Observation Edge Sum 是重复评估的汇总，不是利润、不是资产增长、"
            "不是独立交易机会的期望收益。"
        ),
    )
    def get_edge_summary() -> dict[str, Any]:
        return gateway.call_raw("get_edge_summary", "/api/v1/performance/edge")

    @server.tool(
        name="get_opportunities",
        title="独立机会列表",
        description=(
            "去重后的独立机会(Opportunity Episode)：首次/末次 ACCEPT 时间、"
            "持续时长、重复次数、代表 Edge、模型概率、评估价格、结算与 WIN/LOSS。\n\n"
            "回答「去重后有多少独立机会」时用这个，不要用 ACCEPT 观测次数。\n"
            "同一市场的多个 episode 不是独立结算事件。\n\n"
            "分页：默认 25 条，单次最多 200 条。用返回的 next_cursor 取下一页，"
            "prev_cursor 取上一页；游标属于某一批次，批次变了会返回 409，"
            "此时必须从第一页重新开始，不得拼接两批数据。\n"
            "筛选：model / direction / mode / target_date / outcome / min_edge / max_edge。"
        ),
    )
    def get_opportunities(
        limit: int = 25,
        cursor: str | None = None,
        model: str | None = None,
        direction: str | None = None,
        mode: str | None = None,
        target_date: str | None = None,
        outcome: str | None = None,
        min_edge: float | None = None,
        max_edge: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        for name, value in (
            ("cursor", cursor),
            ("model", model),
            ("direction", direction),
            ("mode", mode),
            ("target_date", target_date),
            ("outcome", outcome),
            ("min_edge", min_edge),
            ("max_edge", max_edge),
        ):
            # Absent filters are not sent at all: an empty string is a filter
            # value, and "no filter" is not the same request.
            if value is not None:
                params[name] = value
        return gateway.call_raw(
            "get_opportunities",
            "/api/v1/performance/opportunities",
            params=params,
        )

    @server.tool(
        name="get_win_rate",
        title="实际胜率",
        description=(
            "已验证结算的真实方向胜率：WIN/(WIN+LOSS)、计数、置信区间、"
            "按模型/方向/模式/目标日/Edge 分档分组。\n\n"
            "回答「最近七天胜率如何」时用这个。\n"
            "只有 RESOLVED 且已验证的样本进分母；未结算不计入，不等于失败。\n"
            "样本不足时返回 null 并说明原因——此时必须如实说样本不足，"
            "不得用 ACCEPT 比例代替胜率。"
        ),
    )
    def get_win_rate() -> dict[str, Any]:
        return gateway.call_raw("get_win_rate", "/api/v1/performance/win-rate")

    @server.tool(
        name="get_settlements",
        title="官方结算",
        description=(
            "Polymarket 官方结算与验证状态：结算状态(RESOLVED/PENDING/DISPUTED/"
            "UNAVAILABLE/UNVERIFIABLE)、赔付、验证结果。\n\n"
            "回答「哪些机会还没结算」时用这个。\n"
            "平台结算与气象观测分别存储，互不替代。"
        ),
    )
    def get_settlements(limit: int = 25, cursor: int = 0) -> dict[str, Any]:
        return gateway.call_raw(
            "get_settlements",
            "/api/v1/performance/settlements",
            params={"limit": max(1, min(limit, 200)), "cursor": max(0, cursor)},
        )

    @server.tool(
        name="compare_models",
        title="模型比较",
        description=(
            "比较四个模型：各自理论 Edge 分布与已验证胜率。\n\n"
            "回答「哪个模型最准确」时用这个。\n"
            "Brier/LogLoss/ECE 等样本外评分由研究评估模块提供；"
            "在结算数据不足时，胜率一侧会显示样本不足——如实转述，不要外推。"
        ),
    )
    def compare_models() -> dict[str, Any]:
        return gateway.call_raw("compare_models", "/api/v1/performance/models")

    @server.tool(
        name="simulate_reference_capital",
        title="参考本金模拟",
        description=(
            "用指定参考本金测算独立机会的理论预期收益与资金占用。\n\n"
            "回答「用 100 USDC 参考本金，理论预期收益是多少」时用这个。\n"
            "参数：capital_usdc(默认 100，必须 >0)、"
            "mode(single_opportunity|portfolio)、max_allocation_fraction(默认 0.10)。\n\n"
            "这是研究假设，不是真实资金、不是虚拟账户、不是已实现利润。\n"
            "盘口深度不足或缺层级证据时标记 NOT_EXECUTABLE，只给理论值。\n"
            "当前系统不下单，实际收益恒为 N/A。"
        ),
    )
    def simulate_reference_capital(
        capital_usdc: float = 100.0,
        mode: str = "portfolio",
        max_allocation_fraction: float = 0.10,
    ) -> dict[str, Any]:
        if capital_usdc <= 0:
            raise ValueError("capital_usdc must be greater than 0")
        if mode not in ("portfolio", "single_opportunity"):
            raise ValueError("mode must be 'portfolio' or 'single_opportunity'")
        if not 0 < max_allocation_fraction <= 1:
            raise ValueError("max_allocation_fraction must be in (0, 1]")
        result = gateway.call_raw(
            "simulate_reference_capital",
            "/api/v1/performance/reference-capital",
            params={
                "capital_usdc": capital_usdc,
                "mode": mode,
                "max_allocation_fraction": max_allocation_fraction,
            },
        )
        result["statistical_scope"] = ["reference_capital_hypothetical"]
        return result

    return server


def _wrap(fn, name: str):
    """Run a tool with audit, mapping transport errors to readable ones."""

    def wrapper(*args, **kwargs):
        import time

        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except TimeoutError as exc:
            audit_event(tool=name, outcome="timeout", duration_ms=(time.perf_counter() - started) * 1000, client="mcp", detail=str(exc))
            return {"tool_name": name, "error": str(exc), "data": None, "warnings": ["upstream timed out; nothing here is current"]}
        except ClientError as exc:
            audit_event(tool=name, outcome="error", duration_ms=(time.perf_counter() - started) * 1000, client="mcp", detail=str(exc))
            return {"tool_name": name, "error": str(exc), "data": None, "warnings": ["dashboard could not answer; no figure is available"]}
        except ValueError as exc:
            audit_event(tool=name, outcome="rejected", duration_ms=(time.perf_counter() - started) * 1000, client="mcp", detail=str(exc), arguments=kwargs)
            return {"tool_name": name, "error": str(exc), "data": None, "warnings": ["arguments rejected; nothing was computed"]}
        audit_event(tool=name, outcome="ok", duration_ms=(time.perf_counter() - started) * 1000, client="mcp")
        return result

    return wrapper

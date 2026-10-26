# MCP Gateway 独立镜像发布 Runbook（WQN-50）

2026-10-26。工作流：`.github/workflows/mcp-release.yml`（已存在，本文档与其
逐一对应）。镜像：`ghcr.io/aorulola/weatherquant-next-mcp`。

**本 Runbook 的立场：合并、发布、部署、验收是四个独立状态。** 一个绿了的
`mcp-release` run 只证明"镜像可发布"，不证明"已发布"，更不证明"生产已
部署且验收通过"。每前进一步，在 §6 的状态表里记一行证据。

---

## 1. 镜像的独立边界（为什么 MCP 有自己的发布流水线）

`mcp-release.yml` 只能触碰 `-mcp` 包：

- 自己的 tag 命名空间（`mcp-v*`），自己的可变指针（`mcp-stable`），
  不可变的回滚锚点（`sha-<commit>`）；
- **绝不**写 collector / shadow / dashboard 的包——那是四个 workflow、
  四个镜像名、各自的 `release` environment 审批门；
- 发布 job 挂在 `release` environment 后面，需要指定评审人放行。

发布一个网关**永远不能**顺带移动喂它的引擎的 tag；反之，采集器发布
也不要求网关跟着动。

## 2. 发布前核对清单（代码合并门）

CI（`pytest -q` + `ruff` + 专项检查）覆盖以下每一条；本机复跑命令在括号里。

| # | 核对项 | 证据 |
|---|---|---|
| 1 | **恰好 13 个只读工具**，名字无任何下单语义（order/trade/buy/sell/execute/cancel/withdraw/transfer） | `pytest tests/test_mcp_gateway.py::test_all_thirteen_tools_are_registered`；socket 级：`tests/test_mcp_http.py::test_the_socket_serves_exactly_thirteen_read_only_tools` |
| 2 | **每个工具对上游只发 GET**，且路径全部落在白名单 | `test_every_tool_call_is_a_get_to_an_allowlisted_endpoint`（真实 socket + 记录动词的替身 dashboard） |
| 3 | **Origin**：不在白名单 → 403；`Origin: null` → 403；无 Origin（非浏览器）→ 仍需 token | `test_a_disallowed_origin_is_refused` 等（socket 级） |
| 4 | **Host**：伪造 Host 头 → 421，上游零接触（DNS rebinding 防护跑在真实传输上） | `test_a_foreign_host_header_never_reaches_a_tool` |
| 5 | **Token**：缺失/错误/非 Bearer → 401；拒绝的调用**永不触达 dashboard**；入站 token 永不转发上游；两个凭据独立配置 | `test_a_request_without_authorization_is_refused`、`test_the_two_credentials_are_configured_independently` 等 |
| 6 | **分页 ≤ 200**：`list_decisions` / `get_opportunities` / `get_settlements` 传 10⁹ 也在中继前被钳到 200 | `test_pagination_arguments_are_clamped_upstream` |
| 7 | **超时/异常码**：上游超时 → 200 + `data:null` + "upstream timed out" 警示；上游 5xx → 同结构的显式无答案。**不是** JSON-RPC traceback | `test_an_upstream_timeout_is_an_explicit_no_answer`、`test_an_upstream_5xx_is_an_explicit_no_answer`（2026-10-26 修复：`_wrap` 此前从未被套用到注册的工具上，异常直接穿透到传输层——单测测的是没人调用的包装器） |
| 8 | **非公网 bind**：无 token 绑 0.0.0.0 → 进程拒绝启动（`require_token`） | `test_a_non_loopback_bind_without_a_token_never_starts_serving`；容器级由 `mcp-release.yml` verify job 的 "refuses to start open" 步骤复验 |
| 9 | **不直接访问生产 DuckDB**：compose 无任何 volume、无 docker.sock、无 Portainer 凭据；源码无 wallet/private_key/place_order | `tests/test_workflows.py::test_the_mcp_container_has_no_data_mount_at_all`、`::test_the_mcp_container_has_no_order_tool_imported` |
| 10 | **限速与并发**：60 次/分钟/调用者、4 并发、全局 4×上限 | `test_the_rate_limit_rejects_a_runaway_loop` 等 |

```bash
# 本机一键复跑 1–10 的测试面
uv run pytest tests/test_mcp_gateway.py tests/test_mcp_http.py tests/test_workflows.py -q
```

## 3. 发布步骤

1. **版本对齐**：tag `mcp-vX.Y.Z` 必须等于 `pyproject.toml` 的
   `version = "X.Y.Z"`（workflow 第一步强制；不一致直接红）。要升版本就
   先改 pyproject、提交，再打 tag。
2. **打 tag 并推送**：
   ```bash
   git tag mcp-v0.1.5 && git push origin mcp-v0.1.5
   ```
   或手动 `workflow_dispatch`（必填 reason）。
3. **verify job 全绿**（≈25 分钟上限）：
   - 版本对齐、ruff、全量 pytest、13 工具无下单语义检查；
   - 构建 `Dockerfile.mcp`（不推送），断言镜像 `Config.Volumes` 为空、
     compose 无 `docker.sock|.duckdb|/portainer`；
   - **容器拒绝无 token 的非回环启动**（exit ≠ 0）；
   - 容器内非 root（uid≠0）、健康探针通、未认证请求 401 / 错 token 401 /
     对 token 非 401。
4. **publish job**：`release` environment 审批 → 推送
   `sha-<full-sha>`（不可变）、`mcp-vX.Y.Z`、移动 `mcp-stable`。
   `latest=false`：没有谁默认跟踪一个没人文档化的指针。

## 4. 部署（NAS，需单独授权）

**生产 MCP 部署是独立授权动作**——批准发布 ≠ 批准部署。

```bash
# docker/compose.mcp.unraid.yaml 的 stack .env：
MCP_SHA=<verify 通过的那个 commit sha>          # 必填：无 sha 拒绝启动
WQ_MCP_DASHBOARD_URL=http://weatherquant-dashboard:8787
WQ_MCP_PLAINTEXT_NETWORKS=172.16.0.0/12          # 只放行真正明文的内网段
WQ_MCP_TOKEN=<新的入站 bearer>                   # 非回环 bind 必需
WQ_MCP_DASHBOARD_TOKEN=<出站凭据>                # 与入站是两个秘密
WQ_MCP_ALLOWED_ORIGINS=                          # 空 = 拒绝所有浏览器 Origin
```

Apply 后核对：

```bash
docker inspect weatherquant-mcp --format '{{.State.Health.Status}}'   # healthy
docker inspect weatherquant-mcp --format '{{json .HostConfig.Binds}}' # null / []
docker exec weatherquant-mcp id -u                                    # != 0

# 工具发现（MCP 客户端 list_tools → 13 个）与一次真实调用
# 非法请求负测（在 NAS 上对生产网关做一次）：
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/mcp                     # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer wrong' \
  http://127.0.0.1:8788/mcp                                                            # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: https://evil.test' \
  -H "Authorization: Bearer $WQ_MCP_TOKEN" http://127.0.0.1:8788/mcp                   # 403
```

**明确不做、且任何工具都做不到的事**：读生产 DuckDB（无挂载可读）、下单
（无下单工具、无钱包、无交易所凭据代码——CI 断言其不存在）。

## 5. 回滚与轮换

```bash
MCP_SHA=<上一个已验证的 sha>   # Apply —— 就这两步
```

无状态、无迁移、无旧 token 残留。轮换 `WQ_MCP_TOKEN` = 改 env + Apply，
旧凭据立即失效。回滚判据见 `performance-deployment.md` §6.4（含"出现任何
下单/钱包/私钥代码路径 → 立即回滚并保留日志"）。

## 6. 状态表（每步一行证据）

| 状态 | 内容 | 当前 | 证据 |
|---|---|---|---|
| 代码合并 | 本 PR：`_wrap` 真正生效 + socket 级安全 e2e/负测 | 🟡 评审中 | CI 全绿 + §2 清单 |
| 镜像发布 | `mcp-v*` tag → verify 全绿 → publish → `mcp-stable` 前进 | ⚪ 未做 | GHCR `sha-<commit>` |
| 生产部署 | NAS stack Apply（单独授权） | ⚪ 未做 | §4 inspect/curl 输出 |
| 验收完成 | §4 全部负测在**生产网关**上复跑并留档 | ⚪ 未做 | 留档输出 |

## 7. 本轮修复的已知缺口（不要回退）

1. **`_wrap` 曾是死代码**（2026-10-26 发现并修复）：13 个工具注册时未套用
   审计/错误映射包装器，超时与上游 5xx 会以 JSON-RPC 异常穿透到客户端，
   而非合同承诺的"显式无答案"；单测测的恰是无人调用的包装器。
   现经 `_register` 装饰器注册，socket 级测试断言映射真实运行。
   `functools.wraps` 是必要条件：SDK 按注册函数签名建参数校验模型，
   裸 `(*args, **kwargs)` 会让每次调用在 pydantic 校验处被拒。
2. 超时测试若用 MockTransport 的 sleep 永远不会触发客户端超时（无 socket
   I/O）；以 `httpx.ConnectTimeout` 从 handler 抛出，与线上等价。

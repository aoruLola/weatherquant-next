# MCP Gateway 独立镜像发布 Runbook（WQN-50）

2026-10-26 初版；2026-11-13（WQN-50）补齐非法参数/未知工具负测、默认 bind
与"不打开生产库"断言、构建与 Host 配置说明。工作流：
`.github/workflows/mcp-release.yml`（已存在，本文档与其逐一对应）。
镜像：`ghcr.io/aorulola/weatherquant-next-mcp`。

**本 Runbook 的立场：合并、发布、部署、验收是四个独立状态。** 一个绿了的
`mcp-release` run 只证明"镜像可发布"，不证明"已发布"，更不证明"生产已
部署且验收通过"。每前进一步，在 §7 的状态表里记一行证据。

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
| 8 | **非法参数 / 未知工具**：traversal 形状的 `market_id`、越界 `capital_usdc`、类型错误的 `limit`、不存在的工具名，均返回显式拒绝且上游零接触 | `test_an_invalid_market_id_is_rejected_before_any_fetch`、`test_an_out_of_range_argument_is_rejected`、`test_an_argument_of_the_wrong_type_is_refused_by_the_schema`、`test_an_unknown_tool_is_refused_without_touching_the_dashboard`（socket 级） |
| 9 | **非公网 bind**：默认监听 `127.0.0.1`；无 token 绑 0.0.0.0 → 进程拒绝启动（`require_token`） | `test_the_default_bind_is_loopback`、`test_a_non_loopback_bind_without_a_token_never_starts_serving`；容器级由 `mcp-release.yml` verify job 的 "refuses to start open" 步骤复验 |
| 10 | **不直接访问生产 DuckDB**：导入入口后 `sys.modules` 中无 duckdb/sqlite3；包内无数据库/锁/写模式代码；compose 无任何 volume、无 docker.sock、无 Portainer 凭据；源码无 wallet/private_key/place_order | `test_importing_the_gateway_never_opens_a_database`、`test_the_gateway_package_contains_no_database_code`、`tests/test_workflows.py::test_the_mcp_container_has_no_data_mount_at_all`、`::test_the_mcp_container_has_no_order_tool_imported` |
| 11 | **限速与并发**：60 次/分钟/调用者、4 并发、全局 4×上限 | `test_the_rate_limit_rejects_a_runaway_loop` 等 |
| 12 | **非法 token 不得变成 5xx**：任意字节（含非 ASCII）的 bearer → 401，比较在字节层面进行、恒定时间 | `test_a_non_ascii_bearer_is_refused_rather_than_crashing` |

```bash
# 本机一键复跑 1–12 的测试面
uv run pytest tests/test_mcp_gateway.py tests/test_mcp_http.py tests/test_workflows.py -q
```

## 3. 构建（`Dockerfile.mcp`）

本地复现 CI 的构建与镜像自检：

```bash
docker build -f Dockerfile.mcp -t weatherquant-mcp:local .
docker inspect weatherquant-mcp:local --format '{{json .Config.Volumes}}'   # null / {}
docker image inspect weatherquant-mcp:local --format '{{.Config.User}}'     # wq
```

镜像只装 `mcp` 依赖组、无 `VOLUME`、无数据目录、以非 root 的 `wq` 运行：
gateway 只调用一个已配置 dashboard 的 HTTP API，没有任何本地存储可读可写。

## 4. 发布步骤

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

## 5. 部署（NAS，需单独授权）

**生产 MCP 部署是独立授权动作**——批准发布 ≠ 批准部署。

```bash
# docker/compose.mcp.unraid.yaml 的 stack .env：
MCP_SHA=<verify 通过的那个 commit sha>          # 必填：无 sha 拒绝启动
WQ_MCP_DASHBOARD_URL=http://weatherquant-dashboard:8787
WQ_MCP_PLAINTEXT_NETWORKS=172.16.0.0/12          # 只放行真正明文的内网段
WQ_MCP_TOKEN=<新的入站 bearer>                   # 非回环 bind 必需
WQ_MCP_DASHBOARD_TOKEN=<出站凭据>                # 与入站是两个秘密
WQ_MCP_ALLOWED_ORIGINS=                          # 空 = 拒绝所有浏览器 Origin
WQ_MCP_ALLOWED_HOSTS=weatherquant-mcp:8788       # 额外接受的 Host 头（反代/服务名）
```

**bind**：容器内 `WQ_MCP_HOST=0.0.0.0`（显式写在 compose 里，不是默认值），
宿主端口发布为 `127.0.0.1:8788:8788`——只有本机可达。要对 LAN 提供服务时，
把发布地址换成 NAS 自己的地址**并**设置 `WQ_MCP_TOKEN`；进程本身也会拒绝
无 token 的非回环 bind。公网入口必须是你自己运维的、做认证的 TLS 反向代理，
本容器不终止 TLS。

**允许的 Origin / Host**：`WQ_MCP_ALLOWED_ORIGINS` 为空表示拒绝一切浏览器
Origin（适合服务端 agent）；浏览器客户端必须逐条列入，`Origin: null` 永远
被拒。`WQ_MCP_ALLOWED_HOSTS` 追加反代或服务名使用的 Host 值，未列出的 Host
由 SDK 的 DNS-rebinding 防护直接回 421。两者都是默认拒绝，不是默认放行。

Apply 后核对：

```bash
docker inspect weatherquant-mcp --format '{{.State.Health.Status}}'   # healthy
docker inspect weatherquant-mcp --format '{{json .HostConfig.Binds}}' # null / []
docker exec weatherquant-mcp id -u                                    # != 0

# 健康检查是进程自身 listener 的 liveness 探针（TCP connect），
# 不依赖 dashboard：上游抖动不会让网关被反复重启。
docker inspect weatherquant-mcp --format '{{json .State.Health}}'

# 工具发现（MCP 客户端 list_tools → 13 个）与一次真实调用
# 非法请求负测（在 NAS 上对生产网关做一次）：
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/mcp                     # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer wrong' \
  http://127.0.0.1:8788/mcp                                                            # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Origin: https://evil.test' \
  -H "Authorization: Bearer $WQ_MCP_TOKEN" http://127.0.0.1:8788/mcp                   # 403
```

**明确不做、且任何工具都做不到的事**：读生产 DuckDB（无挂载可读、进程内不
加载任何数据库驱动——`test_importing_the_gateway_never_opens_a_database`）、
下单（无下单工具、无钱包、无交易所凭据代码——CI 断言其不存在）。

## 6. 回滚与轮换

```bash
MCP_SHA=<上一个已验证的 sha>   # Apply —— 就这两步
```

无状态、无迁移、无旧 token 残留。轮换 `WQ_MCP_TOKEN` = 改 env + Apply，
旧凭据立即失效。回滚判据见 `performance-deployment.md` §6.4（含"出现任何
下单/钱包/私钥代码路径 → 立即回滚并保留日志"）。

## 7. 状态表（每步一行证据）

| 状态 | 内容 | 当前 | 证据 |
|---|---|---|---|
| 代码合并 | WQN-50：`_wrap` 真正生效 + socket 级安全 e2e/负测 + 非 ASCII token 修复 | 🟡 评审中 | CI 全绿 + §2 清单 |
| 镜像发布 | `mcp-v*` tag → verify 全绿 → publish → `mcp-stable` 前进 | ⚪ 未做 | GHCR `sha-<commit>` |
| 生产部署 | NAS stack Apply（单独授权） | ⚪ 未做 | §5 inspect/curl 输出 |
| 验收完成 | §5 全部负测在**生产网关**上复跑并留档 | ⚪ 未做 | 留档输出 |

## 8. 本轮修复的已知缺口（不要回退）

1. **`_wrap` 曾是死代码**（2026-10-26 发现并修复）：13 个工具注册时未套用
   审计/错误映射包装器，超时与上游 5xx 会以 JSON-RPC 异常穿透到客户端，
   而非合同承诺的"显式无答案"；单测测的恰是无人调用的包装器。
   现经 `_register` 装饰器注册，socket 级测试断言映射真实运行。
   `functools.wraps` 是必要条件：SDK 按注册函数签名建参数校验模型，
   裸 `(*args, **kwargs)` 会让每次调用在 pydantic 校验处被拒。
2. 超时测试若用 MockTransport 的 sleep 永远不会触发客户端超时（无 socket
   I/O）；以 `httpx.ConnectTimeout` 从 handler 抛出，与线上等价。
3. **非 ASCII bearer 曾变成 500**（WQN-50 发现并修复）：`check_token` 直接对
   解码后的 str 做 `hmac.compare_digest`，而该函数对含非 ASCII 字符的 str 抛
   `TypeError`——HTTP 头允许任意字节，于是"错误 token"被答成服务器错误而
   不是 401。现改为对 UTF-8 字节比较，仍是恒定时间；socket 级测试用裸
   socket 发送非 ASCII 头断言 401 且上游零接触。不要退回 str 比较。
4. 异常参数与未知工具的负测跑在真实传输上：旧套件只证明了 `market_path`
   会拒绝 traversal，没有证明运行中的 server 拒绝它；`test_an_invalid_market_id_is_rejected_before_any_fetch`
   与 `test_an_unknown_tool_is_refused_without_touching_the_dashboard` 补的就是这段距离。

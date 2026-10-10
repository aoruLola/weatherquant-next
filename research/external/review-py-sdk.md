# Polymarket 官方 SDK 与 API 审计

> WeatherQuant Next 技术选型审计 · 审计日期 2026-10-10
> 审计对象：`Polymarket/py-sdk` 仓库（已 shallow clone 至 `E:/Project/.wqn-audit/py-sdk`）与官方 API 文档（docs.polymarket.com）
> 标注约定：**[已验证]** = 亲自读源码 / 调 API / 查 PyPI 拿到的原始证据；**[推断]** = 基于证据的合理推断。
> 本目录附带证据文件：`data-api-openapi.json`、`clob-openapi.yaml`、`gamma-openapi.yaml`、`weather-market-*.json`、`search-*.json`、测试 venv `.venv-sdktest`。

---

## 0. 元信息（commit, 版本, 最后发布）

| 项目 | 值 | 标注 |
|---|---|---|
| 仓库 | https://github.com/Polymarket/py-sdk | [已验证] |
| clone commit | `f1e2ab2c86d04573c304ca7afe8f1e36fcbd6237`，commit 日期 **2026-10-09**（审计前一天），release-please 合并 PR #324 | [已验证] |
| PyPI 包名 | **`polymarket-client`**（`pip install polymarket-client`，`import polymarket`） | [已验证] |
| PyPI 不存在的名字 | `https://pypi.org/pypi/py-sdk/json` → **HTTP 404**；仓库名 py-sdk 不等于包名 | [已验证] |
| 最新版本 | **0.13.0**，上传时间 **2026-10-09T16:21:12**（PyPI 与 pyproject.toml 一致） | [已验证] |
| 发布节奏 | 首版 0.1.0 于 2026-07-22；此后 0.10.0(09-10)、0.11.0(09-23)、0.12.0(10-01)、0.13.0(10-09)，约每周一版 | [已验证] |
| 生命周期状态 | `Development Status :: 4 - Beta`，语义化版本，0.x 小版本允许破坏性变更（README 明示） | [已验证] |
| Python 版本 | `requires-python >= 3.11`；classifier 含 3.11/3.12/3.13 | [已验证] |
| 开放 issue | 13 个（GitHub issues 页），约半数为 bug；与只读相关的有 #321「sync/async pagination 可能重复请求同一 cursor」(2026-09-29)、#325 事件流字段解析 (10-04)；多数 bug 集中在交易/签名/流式 | [已验证（WebFetch issues 页）] |
| Python 3.12 实测 | 在 `E:/Project/.wqn-audit/.venv-sdktest`（**Python 3.12.10**）`pip install polymarket-client==0.13.0` 成功，import 成功，并完成第 4 节全部真实只读调用 | [已验证] |

---

## 1. 许可证

- **MIT License**，`LICENSE` 文件首行 `MIT License`，`Copyright (c) 2026 Polymarket`；`pyproject.toml` 写明 `license = { text = "MIT" }`，classifier `License :: OSI Approved :: MIT License`。PyPI metadata 亦为 MIT。[已验证]
- OSI 状态：MIT 为 OSI 批准许可证（SPDX `MIT`）。[已验证（分类器自述 + MIT 本身为 OSI 列表内标准许可）]
- 义务：保留版权声明与许可证文本即可；无专利授权条款、无 copyleft、无使用限制。闭源商用、内部分发均无障碍；对 WeatherQuant Next（内部量化系统）**无合规障碍**。[推断（基于 MIT 条款文本，已通读）]
- 对照：`py-clob-client` 亦为 MIT（PyPI classifier）。[已验证]

---

## 2. Polymarket 各仓库辨析（py-sdk / py-clob-client / 其他，各自用途）

### 2.1 `Polymarket/py-sdk` —— 就是官方统一 Python SDK（本审计主角）

- **它不是**纯 Gamma 客户端，**也不是**旧 CLOB 客户端，而是 2026 年新推出的**统一客户端**：README 自述 "Official Python SDK … public data, authenticated account, trading, builder attribution, and wallet workflows"。[已验证]
- 源码证据：`src/polymarket/clients/` 下有 `public.py` / `async_public.py`（免认证只读）与 `secure.py` / `async_secure.py`（需私钥）；内部 transport 同时挂 **gamma、data、clob、rfq** 四个 base URL（`clients/public.py` `__init__`）。[已验证]
- 包结构：`src/polymarket/` 共 198 个 `.py`，含 `models/gamma|clob|data`、`frames/`（pyarrow/pandas/polars 适配）、`_internal/streams/`（CLOB/RTDS/sports WS）、perps、relayer、rfq。[已验证]
- 定位：**面向工作流的统一 SDK**（文档页 SDKs & APIs 明确 TypeScript `@polymarket/client` 与 Python `polymarket-client` 为官方统一 SDK，Rust SDK 在研）。[已验证（docs）]

### 2.2 `Polymarket/py-clob-client` —— 已废弃的旧 CLOB 交易客户端

- GitHub 页面显示 **"Public archive"，归档日 2026-05-25**；README 警告 **"The client is no longer functional and should not be used for new or existing integrations"**，并指向 py-sdk。[已验证（WebFetch）]
- PyPI `py-clob-client`：最新 **0.34.6**，上传 **2026-02-19**（此后无新版本）；`requires_python >=3.9.10`；依赖 `eth-account, eth-utils, poly_eip712_structs, py-order-utils, python-dotenv, py-builder-signing-sdk, httpx[http2]`；MIT。[已验证（PyPI JSON）]
- 用途（历史）：CLOB 下单/撤单、API key 派生、订单簿与报价读取。**不应再用于新项目**。

### 2.3 其他易混淆仓库

| 仓库 | 实际用途 | 状态 | 标注 |
|---|---|---|---|
| `Polymarket/agents` | AI agent 框架（Python 3.9 时代，`Gamma.py` 包 Gamma + `Polymarket.py` 包 CLOB 下单） | **2026-05-11 归档**，3.8k stars，MIT | [已验证（WebFetch）] |
| `Polymarket/py-order-utils` | **404（仓库已不存在）**；PyPI 包 `py-order-utils` 0.3.2 的 homepage 指向改名后的 `github.com/polymarket/python-order-utils`（HTTP 200）。EIP-712 订单签名工具，是 py-clob-client 的依赖，**不是** py-sdk 的依赖 | 存活（改名） | [已验证（curl + PyPI）] |
| `@polymarket/client`（npm） | TypeScript 统一 SDK | 官方推荐 | [已验证（docs）] |
| `py-builder-signing-sdk`、`poly_eip712_structs` | 仅 py-clob-client 的签名依赖 | — | [已验证（PyPI deps）] |

**一句话消歧**：2026 年 5 月起，Polymarket 官方口径 = **新项目一律用 `py-sdk`（PyPI 名 `polymarket-client`）或裸 HTTP**；`py-clob-client` 与 `agents` 均已归档且官方声明不可用。

---

## 3. 依赖与 Python 3.12 / httpx / Pydantic v2 兼容性

`polymarket-client==0.13.0` 运行时依赖（pyproject 与 PyPI requires_dist 一致）[已验证]：

```
eth-abi>=5,<6
eth-account>=0.13,<1
eth-utils>=4,<6
httpx[http2]>=0.27,<1
msgpack>=1,<2
pydantic>=2,<3
typing-extensions>=4.15,<5
websockets>=13,<16
可选 extras: arrow→pyarrow; pandas→pandas>=2,<3+pyarrow; polars; quant→pandas+polars+pyarrow
```

对照 WeatherQuant Next 目标栈（Python 3.12、httpx、Pydantic v2、DuckDB、NumPy/SciPy/Pandas）：

- **Python 3.12**：声明支持，且已用 3.12.10 实测安装 + 真实调用成功。[已验证]
- **httpx**：SDK 本身就用 `httpx[http2]`——与我们同栈，可共享连接池/中间件/重试策略，**零冲突**。[已验证]
- **Pydantic v2**：SDK 硬依赖 `pydantic>=2,<3`，模型全部 v2 写法（`field_validator`、`model_validator`、`TypeAdapter`）。与我们 v2 同栈，可直接复用其模型做类型化落库。[已验证]
- **无 `requests`**（旧生态常见冲突源不存在）。[已验证]
- **无完整 web3.py**：只引入 `eth-abi/eth-account/eth-utils` 纯 Python 包（签名/地址工具），不拉 `web3`，不引 leveldb/lxml 等重物。只读场景这些包只是被动加载。[已验证]
- **DuckDB / NumPy / SciPy / Pandas**：核心依赖完全不触及这些；pandas 只在 `[pandas]/[quant]` extra 里且限定 `>=2,<3`，与常规科学栈一致。**无版本冲突风险**。[已验证]
- py-clob-client 的依赖也不含 requests（httpx 系），但该包已死，不再评估。

---

## 4. 只读能力地图（市场元数据 / 订单簿 / 价格历史 / 成交历史 / 结算）

以下全部通过 **`PublicClient()`（无任何密钥）在 3.12 venv 中真实调通** [已验证]：

| 能力 | SDK 方法（`PublicClient`） | 后端端点 | 实测结果 |
|---|---|---|---|
| 市场元数据（按 id/slug/url） | `get_market(id=…)` / `get_event(...)` | Gamma `GET /markets/{id}`、`/markets/slug/{slug}`、`/events/...` | 返回天气市场题面；注意 SDK 把扁平 JSON 重组成嵌套模型：`m.state.closed`、`m.resolution.uma_resolution_status`、`m.outcomes.yes.price`（**不是** `m.uma_resolution_status`） |
| 市场/事件列表发现 | `list_markets(tag_id=84, closed=False, …)`、`list_events`、`search` | Gamma `GET /markets`、`/events`、`/public-search` | `tag_id=84`（Weather）成功列出开放天气市场 |
| 标签/系列 | `get_tag`、`list_tags`、`get_series`、`list_series` | Gamma `/tags*`、`/series*` | `GET /tags/slug/weather` → id=84 [已验证] |
| **L2 订单簿（仅实时）** | `get_order_book(asset_id=…)`、`get_order_books(...)`（≤批量） | CLOB `GET /book?token_id=`、`POST /books`（≤500） | 返回 bids/asks/timestamp/hash；**只有当前快照** |
| 实时报价 | `get_price(s)`、`get_midpoint(s)`、`get_spread(s)`、`get_last_trade_price(s)` | CLOB `/price(s)`、`/midpoint(s)`、`/spread(s)`、`/last-trade-price(s)` | 源码逐一核对 |
| **价格时间序列（历史）** | `list_price_history(asset_id, interval/start-end/as_of, bucket_seconds, …)` | Data-API `GET /v2/prices-history` | 实测：`interval=max` 得 80 点（12h 桶）；`bucket_seconds=60` 近 5 天可得分钟级 |
| **成交历史（历史全量）** | `list_trades(condition_id=…, page_size=…)`（另有 `user/event_id/side/start/end/full_history` 过滤） | Data-API `GET /v2/trades` | 实测 48 笔全量返回（该市场真实总量），keyset 游标分页 |
| **结算 / UMA** | `get_resolutions(question_id/condition_ids/event_ids)` | Data-API `GET /v2/resolutions` | 实测返回 `status="resolved", price=0`（见第 9 节）；SDK 把哨兵值 `69` 归一化为 `None` |
| 持仓/活动/持有人/OI/PnL（按地址或 condition，公开） | `list_positions`、`list_activity`、`list_market_holders`、`get_open_interests`、`get_user_pnl`、`get_user_volume`、`get_portfolio_value`、`get_user_stats` | Data-API `/v2/positions`、`/v2/activity`、`/v2/holders`、`/v2/oi`、`/v2/user-pnl`、`/v2/user-volume`、`/v2/value`、`/v2/user-stats` | 源码核对；无需密钥 |
| 排行榜/奖励 | `list_trader_leaderboard`、`list_biggest_winners`、`list_builder_leaderboard`、`list_current_rewards`… | Data-API `/v2/leaderboard*`、`/v2/builders/*`；CLOB `/rewards/*` | 源码核对 |
| WS 实时（书/价） | `streams/`（CLOB market 频道等） | `wss://ws-subscriptions-clob.polymarket.com/ws/market`（公开） | 文档核对 [已验证（docs）] |

**SDK 未封装、如需可用裸 HTTP 的公开只读端点**（OpenAPI 里 `security: []`）[已验证]：

- CLOB `GET /prices-history?market=<conditionId>&interval=&fidelity=&startTs=&endTs=`（**按市场**的历史价，interval 枚举 `max|all|1m|1w|1d|6h|1h`，fidelity 分钟粒度——旧版经典端点）
- CLOB `POST /batch-prices-history`（多市场批量历史价）
- CLOB `GET /simplified-markets`、`/sampling-markets`、`/clob-markets/{condition_id}`、`/markets-by-token/{token_id}`、`/time`、`/tick-size`、`/neg-risk`、`/fee-rate`
- Gamma 全部 42 条路径（OpenAPI 中 0 条声明 security）

**明确不存在的能力**：

- **历史订单簿快照：任何官方端点都拿不到**（详见第 8 节）。[已验证（三份 OpenAPI 全路径清点 + 文档只描述实时 /book）]
- 官方**没有 "forecast/预测" API**——市场隐含概率即价格序列本身。[推断（基于全部端点清单中无此类资源）]

---

## 5. 认证要求（哪些公开、哪些需要密钥）

**P0–P1 只读研究：零密钥即可**。实测 `PublicClient()` 不构造任何认证 header（`clients/public.py` `__init__` 仅 `SyncTransport(base_url=...)` × gamma/data/rfq/clob），全部只读调用成功。[已验证]

| 服务 / 端点 | 认证 | 标注 |
|---|---|---|
| Gamma 全部（42 paths） | **公开，无任何 security 声明** | [已验证（gamma-openapi.yaml）] |
| CLOB 行情：`/book(s)`、`/price(s)`、`/midpoint(s)`、`/spread(s)`、`/last-trade-price(s)`、`/prices-history`、`/batch-prices-history`、`/simplified-markets` 等 | **公开**（`security: []`） | [已验证（clob-openapi.yaml）] |
| Data-API `/v2/trades`、`/v2/prices-history`、`/v2/resolutions`、`/v2/tokens`、`/v2/status` | **公开**（无全局 security；实测裸 curl 成功） | [已验证] |
| Data-API 按 `user=` 查持仓/活动/PnL | 公开（无需自己密钥；查的是该地址的公开数据） | [已验证（实测 + OpenAPI）] |
| CLOB `/data/trades`（账户成交）、`/data/orders`、`/order(s)`、`/auth/*`、`/balance-allowance` | **需要 L2 API key**（`POLY_ADDRESS/SIGNATURE/TIMESTAMP/API_KEY/PASSPHRASE`，HMAC-SHA256）；下单另需 L1 EIP-712 钱包签名 | [已验证（clob-openapi security + docs）] |
| WS `…/ws/market` | 公开；`…/ws/user` 需账户凭证 | [已验证（docs）] |
| Relayer、Perps、RFQ 写操作 | 钱包/会话签名 | [已验证（docs + 源码）] |

注意：`SecureClient`（下单）需要 `POLYMARKET_PRIVATE_KEY` 等，与我们 P0 无关。只读场景**连 API key 都不需要**（CLOB 甚至提供 readonly 级 key，但我们用不上）。[已验证]

---

## 6. 官方 API 端点清单（base URL + 关键路径）

base URL 三处独立来源互相印证（`py-sdk/src/polymarket/environments.py` 生产配置、docs、OpenAPI）[已验证]：

### Gamma — `https://gamma-api.polymarket.com`（市场/事件元数据）

```
GET /markets                列表（closed, tag_id, slug, condition_ids, clob_token_ids,
                            uma_resolution_status, volume/liquidity 排序与过滤, end_date_* …）
GET /markets/{id}           GET /markets/slug/{slug}     GET /markets/keyset（游标）
GET /markets/{id}/description   GET /markets/{id}/tags    GET /markets/abridged | /markets/information
GET /events                 列表（tag_id=84 即天气）      GET /events/keyset   GET /events/results
GET /events/{id}            GET /events/slug/{slug}      GET /events/{id}/tags
GET /public-search?q=…      GET /tags/slug/{slug}        GET /series  GET /comments …
```

### CLOB — `https://clob.polymarket.com`（行情 + 交易）

```
公开行情：
GET  /book?token_id=            POST /books            （≤500，批量）
GET  /price | /midpoint | /spread | /last-trade-price   （+ /prices /midpoints /spreads /last-trades-prices 批量）
GET  /prices-history?market=<conditionId>&interval=&fidelity=&startTs=&endTs=
POST /batch-prices-history
GET  /simplified-markets  /sampling-markets  /clob-markets/{condition_id}  /markets-by-token/{token_id}
GET  /time  /tick-size  /neg-risk  /fee-rate
需 L2 key：
GET  /data/trades  /data/orders  /data/order/{orderID}  /auth/*  /balance-allowance  /notifications  /rewards/*
POST/DELETE /order(s)  /cancel-* 
```

WebSocket：`wss://ws-subscriptions-clob.polymarket.com/ws/market`（公开盘口流）、`/ws/user`（认证）。[已验证（docs）]

### Data-API — `https://data-api.polymarket.com`（分析/统计，v2 现行）

来自官方 `https://data-api.polymarket.com/v2/openapi.json`（题名 "Polymarket Data API v2"）完整路径清单 [已验证]：

```
GET /v2/trades          成交历史（condition/event_id/user/side/start/end，cursor 分页）
GET /v2/prices-history  价格序列（token_id, interval|start/end|as_of, bucket_seconds, limit, cursor）
GET /v2/resolutions     结算状态（question_id | condition | event_id 三选一）
GET /v2/positions       /v2/positions/combos   /v2/activity   /v2/activity/combos
GET /v2/holders         /v2/oi    /v2/value    /v2/user-pnl   /v2/user-stats   /v2/user-volume
GET /v2/leaderboard     /v2/biggest-winners   /v2/builders/leaderboard  /v2/builders/volume
GET /v2/live-volume     /v2/tokens            /v2/approvals  /v2/status
```

（v1 旧端点 `/trades`、`/positions`、`/closed-positions` 仍在服务并有限速记录，但官方迁移指南 `migrate/data-api-v1-to-v2` 指向 v2。）[已验证（docs sitemap + rate-limits 页）]

### 其他

- Relayer `https://relayer-v2.polymarket.com`（代付 gas 提交交易）
- RTDS 公开行情/评论流 `wss://ws-live-data.polymarket.com`（文档另有 `ws-live-v2.polymarket.com/asyncapi.json`）
- Sports `wss://sports-api.polymarket.com/ws`；Perps `https://api.perpetuals.polymarket.com`（实验性）
- OpenAPI/AsyncAPI 全部可下载：`/api-spec/gamma-openapi.yaml`、`/api-spec/clob-openapi.yaml`、`data-api …/v2/openapi.json`（本审计已落盘）

---

## 7. 速率限制

官方文档 `https://docs.polymarket.com/api-reference/rate-limits` 给出明确数字（单位均为「每 10 秒」，Cloudflare 按 **IP** 滑动窗口，超限**延迟/排队而非立即拒绝**）[已验证（docs）]：

| 服务 | 限额 |
|---|---|
| 全局 | 15,000/10s（`/ok` 100/10s） |
| Gamma | 总 4,000/10s；`/events` 500；`/markets` 300（markets+events 合计 900）；`/comments` 200；`/tags` 200；`/public-search` 350 |
| Data-API v2 | 总 800/10s；`/v2/trades` **300**；`/v2/positions`+combos 200；`/v2/activity`+combos 200；**`/v2/prices-history` 200**；`/v2/status` 100 |
| Data-API v1 | 总 1,000/10s；`/trades` 200；`/positions` 150 |
| CLOB | 总 9,000/10s；行情单查 `/book`/`/price`/`/midpoint` 各 **1,500**，批量 `/books`/`/prices`/`/midpoints` 各 500；**`/prices-history` 1,000**；`/data/trades` 500；下单 `POST /order` 5,000/10s + 120,000/10min（另有 per-signer 令牌桶，`Poly-RateLimit-*` 响应头） |
| Relayer `/submit` | 25/分钟 |

对我们的含义：回测批量拉取时，`/v2/prices-history` 200 次/10s ≈ 每秒 20 请求，**配 15 天/请求窗口 + cursor 分页，历史价回填吞吐充足**，无需鉴权配额。SDK 内建 `RateLimitError` + `Retry-After` 退避（`_internal/retry.py`：默认 2 次重试、单次延迟 ≤5s）。[已验证]

---

## 8. 历史数据可获得性（关键：能否取到历史订单簿？价格时间序列粒度？）

### 8.1 历史订单簿 —— **拿不到** [已验证]

- 三份官方 OpenAPI（Gamma 42 路径、CLOB 全路径、Data-API 21 路径）中**不存在任何历史 book 端点**（无 `/book/history`、无 depth 快照类资源）；CLOB 只有实时 `GET /book`、`POST /books`。[已验证]
- 文档「Prices and Order Books」页也只描述实时书与 `hash`（用于检测盘口变化）。[已验证]
- 唯一替代：WS `…/ws/market` 从**上线起持续订阅自建**盘口快照库；或从成交流（`/v2/trades`）+ 盘口采样自行重建近似。**结论：迁移/回测设计中 L2 深度历史必须自采，平台不供给。**[已验证 + 推断（自采方案为推断）]

### 8.2 价格时间序列 —— **可获得，粒度与保留期如下** [已验证]

主端点：`GET https://data-api.polymarket.com/v2/prices-history?token_id=…`

- 三种选窗（互斥）：`interval=max|all|1m|1w|1d|6h|1h`；绝对窗口 `start`/`end`（epoch 秒，**单窗 ≤15 天**，end 开区间——SDK 实测越界直接抛 `UserInputError`）；或 `as_of` 定点。`bucket_seconds ∈ [60, 86400]`（对 `max/all/1m` 下限 600，对 `1w` 下限 300）。cursor 分页，默认页 10000。[已验证（docs + SDK 源码 + 实测）]
- 响应点：`{timestamp, price, resolution_seconds}`；`resolution_seconds=0` 表示精确 tick，非零为桶宽（43200 = 12h 桶）。[已验证]
- 官方保留期（文档原话）：**1 分钟数据至少 7 天、5 分钟至少 60 天、30 分钟至少 90 天、3 小时与 12 小时永久**。[已验证（docs）]
- **实测保留期交叉验证**（对 2026-09 结算的天气 token，2026-10-10 执行）：

  | bucket_seconds | 窗口 | 返回点数 | 结论 |
  |---|---|---|---|
  | 60 | 5–4 天前 | 1316 | 7 天内分钟级可用 ✓ |
  | 60 | 10–9 天前 | **0** | 分钟级已过期 ✗ |
  | 60 | 40–39 天前 | 0 | 已过期 |
  | 300 | 10–9 天前 | 288（全天满值） | 5 分钟可用 ✓ |
  | 300 | 40–39 天前 | 140 | 40 天前 5 分钟仍可用 ✓ |
  | 1800 | 40–39 天前 | 25 | 30 分钟可用 ✓ |
  | 43200 / interval=max | 全周期 | 80 点 / 全史 | 12 小时桶覆盖全史 ✓ |

  [已验证]（个别桶数少于理论满值，疑为无成交/无报价时段不出点 [推断]）
- **含义**：任意历史时点要拿到最细粒度，必须**在数据新鲜时抓取**（≤7 天内抓 1 分钟），过期只能降粒度。回测若需分钟级，管线必须**持续落盘**；30 分钟级可事后回填 90 天内，12 小时级可随时回填全史。[已验证 + 推断（管线设计为推断）]
- 另有 CLOB `GET /prices-history`（按 `market`=conditionId，`fidelity` 分钟数，公开，SDK 未封装）可作补充源。[已验证（clob-openapi）]

### 8.3 成交历史 —— **全量可获得** [已验证]

- `GET /v2/trades?condition=…`（或 `event_id`、`user`）公开、cursor 分页、可 `start/end` 过滤；字段含 `price,size,side,timestamp,outcome,outcome_index,transaction_hash,proxy_wallet,…`。实测 48 笔一次取全，最早一笔 2026-09-15（覆盖市场整个生命周期）。[已验证]
- 这是从 L2 成交重建"准盘口"（mid/last/深度代理）的可行底料。[推断]

### 8.4 结算 / 历史"预测" —— **可获得** [已验证]

- `GET /v2/resolutions?condition=…` 返回 UMA 生命周期：`status ∈ initialized|posed|proposed|challenged|reproposed|disputed|active|arbitration|resolved`（SDK `ResolutionStatus` 枚举与实测一致）、`proposed_price/reproposed_price/price`、`was_disputed`、`transaction_hash`、`last_update_timestamp`，以及可选 `payouts(两结局分摊)`、`reporter(UMA_OO|CHAINLINK|EOA)`、`resolved_at/resolved_block`、`expected_settlement_time`。**未决时 `price=69`（哨兵值），SDK 自动转 `None`；裸 HTTP 必须自己剔 69**。[已验证（实测 posed 态 price=69；源码 `_unset_price`）]
- 历史"预测"没有专门 API；**市场隐含概率 = `/v2/prices-history` 本身**。[已验证/推断见 4]

---

## 9. 天气类市场定义与结算字段

### 9.1 市场如何被定义 [已验证（实测 Gamma）]

- **发现入口**：标签 **`weather`（tag id=84，label "Weather"）**，`GET /events?tag_id=84&closed=false` 与 SDK `list_markets(tag_id=84)` 均可用。注意部分气温市场还挂在 series/其他标签下，需并用关键词 `public-search?q=temperature`。[已验证]
- **两种结构**：
  1. **单场二元市场**（如 "Will global temperature increase by less than 1.30°C in October 2026?"）：一个市场 = 一个阈值命题，outcomes `["Yes","No"]`，`negRisk` 分组下相邻阈值构成温度区间。
  2. **事件多市场（温度分箱）**（如 "Highest temperature in Panama City on October 11?" 含 11 个市场）：event = 城市×日期；每个市场 `groupItemTitle` = 分箱序号（0,1,2…），outcomes 仍是 Yes/No，event 级 `negRisk` 互斥穷尽。[已验证]
- **规则全文在 `description` 字段**，包含：观测站（例：*Incheon Intl Airport Station (RKSI)*）、变量（当日最高温）、**取整规则**（例："measures temperatures to whole degrees Celsius… this is the level of precision"）、修订条款、"数据 finalized 后才能 resolve Yes" 等边界条件。[已验证]
- **`resolutionSource` 字段 = 官方裁决数据源 URL**：
  - 全球月度距平：`https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt`（NASA GISS）
  - 城市日最高温：`https://www.wunderground.com/history/daily/kr/incheon/RKSI`（Wunderground 站点历史页）[已验证]
- 每市场关键字段：`conditionId`（CTF 条件）、`questionID`（UMA question id）、`clobTokenIds`（[Yes, No] 两个 ERC1155 token）、`outcomePrices`、`endDate`、`closed/closedTime`、`resolvedBy`（UMA 合约地址 `0x69c47De9…5f24`）、`umaResolutionStatus`。[已验证]

### 9.2 结算（UMA）与程序化取已决结果 [已验证（docs + 实测）]

- UMA Optimistic Oracle 流程（docs/concepts/resolution）：任何人缴 bond（约 $750）提案 → **2 小时挑战窗** → 无争议即生效；一次争议 → 二次提案；二次争议 → UMA DVM 代币投票（约 48h，争议总耗时 4–6 天）；"Unknown/50-50" → **市场按 0.50/0.50 分配**。up-down 类市场走 Chainlink TWAP（确定性，无争议循环）。
- **程序化判定已决结果的两条路径**：
  1. **推荐**：`GET https://data-api.polymarket.com/v2/resolutions?condition=<conditionId>` → `status=="resolved"` 时 `price` = **Yes 结局的每份额赔付（0 或 1）**；实测 2026-09 全球温度市场 `status=resolved, price=0` → **No 获胜**。SDK：`client.get_resolutions(condition_ids=…)` → `Resolution.status/.price`。
  2. Gamma 市场字段交叉验证：closed 后 `umaResolutionStatus="resolved"` 且 `outcomePrices=["0","1"]`（与上例一致）；开放市场该字段缺失、`outcomePrices` 为实时报价 `["0.0255","0.9745"]`。**判已决必须同时看 `closed==true` 与 `umaResolutionStatus==resolved`，不能只看 outcomePrices**（活跃市场也有数值）。[已验证]
- `umaResolutionStatus` 枚举（SDK `UmaResolutionStatus`）：`requested | proposed | disputed | resolved`。[已验证（源码）]
- 补充：交易所层面「结算完成」还可看 Data-API Resolution 的 `resolved_at/resolved_block/payouts/reporter`（部分行可能缺省，实测该行 reporter/payouts/resolved_at 为 None）。[已验证]

---

## 10. SDK vs 裸 HTTP 选型建议（面向 P0 只读研究）

**结论：P0 首选官方 `polymarket-client`（py-sdk）作为只读客户端，外包一层薄 Repository 以便必要时替换为裸 httpx；SDK 未封装的两个公开端点（CLOB `/prices-history`、`/batch-prices-history`）用裸 httpx 补齐。**

理由：

1. **官方背书与存续**：py-clob-client 已归档且官方声明"不再可用"，生态正向 py-sdk 收敛；裸 HTTP 虽稳定，但 Gamma/Data-API 字段仍在演进（v1→v2 迁移指南存在），**官方 SDK 是唯一有维护承诺的封装层**。[已验证]
2. **栈完全同构**：SDK 就是 httpx + Pydantic v2 构建，Python 3.12 实测通过；其 `Market`/`Trade`/`Resolution`/`PriceHistoryPoint` 模型可直接序列化进 DuckDB，省去自维护 schema。[已验证]
3. **白送的工程件**：keyset/cursor 分页（`Paginator`）、`Retry-After` 退避、批量端点（`/books` ≤500）、sync/async 双客户端、Windows/Notebook 友好的 repr。裸 HTTP 这些都要自己写。[已验证]
4. **只读面覆盖完整**：第 4 节 13 类能力全部实测通过，无一需要密钥。[已验证]
5. **保留裸 HTTP 后路**：0.x 语义化版本允许 minor 破坏（README 明示）+ #321 分页 bug 未修——所以**不要让领域模型直接继承 SDK 类型**，Repository 层隔离。[已验证 + 推断（架构建议）]

不推荐 P0 就上 `SecureClient`/交易面：我们 P0–P1 无交易需求，且 bug issue 集中在交易/签名路径。[推断]

---

## 11. 设计缺陷 / 风险

1. **0.x 破坏性变更风险**：README 承认 minor 可 breaking（Perps 全部标 experimental）；`list_markets` 文档明示旧 cursor 不可跨版本续用。→ 锁版本（`polymarket-client==0.13.0`）+ 落盘原始 JSON 双保险。[已验证]
2. **已知未修 bug #321（2026-09-29）**：sync/async 分页可能重复请求同一 cursor——**影响只读批量拉取**，需在 Repository 层加 cursor 去重/循环上限。[已验证]
3. **`list_markets` 会静默丢弃无法用二元 `Market` 模型表示的市场**（源码 docstring："Markets that cannot be represented by the binary Market model are omitted"）——统计全市场数时不能只信 SDK，必要时裸 HTTP 打 Gamma `/markets`。[已验证]
4. **模型重构陷阱**：Gamma 扁平字段被嵌进 `m.state`/`m.resolution`/`m.outcomes`/`m.prices`；直接 `m.uma_resolution_status` 会 `AttributeError`（实测踩坑）。[已验证]
5. **历史数据不可回溯的硬约束**：历史 L2 深度完全不可得；分钟级价格仅存 7 天——**P0 第一天就要开始持续采集成交 + 盘口快照 + 1 分钟价**，否则永久缺失。这是排期风险，不是代码风险。[已验证 + 推断]
6. **15 天/请求窗口 + 200 次/10s 限速**：全史回填需分片调度；超窗报错（SDK 抛 `UserInputError`）。[已验证]
7. **裸 HTTP 的 `69` 哨兵**：`/v2/resolutions` 未决时 `price=69`，误当真实赔付会污染回测标签；SDK 已归一化，裸 HTTP 必须自行剔除。[已验证]
8. **只读也带 `eth-*` 签名依赖**：无法避免（硬依赖），但不含私钥逻辑入口于 `PublicClient`，攻击面有限。[已验证 + 推断]
9. **Windows 控制台 GBK 编码**：实测打印 ºC 字符会 `UnicodeEncodeError`，CLI 环境需 `PYTHONIOENCODING=utf-8`。[已验证]
10. **`polymarket-client` 极新（首版 2026-07）**：文档面（API reference 由 Sphinx 生成）仍在补（issue #219）；遇疑以 OpenAPI + 源码为准。[已验证]

---

## 12. 选型结论与理由

1. **`Polymarket/py-sdk` = 官方统一 Python SDK，PyPI 名 `polymarket-client`（≠ py-sdk），0.13.0（2026-10-09），MIT，Beta，周更活跃**；旧 `py-clob-client`（归档 2026-05-25，末版 0.34.6）与 `agents`（归档 2026-05-11）**均不可再用**。[已验证]
2. **P0（只读研究）选型：`polymarket-client==0.13.0` 为默认客户端**——与目标栈（Py3.12/httpx/Pydantic v2）零冲突并已实测全链路；外加裸 httpx 补 CLOB `/prices-history`（market 级历史价）与批量端点；Repository 层隔离 0.x 变更风险。[已验证 + 推断]
3. **零密钥**：Gamma/CLOB 行情/Data-API v2 的市场级只读全部公开（42/42 Gamma 路径无 security；实测通过）；密钥仅在下单、账户级 CLOB `/data/*`、Relayer/Perps 时才需要——P0–P1 完全不涉及。[已验证]
4. **历史数据边界（迁移/回测设计的决定性事实）**：
   - ✅ 全史价格序列（12h 桶永久；30min ≥90d；5min ≥60d；**1min 仅 ≥7d**，须新鲜抓取）
   - ✅ 全史成交打印（`/v2/trades`，全生命周期覆盖）
   - ✅ 全史结算状态（`/v2/resolutions` + Gamma `umaResolutionStatus`/`outcomePrices`）
   - ✅ 全量市场元数据与规则文本（`description`/`resolutionSource`/天气 tag 84）
   - ❌ **历史订单簿 L2 深度：官方无任何端点，只能自建采集**（WS `…/ws/market` + `/book` 轮询）
   - ❌ 无官方 forecast API；隐含概率即价格序列 [推断]
5. **首要行动项**：P0 开工第一天即启动**持续采集器**（1 分钟价 + 盘口快照 + 成交），否则最细粒度数据永久流失；同时锁定 SDK 版本并加 cursor 去重护栏。[已验证 + 推断]

---

### 证据索引

- 本地 clone：`E:/Project/.wqn-audit/py-sdk`（commit `f1e2ab2`，2026-10-09）
- 关键源码：`src/polymarket/clients/public.py`（1372 行只读面）、`src/polymarket/environments.py`（base URL）、`src/polymarket/_internal/actions/{gamma,clob,data,account}.py`（端点）、`src/polymarket/models/gamma/market.py`、`src/polymarket/models/data/resolutions.py`、`src/polymarket/rate_limit.py`、`src/polymarket/_internal/retry.py`
- 下载的规格：`data-api-openapi.json`、`clob-openapi.yaml`、`gamma-openapi.yaml`
- 实测样本：`weather-market-open.json`（开放）、`weather-market-closed.json`（已决）、`weather-market-seoul.json`（Wunderground 源）、`search-weather.json`、`one-weather-event.json`
- 3.12 实测 venv：`.venv-sdktest`（`pip install polymarket-client==0.13.0`）
- 文档：`docs.polymarket.com/llms.txt`（全站索引）、`/api-reference/rate-limits`、`/market-data/prices-order-books`、`/concepts/resolution`、`/getting-started/api`、`/getting-started/sdks-apis`、`resources/error-codes`

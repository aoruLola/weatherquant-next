# Performance + MCP 部署与接入（v0.1.4）

本文件覆盖：镜像、数据目录、首次回填、健康检查、回滚、MCP 客户端接入。

**本轮所有改动只在代码与测试层面。生产 Collector / Shadow / Dashboard
**未被触碰**——没有容器操作、没有数据库访问、没有 compose 修改。**本文件
描述的部署需要你明确批准后才执行。**

契约：[`performance-data-contract.md`](performance-data-contract.md)
审计：[`p2-0.1-audit.md`](p2-0.1-audit.md)

---

## 1. 组件与镜像

| 组件 | 镜像 | 依赖组 | 数据访问 |
|---|---|---|---|
| Research Analytics | 无独立镜像（CLI，随 `weatherquant-next`） | `dev` | 研究库 + Shadow 导出（只读） |
| Dashboard | `weatherquant-next-dashboard` | `dashboard` | 心跳 / 报告 / 明细快照 / **performance 快照**（全只读） |
| **MCP Gateway** | `weatherquant-next-mcp`（**新增**） | `mcp` | **无任何挂载**，只经 HTTP 调 Dashboard |

### 依赖隔离

`mcp` 是独立依赖组。Collector / Shadow / Dashboard 三个镜像都用
`uv sync --frozen --no-dev`（或 `--group dashboard`）安装，**SDK 不会进入
任何一个已有镜像**。MCP 镜像只装 `--group mcp`，不含 fastapi。

有测试断言：`Dockerfile.mcp` 不得出现 `--group dashboard`。

---

## 2. 数据目录

```
/mnt/user/appdata/weatherquant-next/        Collector（不变）
/mnt/user/appdata/weatherquant-shadow/      Shadow（不变）
    reports/                                明细快照（既有）
/mnt/user/appdata/weatherquant-performance/ 【新增】Performance 快照
    summary.json  edge.json  win-rate.json
    opportunities.json  settlements.json  models.json
```

Dashboard 需要新增一条只读挂载：

```yaml
- /mnt/user/appdata/weatherquant-performance:/sources/performance:ro
```

**MCP Gateway 不挂载任何目录。** 这是安全属性，不是遗漏——它只经 HTTP
调用 Dashboard，因此被攻破也无法触及 DuckDB。

---

## 3. 首次回填（Research Analytics）

回填是**离线、可重复**的过程，不接触生产 Shadow 的数据库。

### 3.1 决策历史的获取（单写者规则）

**禁止**直接打开运行中的 `shadow.duckdb`（引擎持独占租约）。两条合法路径：

1. **Shadow 导出**（推荐，无停机）：Shadow 在自己持有的连接上按周期原子
   发布 `shadow/reports/decisions-export.parquet`（契约 §11）；
2. **一致性备份**（需批准）：`docker stop weatherquant-shadow` → 复制
   `.duckdb` + `.wal` → 恢复容器。**仅在维护窗口执行，且需你确认。**

### 3.2 结算回填

输入是一份**市场清单文件**（JSON 列表），不是数据库连接：

```json
[
  {"market_id": "562360", "event_id": "31936", "target_date": "2026-10-13",
   "condition_id": "0x1d74…", "token_yes": "2891…", "station": "KLGA"}
]
```

```bash
weatherquant-research backfill-settlements \
  --markets /tmp/markets.json \
  --out /tmp/settlements.json \
  --dataset live \
  --verified /tmp/verified.json
```

| 参数 | 说明 |
|---|---|
| `--markets` | **必填**。市场清单 JSON 路径 |
| `--out` | **必填**。结算索引输出路径 |
| `--dataset` | **必填**，`live` 或 `fixture`。**没有默认值**——一次未标注的运行会被拒绝 |
| `--verified` | 可选。已通过观测交叉核对的 `{market_id, target_date}` 列表；不提供时全部记为 `UNVERIFIED` |
| `--timeout` | 可选，默认 20s |

结算判定只采信 **`data-api.polymarket.com/v2/resolutions`**（契约 §4.1 实测），
Gamma 仅作交叉参考。`price="69"` 是哨兵值必须归一为 `None`；未决时返回
`PENDING`，condition 不存在时返回 `UNAVAILABLE`——**不是失败**。

> 该命令**需要能访问 Polymarket**。若主机不可达上游，用
> `tests/fixtures/polymarket/*.json` 做离线验证，并且必须显式标
> `--dataset fixture`。**不得把 fixture 结果标记为生产数据。**

### 3.3 生成 Performance 快照

```bash
weatherquant-research build-performance \
  --export /mnt/user/appdata/weatherquant-shadow/reports/decisions-export.jsonl \
  --settlements /tmp/settlements.json \
  --out /mnt/user/appdata/weatherquant-performance \
  --dataset live \
  --capital-usdc 100 \
  --max-allocation-fraction 0.10
```

| 参数 | 说明 |
|---|---|
| `--export` | **必填**。决策导出，支持 `.json` / `.jsonl` / `.parquet`（parquet 需 pyarrow） |
| `--out` | **必填**。快照输出目录 |
| `--dataset` | **必填**，`live` 或 `fixture`。**没有默认值** |
| `--settlements` | 可选。§3.2 产出的结算索引 |
| `--capital-usdc` | 默认 100，必须 > 0 |
| `--max-allocation-fraction` | 默认 0.10，研究假设而非已验证的风险规则 |
| `--gap-window-seconds` | 默认 900 |

写入是**原子的**（临时文件 + fsync + rename），读者永远看不到半个 JSON。
六个文件**一次性全部构建后**才写入：中途失败会保留上一份完整集合，
不会留下三个新、三个旧的混合状态。

**不发布快照 ≠ 出错。** Dashboard 会显示 `missing` + "回填尚未产生该快照"，
不会显示 0，也不会编造图表。

验证命令确实存在：

```bash
weatherquant-research --help          # 应列出 backfill-settlements 与 build-performance
weatherquant-research build-performance --help
```

---

## 4. 部署顺序

**先 Shadow / Dashboard，后 MCP。** MCP 只依赖 Dashboard API。

| 步骤 | 操作 |
|---|---|
| 1 | 确认既有三个容器仍健康（`/healthz` 三源 `fresh`） |
| 2 | 更新 Dashboard compose，加 `:ro` 挂载 `/sources/performance` |
| 3 | 执行回填（§3），确认六个文件已原子发布 |
| 4 | Dashboard **Apply**（新镜像 `sha-<commit>`） |
| 5 | 验证 `/api/v1/performance/summary` 返回 `fresh` |
| 6 | MCP compose 填入 `MCP_SHA` / `WQ_MCP_DASHBOARD_URL` / `WQ_MCP_PLAINTEXT_NETWORKS` / `WQ_MCP_TOKEN` |
| 7 | MCP **Apply** |
| 8 | 验证 `/healthz` 与工具发现 |

**Collector 全程不重启。**

---

## 5. 健康检查

| 服务 | 检查 |
|---|---|
| Dashboard | `GET /healthz` → 三源 `fresh` |
| Performance 快照 | `GET /api/v1/performance/summary` → `freshness.state == "fresh"` |
| MCP | `docker inspect weatherquant-mcp` → healthy（进程级探活，**不依赖上游**） |
| MCP 工具 | 用 MCP 客户端 `list_tools` 应返回 13 个工具 |

> MCP 的健康检查刻意**不触达 Dashboard**：上游抖动不应导致网关重启。

---

## 6. 回滚

### 6.1 MCP

```
MCP_SHA=<上一个已验证的 sha>    → Apply
```

无数据、无迁移，回滚只影响一个无状态进程。

### 6.2 Dashboard

```
DASHBOARD_SHA=<上一个已验证的 sha>    → Apply
```

Performance 快照文件仍在磁盘上；旧版 Dashboard 忽略它们，**不会报错**。

### 6.3 回填数据

快照是**只读产物**。回滚不需要删除；重新回填会原子覆盖。
若回填结果可疑，直接删除 `weatherquant-performance/` 目录即可——
Dashboard 随即显示 `missing`，不会显示错误数据。

### 6.4 回滚判据

1. `/api/v1/performance/*` 持续 5xx 或非 `fresh` 且无法解释；
2. MCP 工具返回与 Dashboard API 数值不一致；
3. MCP 容器持续重启；
4. 出现任何**下单 / 钱包 / 私钥**相关代码路径（**立即回滚并保留日志**）。

---

## 7. MCP 客户端接入

网关只监听 **`127.0.0.1:8788`**（默认）。远程客户端必须经你自行运维的
**带认证的 HTTPS 反向代理**——网关本身不终止 TLS。

### 7.1 环境变量

| 变量 | 方向 | 说明 |
|---|---|---|
| `WQ_MCP_DASHBOARD_URL` | 出站 | Dashboard 原点。**必填** |
| `WQ_MCP_PLAINTEXT_NETWORKS` | 出站 | 允许明文 http 的内部 CIDR，逗号分隔。**未设置则拒绝所有非回环明文** |
| `WQ_MCP_TOKEN` | **入站** | MCP 客户端必须出示的 Bearer |
| `WQ_MCP_DASHBOARD_TOKEN` | **出站** | 我们向 Dashboard 出示的凭据。**与入站是两个不同的秘密** |
| `WQ_MCP_ALLOWED_ORIGINS` | 入站 | 允许的浏览器 Origin，逗号分隔。空 = 拒绝所有浏览器 Origin |
| `WQ_MCP_ALLOWED_HOSTS` | 入站 | Host 头白名单（DNS rebinding 防护） |

**入站 Bearer 永远不会转发给 Dashboard。** 两者独立配置、独立轮换。

`WQ_MCP_PLAINTEXT_NETWORKS` 的取值示例（Docker 默认网段）：

```
WQ_MCP_PLAINTEXT_NETWORKS=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8
```

> Docker 网络**不是**完整的安全边界。它是便利设施，不是信任边界——
> 所以明文放行是**按 CIDR 显式授予**的，而不是因为"在同一个 compose
> 里"就默认成立。名字必须解析到这些网段之内，否则拒绝。

### 7.2 本地 Agent（stdio，无需 token）

```json
{
  "mcpServers": {
    "weatherquant": {
      "command": "weatherquant-mcp",
      "args": ["--transport", "stdio"],
      "env": { "WQ_MCP_DASHBOARD_URL": "http://192.168.10.168:8787" }
    }
  }
}
```

### 7.3 远程客户端（Streamable HTTP，必须带 token）

```json
{
  "mcpServers": {
    "weatherquant": {
      "type": "http",
      "url": "https://your-proxy.example.com/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

> 不同客户端对自定义 MCP 的支持与订阅档位各不相同，请以该客户端
> **当时的官方文档**为准；本文件不假定所有计划都能直接安装。

### 7.4 密钥轮换

改 `WQ_MCP_TOKEN` → **Apply**（重启容器）。无状态、无旧 token 残留、
无需迁移。轮换后旧凭据立即失效。

---

## 8. 安全姿态（可核对）

有测试断言以下每一条，违反即 CI 红：

| 项 | 状态 |
|---|---|
| 无任何 volume 挂载 | ✅ 断言 `volumes:` 不存在 |
| 无 Docker socket / Portainer | ✅ 断言 |
| 无下单工具 / 钱包 / 私钥代码 | ✅ 断言 `src/weatherquant/mcp/` 全文 |
| 默认绑定 loopback | ✅ `- "127.0.0.1:8788:8788"` |
| 非回环绑定必须有 token | ✅ 进程级拒绝启动 |
| 非回环明文 base URL 拒绝 | ✅ 客户端级拒绝 |
| 路径白名单 | ✅ 15 条，新增需显式改代码 |
| 不接受任意 URL / 不代理任意请求 | ✅ 调用方永不提供 URL |
| 不跟随重定向 | ✅ |
| 速率 + 并发上限 | ✅ 默认 60/min、4 并发 |
| Origin 校验 | ✅ 默认拒绝所有浏览器 Origin |
| 审计日志不含凭据 | ✅ 有测试断言 |
| 响应体与超时上限 | ✅ 2 MiB / 20s |

**仍需你负责的部分**：反向代理的 TLS 与认证、token 的保管、
不把 8788 端口映射到公网。

---

## 9. 已知限制

1. **历史盘口层级不在 Shadow 库里**（`market_snapshots` 只存 best bid/ask
   与深度，`token_books` 只有最新一份）。参考本金对无层级的 episode 返回
   `NOT_EXECUTABLE`，**不用价格×数量顶替**。要让历史 episode 可执行，
   需要从 Collector 归档回填层级。
2. **`sell_yes` 不给盈亏数字**（契约 §4.4）：仓位语义未证明前，只给
   "方向判断正确率"，不并入可执行策略胜率。
3. **胜率样本量**：结算数据积累不足时返回 `null` + 原因，
   **不得用 ACCEPT 比例代替**。
4. **Brier / LogLoss / ECE** 由研究评估模块提供，`/api/v1/performance/models`
   目前转发 Edge 与胜率两组对比；评分项需回填 `models.json` 的评分段。

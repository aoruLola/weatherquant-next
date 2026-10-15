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
    reports/decisions-export/               决策导出分片（既有，流式引擎的输入）
/mnt/user/appdata/weatherquant-performance/ 【新增】Performance 快照
    summary.json  edge.json  win-rate.json
    opportunities.json  settlements.json  models.json
/mnt/user/appdata/weatherquant-performance-state/   【新增】流式引擎状态库
    performance-state.duckdb                可删可重建，见 §3.4
```

Dashboard 需要新增一条只读挂载：

```yaml
- /mnt/user/appdata/weatherquant-performance:/sources/performance:ro
```

**MCP Gateway 不挂载任何目录。** 这是安全属性，不是遗漏——它只经 HTTP
调用 Dashboard，因此被攻破也无法触及 DuckDB。

> **状态库不要挂给 Dashboard。** 它是研究进程的私有状态，不是发布物；
> Dashboard 只读 `weatherquant-performance/` 下的世代目录。


---

## 3. 首次回填（Research Analytics）

回填是**离线、可重复**的过程，不接触生产 Shadow 的数据库。

### 3.1 决策历史的获取（单写者规则）

**禁止**直接打开运行中的 `shadow.duckdb`（引擎持独占租约）。

**导出已由 Shadow 自动执行**（`runner._export_decisions`，默认每 300s 一批），
产物在 `shadow/reports/decisions-export/` —— 追加式分片加上一个 `state.json` 水位。
引擎用自己持有的连接写入，Research 只读该目录，**不需要停机，也不需要手动导出命令**。

分片布局、游标语义与失败恢复见契约 §11。

> 需要一次性追平时，可临时调小 `WQ_SHADOW_EXPORT_SECONDS`，观察
> `reports/decisions-export/state.json` 的 `rows_exported` 增长后恢复默认。

**回退方案（需批准）**：一致性备份 —— `docker stop weatherquant-shadow`，
复制 `.duckdb` 与 `.wal`，再恢复容器。**仅在维护窗口执行。**

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
  --export /mnt/user/appdata/weatherquant-shadow/reports/decisions-export \
  --settlements /tmp/settlements.json \
  --out /mnt/user/appdata/weatherquant-performance \
  --dataset live \
  --state-db /mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb \
  --capital-usdc 100 \
  --max-allocation-fraction 0.10
```

**`--export` 指向目录时走流式引擎**（生产路径）：一次只读一个分片，状态存进
`--state-db`，崩溃可续跑，内存由 `--memory-limit`（默认 1GB）限定而不是由历史长度决定。
`--export` 指向**文件**时走全内存构建器——那是**参考实现**，不是生产路径。

| 参数 | 说明 |
|---|---|
| `--export` | **必填**。目录 = 流式引擎（生产）；`.json`/`.jsonl`/`.parquet` = 全内存构建器 |
| `--out` | **必填**。快照输出目录 |
| `--dataset` | **必填**，`live` 或 `fixture`。**没有默认值** |
| `--state-db` | 状态库路径，默认 `WQ_PERFORMANCE_STATE_DB_PATH` |
| `--settlements` | 可选。§3.2 产出的结算索引。**不给则沿用状态库里已有的结算**（v0.1.5 起） |
| `--capital-usdc` | 默认 100，必须 > 0 |
| `--max-allocation-fraction` | 默认 0.10，研究假设而非已验证的风险规则 |
| `--gap-window-seconds` | 默认 900。**改变它必须 `--reset`**（episode 边界依赖它） |
| `--memory-limit` | DuckDB 内存上限，默认 `1GB`；超出即落盘到 `--temp-dir` |
| `--reset` | 清空状态库并完整回填（首次回填、算法升级、换导出目录时使用） |
| `--no-ingest` | 不读导出，仅从状态库重算并发布（结算修订后使用） |
| `--verify-parts` | 重新哈希**全部**已消费分片（默认只验边界分片） |
| `--max-parts-per-run` | 单次最多消费的分片数（首次回填可分批） |
| `--in-memory` | 强制使用全内存构建器（会打印警告，见 §10.2） |
| `--detail-window-episodes` | 可浏览明细的条数上限，默认 20000（契约 §14.1） |
| `--detail-window-days` | 可浏览明细的时间窗，默认 90 天 |
| `--settlements-replace` | 把 `--settlements` 当作**完整**结算集：其中没有的答案会被撤销 |

输出是**一个世代**：`performance/generations/<id>/` 内放六份快照与 `manifest.json`，
最后以一次 rename 切换 `performance/current.json` 指针（契约 §7.1）。
读者要么看到完整的一批，要么看到上一批，**不会**看到三个新、三个旧。
中途失败时新世代无人跟随，上一批仍然 current。

`manifest.json` 记录 `dataset`、输入水位 `watermark`、覆盖范围 `coverage`、
`algorithm_version`、`input_source`、`input_range`、`streaming` 与各文件大小 —— 每一批都可被审计与回溯（契约 §13.5）。

**不发布快照 ≠ 出错。** Dashboard 会显示 `missing` + "回填尚未产生该快照"，
不会显示 0，也不会编造图表。

### 3.4 状态库的运维

| 情况 | 做法 |
|---|---|
| 首次回填 | 直接运行（状态库为空 = 从头开始）。分片多时可用 `--max-parts-per-run` 分批 |
| 日常增量 | 同一命令重复运行；已消费的分片不再读，只处理新的 |
| 崩溃/断电 | 直接重跑。**同一事务**提交行与水位，未提交的分片会重读且不会重复计数 |
| 换导出目录 | 会因 `input_source` 不符而拒绝 → 确认无误后 `--reset` |
| 升级算法 | `algorithm_version` 变化后必须 `--reset` |
| 结算修订 | 带新的 `--settlements` 重跑，或 `--no-ingest` 只重算发布 |
| 怀疑状态库损坏 | 直接删除状态库目录后重跑（全部内容都是派生物） |

**禁止**：手工编辑状态库；在两次运行之间修改已发布的分片（会被检出并拒绝）。

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

---

## 10. 两次生产事故的实测记录（供后续维护对照）

### 10.1 决策导出停摆（2026-10-15，已修，PR #9）

导出停在 700,000 行约 8 小时：无异常、水位不动、无质量事件，而每个 cycle
都自报健康。**失败模式是"什么都没写的 tick"与"已追平的 tick"可观测状态完全相同。**

根因：`DecisionExporter._load()` 为统计行数读遍所有分片，而它跑在 tick 的
墙钟预算之内。对真实 1.1 GB 导出目录逐项计时：

| 步骤 | 耗时 |
|---|---|
| `glob` 35 个分片 | 0.001s |
| 读 `state.json` | 0.000s |
| 末行游标恢复 | 0.001s |
| **`_count_rows`（读全部分片）** | **5.287s** |

`shadow_export_max_seconds` 默认 5.0s。计数随导出增长而变慢，越过阈值处正是
停摆起点（part 35）。修复后 `_load()` 在稳态为 **0.01s**（只统计 state 未覆盖的
分片），恢复结果写入回磁盘，代价只付一次。

**注意它是间歇性的，不是永久性的**：实测期间它曾在 03:04 自行前进过一个分片
（700,000 → 720,000）——当某次重算恰好跑进 5 秒以内时就会前进。因此判断此类
故障不能只看"水位是否长期不动"，要看 `_count_rows` 相对预算的余量。

升级后首 tick 7 秒内写出 60,000 行；游标排他（`> ?` OR `= ? AND id > ?`），
从原水位续跑，**不重置、不重读**。完整性已对一致性备份核对：
备份中 `decision_time_utc ≤ 游标` 的决策数与导出行数**精确相等**。

**未解决的相邻风险**：游标只能前进，而 REPLAY 会写入*历史*时间戳。若某行在
游标越过其时刻之后才写入，它将永不导出。上述"无遗漏"结论**只对游标之前成立**。

### 10.2 Performance 全量重建的内存墙（2026-10-15）

`build_performance()` 把每条决策以 Python dict 全量驻留（实测约 6.8 KB/行）：

| 行数 | 需求 | 结果 |
|---|---|---|
| 700,000 | ~4.6 GB | 90 秒完成 |
| 960,000 | ~6.8 GB | 6.5 GB 上限 **OOM** |
| 960,000 | — | 7.5 GB 上限成功，但**耗时 35 分钟，主机在 04:08–04:43 完全无响应**（ping 通、HTTP 超时、SSH 认证超时） |
| 1,461,680（当时全量） | ~9.5 GB，另加 Shadow/Collector ~4 GB | **超过本机 16 GB，无法完成** |

导出只增不减，所以**全量重建在这台 16 GB / 无 swap 的主机上永远做不成**。
在修复为流式之前，**不得再在生产 NAS 上执行全量构建**。

> 判断依据不是"有没有加 `--memory` 上限"：上限只保证容器被杀而不是主机被杀，
> 不保证主机保持可响应。7.5 GB 那次容器没有 OOM，主机仍然失联 35 分钟。

---

## 11. 流式引擎的隔离压力测试（v0.1.5）

**全部在隔离机器上执行，未触碰生产 NAS、未打开生产库、未重启任何容器。**

脚本：`scripts/bench_performance_streaming.py`。合成历史按生产导出的**分片尺寸**
（约 19,000 行/分片）生成，含 24 个市场、3 个模型、两种 direction、LIVE/REPLAY、
以及会超过 gap 窗口的全局静默段。每个规模**独立进程**运行，峰值取
`ru_maxrss`（内核高水位，含 DuckDB 原生分配）。

| 决策行数 | 分片 | 墙钟 | **峰值 RSS** | 状态库 | 快照落盘 | episode | ACCEPT 观测 |
|---|---|---|---|---|---|---|---|
| 1,500,000 | 79 | **78 s** | **299 MB** | 28 MB | 96 MB | 73,188 | 673,582 |
| 3,000,000 | 158 | **169 s** | **368 MB** | 32 MB | 191 MB | 146,110 | 1,345,160 |
| 5,000,000 | 264 | **261 s** | **477 MB** | 70 MB | 317 MB | 242,570 | 2,236,360 |
| 10,000,000（超出要求，用于证明非线性） | 527 | 890 s | **613 MB** | 129 MB | 634 MB | 485,402 | 4,477,516 |

对照：旧的 `build_performance()` 在 960,000 行、7.5 GB 上限下耗时 35 分钟且主机失联
（§10.2）；它在生产规模上**从未完成过**，因此没有可比的峰值数字。

**内存曲线不是线性的**：299 → 368 → 477 → 613 MB（1.5M → 10M，6.7 倍行数只增
2.05 倍）。若随历史线性增长，10M 行应是约 950 MB。增长来自 DuckDB 缓冲池预热
（被 `memory_limit` 封顶）与更大的单个结果集。**时间**则是超线性的
（5M → 10M 行数翻倍、时间 3.4 倍），因为排序/聚合是 `n log n` 且导出本身已达
4.2 GB、受 I/O 限制——这是**时间**问题，不是内存问题。

### 11.1 上界来自 `memory_limit`，不是来自历史

5,000,000 行、**同一个输入**，只把 `--memory-limit` 从 1GB 改成 256MB：

| 配置 | 峰值 RSS | 墙钟 | `generation_id` |
|---|---|---|---|
| `--memory-limit 1GB` | 477 MB | 261 s | `20261015T120000Z-77c65e614411` |
| `--memory-limit 256MB` | **437 MB** | 359 s | `20261015T120000Z-77c65e614411`（**相同**） |

三点结论：

1. **上限缩小 4 倍，结果完全不变**（同一 `generation_id`，同一份 summary）——
   内存上限只决定**落盘多少**，不决定算出来什么。
2. 代价是时间（261 s → 359 s），也就是溢出的代价，而不是正确性的代价。
3. 峰值并没有随上限线性下降，因为峰值里还有两项与**批**同阶的常数：
   Python 侧的分片缓冲（一个分片 ≈ 19,000 行）与 DuckDB 物化的**一个结果集**。
   两者都与历史长度无关。

**与历史的对照**（默认 1GB 上限）：1.5M → 299 MB，3M → 368 MB，5M → 477 MB，
10M → 613 MB。若随历史线性增长，1.5M → 10M（6.7 倍行数）应是约 2 GB；
实测 2.05 倍。增长来自 DuckDB 缓冲池的预热（被 `memory_limit` 封顶）与更大的
单个结果集，不是"记住更多历史"。

### 11.2 宿主可响应性

压力测试（5M 行 / 256MB 上限）期间，在同一台机器上每秒采样一次空命令的端到端延迟：

| 指标 | 值 |
|---|---|
| 采样数 | 354 |
| 中位延迟 | **6 ms** |
| p95 延迟 | **17 ms** |
| 最大延迟 | **32 ms** |

对比 §10.2：旧构建期间主机 ping 通，但 HTTP 与 SSH 全部超时，持续 35 分钟。
本轮压力测试期间宿主的交互式命令**没有出现可观测的停顿**——
上面这份采样与 5M 行构建是并行的。

### 11.3 复现

```bash
uv run python scripts/bench_performance_streaming.py --all --work-dir /path/to/scratch
uv run python scripts/bench_performance_streaming.py --rows 5000000 --no-generate \
  --reset --memory-limit 256MB
uv run python scripts/bench_performance_api.py --generation /path/to/performance
```

产物：`report-<rows>.json`（每个规模的墙钟、峰值 RSS、落盘体积、状态库计数、
六份 payload 的关键统计），可直接作为验收证据附在 PR 上。

> 脚本会写入 `--work-dir` 下的合成分片（5M 行约 2 GB），**不要指向生产目录**。

---

## 12. 明细体积与 Dashboard 读取（v0.1.5 分页）

§11 解决的是**研究进程**的内存。快照本身仍然随历史增长，而 Dashboard 会整文件读取它：
实测 500 万行时 `summary.json` 131 MB、`opportunities.json` 163 MB、`edge.json` 23 MB，
于是 `/summary` 返回 131 MB、`/opportunities` **解析 163 MB 只为返回 50 行**。

修复见契约 §14：明细变成**窗口**（默认 20,000 条 / 90 天）并按**分片**发布，
Edge 分带改为分布，参考本金只发汇总与有界样本。

### 12.1 落盘体积（1,500,000 行，实测）

| 文件 | 修复前 | 修复后 |
|---|---|---|
| `summary.json` | 39.6 MB | **31 KB** |
| `opportunities.json` + `opportunities/` | 49.1 MB | **5.6 KB + 13 MB** |
| `edge.json` | 6.8 MB | **18 KB** |
| `win-rate.json` / `models.json` / `settlements.json` | 0.2 MB | 0.2 MB |
| **世代合计** | **95.8 MB** | **13.8 MB** |

统计结果**逐位不变**（同一次运行：73,188 episode、673,582 ACCEPT 观测、
胜率 0.6720234347195225），构建墙钟 78 s → 75 s，峰值 RSS 299 MB → 306 MB。

### 12.2 Dashboard 端点（1,500,000 行，本机，12 次采样）

| 端点 | 响应体 | 中位 | p95 | 单次请求峰值分配 |
|---|---|---|---|---|
| `/summary` | 29.7 KB | 1.5 ms | 2.0 ms | 0.11 MB |
| `/edge` | 17.4 KB | 1.2 ms | 1.3 ms | 0.08 MB |
| `/win-rate` | 33.0 KB | 2.0 ms | 2.1 ms | 0.11 MB |
| `/opportunities`（第 1 页，50 条） | 33.1 KB | 10.7 ms | 14.0 ms | 2.48 MB |
| `/opportunities`（第 2 页） | 33.3 KB | 10.8 ms | 11.0 ms | 2.48 MB |
| `/opportunities`（带筛选） | 33.3 KB | 166 ms | 168 ms | 4.35 MB |
| `/opportunities`（limit=200） | 126 KB | 12.3 ms | 12.7 ms | 2.48 MB |
| `/settlements` | 10.5 KB | 5.2 ms | 5.7 ms | 0.56 MB |
| `/models` | 4.2 KB | 2.6 ms | 2.7 ms | 0.04 MB |
| `/reference-capital`（按需本金） | 27.3 KB | 484 ms | 515 ms | 49 MB |

三点说明：

1. **无筛选分页只打开需要的分片**：请求第 2400 条时只读 `part-00002.json`
   （有测试断言这一点）。带筛选的请求必须扫描窗口——不可变文件没有索引，
   而"扫窗口"正是窗口存在的意义；代价是 166 ms，与历史长度无关。
2. **`/reference-capital` 是最慢的一条**：它要重建明细窗口（默认 20,000 条）才能按新本金
   重跑资金模拟，峰值 49 MB 由窗口而非历史决定。UI 已把它从 60 s 轮询改为 300 s
   （改动本金/模式会立即重新请求）。要更小就调小 `--detail-window-episodes`。
3. **任何端点的响应体都不随历史增长**：页面是页面。

### 12.3 内存与宿主

Dashboard 容器因此不需要为"解析快照"留出与历史成比例的内存：最坏一条
（`/reference-capital`）由窗口决定，其余都在几 MB 以内。



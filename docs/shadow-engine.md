# Shadow Trading Engine：架构

2026-10-11。P2-0。
实现：`src/weatherquant/shadow/`、`src/weatherquant/models/{artifacts,factory}.py`、
`src/weatherquant/storage/shadow.py`、`src/weatherquant/storage/migrations_shadow/`。

风险闸门与状态机见 [`shadow-risk-rules.md`](shadow-risk-rules.md)；
NAS 部署见 [`shadow-unraid-deployment.md`](shadow-unraid-deployment.md)；
本轮实际验证结果见 [`../research/shadow-validation.md`](../research/shadow-validation.md)。

---

## 1. 目标与边界

在 Oreochromis NAS 上 24 小时无人值守地对**真实** Polymarket 行情做
**时间一致、成本一致、可审计**的预测决策观察。

| 允许 | 禁止 |
|---|---|
| 实时概率预测 | 真实下单 |
| 获取实时行情（经由采集器） | 钱包 / 私钥 |
| 计算预期净收益与 Edge | 真实资金管理 |
| 模拟评估订单是否可成交 | 为增加交易数量放宽阈值 |
| 记录 ACCEPT / REJECT / NO_TRADE | 用未来天气数据计算历史信号 |
| 统计理论机会与拒绝原因 | 把中间价当作可成交价格 |
| | 修改生产 Collector 数据库 |
| | 与生产 Collector 争抢 DuckDB 写锁 |

**结构性保证**：`weatherquant.shadow` 及其依赖里没有下单客户端、没有订单
ID 字段、没有持仓表。不存在能打开它们的开关 —— 这是模块边界，不是配置。

---

## 2. 运行架构

```
Unraid / Oreochromis
┌─────────────────────────────┐      ┌──────────────────────────────────┐
│ 容器 A  weatherquant-collector│      │ 容器 B  weatherquant-shadow       │
│ 镜像 :next:stable            │      │ 镜像 :next-shadow:shadow-stable   │
│                             │      │                                  │
│  /data  ←─ RW               │      │  /data       ←─ RW（自己的目录）  │
│   ├ weatherquant.duckdb      │      │   ├ shadow/shadow.duckdb         │
│   ├ raw/{gamma,rest,ws}/     │      │   └ health/heartbeat.json        │
│   ├ health/heartbeat.json    │      │                                  │
│   └ .collector.lock          │      │  /collector-raw  ←─ RO 挂载 ──────┤
│                             │      │   = 采集器的 raw/                 │
│  职责：采集、落盘、心跳        │      │  /app/models      （镜像内置）     │
│                             │      │  /app/configs/shadow-thresholds   │
└─────────────────────────────┘      │                                  │
                                     │  职责：只读尾随、推断、评估、记录   │
                                     │  /data/.shadow.lock              │
                                     └──────────────────────────────────┘
```

四个不可破坏的性质：

1. **只读输入。** 影子挂载采集器的 `raw/` 为 `:ro`，从不打开采集器的
   DuckDB —— 代码里根本没有指向它的路径。
2. **独立存储。** `shadow.duckdb` 是第三个文件，用同一台 checksum 前向
   迁移器指向第三个迁移目录。`ShadowDatabase` 会**按名字拒绝**采集器和
   研究库（`_guard_foreign_database`），而不是等迁移器报一个晦涩的
   checksum 错误。
3. **独立单实例。** `.shadow.lock` 租约在**打开数据库之前**获取，拒绝时
   退出码 4 —— 那是一次成功的拒绝，不是崩溃。
4. **不加公网端口。** 影子只出站（Open-Meteo）与读挂载文件，不提供任何服务。

---

## 3. 数据通道：只读尾随 JSONL

### 3.1 为什么是文件而不是数据库

规范的优先方案就是"影子以只读方式读取采集器已完整持久化的原始事件"。
两个原因让它比读库更合适：

- DuckDB 的文件锁是**跨进程排他的**，连只读打开都会被写入方阻断
  （本项目已有此教训）。挂一个 `.ro` 的文件目录没有这个问题。
- 归档里是**逐帧原样**的上游载荷，而库里是"schema 预料到的字段"。
  真正需要判断"这条消息到底是什么"时，原样载荷才是证据。

### 3.2 封面（采集器写入，影子只读）

`raw/<source>/YYYY-MM-DD.jsonl`，每行：

```json
{"archive_seq":1234,"source":"ws","received_at_utc":"...","venue_ts_utc":null,
 "venue_ts_origin":"missing","quality":"ok","run_id":"...","payload":{...}}
```

`received_at_utc` 是**采集器的时钟**，`shadow_received_at_utc` 是**本进程读到
它的时刻**。两者永不合并 —— 否则一个慢影子会被记成一个快采集器。

### 3.3 事件分类（`shadow/events.py`）

原始归档里有四件都长得像"关于价格的消息"的东西，它们**不可互换**：

| kind | 是什么 | 能否用于定价 |
|---|---|---|
| `book_snapshot` | 一个时刻的**完整** L2 深度 | ✅ 唯一可以逐档走的 |
| `book_delta` | 变动的**一个**价位 | ❌ 只有 1/N，记录不计价 |
| `trade_print` | 一笔成交，无深度 | ❌ |
| `market_metadata` | 括号、token、费率、生命周期 | ✅（但不提供深度） |

**为什么增量消息绝不当盘口用**：`price_change` 每条只带一个价位，不知道
其余价位是什么；本项目与采集器**都没有序列号机制**能把它们安全地拼回
完整状态。把它当成盘口，会在一个从未存在过的盘口上算出一个看起来很确定
的成交价。所以它被**计数并记录**，从不参与定价。

### 3.4 尾随契约（`shadow/ingest.py`）

- **只消费完整行。** 偏移量只越过 `\n`；半行永远不被解码。
- **偏移与效果同事务提交。** 崩溃要么整批回放，要么整批都没有 ——
  "偏移前进了但效果没有"这种状态**无法表示**。这是不需要逐行存表就能做到
  exactly-once 的原因。
- **先提交，后接管（`ArchiveTailer.adopt`）。** `poll()` 对尾随器自己的状态
  **只读不写** —— 它返回 offsets，调用方在 DuckDB 事务**成功提交之后**才把它们
  接管进内存。顺序反了就是丢数据：内存先走、事务回滚，下一轮会从已经"越过的"
  位置继续，那批字节既不在库里也不会再被读。反了顺序只是重读一批，可以恢复。
  - **这个顺序曾经是反的**：`poll()` 返回新 offsets，但没人写回
    `self.checkpoints`，于是**每个周期都从字节 0 重读**。生产症状是
    `collector_last_seen_utc` 冻结、盘口场地时间戳不前进、而
    `shadow_received_at` 照常跳动 —— 因为"读到了"和"读到的是新的"是两回事。
    回归测试 `tests/test_shadow_checkpoint_advance.py` 驱动的是 **Runner**
    而不是 tailer：早期 ingest 测试手动合并 offsets，做的正是生产代码漏掉的
    那一步，所以它们在缺陷存在时依然全绿。
- **损坏必须留痕。** 截断、文件消失、整行损坏、单行超长 —— 各自记为
  `data_quality_events`，一个都不静默跳过。静默跳过，等于假设文件没问题。
- **跨天缺口显式记录。** 同一 source 相邻两个日期文件之间缺了 N 天，
  记 `archive_gap`，而不是让时间轴看起来连续。
- **读取延迟可测。** `shadow_lag_ms = shadow_received − collector_received`。

重启语义：偏移、目录、盘口三者同批提交；重启后从库里重建目录与最新盘口，
从偏移处继续尾随 —— **不回放、不重复、不问上游**。

### 3.5 盘口来源

- `rest`（采集器每 5 秒轮询 `/book`）—— **权威快照**，复用
  `weatherquant.data.books.parse_book_payload`，因此影子算出的 `book_id`
  与采集器**逐字节相同**，可按值交叉核对。
- `ws` 的 `event_type="book"` 数组帧 —— 也是完整快照，标记 `book_source='ws'`。
- `ws` 的 `price_change` / `last_trade_price` / `tick_size_change` —— 记录，
  不定价。

---

### 3.6 LIVE 与 REPLAY：历史不能冒充实时

**问题**：同一份代码既可以尾随正在被写入的归档，也可以重读三个月前的归档。
如果两者产出的行长得一样，一份历史重放就会被当成实时决策读走。

**判定（每个进程只做一次）**：

```
WQ_SHADOW_MODE = live    强制 LIVE
WQ_SHADOW_MODE = replay  强制 REPLAY
WQ_SHADOW_MODE = auto    （默认）看最新归档行的年龄：
                         age <= shadow_collector_silence_seconds → LIVE
                         age >  那个窗口                        → REPLAY
```

**只判定一次**，然后整个运行期保持不变。原因很具体：采集器在影子启动
**之后**停机时，必须表现为 `COLLECTOR_DISCONNECTED` —— 如果此刻把模式翻成
REPLAY，一次故障就会被改写成"我们在读历史"，而那正是要防的混淆。

**两种模式的差别，只在"时刻"和"我们的回执"上：**

| | LIVE | REPLAY |
|---|---|---|
| `decision_time` | 决策那一刻的时钟 | **归档自己最新的事件时刻** |
| 预报按何时点解析 | `decision_time` | 同左 → 只有当时已可发布的运行可用 |
| `publishable_at ≤ decision_time` | 强制 | **同样强制**（这是时间点一致的核心） |
| 本进程的 `shadow_received` / 预报 `received_at` 晚于决策 | 拒绝（TIME_MISMATCH） | **豁免** —— 重放本来就是现在读的，不是泄漏 |
| 场所时间戳 vs 采集器时间戳、负延迟 | 拒绝 | **同样拒绝** —— 两个都是上游时钟，与我们无关 |
| 采集器静默 | `COLLECTOR_DISCONNECTED` | **不报** —— 本来就没有实时源要等 |

**每条决策都写 `mode` 与 `mode_reason`**（迁移 `0003_decision_mode`）。
默认值是 `UNKNOWN` 而不是 `LIVE`：忘记标注模式的调用方不能拿到一行
声称自己是实时的记录。

**报告分开计数**：

```
  by mode                  LIVE=66
  ACCEPT live / replay     6 / 0
```

`replay_accepted > 0` 会进入 `warnings`，明确写出"这些不是当代信号"。

**验证**（真实归档，非构造数据）：归档最新事件 15:44:46Z、时钟 17:51Z，
`auto:archive_age_7625s_gt_900s` → 全部 66 条 `REPLAY`，
`decision_time = 2026-10-11T15:44:46Z`（**不是** 17:51），
`ACCEPT live / replay = 0 / 6`，且无 `COLLECTOR_DISCONNECTED`。

---

## 4. 天气模型实时推断

**复用 P1-2，不重写任何一条概率公式。**

### 4.1 模型工件（`models/artifacts.py` + `models/factory.py`）

研究管线是"拟合 → 打分 → 丢弃"；影子必须**把决策时用的那组参数原样
重新加载**。于是：

```
models/<name>/<version>.json     ← 不可变，拒绝覆盖
models/manifest.json             ← 固定用哪个版本
```

三条性质，进出都强制：

| 性质 | 实现 |
|---|---|
| **版本不可变** | `save_artifact` 对已存在版本抛 `FileExistsError` —— 参数会变的版本不是版本 |
| **截止日期随参数走** | `training_cutoff_utc` 在工件上；`usable_at(decision_time)` 拒绝截止日晚于决策时刻的工件 |
| **源数据被哈希** | `source_data_hash` = 拟合所见行的 SHA-256，"同版本"可被验证而非假定 |

**工件与代码同库存放**（仓库根 `models/`，非 `data/`）：随代码一起评审、
一起打进镜像，于是"哪个模型做的这个决定"由镜像 tag 单独回答。
`fit-models --cutoff` **必填** —— "用库里现有一切"作为默认值，等于训练窗口
没人选过。

```bash
uv run weatherquant-research fit-models --cutoff 2026-10-08 --version v1 --pin
```

四个模型与研究基线一一对应：

| 工件 | 点预报 | 偏置 |
|---|---|---|
| `ecmwf_raw` | ECMWF 原始 | 0 |
| `gfs_raw` | GFS 原始 | 0 |
| `fusion` | 两模型**同一次运行**的均值 | 0 |
| `calibrated` | 最优 raw + 拟合偏置 | 拟合 |

`raw` 与 `calibrated` 共用同一套 σ —— 消融设计不变，分数差异只能归因于偏置。

### 4.2 可发布性（`shadow/forecast.py`）

```
publishable_at = run_time + 12h     basis = 'estimated'
```

与点位历史用的是**同一个常量、同一个 basis、同一套算术** —— 两个"可用"
定义，就是实盘引擎与它自己的回测开始背离的方式。

- `publishable_at > decision_time` → 不是"慢"，是**还不属于我们**，拒绝。
- 复用 `SingleRunsClient`，`aggregate_daily_max`，`local_day_bounds_utc`
  （时区与夏令时由 zoneinfo 处理，不是 `date + 24h`）。
- `usable_at(now)` 决定能否交易；`research_eligible_at(target_date)`
  记录是否可进严格回测。**两者分别产出，互不套用。**

**fusion 必须同一次运行**：00Z 的 ECMWF 与 06Z 的 GFS 是大气的两个不同状态，
取均值再叫"融合"是凭空造一个两个模型都没发过的预报。没有共同的可发布运行
时，融合就**没有预报** —— 这是拒绝，不是部分混合。

### 4.3 缓存

| 键 | 复用条件 |
|---|---|
| 分布 | `(model, version, run_time, target_date, event)` 完全相同 |
| 上游运行 | `ok` / `unavailable` 永久缓存；`failed` 按 1h 冷却重试 |

**分发缓存键含 `event`**：同一目标日可能有两个事件、两套阶梯，只按日期缓存
会把一套阶梯的概率喂给另一套。

网络失败 → 记 `forecast_fetch_failed` + 指数退避，绝不让上游故障变成压测。

---

## 5. 实时 Edge 计算

**唯一算术来源：`weatherquant/execution/pricing.py`。** 任何交易成本只能由
该模块计算，影子只做组合，不重新推导。

| 方向 | 走哪一侧 | 期望 |
|---|---|---|
| 买入 YES | 真实 **ask** | `q·shares − (fill + fees + buffer)` |
| 卖出 YES | 真实 **bid** | `(fill − fees − buffer) − q·shares` |

两个方向共用 `ExecutionCost` 的**同一个三项分解**，因此手续费不可能在买方
被计入、卖方被忘记，也不可能任何一侧重复计费。

`build_fill_plan` 自己知道哪些缺陷会阻断哪一侧，所以**闸门与定价器不可能
对"什么可执行"产生分歧**。

每条决策记录（规范 §5 要求的字段全在 `shadow_decisions` / `market_snapshots`）：

```
book_id, venue_ts, collector_received_at, shadow_received_at, decision_time,
snapshot_age_ms, model_run_id, proposed_shares, estimated_fill_price,
estimated_fees_usd, estimated_buffer_usd, expected_net_profit_usd, net_edge,
decision_latency_ms, thresholds_version
```

---

## 6. 决策状态机

见 [`shadow-risk-rules.md`](shadow-risk-rules.md)。要点：

- `CANDIDATE → NO_TRADE | DATA_UNAVAILABLE | REJECT | ACCEPT`
- **缺失 → DATA_UNAVAILABLE，存在但被拒 → REJECT**（机械规则）
- 主原因 = 因果序中最靠前者，与闸门触发顺序无关
- 一条记录可带多个原因；`rejection_reasons` 表存全部，`is_primary` 标一个

---

## 7. Shadow 数据库

`data/shadow/shadow.duckdb`，两个前向迁移：

| 迁移 | 表 |
|---|---|
| `0001_shadow_schema` | shadow_runs, model_versions, model_predictions, market_snapshots, shadow_decisions, rejection_reasons, data_quality_events, shadow_metrics |
| `0002_ingest_state` | ingest_checkpoints, market_catalog, token_books, forecast_runs, forecast_daily_max |

- **自然键幂等**：`ON CONFLICT (自然键) DO UPDATE`，同一批次回放会收敛而不是翻倍。
- **单写者**：`SingleInstanceLock` 在 `duckdb.connect` 之前获取。
- **批量事务**：`ShadowDatabase.transaction()` 把效果与偏移绑在同一提交。
- **括号列 first-write-wins**：括号是市场的身份，不随刷新漂移
  （与采集器自己的 markets upsert 一致）；生命周期字段照常刷新。
- **盘口不倒退**：晚到的旧快照不会把盘口回滚。

与研究库交换数据只通过**显式导入导出与版本化工件**（`fit-models`），
绝不共用文件。

---

## 8. 监控与运行报告

CLI：`weatherquant-shadow report [--json]`。四个区块**分开**，不混成一个数：

| 区块 | 内容 | 明确不包含 |
|---|---|---|
| `prediction_quality` | 各模型预测条数、覆盖、被拒次数 | **Brier / LogLoss** —— 需要结算，而结算在决策时还没发生；`scored: false` 明说 |
| `theoretical_edge` | 均值、正 edge 数量、ACCEPT 的均值 | **已实现收益** —— 注释写死"不是已实现回报" |
| `liquidity` | 快照数、平均价差、两侧深度、深度不足的拒绝数 | |
| `decisions` | 评估数、ACCEPT/REJECT/NO_TRADE/DATA_UNAVAILABLE、主原因分布、P95 延迟 | |

另有 `liveness`（采集器最后可见、最新盘口与预报年龄）、`coverage`
（预报覆盖率、盘口覆盖率）、`data_quality`（数据缺口数、错误数）、
`warnings`（含仍停在 CANDIDATE 的中断行）。

**`equity_curve` 恒为 `null`**，并附一句为什么：没有成交就没有已实现收益，
画一条假想曲线是本模块能产出的最具误导性的东西。

---

### 8.1 报告从快照读，不打开数据库

**问题**：影子主进程对 `shadow.duckdb` 持有排他租约，而 **DuckDB 连只读都不允许
第二个进程打开**（本项目早有教训：旧库在 `quant.cli collect` 运行时连
`connect(read_only=True)` 都报 `another program is using it`）。于是
`weatherquant-shadow report` —— 运维在引擎**运行中**最需要的那条命令 —— 恰好是
唯一跑不起来的那条。

**修复是把"读"的位置换掉**，不是去解锁：

```
引擎（已有连接）──算──► /data/reports/report.json（原子替换）──► report CLI 读
```

| | |
|---|---|
| 谁写 | **主进程**，用它**已经打开**的连接，从不再开第二个 |
| 何时写 | `start()` 立即一次，之后每 `shadow_report_seconds`（默认 60s）一次 |
| 怎么写 | 临时文件 + `os.replace` 原子替换，`fsync` 后 `chmod 644`（`mkstemp` 默认 0600，不放宽则只有写者自己能读） |
| 何时算 | **周期末尾、提交之后** —— 读到的永远是已落库的状态 |
| 写失败 | 记 `report_publish_failed`（warning）并继续 —— **报告不是任务，报告失败不能停引擎** |

CLI 默认读快照，**带 `--json`**，输出含：

```
generated_at_utc   数字是何时算出来的（不是文件何时写的）
expires_at_utc     generated + stale_after_seconds
stale / age_seconds / seconds_past_expiry
source             shadow-main | offline-db
```

文本模式额外打印 `[snapshot]` 块；**过期仍会打印**，只是标 `EXPIRED` 并说明
"这些数字描述的是过去" —— 一份带警告的过期报告，好过一份没有。

**离线能力保留**：`report --offline`（或显式给 `--db`）直接开库，用于引擎已停、
没有快照的场景。若此时库被占用，返回**退出码 3** 并说明原因，而不是抛栈：

```
cannot open ...duckdb: ...
The shadow engine holds an exclusive lease on that file while it runs, and DuckDB
blocks other processes even read-only. Drop --offline (and --db) to read the
snapshot the engine publishes at /data/reports/report.json
```

退出码：`0` 成功 · `2` 库与快照都没有 · `3` 库被占用。

共享原语在 `shadow/publish.py`（`atomic_write_json`）—— 心跳与报告快照共用一份
实现，因为"两份十五行的复制体"正是本轮 CI 检查漂移的方式。

**Docker 验收**：`shadow-ci.yml` 在引擎持续运行时连续调用 report 10 次
（`--json` 与文本各一次），断言全部成功、`source == shadow-main`、心跳
`updated_at_utc` 期间仍然前进、宿主能读到 `/data/reports/report.json`。

---

## 9. 运行调度（`shadow/runner.py`）

一个周期四个阶段，顺序有承载力：

```
1  ingest      尾随 → 更新内存视图 → 【事务】效果 + 字节偏移 + 质量事件
2  forecast    网络 I/O —— 故意在事务之外：挂起的上游不能持有数据库锁
3  evaluate    解析预报 → 分布 → 闸门 → 【事务】预测 + 决策
4  health      重写 heartbeat（原子写），容器探针只读这个文件
```

防 CPU 浪费的三层键控：

1. 分布按 `(model, version, run, date, event)` 备忘，同一运行只算一次；
2. 上游抓取间隔 `shadow_forecast_refresh_seconds`（默认 10 分钟），
   **不是每个 tick** —— 归档突发不会变成上游请求突发；
3. 每周期每模型最多 `shadow_max_new_runs_per_cycle` 个新运行。

**只评估当前 tradeable 的市场** —— 市场一关闭就不再消耗周期。
`SIGTERM/SIGINT` → 置停止位 → 跑完当前周期 → 提交 → 写心跳 → 释放租约 →
退出码 0。

崩溃恢复：偏移 + 目录 + 盘口同批，所以重启后既不回放也不丢；中断的评估
留下 `CANDIDATE` 行，报告的 `warnings` 会点名。

---

## 10. 与研究管线的关系

| | 研究 | 影子 |
|---|---|---|
| 数据 | 研究库（回溯批次） | 采集器归档（实时只读） |
| 时间规则 | `publishable ≤ 目标日开始` | `publishable ≤ 决策时点`（严格规则另行记录） |
| 模型 | 拟合即丢弃 | 拟合 → 冻结 → 固定版本加载 |
| 输出 | Brier / LogLoss / 覆盖 | 决策与拒绝原因 |
| 写入 | `weather-research.duckdb` | `shadow.duckdb` |

**影子不给模型打分**，它没有结算；**研究不下决策**，它没有实时盘口。
两者只通过版本化工件（`fit-models --pin`）和显式导出交换。

---

## 11. 模块地图

```
src/weatherquant/shadow/
├ config.py      ShadowSettings + Thresholds（阈值从版本化 JSON 加载）
├ events.py      封面解码、事件分类、gamma→目录、完整盘口解析
├ ingest.py      只读尾随：偏移、半行、截断、缺口、分类、统计
├ state.py       内存视图（目录 + 最新盘口），可从库重建
├ forecast.py    可发布性、issue 时次、fusion 对齐、抓取缓存与重试
├ inference.py   工件加载、分布计算与备忘、拒绝原因
├ decision.py    状态机、因果序、逐条闸门（纯函数，无 I/O）
├ store.py       全部写入：自然键幂等 + 单事务批量
├ runner.py      四阶段循环、单实例、信号、心跳、指标
├ health.py      原子心跳写 + 探针判定（不碰数据库）
├ report.py      四区块机器可读报告
└ cli.py         weatherquant-shadow {init,run,report,status}

src/weatherquant/models/artifacts.py   不可变工件 + manifest 固定
src/weatherquant/models/factory.py     四个基线的拟合与冻结
src/weatherquant/storage/shadow.py     ShadowDatabase + 迁移 + 归属守卫
src/weatherquant/storage/migrations_shadow/  0001_*, 0002_*
```

依赖方向遵守 [`architecture.md`](architecture.md) 的"只向下"：
`shadow → execution / models / markets / storage / weather / data`，
`storage` 不 import 任何领域模块，没有任何东西 import `legacy`。

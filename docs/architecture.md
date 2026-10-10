# WeatherQuant Next 架构设计

P0 版本，2026-10-10。

---

## 1. 设计约束从哪来

本架构不是偏好清单，而是把规范里的硬性要求翻译成代码结构。每一条下面标注它约束了什么。

| 规范要求 | 架构上的落点 |
|---|---|
| 概率必须经过历史**样本外**验证 | `models/metrics.py` 提供评分函数；`date_level_summary` 强制按日期聚类取不确定度 |
| 不假设市场必然错误定价 | 信号输出的是"模型自身期望"，命名即 `expected_net_profit_*`，不叫 "profit" |
| 同目标日多次预报是**相关样本** | `date_level_summary(values, cluster_ids)`；<2 个日期直接抛错，不编造区间 |
| 市场概率比较须**时间点一致** | `ForecastRun.publishable_at_utc` + `OrderBook.venue_ts`/`fetched_at_utc` 两个独立时钟 |
| 信号与模拟成交**共享定价逻辑** | 唯一来源 `weatherquant/execution/pricing.py` |
| 不允许 midpoint 冒充成交价 | 成交价只能由 `build_fill_plan()` 走真实深度得出；midpoint 属性仅标注为 "analysis only" |
| 费用不能重复计算 | `ExecutionCost` 三段相加，`total_cost_usd` 是属性而非可写字段 |
| 观测**永远不是**官方结果 | `Observation` 类**没有** `is_official` 字段——这是结构，不是可翻转的布尔 |
| 不使用 LLM 猜测天气 | 本仓库不含任何 LLM 调用 |
| 研究操作不得修改模拟交易账本 | 见 §4 |
| 完全可复现 | 版本化迁移 + 校验和 + 一切时间戳为绝对 UTC |

---

## 2. 模块划分与依赖方向

```
weatherquant-next/
├── src/weatherquant/
│   ├── data/         上游采集与原始载荷归档          [P1]
│   ├── markets/      Polymarket 市场/事件/订单簿模型  [P0]
│   ├── weather/      预报与气象站观测模型             [P0]
│   ├── models/       概率模型与评估指标               [P0: metrics]
│   ├── execution/    共享定价/成本/Edge 计算          [P0]
│   ├── storage/      DuckDB 连接、schema、迁移        [P0]
│   ├── legacy/       旧库只读导入                     [P0]
│   ├── strategies/   信号逻辑                         [P1]
│   ├── backtest/     研究引擎                         [P1]
│   ├── paper/        模拟交易引擎                     [P2]
│   └── risk/         风险限制                         [P2]
```

### 依赖方向（只允许向下）

```
strategies ──┐
             ├──> execution ──> markets
paper ───────┘         │
                       └──────> (weather, models/metrics)
backtest ──> models/metrics, execution, storage
data ──> storage, markets, weather
legacy ──> storage, markets/weather (类型)
storage ──> (无内部依赖)
```

**禁止的依赖：**

- `strategies` ⇄ `paper` 互不 import。二者只能通过 `execution` 与领域模型通信。
- `storage` 不 import 任何领域模块（它只管字节落盘）。
- 任何模块不得 import `legacy`（旧数据是外部输入，不是内部依赖）。

### 关于新增 `execution/` 的说明

推荐目录列表中没有它。规范要求"交易信号和模拟成交应共享同一套定价计算逻辑"——若把定价放进
`strategies`，`paper` 就要反向依赖 `strategies`；放进 `paper`，`strategies` 又要依赖 `paper`。
两者都错。因此新增一个中立的 `execution` 包，作为**全库唯一**的成本计算来源。

---

## 3. 两个引擎：Research 与 Paper

```
┌──────────────────────────────────┐   ┌──────────────────────────────────┐
│       Research Engine            │   │      Paper Trading Engine        │
│       (backtest/)                │   │      (paper/)                    │
│                                  │   │                                  │
│  历史数据 → 模型训练 → 概率校准   │   │  实时行情 → 模型推断 → 交易信号   │
│  → 回测 → 指标比较               │   │  → 风险限制 → 虚拟订单            │
│                                  │   │  → 持仓与结算                    │
└───────────────┬──────────────────┘   └───────────────┬──────────────────┘
                │                                      │
                │        ┌──────────────────┐          │
                └───────>│  execution/      │<─────────┘
                         │  (同一套定价)     │
                         └────────┬─────────┘
                                  │
                ┌─────────────────┴──────────────────┐
                │  shared: markets/ weather/ models/  │
                │          storage/                   │
                └────────────────────────────────────┘
```

### 隔离保证（P2 起需要落实，P0 只定边界）

| 保证 | 机制 |
|---|---|
| 研究操作**不能**改写交易账本 | 两个引擎写入**不同的表**；`account`/`orders`/`fills`/`positions`/`equity_snapshots` 只有 `paper/` 可写 |
| 研究操作**不能**改写模拟账本的读取视图 | `backtest/` 只读 `forecast_*`、`observations`、`order_books`、`settlements`、`price_history` |
| 同一套定价 | 二者都必须 `from weatherquant.execution import build_fill_plan, ExecutionCost, edge_usd_per_share`；**任何模块不得自行重算成本** |
| 单写者 | DuckDB 单进程写；worker 持租约（沿用旧版 `process_locks` 模式，P2 实现） |

P0/P1-0 阶段这两个引擎**尚不存在**（只有共享层 + 采集器）。`strategies/`、`backtest/`、`paper/`、`risk/` 目前只有 `.gitkeep`，**没有任何代码**——规范要求不为未来阶段生成占位代码，空桩会让某个能力看起来已经存在。

---

## 4. 数据模型

### 4.1 三类数据严格分开，永不混用

| 类别 | 表 | 含义 |
|---|---|---|
| **预报** | `forecast_runs` / `forecast_hourly` / `forecast_daily` | 模型对**未来**日最高温的预测 |
| **观测** | `observations` / `daily_max_temperatures` | 站点**实际测到**什么 |
| **结算** | `settlements` | Polymarket **官方判定**了什么 |

三者的区别是结构上的，不是约定：`Observation` 没有 `is_official` 字段，`Settlement.yes_price`
有 `CHECK (yes_price IN (0,1))`，`forecast_daily` 受 `FOREIGN KEY` 约束必须有对应的
`forecast_runs` 行。

### 4.2 单位即字段名

规范要求"所有概率、成本和收益都必须标注单位"。做法是把单位编进字段名，让错误在类型层就暴露：

| 单位 | 字段名样式 | 例 |
|---|---|---|
| 美元 | `*_usd` | `fill_cost_usd`、`fees_usd` |
| 每股美元 | `*_usd_per_share` | `price_usd_per_share` |
| 股数 | `*_shares` | `filled_shares`、`size_shares` |
| 温度 | `*_f` / `*_c` | `predicted_max_f`、`temp_c` |
| 时间 | `*_utc` | `run_time_utc`、`venue_ts`（venue 自带时间，语义特殊故不加后缀） |

**不接受裸 `price`、裸 `cost`。** 需要猜测单位的字段一律视为 bug。

### 4.3 时钟：两个，不是一个

```
ForecastRun.run_time_utc         模型初始化时刻（不是发布时间）
ForecastRun.publishable_at_utc   最早可用时刻 = run_time + lag（默认 6h）
                                 CHECK: publishable_at_utc >= run_time_utc

OrderBook.venue_ts               场所自己的事件时刻（用于判定陈旧）
OrderBook.fetched_at_utc         我们拉取的时刻
```

任何决策时点 `t` 必须满足 `t >= publishable_at_utc` **且**依据的订单簿 `venue_ts`（或回退到
`fetched_at_utc`）足够接近 `t`。**两者缺一即时间点不一致**——这正是规范"市场概率比较必须保证
时间点一致"的落地。

`venue_ts` 可为 `None`（旧数据全部如此），此时该订单簿**不可用于执行**（`defects()` 不会因此
报错，但 `legacy` 分类会标 `pending_verification`）。

### 4.4 概率分布是一个类型

`BracketDistribution` 在构造时强制：括号不重复、概率 ∈ [0,1]、**总和 = 1（容差 1e-6）**。
模型给不出合法分布就必须在构造处失败，而不是流进信号引擎。

### 4.5 括号与取整：唯一的换算点

`Bracket.probability_interval_f()` 把场所报价的整数桶换算成**真实连续温度**的半开区间
`[lo, hi)`，是全库唯一实现。默认 `RoundingConvention.NEAREST_INTEGER_F`（`floor(t+0.5)`）。

这是刻意做成**显式字段**而非隐含假设的：审计发现的两个开源项目分别在两处把这个换算搞错
（漏半度 / 用 °C 容差套 °F 市场），各造成 ~2× 与 ~1.8× 的概率偏差。
**该默认值仍需逐市场对照 `rules_text` 核实** —— 见 [`roadmap.md`](roadmap.md) P1 任务 V-1。

---

## 5. 数据库设计

### 5.1 迁移机制

- 迁移是 `storage/migrations/NNNN_name.sql`，按版本号字典序应用。
- 每次应用在 `schema_migrations` 记录 **SHA-256 内容校验和**。
- 再次运行是 no-op；**已应用的迁移若内容被改动 → `MigrationError`**。
  可复现的数据库不能在历史被悄悄改写后还声称可复现。
- 每个迁移在事务内执行，失败即回滚，不留半成品 schema。
- **没有 down-migration**。可复现性来自从原始数据重跑流水线，而不是编辑历史。

### 5.2 当前 schema（P0）

| 迁移 | 内容 |
|---|---|
| `0001_initial_schema.sql` | events, markets, order_books, forecast_runs, forecast_hourly, forecast_daily, observations, daily_max_temperatures, settlements |
| `0002_price_history.sql` | price_history（`mid_usd_per_share`，列名明示是**中间价**） |
| `0003_legacy_import.sql` | 各表的 `verification_status` / `verification_reason` / `legacy_source_table` / `legacy_source_db`，以及 `legacy_import_runs` 清单表 |

### 5.3 schema 层面强制的约束

| 约束 | 位置 | 为什么放这里而不只放代码里 |
|---|---|---|
| `publishable_at_utc >= run_time_utc` | `forecast_runs` CHECK | 时间泄漏是本项目最高危错误，不能只靠 Python 校验 |
| `valid_time_utc >= run_time_utc` | `forecast_hourly` CHECK | 同上 |
| `yes_price IN (0,1)` | `settlements` CHECK | 半结算状态不得入库 |
| `mid_usd_per_share BETWEEN 0 AND 1` | `price_history` CHECK | 概率型价格越界即数据损坏 |
| `bracket_low <= bracket_high` | `markets` CHECK | 桶反了会让概率整体错位 |
| `n_hourly_points > 0` / `n_readings >= 0` | CHECK | 空聚合不得冒充预测/观测 |
| FK: `forecast_daily → forecast_runs` | | 无法给不存在的 run 挂预报 |
| FK: `settlements → markets`、`forecast_hourly → forecast_runs` | | 同上 |
| 全部时间列 `TIMESTAMPTZ` + 会话 `TimeZone='UTC'` | `db.connect` | 同一时刻在任何主机上序列化为相同字节 |

**DuckDB 限制**：`ALTER TABLE ... ADD COLUMN` 不支持 `NOT NULL` 或 `CHECK`
（报 "Adding columns with constraints not yet supported"）。因此 `0003` 只能给
`DEFAULT`，值域由 `legacy.rules.VerificationStatus` 枚举在写入侧把关，
并有测试断言该枚举是状态字符串的唯一来源。

---

## 6. 定价与 Edge：唯一来源

`weatherquant/execution/pricing.py` 是全库唯一计算"一笔交易要花多少钱"的地方。

```
build_fill_plan(book, side, shares)      → FillPlan    走真实深度，绝不假设全额按最优价成交
taker_fee_usd(shares, price, fee_rate)   → float       官方公式，rate=None 时抛异常
execution_buffer_usd(fill_cost, frac)    → float       预算缓冲（非场所收费）
ExecutionCost(fill, fees, buffer)        → .total_cost_usd = 三段精确相加
expected_net_profit_usd(q, shares, cost) → q × shares − total
edge_usd_per_share(...)                  = 上式 / shares = q − cost/share
```

### 6.1 三条不变量（均有测试）

1. **`total = fill + fees + buffer`，恰好三项。** fee 变动 Δ ⇒ total 恰好变动 Δ。
   这就是"费用不能重复计算"的形式化。
2. **成交价只能来自 `FillPlan`。** `ExecutionCost` 不接受任何独立的 `price` 参数，
   所以没有"用另一个价格算成本"的通道。
3. **`per_share = q − cost/share`** 是代数恒等式，由测试断言而非假定。

### 6.2 走深度，不走中间价

`build_fill_plan` 逐档累计，返回 `filled_shares` / `fill_cost_usd` / `levels_consumed` /
`worst_price_usd_per_share` / `unfilled_shares`。深度不足时返回**部分成交计划**而非抛错——
调用方需要这个数字来判断市场有多薄，丢掉它等于掩盖流动性问题。

只有会**污染本次成交**的缺陷才阻断：买单看卖侧单调性与缺失、卖单看买单侧；**交叉盘两侧都阻断**
（此时任一侧价格都无意义）。另一侧的缺陷记录在 `OrderBook.defects()` 上供存档审计，
但不阻断本次定价。

### 6.3 手续费口径（已定，待实测确认）

按整笔成交的**成交量加权均价**计一次费。因为 `p(1−p)` 是凹函数，由 Jensen 不等式，此口径
**不小于**逐档累加，是两种读法中更保守的一种，且保证全库只有一个费用函数。
`test_fee_is_charged_on_the_plan_vwap_not_per_level` 把这个选择及其保守性一起钉死。

**待 P1 实测**：Polymarket 对跨多档的 taker 单究竟是按 VWAP 还是逐笔 match 计费——
文档未明确说明。见 [`roadmap.md`](roadmap.md) 任务 V-2。

---

## 7. 可复现性

| 保证 | 机制 |
|---|---|
| 数据库状态可判定 | `schema_migrations` 校验和；改动已应用迁移即报错 |
| 时刻可比较 | 全部 `TIMESTAMPTZ`，会话时区钉死 UTC |
| 民用日期可比较 | `America/New_York` 用 `zoneinfo`，23/25 小时日正确处理（有测试） |
| 评分可比较 | `brier_multiclass` / `log_loss` 的口径在模块 docstring 中**固定**（不除以类数、clip 到 1e-15） |
| 不确定度可比较 | `date_level_summary` 按日期聚类，禁止把同日多 run 当独立样本 |
| 溯源可追 | 每行带 `source`、`fetched_at_utc`、`legacy_source_*`、`verification_status` |
| 原始载荷 | `data/` 归档上游原始 payload（P1 落地），可无网络重放 |

---

## 8. 测试策略

- **完全离线**：不打开 socket。旧库测试用**合成** legacy 文件，避免依赖 100 MB 真实库的文件锁。
- **独立期望值**：不调用被测代码生成期望值。桶概率用 stdlib `statistics.NormalDist` 复算；
  手续费用官方文档算例数字；Brier/LogLoss 手算；日期级 SE 手算。
- **不变量优先**：断言关系（`total` 恰好三项、`per_share = q − cost/share`、
  `SE_clustered > SE_naive`），而非断言具体数值——关系错了任何数值都不可信。
- **回归锁**：`0.382924` / `0.197412` 这对数字把审计发现的 bug 永久锁在测试里。

当前状态：**145 个测试全部通过，语句覆盖率 94%**，`ruff check .` 无告警。

---

## 9. 本阶段明确**不**包含

| 内容 | 状态 | 阶段 |
|---|---|---|
| ECMWF / GFS / 融合 / 偏差校准 / 市场概率基线 | 未实现 | P1 |
| 上游采集器与原始载荷归档 | 未实现 | P1（**首项**，见 roadmap） |
| 回测引擎 | 未实现 | P1 |
| 旧数据的行级转换与写入 | **未实现**（只读访问 + 分类 + 计划已实现） | P1 |
| Paper trading 引擎 | 未实现 | P2 |
| 风险限制 | 未实现 | P2 |
| 多城市扩展 | 未实现 | P3 |
| Dashboard | **明确禁止** | — |
| 真实下单 / 交易机器人 | **明确禁止** | — |

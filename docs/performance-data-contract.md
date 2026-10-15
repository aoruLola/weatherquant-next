# Performance & MCP 数据契约（v0.1.4）

**本文件是 Edge、Settlement、Performance API、Performance UI、MCP 的唯一口径来源。**
生产者与消费者各自实现本文档，互不读对方代码；改动先改本文档。

版本：`performance_schema_version = 1`（字段只增不改语义；破坏性变更必须升版本）。

---

## 0. 通用规则

1. **原子发布**：临时文件 + `flush`/`fsync` + `os.rename`（复用
   `weatherquant.shadow.publish.atomic_write_json`）。读者永远看不到半个 JSON。
2. **信封字段**（每个快照与每个 API/MCP 响应的顶层）：

   ```json
   {
     "performance_schema_version": 1,
     "generated_at_utc": "…", "source": "weatherquant-research",
     "as_of_utc": "…", "stale_after_seconds": 300,
     "freshness": {"state": "fresh|stale|missing|corrupt|unavailable",
                    "age_seconds": 12.0, "reason": null},
     "dataset": "live|fixture",
     "statistical_scope": "…见 §9", "sample_size": 0,
     "mode": "LIVE|REPLAY|null", "warnings": ["…"],
     "data": { }
   }
   ```

3. **一切统计都带 `statistical_scope`**，见 §9。
4. **快照只放窗口，不放全表**：默认 ≤500 行 / ≤90 天；禁止把 GB 级数据搬进快照。
   v0.1.5 起由 §14 落实：明细是窗口（默认 20000 条 / 90 天）且按分片发布，汇总覆盖全历史。
5. **禁止** Dashboard / MCP 直接打开任何 `.duckdb`。
6. **禁止** 任何模块自行重算另一模块已定义的指标——单一实现。

---

## 1. 角色与数据流

```
Research Analytics (离线) → Performance Snapshot (原子 JSON) → Dashboard REST API
                                                              → Dashboard UI
                                                              → MCP Gateway (只读代理)
```

- **Research Analytics**：唯一可以读研究库 / 导出的决策历史的写方。离线计算、增量更新。
- **Dashboard API**：只读快照 + 计算量小的**纯函数**（如参考本金模拟）。
- **MCP**：**只转发 API 结果，绝不自己算**。

---

## 2. Opportunity Episode（独立机会）

### 2.1 身份键

```
episode_key = (market_id, target_date, model_name, direction, mode)
```

五元组缺一不可：不同模型、不同方向、LIVE/REPLAY **必须隔离**。

### 2.2 状态机

| 事件 | 动作 |
|---|---|
| 首次 ACCEPT（无进行中 episode） | 开新 episode |
| 连续 ACCEPT | 归入当前 episode，**不新建**；更新统计 |
| 变为非 ACCEPT（REJECT / NO_TRADE / DATA_UNAVAILABLE 等） | 结束 episode，`end_reason = "state_change"` |
| 间隔 > `gap_window_seconds`（可配置，默认 900） | 结束，`end_reason = "gap"`；**必须区分**见 §2.3 |
| gap 后再次 ACCEPT | 开新 episode |
| 进程重启 | episode 跨重启延续（依据 `decided_at_utc` 而非进程内计数） |

**`end_reason` 枚举**：`state_change` | `gap_data_missing` | `gap_signal_break` | `end_of_data`。

### 2.3 gap 必须区分"数据缺失"与"信号中断"

| 判据 | `end_reason` |
|---|---|
| gap 期间 `shadow_decisions` **完全没有该 episode_key 的行**（Collector 静默 / DATA_UNAVAILABLE） | `gap_data_missing` |
| gap 期间**有其他 episode_key 的行**（说明引擎在跑，只是本市场不再 ACCEPT） | `gap_signal_break` |

**`gap_data_missing` 不得计为"机会结束"，也不得进入胜率分母的时间权重计算。**

### 2.4 Episode 字段

```
episode_id            全局唯一（episode_key + first_seen 的哈希，稳定可复现）
episode_key           见 §2.1
first_seen_utc        首次 ACCEPT 时刻
last_seen_utc         最后一次 ACCEPT 时刻
duration_seconds      last_seen - first_seen
observation_count     ACCEPT 次数
first_edge            首次有效 ACCEPT 的 net_edge_usd（**代表值**）
last_edge / mean_edge / max_edge / min_edge
first_market_price    首次 ACCEPT 时的评估价格（盘口快照）
first_model_probability
direction             buy_yes | sell_yes
mode                  LIVE | REPLAY
target_date, station, bracket_label, available_depth_usd
end_reason
settlement_status     见 §4
outcome               WIN | LOSS | UNVERIFIABLE | null(未结算)
```

**无有效 `net_edge_usd`（null / 非有限值）的 ACCEPT 不得开始 episode**——
记入 `warnings`，不伪造代表值。

### 2.5 汇总层级（必须分别展示，不可互相替代）

| 指标 | 定义 |
|---|---|
| `accept_observations` | 全部 ACCEPT 行数（重复评估） |
| `opportunity_episodes` | episode 数 |
| `distinct_market_direction_pairs` | distinct (market_id, direction) |
| `accept_opportunity_distinct_markets` | distinct market_id，**口径 = 机会（episode）** |
| `accept_opportunity_distinct_target_dates` | distinct target_date，**口径 = 机会（episode）** |

> **口径必须写进字段名。** 这两个字段曾叫 `distinct_markets` / `distinct_target_dates`，
> 与 `manifest.coverage` 中的同名字段口径不同——同一世代里一个报 55、一个报 28，
> 读者无法判断哪个在描述哪个总体。`coverage` 现在叫
> `all_decision_distinct_markets` / `all_decision_distinct_target_dates`（口径 = 读入的全部决策）。
>
> 旧名 `distinct_markets` / `distinct_target_dates` 在 `summary` 中作为**已废弃别名**保留，
> 取值与 `accept_opportunity_*` 相同，供已部署的 Dashboard 过渡使用。
>
> 注意还有第三处：`win_rate.overall.distinct_markets`，口径是**已结算（RESOLVED + VERIFIED）**
> 的 episode，与上面两者都不同。改名时未动它，但它的含义同样受此规则约束。

```
Observation Edge Sum   = Σ 所有 ACCEPT 行的 net_edge_usd   ← 重复评估
Unique Opportunity Edge Sum = Σ episode.first_edge          ← 去重代表值
```

**两者都是模型理论期望的汇总，不是利润、不是资产增长、不是已实现收益。**
该声明必须原样出现在 API、UI 和 MCP 的 `warnings` 中。

---

## 3. Edge 统计口径

### 3.1 Observation（全部 ACCEPT）

`sum / mean / median / min / max / std / p05 p25 p50 p75 p95`，
按 `model_name`、`direction`、`target_date` 分组，另附每日趋势。

只统计 `state == "ACCEPT"` 且 `net_edge_usd` 非 null 的行。

### 3.2 参考本金模拟（Reference Capital）——**纯函数，单一实现**

实现位置：`weatherquant.performance.capital`（新模块）。
**Dashboard API 与任何内部调用方都必须调用此模块**；MCP 只调 API。

```
simulate(capital_usdc, mode, *, episodes, start_date=None, end_date=None,
         model=None, direction=None, max_allocation_fraction=0.10)
```

#### 模式 A：`single_opportunity`

对**每一个** episode 计算：

```
capital          = capital_usdc (> 0，否则 ValueError)
fill             = build_fill_plan(...)          ← 必须复用 execution/pricing.py
shares           = fill.shares（受盘口深度与最小下单量约束）
capital_required = fill.cost（含 fee 与 buffer，来自 execution_cost）
usable           = min(capital, capital_required)  或按比例缩放
edge_total       = edge_usd_per_share × shares    ← 来自 pricing，禁止手算
roi              = edge_total / capital_required
win_net          = 结算获胜的假设净收益（pricing 口径）
loss_net         = 结算失败的假设净亏损
capacity_limited = capital_required < capital
executable       = bool 且有明确原因
not_executable_reason ∈ {…见下}
```

`not_executable_reason` 枚举（任一命中即 `executable=false`，UI/MCP 必须标 `NOT_EXECUTABLE`）：

| reason | 含义 |
|---|---|
| `NO_BOOK` | 无盘口 / 盘口不可用 |
| `INSUFFICIENT_DEPTH` | 所需资金超过当时可成交深度 |
| `BELOW_MIN_ORDER` | 低于市场最小下单量 |
| `STALE_SNAPSHOT` | 盘口快照过期（超过 stale 阈值） |
| `DEPTH_NOT_MEASURED` | 缺少深度证据 |
| `TIME_MISMATCH` | 价格与概率非同一时刻 |
| `NO_SETTLEMENT` | 无结算（模拟收益不可验证） |
| `CROSSED_OR_ONE_SIDED` | 盘口交叉或单边 |

**`executable=false` 时只能显示理论值，禁止显示为可实现收益。**

#### 模式 B：`portfolio`

以 `capital_usdc` 作为**整个组合**的初始资金（不是每个机会各有一份）。

```
可用余额          balance = capital_usdc
单机会上限        cap = capital_usdc × max_allocation_fraction（默认 0.10）
排序              按 first_seen_utc 升序（不按 Edge 排序——那会引入前视）
分配              alloc = min(cap, balance, episode 资金需求)
同时占用          peak_open_allocation（任意时刻未结算占用的最大值）
资金释放          仅在 settlement_status == RESOLVED（或 DISPUTED 终局）时释放
未结算不得释放    PENDING / UNAVAILABLE / UNVERIFIABLE → 占用直到 end_date
费用              累加 execution_cost 的 fee 与 buffer
关联风险          同一 market_id 的多个 episode 共享 cap 配额（按 market 聚合限制）
```

**输出必须含**：`capital`、`allocated_total`、`peak_open_allocation`、
`utilization`、`balance_remaining`、`capacity_limited_count`、
`executable_episodes`、`theoretical_expected_roi`、
`backtestable(bool)`、`not_backtestable_reason`。

**`backtestable = false` 的情形**（必须明说，不得构造收益曲线）：
缺历史盘口深度、缺结算、样本 < 最小阈值、资金时间线无法重建。

#### 分级展示（UI 必须严格区分五档，禁止混用）

1. 每份合约 Edge（per-share）
2. 每机会理论预期收益
3. 组合理论预期收益
4. **经历史数据验证的模拟收益**（仅当 `backtestable`）
5. 实际收益 → 恒为 `N/A`（**无真实交易**）

---

## 4. Settlement

### 4.1 来源与状态

**本节全部字段已于 2026-10-13 对生产 API 实测核验**（任务 4.1 要求）。
实测与既有文档有三处出入，实现必须以本节为准。

| 来源 | 用途 | 实测契约 |
|---|---|---|
| **Data-API** `GET https://data-api.polymarket.com/v2/resolutions?condition=<conditionId>` | **平台正式结算（唯一采信）** | 见下方实测 |
| Gamma `GET /events?series_slug=nyc-daily-weather` | 发现市场、取 `conditionId` | `umaResolutionStatus`、`outcomePrices`、`resolutionSource` |
| Gamma `umaResolutionStatus` / `outcomePrices` | 交叉参考 | **不得单独作为结算依据** |
| KLGA 官方观测（NOAA / GHCN TMAX / IEM） | **独立观测** | 只入 `klga_observations` / `klga_daily_max` |
| WeatherQuant 研究库 | 历史评分 | 复用现有表 |

**关键：`/v2/resolutions` 不在 `clob` 也不在 `gamma`**，而在 **`data-api.polymarket.com`**。
在前两者上请求一律 404（实测）。

#### 实测响应（2026-10-13，真实 conditionId）

已结算：

```json
{"data": [{
  "question_id": "0x0ee0…", "condition_id": "0x1d74…",
  "status": "resolved", "extended_review": false, "was_disputed": false,
  "proposed_price": "0", "reproposed_price": "69", "price": "0",
  "transaction_hash": "0x7ac0…", "log_index": "373",
  "last_update_timestamp": "1791868379"
}]}
```

未结算（同一结构，不是空）：

```json
{"data": [{ "status": "posed", "proposed_price": "69",
            "reproposed_price": "69", "price": "69", … }]}
```

不存在的 condition：`{"data": []}`。

#### 实现必须遵守的四条（均为实测得出）

1. **响应包在 `data` 数组里**，不是裸对象。直接 `json["status"]` 会 `KeyError`。
2. **`price` 是字符串**，不是数字。`"69"` 是哨兵值 → 必须归一为 `None`，
   否则会把 69 当成每份赔付。判定只认 `price in {"0", "1"}`。
3. **`status` 不止 resolved**：实测见到 `"posed"`（已提议未定案）。
   只有 `status == "resolved"` **且** `price in {"0","1"}` 才是已结算。
4. **必须用 `httpx`，不能用 `urllib`**：Python 默认 UA 触发 Cloudflare
   `error code: 1010` → HTTP 403（实测）；`httpx` 默认 UA → 200。
   仓库的 `weatherquant.data.http.ResilientHttp` 已是 httpx，直接复用。

**状态映射**：

| 实测 | 本契约 `settlement_status` |
|---|---|
| `status=="resolved"` 且 `price in {"0","1"}` | `RESOLVED` |
| `was_disputed == true` | `DISPUTED` |
| `status=="posed"` 或 `price=="69"` | `PENDING` |
| `{"data": []}`（condition 不存在） | `UNAVAILABLE` |
| 无法取得响应 / 响应畸形 | `UNVERIFIABLE` |

`transaction_hash` + `log_index` + `last_update_timestamp` 一并落库作
`raw_source_reference`，使每个结算结论可回溯到链上凭据。

### 4.2 结构（新增字段在既有研究库之上扩展）

平台结算与天气观测**分别存储**，只通过 `event_id` 关联：

```
market_id, event_id, token_id, target_date, station
settlement_status      PENDING | RESOLVED | DISPUTED | UNAVAILABLE | UNVERIFIABLE
resolved_outcome       1 | 0 | null
resolution_source      "clob:/v2/resolutions" | "gamma:umaResolutionStatus" | …
resolved_at_utc, fetched_at_utc, raw_source_reference
observation_max_f, observation_source, observation_rule
observation_retrieved_at_utc, verified_at_utc
verification_status    VERIFIED | MISMATCH | UNVERIFIED
discrepancy_reason
```

**禁止**：用气象实测代替平台结算；用平台结算反推气象读数。
矛盾必须写入 `discrepancy_reason`，不得静默取其一。

### 4.3 WIN / LOSS

仅 `settlement_status == RESOLVED` **且** `verification_status == VERIFIED`
的 episode 进入胜率分母。

| 方向 | 规则 |
|---|---|
| `buy_yes` | YES 支付 $1 → `WIN`；YES 支付 $0 → `LOSS` |
| `sell_yes` | **语义未证明前**只给"方向判断正确率"，`outcome = UNVERIFIABLE`，**不得混入可执行策略胜率** |
| 其他 | `UNVERIFIABLE` |

`sell_yes` 必须先证明：是卖出既有 YES 持仓，还是被定义为某种做空。
未证明收益结构与执行可行性之前，禁止并入盈利胜率。

---

## 5. 胜率统计

```
Win Rate = WIN / (WIN + LOSS)
```

- 未结算、争议未决、不可验证 → **不进分母**。
- 同一市场的多个 episode 可在 episode 层展示，但**市场级胜率与推断中不得视为独立结算事件**。
- **禁止**把 ACCEPT 比例当胜率。

**必须提供**：`valid_samples`、`distinct_markets`、`distinct_target_dates`、
`confidence_interval`、`gaps_and_exclusions`（含原因）。

**置信区间**：必须处理 target_date 聚类与重复市场相关性。
- 首选：按 `target_date` 分层 bootstrap（默认 2000 次，固定随机种子保证可复现）。
- 样本不足时降级为 Wilson 区间并**在响应中标注方法**。
- `valid_samples < min_samples`（默认 20）或 `distinct_target_dates < 2` →
  `win_rate = null`，`reason = "N/A — insufficient resolved independent samples"`。

**分组**：BUY YES / SELL YES 分列、LIVE / REPLAY 分列、按模型、按 target_date、
按 Edge 区间（默认 `<0.02 | 0.02–0.05 | 0.05–0.10 | ≥0.10`）。

计数：`WIN / LOSS / PENDING / DISPUTED / UNVERIFIABLE / UNAVAILABLE`。

---

## 6. 模型准确度

复用 `weatherquant.models.metrics` 与 `weatherquant.models.evaluation`，
**不得重写**：

| 指标 | 现有实现 |
|---|---|
| Brier | `brier_multiclass` |
| Log Loss | `log_loss` |
| Calibration / ECE | `calibration_bins` + `expected_calibration_error` |
| MAE / RMSE | `mae` / `rmse` |
| 时序切分 | `chronological_split` |
| 评分 | `score` / `evaluate_pairwise` |
| Top-1 Accuracy | 在 `score` 基础上新增，不得另起一套 |
| Model vs Market Baseline | `models.market_baseline` |

**严格样本外、按时间顺序**：`forecast_run_time_utc` ≤ `training_cutoff_utc` ≤
市场报价时刻 ≤ `resolved_at_utc`。四者都要在响应中可追踪。
**禁止未来数据泄漏。**

---

## 7. Performance API

全部沿用现有 Dashboard 信封与分页/筛选/422 规则。

| 端点 | 主要来源 |
|---|---|
| `GET /api/v1/performance/summary` | `performance/summary.json` |
| `GET /api/v1/performance/edge` | `performance/edge.json` |
| `GET /api/v1/performance/win-rate` | `performance/win-rate.json` |
| `GET /api/v1/performance/opportunities` | `performance/opportunities.json` 描述符 + `opportunities/part-*.json`（服务端分页 + 筛选，§14.4） |
| `GET /api/v1/performance/settlements` | `performance/settlements.json`（分页） |
| `GET /api/v1/performance/models` | `performance/models.json` |
| `GET /api/v1/performance/reference-capital` | **纯函数** `capital.simulate(...)`，参数：`capital_usdc`(默认100,>0)、`mode`、`max_allocation_fraction`。返回汇总 + 有界样本，总体为明细窗口（§14.6） |

**快照缺失时**：`freshness.state = "missing"` + 原因，**禁止返回伪造数据**。

### 7.1 世代目录与一致性

六份快照**作为一个世代发布**，不是六次独立 rename：

```
performance/
    current.json                      # {"generation_id": "<id>"}  原子指针
    generations/<id>/
        summary.json edge.json win-rate.json
        opportunities.json settlements.json models.json
        manifest.json                 # 数据集、水位、覆盖范围、文件清单
```

**为什么不能直接 rename 六次**：那不是跨文件的原子提交。读者若在中途到达，
会拿到「旧的 opportunities + 新的 win-rate」——两份从未同时存在的数据
拼在一起，而这种不一致不会报错，只会被拿去评分。

流程：全部写入 `generations/<id>/` → 全部 fsync → 写 manifest →
**最后**一次 rename 切换 `current.json`。指针切换前，旧世代仍然 current；
切换后，新世代 current。中途失败则新目录无人跟随，成为惰性残留。

**读取时校验**：Dashboard 读到的文件必须声明自己属于指针所指的世代；
不符则拒收（`refusing to mix generations`），而不是混用两批数据。
`manifest.json` 缺失同样拒收——那是**发布不完整**，不是"回填没跑"，
把前者说成后者会把人送去重跑一次已经成功的作业。

**兼容性**：无 `current.json` 时回退到扁平布局，因此既有部署不受影响。
MCP 感知不到目录结构——它只调 Dashboard API。

---

## 8. MCP 响应信封

13 个工具全部返回 §0 信封，外加：

```
tool_name, arguments_echo, duration_ms,
source_endpoint   ← 证明数据来自哪个 API（禁止 MCP 自算）
```

**必须保留**：`schema_version`、`source`、`generated_at_utc`、`as_of_utc`、
`freshness`、`dataset`、`mode`、`sample_size`、`statistical_scope`、`warnings`。

**禁止**（违反即为缺陷）：
- 过期盘口包装成实时机会（`freshness.state != fresh` 时必须显式 STALE）；
- REPLAY 包装成 LIVE；
- 把理论净 Edge 说成已实现利润；
- 无结算数据时回答具体实际胜率；
- 用模型自身推测填补缺失的报价 / 结算 / 观测。

**工具清单（13）**：
`get_overview` `get_system_health` `list_markets` `get_market_detail`
`get_forecasts` `list_decisions` `get_shadow_report`
`get_edge_summary` `get_opportunities` `get_win_rate` `get_settlements`
`compare_models` `simulate_reference_capital`

前 7 个 → 既有 Dashboard API；后 6 个 → Performance API。

---

## 9. `statistical_scope` 取值（必须显式）

| 值 | 含义 |
|---|---|
| `observation_all_accepts` | 全部 ACCEPT 评估（含重复） |
| `opportunity_episodes` | 去重后的独立机会 |
| `resolved_verified_only` | 仅 RESOLVED + VERIFIED（胜率分母） |
| `out_of_sample_chronological` | 按时间顺序样本外 |
| `reference_capital_hypothetical` | 参考本金假设，非实盘 |
| `live_only` / `replay_only` / `live_and_replay` | 模式范围 |
| `database_lifetime` / `window:<n>d` | 时间范围 |

**每个响应至少一个**，可组合（数组）。

---

## 10. 字段可得性总表

| 数据 | 状态 | 说明 |
|---|---|---|
| Shadow 决策全量字段 | ✅ 可用 | `shadow_decisions`（需 §11 导出机制） |
| `accept_scope` / `by_model` | ✅ 报告有、**UI 缺透传** | 见 Part F 8.1 |
| Edge 汇总 | ⏳ 本轮实现 | Research Analytics |
| Episode | ⏳ 本轮实现 | 状态机 |
| 平台结算 | ⏳ 本轮实现 | 本机不可达上游，见 §4.1 |
| KLGA 观测 / 研究库 | ✅ 已存在 | `migrations_research` |
| 指标（Brier/ECE/MAE/RMSE） | ✅ 已存在 | `models/metrics.py` |
| 参考本金 | ⏳ 本轮实现 | 复用 `execution/pricing.py` |
| Performance API / UI / MCP | ⏳ 本轮实现 | |

---

## 11. 生产数据导出（单写者）——已实现

研究进程需要 Shadow 全量历史决策。**禁止**绕过 DuckDB 锁读运行中的生产库。

导出由 `weatherquant.shadow.export.DecisionExporter` 在**持有租约的引擎进程内**
执行（`runner._export_decisions`，独立节流，默认 300s）。

### 11.1 布局

```
shadow/reports/decisions-export/
    state.json                  # 水位缓存：{watermark, rows_exported, next_part}
    parts/part-000000001.jsonl  # 追加式分片，发布后不可变
    parts/part-000000002.jsonl
```

### 11.2 游标：`(decision_time_utc, decision_id)`

**不只用时间戳。** 同一周期内的多条决策共享同一时刻；仅以时刻为游标会
重复或丢失其中之一。`decision_id` 是 `sha256(run|cycle|market|direction)[:32]`，
**唯一但无序**——因此它只作并列打破者，不作排序键。

### 11.3 水位：以分片为准，state 只是缓存

写入顺序是 **先分片、后 state**。进程若在两者之间崩溃：

- 分片已落盘而 state 未推进 → 下次运行**从分片最后一行重新推导水位**，
  不会重复导出；
- state 若先推进而分片缺失 → 数据丢失且无法察觉。

因此 state 仅用于快速启动，**权威来源是分片本身**。已由测试
`test_losing_the_state_file_does_not_duplicate_a_range` 验证。

### 11.4 有界与非阻断

每 tick 受三重约束：`batch_rows`（默认 20,000）、`max_seconds`（默认 5s）、
`max_parts_per_run`（默认 5）。初次回填因此由多个 tick 完成，而非阻塞一次决策。

导出失败**不抛出**：返回带 `notes` 的结果，Runner 记录
`decision_export_failed` 质量事件。**导出是旁路，决策是任务。**

### 11.5 字段

导出包含 45 列，覆盖 Episode 身份键、Edge、模型概率、盘口深度、
`mode`（LIVE/REPLAY 隔离所必需）、`thresholds_version`，以及从
`market_catalog` 联接的 `fee_rate` / `fee_exponent`——定价模块拒绝假设费率为零，
缺了它每一笔参考本金测算都会是 `FEE_UNKNOWN`。

### 11.6 回退方案

若导出不足，唯一合法替代是**维护窗口内的一致性备份**
（先 `docker stop weatherquant-shadow`，再复制 `.duckdb` + `.wal`），
须经用户批准后执行。

---

## 12. 安全底线（MCP）

1 独立容器 · 2 独立最小权限身份 · 3 不挂载生产 DuckDB · 4 不挂 Docker Socket ·
5 不挂 Portainer 凭证 · 6 无下单工具 · 7 不接受任意 SQL · 8 不支持任意文件读取 ·
9 不做任意 URL 代理 · 10 限制参数/输出/速率 · 11 请求超时与并发上限 ·
12 认证与密钥轮换 · 13 校验 HTTP Origin · 14 仅允许配置的 Dashboard API 地址 ·
15 审计日志不含凭据。

默认仅监听内网/本机；远程接入必须走认证 HTTPS（反向代理或受控隧道）。
**无认证的 MCP / Dashboard / Portainer / 生产库禁止暴露公网。**

---

## 13. 流式增量引擎（v0.1.5 新增）

审计与状态模型全文见 [`performance-streaming-audit.md`](performance-streaming-audit.md)。
本节只规定**对外可见的契约**：状态存哪、水位怎么算、哪些字段新增、哪些行为改变。

### 13.1 状态存储

```
data/research/performance-state.duckdb        # 可用 WQ_PERFORMANCE_STATE_DB_PATH 覆盖
```

- **独立文件**，与生产 Shadow 库、与 `weather-research.duckdb` 都不是同一个。
- **全部内容都是派生物**：删除它不会丢任何原始数据，下一次运行会从导出重建。
- 单写者：第二个进程打开会被 DuckDB 拒绝（文件锁），不是静默并发写。
- 内存上限由 `memory_limit`（默认 `1GB`）+ `temp_directory` 显式限定，超出即落盘。

### 13.2 消费水位

水位 = **已消费分片的游标 + 分片指纹**，不是时间戳：

| 字段 | 含义 |
|---|---|
| `last_part_index` | 已消费的最后一个分片序号；下一个待读 = +1 |
| `last_end_cursor` | 该分片末行的 `(decision_time_utc, decision_id)` |
| `performance_parts[index]` | `(rows, end_cursor, sha256, consumed_at)` |

规则（均为硬约束，违反即拒绝运行）：

1. 分片序号必须 `1..N` **连续**；缺一个就**不读后面的**（游标是高水位，越过缺口即谎报覆盖）。
2. 分片内容必须按 `(decision_time_utc, decision_id)` **非降**；违反即拒绝（这也是 REPLAY 迟到写入的可观测化）。
3. 已消费分片的 `sha256`/行数变化 → 拒绝，要求重建（发布后的分片不可变）。
4. **行、Episode 状态、计数器、分片记录、游标在同一个事务内提交**；崩溃回滚全部，重跑不会重复计数。

### 13.3 增量与局部重算

- 输入层（Episode / Edge 观测 / 计数器）：**增量**，只处理水位之后的分片。
- 派生层（outcome / 胜率 / 置信区间 / 参考本金）：**每次发布重算**，不落库。
  因此**结算修订不需要重读任何决策**——它只改派生层。
- `--no-ingest`：不读导出，仅从状态库重算并发布。
- `--reset`：清空状态库并完整回填。

### 13.4 行为改变（**唯一一处**）

**结算索引现在是粘性的。** 不带 `--settlements` 运行，会沿用状态库里已有的结算答案，
而不是像 v0.1.4 那样把本次运行的结算集当作全部（即"不给就全丢"）。

理由：结算是外部事实且会被修订；一次不带索引的运行不应抹掉上一次已核验的结论。
带索引运行时按 `(market_id, target_date)` **upsert**，新值覆盖旧值。

### 13.5 manifest 新增字段（纯新增，旧读者忽略）

```json
{
  "generated_at_utc": "…",
  "algorithm_version": "performance-streaming-1",
  "input_source": "/path/to/decisions-export",
  "input_range": {"first_decision_utc": "…", "last_decision_utc": "…"},
  "streaming": {
    "parts_consumed": 79, "rows_consumed": 1481414, "row_seq": 1481414,
    "episodes": 0, "open_episodes": 16,
    "gap_window_seconds": 900.0, "accepted_state": "ACCEPT",
    "state_db": "…/performance-state.duckdb"
  }
}
```

`algorithm_version` 变化后**不得**继续增量（状态库里的聚合会是两个算法的混合物），
必须 `--reset` 重建。`gap_window_seconds` / `accepted_state` / `input_source` 同理。

### 13.6 统计口径不变（含"逐位相等"承诺）

流式实现**没有**改变任何公式。逐项对照：

| 量 | 实现 | 相等性 |
|---|---|---|
| `sum` | `math.fsum`（对游标流式） | 逐位（fsum 与顺序无关、正确舍入） |
| `mean` | `sum / count` | 逐位 |
| `std` | `statistics.pstdev`（对游标流式，单趟精确有理数） | 逐位 |
| `median` | 按秩取中间两个顺序统计量，`(a+b)/2` | 逐位 |
| `p05…p95` | 按秩取上下邻居，`a*(1-w)+b*w` | 逐位 |
| `min`/`max`/`count` | SQL | 精确 |
| `episode.mean_edge` | 增量左折叠 `sum_edges` | 逐位（累加顺序相同） |
| `unique_opportunity_edge_sum` | 内建 `sum` 左折叠，发布顺序复刻原稳定排序 | 逐位 |
| 胜率 / 聚类 bootstrap | 同一段 bootstrap 代码，簇插入序复刻（先 WIN 后 LOSS） | 逐位 |
| 参考本金 | 同一段走账代码（`PortfolioWalk`） | 逐位 |

**明确不使用**：DuckDB `quantile_cont`（实测 1200 次抽样中 74 次差 1 ULP，
插值算式不同）、DuckDB `sum`（相对差 ~1.7e-14）。
分位数一律由**按秩取顺序统计量 + 现有插值函数**得到。

### 13.7 落盘体积

`by_edge_band`（全量数值数组）与 `opportunities.episodes`（全量 episode）
**形状不变**，因此落盘体积仍随历史线性增长；改变的是**研究容器的峰值内存**
（流式写出，不再全量驻留）。契约 §0.4 的"≤500 行"窗口仍未实现，
属既有偏差，见审计文档 §9 风险 3。

---

## 14. 有界明细与分页（v0.1.5 新增）

§13 解决了**研究进程**的内存，但没有解决**快照体积**：它仍随历史线性增长，
Dashboard 读一个快照就要把整段历史读进内存。实测（500 万行）：

| 文件 | 修复前 | 全部体积来自 |
|---|---|---|
| `summary.json` | 131.3 MB | `reference_capital.results[]` |
| `opportunities.json` | 162.9 MB | `episodes[]` |
| `edge.json` | 22.6 MB | `by_edge_band.*[]` |

`/api/v1/performance/summary` 因此返回 131 MB，`/opportunities` 解析 163 MB 只为返回 50 行。
本节是修复后的契约。

### 14.1 明细是一个**窗口**，并且明说

**§0.4 现在被真正实现**：可浏览的明细只覆盖最近一段历史。

```json
"window": {
  "episodes": 20000,          // 窗口内条数（= 可分页的 total）
  "episodes_all": 242570,     // 全部 episode 数
  "max_episodes": 20000,      // 发布时的上限
  "days": 90,                 // 发布时的时间窗
  "first_seen_utc_from": "…", "last_seen_utc_to": "…",
  "complete": false           // false = 明细是窗口，汇总才是全历史
}
```

- 窗口由发布参数决定：`--detail-window-episodes`（默认 20000）、`--detail-window-days`（默认 90）。
- 选择规则（两个引擎共用）：`first_seen_utc >= now - days`，按
  `(first_seen_utc, episode_id)` **降序**取前 N 条。
- **汇总类指标（总数、Edge 分布、胜率、置信区间、参考本金汇总）始终覆盖全部历史。**
- 窗口不完整时：`data.warnings` 有一条说明，`statistical_scope` 增加 `window:<n>d`。

### 14.2 明细按**分片**发布

```
generations/<id>/
    opportunities.json           # 描述符：窗口 + 分片清单 + 每片首末排序键
    opportunities/part-00000.json  # ≤1000 条，按 (first_seen_utc, episode_id) 降序
    opportunities/part-00001.json
```

`opportunities.json` 的 `data.episodes` **不再是数组**，而是描述符：

```json
{"collection":"episodes","partitioned":true,"directory":"opportunities",
 "partition_size":1000,"total":20000,
 "partitions":[{"file":"part-00000.json","items":1000,"bytes":712345,
                "first_key":["2026-10-15T…","<episode_id>"],
                "last_key":["2026-10-14T…","<episode_id>"]}]}
```

分片写在**世代目录内、指针切换之前**，所以批量发布仍是原子的。

### 14.3 其余体积修复

| 字段 | 变更 | 理由 |
|---|---|---|
| `edge.data.by_edge_band.<band>` | **数组 → 分布**（`count/sum/mean/median/min/max/std/p05…p95`，与其它分组同形） | 只被用来计数；原本是 `edge.json` 的全部体积 |
| `summary.data.reference_capital.results` | **移除**，改为 `results_sample`（≤50 条）+ `not_executable_reason_counts` + `results_total` + `results_sampled` | 计数才是读者要的答案；逐条结果随历史线性增长 |
| `reference-capital` 响应的 `per_opportunity_expected_edge` | 改名 `per_opportunity_expected_edge_sample`（≤50）+ `_sampled`/`_sample_size` | 每个可执行机会一条，同样随历史增长 |
| `summary.data.settlement_freshness` / `settlements.data.freshness` | **新增** | 粘性结算层必须能区分"沿用"与"刚验证"（§13.8） |

### 14.4 `GET /api/v1/performance/opportunities`

| 参数 | 取值 | 默认 |
|---|---|---|
| `limit` | 1..200 | 50 |
| `cursor` | 不透明游标（见下） | 首页 |
| `model` | 模型名 | 全部 |
| `direction` | `buy_yes` / `sell_yes` | 全部 |
| `mode` | `LIVE` / `REPLAY` | 全部 |
| `target_date` | `YYYY-MM-DD` | 全部 |
| `outcome` | `WIN`/`LOSS`/`PENDING`/`DISPUTED`/`UNVERIFIABLE`/`UNAVAILABLE` | 全部 |
| `min_edge` / `max_edge` | 浮点，作用在 `first_edge` 上 | 不限 |

**排序**：`(first_seen_utc, episode_id)` 降序——**全序**，因此分页无遗漏、无重复。

**游标**：`base64url("<generation_id>|<offset>")`。

- 游标**属于某一批次**：批次变化后使用旧游标返回 **409**，不得静默改写为新区间的偏移。
- 传纯整数（旧式 offset）仍然接受，视为当前批次，标注为兼容用法。
- 非法游标返回 **422**。
- `limit` 超过 200 一律 **422**，不能通过放大 `limit` 绕过。

**响应**（信封内，除 `items`/`total`/`next_cursor`/`prev_cursor` 外）：

```json
"data": {"collection":"episodes","returned":50,"total":20000,"offset":0,"limit":50,
         "cursor":null,"filters":{"model":"calibrated"},
         "window":{…},"total_all":242570,
         "statistical_scope":[…],"warnings":[…]}
```

`total` = **窗口内**符合筛选的条数（精确）；`total_all` = 全部 episode 数。
无筛选时按分片清单的累计条数直接定位页所在分片（**只打开需要的分片**）；
有筛选时扫描窗口内分片（**有界**，因为窗口有界）。

### 14.5 `GET /api/v1/performance/settlements`

分页保持 offset 游标（该文件天然有界：一个市场一个目标日一行，不是一条决策一行），
新增 `status` 与 `market_id` 筛选。

### 14.6 `GET /api/v1/performance/reference-capital`

- 仍复用 `weatherquant.performance.capital`（**同一实现**），仍支持 `capital_usdc` / `mode` /
  `max_allocation_fraction`。
- 返回**汇总统计 + 有界样本**，不再复制逐条结果。
- 计算总体是**已发布的明细窗口**（不是全历史），响应里带 `window`，窗口不完整时追加一条
  warning 指明"已发布的 100 USDC 汇总是全历史的"。

### 14.7 MCP

MCP 仍**只调 Dashboard API**，不新增任何统计逻辑：

- `get_opportunities` 增加同样的筛选参数，`limit` 上限 200；
- 只发送调用方实际给出的筛选（不把"未筛选"变成空串筛选）；
- 响应仍带 `generation_id` / `dataset` / `as_of_utc` / `freshness` / `statistical_scope` /
  `sample_size` / `warnings`；批次变化时游标返回 409，**不得拼接两批数据**。



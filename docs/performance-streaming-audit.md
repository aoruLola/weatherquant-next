# Performance 流式增量重构：架构审计与状态模型（Part A）

本文件是**实现之前的**审计结论与状态模型。所有“现状”条目都对应到具体代码行，
所有“必须跨批次保存”的条目都给出不变量。凡未实际验证的一律标注**未验证**。

审计对象：`origin/main` = `635acb5`，工作分支 `feature/performance-streaming`
（基于 `feat/streaming-performance`，含上一轮的字段重命名与事故记录）。

> 本文件只描述设计，不改口径。统计语义的唯一来源仍是
> [`performance-data-contract.md`](performance-data-contract.md)。任何必须改变的
> 算法都写在本文件 §8，不藏在“浮点差异”里。

---

## 1. 现状：Performance 计算链审计

### 1.1 调用链

```
cli: build-performance
  └─ backfill.load_decisions_with_watermark(export_dir)      ← 全量 list[dict]
  └─ backfill.build_performance_snapshots(decisions=…)
       └─ snapshot.build_performance(decisions, settlements)
            ├─ episodes.build_episodes(rows)      → list[Episode]   ← 全量排序 + 折叠
            ├─ edge.observation_edge(rows)        → 分布 + 全量 edges list
            ├─ winrate.assign_outcomes(episodes)  → list[ScoredEpisode]
            ├─ winrate.win_rate(scored)           → 聚类 bootstrap
            ├─ capital.simulate_portfolio(episodes)
            └─ 六份 payload（含 by_edge_band 的全量数值数组）
       └─ snapshot.publish_performance(payloads, out)  → 世代目录 + current.json
```

实测内存：**约 6.8 KB / 决策**（上一轮 §10.2），全部来自 `load_decisions` 的
`list[dict]` 与 `build_episodes` 的 `normalised.sort()`。

### 1.2 逐项审计

| # | 项 | 现状实现 | 位置 |
|---|---|---|---|
| 1 | 决策历史读取 | 目录：`read_export_with_watermark()` 一次性读入全部 JSONL；文件：`json.load` 全量 | `shadow/export.py:469`、`performance/backfill.py:69` |
| 2 | Episode 状态机 | 全局按 `decided_at_utc` 稳定排序 → `open_by_key` 折叠；五元组身份 | `performance/episodes.py:394` |
| 3 | Observation Edge Sum | `math.fsum` 全部 ACCEPT 且 edge 可用的行 | `edge.py:83` |
| 4 | Unique Opportunity Edge Sum | `sum(e.first_edge)`（内建 `sum`，按 episode 顺序左折叠） | `episodes.py:509` |
| 5 | 精确分位数 | `sorted()` + 线性插值 `lo*(1-w)+hi*w` | `edge.py:44` |
| 6 | 已验证方向胜率 | 分母 = RESOLVED + VERIFIED 且非 `sell_yes`；`WIN/LOSS` 六态计数 | `winrate.py:153` |
| 7 | 日期聚类置信区间 | `random.Random(20261013)`，按 `target_date` 重采样 2000 次，<2 簇退化 Wilson | `winrate.py:217` |
| 8 | Settlement 状态更新 | **每次构建**由外部结算索引重新推导 outcome；不落库 | `snapshot.py:98`、`winrate.py:153` |
| 9 | Reference Capital | `simulate_portfolio`：按 `first_seen_utc` 排序、单市场配额、只减不加 | `capital.py:556` |
| 10 | 六份快照发布 | 六文件写入 `generations/<id>/`，最后 rename `current.json` | `snapshot.py:347` |

### 1.3 审计中发现的、决定设计的事实

**(a) `activity_between` 只需要“上一个不同时刻”，不需要全局索引。**

现状（`episodes.py:433`）问的是：在 `(last_seen, row_time)` **开区间**内，
是否存在**任何**决策时刻（跨全部市场）。折叠按时刻非降序进行，因此在处理时刻
`t` 的行时，所有 `instant < t` 的行**都已处理**，而 `instant > t` 的行不可能落在
开区间内。于是：

```
activity_between(last_seen, t)  ⟺  prev_instant > last_seen
```

其中 `prev_instant` = 已处理行中**严格小于** `t` 的最大时刻。**O(1) 状态**，
精确等价（含重复时刻：同一时刻的行不改变 `prev_instant`）。
上一轮交接文档 §5.4 提出的“持久化查询 / 有界结构”不需要——两者都更贵且更易错。

**(b) 边界时刻的行必须计入“活动”，但不计入 episode。**

`decided_at` 由 `normalised` 构造，而 `normalised` **剔除 `market_id` 为空的行**
（`episodes.py:418`）。所以活动索引只由**有 market_id 的行**推进。
而 `observation_edge` / `_coverage_of` **不剔除**（`edge.py:98` 无过滤）。
两者必须分别计数，否则 `decisions_seen` 与 `coverage.decisions` 会对不上。

**(c) episode 内部 `observation_count == len(edges)`。**

`_OpenEpisode.observe()` 只在 `is_accept`（ACCEPT 且 edge 可用）时调用
（`episodes.py:454-470`），因此每个 ACCEPT 且 edge 可用的行**恰好属于一个
episode**。推论：`accept_observations = Σ observation_count`；
`Observation Edge Sum = Σ episode.sum_edges`。两处数字天然一致。

**(d) 一个 ACCEPT 但 edge 不可用的行会以 `state_change` 结束已开 episode。**

`episodes.py:471` 的 `elif current is not None` 分支。流式折叠必须保留这一行为。

**(e) `by_edge_band` 与 `opportunities.episodes` 是全量数组。**

`edge_by_bin()` 返回**原始数值列表**（`edge.py:221`），`opportunities` 返回
**全部 episode**（`snapshot.py:239`）。两者都随历史线性增长，因此**必须流式写出**，
否则“峰值内存不随历史线性增长”不可能成立。前端只使用 `values.length`
（`Performance.tsx:243`），但形状**不得改变**（契约 §7 兼容性）。

**(f) 三个不同的“distinct markets”母体**（契约 §2.5，上一轮已改名）：
`coverage.all_decision_distinct_markets`（全部决策）、
`summary.accept_opportunity_distinct_markets`（opportunities）、
`win_rate.overall.distinct_markets`（RESOLVED+VERIFIED）。
流式实现必须分别维护，**不得复用同一个集合**。

---

## 2. 四种顺序必须分开

| 顺序 | 定义 | 现状保证 |
|---|---|---|
| 业务时间 | `decision_time_utc` | 由引擎写入，**可回退**（REPLAY 写历史时刻） |
| 数据库写入顺序 | `created_at_utc` / 行插入顺序 | 单调，但不导出 |
| 导出分片顺序 | `part-00000000N.jsonl`，索引连续 | 分片内按 `(decision_time_utc, decision_id)` 升序 |
| 消费顺序 | 本引擎读取分片的顺序 | **必须等于分片顺序** |

**导出游标是 `(decision_time_utc, decision_id)` 且排他**（`export.py:328`）：
`decision_time_utc > ? OR (= ? AND decision_id > ?)`。因此：

1. 分片内与分片间 `(decision_time_utc, decision_id)` **严格非降**——这是可验证的
   结构性质，流式引擎**必须验证**并在违反时拒绝。
2. **REPLAY 晚写入但业务时间更早的行永远不会被导出**（游标已越过其时刻），
   所以它不会出现在导出里、也不会破坏顺序——但它也**永远不会被统计**。
   这是契约 §11 / 上一轮 §5.3 已记录的**未解决风险**，本轮不改变导出游标，
   只是把它变成**可观测**的：`performance_parts` 记录每个分片的
   `(index, rows, end_cursor, sha256)`，分片被重写/截断会被检出。

**结论：消费顺序 = 分片顺序 = 游标顺序；统计口径中的“时间”一律用
`decided_at_utc`（业务时间），水位一律用游标（写入顺序）。两者不混用。**

---

## 3. 状态模型：必须跨批次保存什么

### 3.1 输入水位与完整性

| 状态 | 不变量 |
|---|---|
| `last_part_index` | 已消费的分片索引；下一个待读分片 = `last_part_index + 1` |
| `last_end_cursor` | = 已消费最后一个分片的末行游标；与分片内容**逐字节**一致 |
| `performance_parts[index]` | `(rows, end_cursor, sha256)`；分片发布后不可变 |
| `rows_consumed` | 已消费行数；= Σ parts.rows |
| 分片连续性 | 索引必须 `1..N` 连续；缺失即**拒绝推进**（不读后面的分片） |

### 3.2 Episode 折叠（跨批次的核心）

每个**未关闭** episode 必须持久化：

```
key(5) · first_seen · last_seen · observation_count · sum_edges(左折叠)
first_edge · last_edge · max_edge · min_edge
first_market_price · first_model_probability · first_available_depth_usd
station · bracket_label · market_question · fee_rate
opened_seq（开仓行序号，用于发布顺序）· closed_seq（关仓行序号）
```

已关闭 episode 需要同一组字段加上 `end_reason`（**发布顺序与
`unique_opportunity_edge_sum` 的浮点顺序都依赖它**）。

**全局折叠游标**（跨批次）：

| 状态 | 含义 |
|---|---|
| `row_seq` | 单调行序号（跨 run 连续），决定 `opened_seq` / `closed_seq` |
| `last_instant` | 最后一个**有 market_id** 的行的时刻 |
| `prev_instant` | 严格小于 `last_instant` 的最大时刻（§1.3a 的 O(1) 活动索引） |
| 折叠参数 | `gap_window_seconds`、`accepted_state`、`algorithm_version` |

> **发布顺序为什么必须逐位复刻**：`unique_opportunity_edge_sum` 是内建 `sum`
> 的左折叠，而 `closed.sort(key=first_seen_utc)` 是**稳定排序**——同 `first_seen`
> 的 episode 顺序决定浮点求和顺序。复刻规则：
> `(first_seen_utc, end_reason == "end_of_data", seq)`，
> 其中关仓用 `closed_seq`、仍开仓的用 `opened_seq`（对应原实现的
> “先追加所有关仓，再按 dict 插入序追加未关仓”）。

### 3.3 Edge 观测（精确分位数与全量数组）

`performance_edge_observations`：每个 **ACCEPT 且 edge 可用** 的行一行
`(seq, target_date, model_name, direction, mode, edge)`。

- `seq` 用于 `by_edge_band` 的**数组顺序**与原实现逐位一致；
- 不需要 `decided_at_utc`、`market_id`：没有任何 payload 用到它们
  （`daily` 按 `target_date` 分组，不是按决策时刻）。

### 3.4 计数与去重集合

| 状态 | 用途 |
|---|---|
| `decisions_seen` | `edge.decisions_seen`、`coverage.decisions`（**含**空 market_id） |
| `decisions_with_market` | `summary.episode_build.decisions_seen`（**不含**空 market_id） |
| `accepts_seen` / `not_accept` | `edge.accepts_seen` / `refused.not_accept` |
| `accepts_without_edge`（全量） | `edge.refused.accept_without_usable_edge` |
| `accepts_without_edge_with_market` | `summary.episode_build.accepts_without_edge` |
| `first/last_decision_utc` | `coverage.first/last_decision_utc` |
| `markets_seen` / `target_dates_seen` / `models_seen` | `coverage.*`、`models.models`（**全部决策**，非仅 ACCEPT） |

### 3.5 结算与发布层（**不**进入 episode 状态）

结算状态在**每次发布时**重新推导（现状 §1.2-8 本来就是这么做的）。
因此“结算晚到 / 被修订”天然支持**局部重算**：episode 是不可变的输入层，
outcome 是每次发布重算的派生层。

`performance_settlements`：`(market_id, target_date, status, payout, verified, fetched_at)`。
外部结算索引**增量 upsert**；`--settlements` 缺省时沿用库内既有答案
（现状是“不给就全丢”，这会让修订过的结算在下次运行时消失——这是必须修的）。

### 3.6 不保存什么（并说明理由）

- **不保存每条决策**。游标是**全序**且导出按游标排序，所以“已消费集合”就是
  游标定义的**前缀**；逐行 id 列表既无必要也不可比（5M 行 × 32B = 160MB）。
- **不保存 episode 的全部 edge 值**。Episode 只需 `sum/max/min/first/last`，
  且 `mean = sum/count` 与原实现的左折叠**逐位一致**。
- **不保存研究算法之外的任何东西**；不写生产 Shadow 库（契约 §11）。

---

## 4. 存储选型：DuckDB（独立文件）

| 候选 | 结论 |
|---|---|
| **DuckDB（选用）** | 已是依赖；单文件 ACID；`INSERT OR IGNORE` / 事务提交与水位推进可同一事务；列存 + **精确** `min/max/count`；窗口函数按秩取**精确顺序统计量**（分位数不近似）；`memory_limit` + `temp_directory` 可**显式**限定内存并落盘溢出；`GROUP BY` 流式游标可 `fetchmany` 分批拉回 Python |
| SQLite | 事务与崩溃恢复合格，但无原生 `quantile`、无分析型溢出策略；按秩取顺序统计量要靠 `LIMIT/OFFSET`（每组一次全排序）；列存扫描与聚合都更慢 |
| Parquet | 适合不可变的**快照**，不适合需要**增量 upsert**（未关闭 episode、结算修订、水位）的状态；没有事务，崩溃恢复要自己写 |

**落点**：独立文件 `data/research/performance-state.duckdb`（可用
`WQ_PERFORMANCE_STATE_DB` 覆盖），**不复用** `weather-research.duckdb`：
两者生命周期与写入者不同（研究库有 legacy 导入 / 预报回填），独立文件避免写锁竞争，
也允许单独删除重建。迁移仍复用仓库既有的**校验和 + 前向唯一**迁移机制
（新增 `storage/migrations_performance/`）。

**内存预算**：DuckDB `memory_limit` 默认 **1 GiB**、`temp_directory` 指向状态文件旁的
临时目录；Python 侧峰值 = 未关闭 episode 集合 + 单分片缓冲 + 单组分位数缓冲，
与历史长度无关。

**一个已知的有界项**：DuckDB 的 Python `execute()` 会**物化结果集**（`fetchmany`
只减少 Python 侧持有量）。因此峰值里还包含**一个结果集**——最大的是
`SELECT edge … ORDER BY seq` 的分位/分带扫描。实测 150 万行时整进程峰值
**299 MB**，其中结果集是十几 MB 量级；5M 行时按比例约 2.2M 个 double ≈ 18 MB。
也就是说上界是“一次一个结果集”，不是零，而它是**与批大小同阶**的常数项，
不随累计历史线性增长。

---

## 5. 正确性不变量（可测）

| # | 不变量 | 测法 |
|---|---|---|
| I1 | 同一输入下，流式引擎六份 payload 与 `build_performance()` **逐位相等** | 固定数据集全量比对（§6 十项） |
| I2 | 多批次 == 单批次 | 同一数据分 1/2/7 个分片各跑一遍 |
| I3 | 中断恢复 == 不中断 | 在第 k 个分片后强杀进程，重跑 |
| I4 | 重复输入不重复计数 | 同一分片重放两次 |
| I5 | 缺分片**拒绝推进** | 删除 part-3 后运行必须报错且水位不动 |
| I6 | 分片被改写可检出 | 改 part-N 末行后运行必须报错 |
| I7 | 结果提交成功后才推进水位 | 事务内 `rows + 水位` 同提交；异常路径断言水位不变 |
| I8 | 结算修订可更新结果 | 同一 store，改结算索引后重发布，胜率变化 |
| I9 | 参数漂移拒绝 | `gap_window_seconds` / `algorithm_version` 变化 → 拒绝增量 |

---

## 6. 验收对照（十项 + 十二项）

对照方式：`scripts/` 下的等价性 harness 用**同一份固定数据集**跑
`build_performance()`（旧）与流式引擎（新），逐项比较：

1. ACCEPT 观测数（`accept_observations`）
2. episode 数与身份（`episode_id` 集合 + 顺序）
3. Observation Edge Sum（`math.fsum`）
4. Unique Opportunity Edge Sum（内建 `sum` 左折叠）
5. 胜率分子/分母（`WIN`/`LOSS` 计数）
6. 聚类置信区间（2000 次 bootstrap 的两端）
7. 100 USDC 组合模拟（`allocated_total` / `peak_open` / `balance_remaining` / 每条 result）
8. 中断恢复后一致（I3）
9. 多批次 == 单批次（I2）
10. 缺数据仍显示 N/A（`win_rate=null` + 原因）

外加：11. 旧 Dashboard API 无破坏性变化（形状 + 既有 dashboard 测试全绿）；
12. 缺盘口仍为 `NOT_EXECUTABLE`/`N/A`。

---

## 7. 统计语义的保持策略：逐位相等（不是“近似”）

流式实现**不重写**统计公式，而是**改变数据的取法**：

| 量 | 旧实现 | 流式实现 | 是否逐位相等 |
|---|---|---|---|
| `sum` | `math.fsum(values)` | `math.fsum(游标生成器)` | ✅ fsum 与顺序无关、内部部分和数有界 |
| `mean` | `sum/count` | `sum/count`（同值） | ✅ |
| `std` | `statistics.pstdev(values)` | `statistics.pstdev(游标生成器)` | ✅ 实测逐位相等（`_ss` 单趟精确有理数） |
| `median` / `p05…p95` | `sorted()` 后线性插值 | SQL 按**秩**取上下两个顺序统计量，再用**同一个**插值函数 | ✅ 秩来自精确排序，插值公式复用 `edge.percentile` |
| `min` / `max` / `count` | Python | SQL `MIN/MAX/COUNT` | ✅ 精确 |
| `episode.mean_edge` | `sum(edges)/len(edges)`（左折叠） | 增量左折叠累加 `sum_edges` | ✅ 顺序相同 |
| 胜率 / 聚类区间 | `win_rate(scored)` | `WinRateAccumulator`（同一 bootstrap，同一簇插入序） | ✅ 由同一份 bootstrap 代码产生 |
| 组合模拟 | `simulate_portfolio` | `PortfolioWalk`（**同一段走账代码**，只是不物化 results） | ✅ |

**明确否定的做法**：DuckDB `quantile_cont` **不逐位相等**
（实测 1200 次里 74 次差 1 ULP，插值算式不同），因此**不使用**；
DuckDB `sum` 也不等于 `math.fsum`（实测相对差 ~1.7e-14），因此**不使用**。
`kahan_sum` 在实测中与 `fsum` 相等，但 Kahan 无正确舍入保证，故只作为
未采用的备选记录在此。

---

## 8. 必须显式声明的算法变化

1. **结算索引变为“粘性”**：`--settlements` 缺省时沿用库内既有答案，而不是
   当作“没有任何结算”。理由：结算是外部事实，会被修订；一次不带索引的运行
   不应把已核验的结算抹掉。**这是行为改变，写入契约 §13。**
2. **`manifest` 新增字段**：`algorithm_version`、`input_source`、`input_range`、
   `generated_at_utc`。纯新增，旧读者忽略。
3. **水位语义更严**：水位由“分片末行游标”升级为“分片末行游标 + 分片 sha256 +
   行数”，并记录在 `manifest.coverage`/`input_range` 之外的新段落
   （`manifest.streaming`）。纯新增。
4. **不改变任何统计公式、任何字段名、任何既有 payload 形状。**

---

## 9. 未解决风险（本轮不修，必须记录）

1. **导出游标只能前进**：REPLAY 写入历史时刻的行可能永不导出（§2）。本轮
   只做**可观测**（分片 sha256 + 游标），不重置生产游标。
2. **分片无发布者签名**：结构校验（连续性、非降序、末行游标、sha256）能检出
   截断与改写，但不能证明分片内容由 Shadow 写出。
3. **快照体积仍随历史线性增长**：`by_edge_band`（全量数值）与 `opportunities`
   （全量 episode）仍是全量数组。本轮只保证**研究容器**峰值内存有界，
   不改变落盘体积；Dashboard 需要整文件 `json.load`，这是**下游**待办
   （契约 §0.4 的“≤500 行”目前并未被实现满足，属既有偏差）。
4. **DuckDB 1.5.6 单写者**：状态库同时只允许一个写进程；并发运行必须靠
   文件锁或运维纪律（本轮以“单写者 + 明确报错”处理）。

---

## 10. 实施记录（实现之后回填，与上面设计对照）

### 10.1 实现落点

| 文件 | 作用 |
|---|---|
| `performance/state.py` | 状态库：迁移、水位、分片完整性、Episode 检查点、批量写入 |
| `performance/streaming.py` | 分片读取、折叠驱动、六份 payload 的流式构建、发布 |
| `storage/migrations_performance/0001_performance_state.sql` | 状态库 schema |
| `performance/episodes.py` | `EpisodeFolder` / `OpenEpisode` / `EpisodeSummary`（单一实现，两条驱动共用） |
| `performance/winrate.py` | `WinRateAccumulator`（bootstrap 单一实现） |
| `performance/capital.py` | `PortfolioWalk`（走账单一实现，可流式输出 results） |
| `performance/edge.py` | `distribution_from_parts` / `median_from_ranks` / `quantile_from_ranks`（按秩精确分位数） |
| `shadow/publish.py` | `StreamedArray` + `atomic_write_json_streaming`（与 `atomic_write_json` 逐字节一致） |
| `cli.py` | `build-performance` 目录=流式、文件=参考实现，新增 8 个参数 |
| `scripts/bench_performance_streaming.py` | 隔离压力测试与峰值 RSS 测量 |

### 10.2 实现期间修正的设计

1. **Episode 主键不能用五元组身份键。** 最初用 `key_hash`（五元组）作主键，
   结果被同一 key 的后一个 episode 覆盖：真实数据里 gap 关闭一个 episode 后
   会再开一个，两者身份键相同而 `first_seen` 不同。等价性测试立刻发现
   （252 → 178 个 episode）。改为 `episode_id`（= key + first_seen 的哈希）作主键。
2. **`executemany` 不能用于状态库写入。** 实测 DuckDB 1.5.6 对带主键的表
   `executemany` 约 **9 ms/行**（6000 行 episode 花了 60 秒，占满整个 ingest）。
   改为 DataFrame 批量 `INSERT ... SELECT` 后约 **12 µs/行**（同一事务内，已用
   回滚验证），10,000 行的 ingest 从 69 秒降到 1.3 秒。
3. **`activity_between` 的确可以用 O(1) 状态回答**（§1.3a 的推断已被等价性
   测试验证：`prev_instant > last_seen`）。
4. **结算索引必须粘性**：v0.1.4 的“不给就全丢”会让已核验的结算在下次运行时
   消失，与“结算可修订”直接冲突。这是本轮**唯一**的对外行为改变（契约 §13.4）。

### 10.3 等价性证据

`tests/test_performance_streaming.py` 用同一份固定数据集（900 行，含 NaN/inf
edge、null model、两种 direction/mode、4 个 target date、跨窗口与窗口内 gap）
同时跑两条路径，断言**六份 payload 逐字段相等**（除 `generation_id` 外），
并单独断言验收清单的十项。另有 10,000 行的同规模对照。

`tests/test_performance_streaming_state.py` 覆盖：任意分片数结果一致、跨分片
episode 不拆、中断续跑不重复计数、重复输入幂等、缺分片拒绝、分片被改写拒绝、
乱序分片拒绝、参数漂移拒绝、`--reset` 重建、结算修订只重算发布层、
`--no-ingest` 局部重算、manifest 字段、峰值内存不随历史增长。

### 10.4 压力测试结果

见 `docs/performance-deployment.md` §11（由
`scripts/bench_performance_streaming.py` 在隔离目录生成，未触碰生产 NAS）。


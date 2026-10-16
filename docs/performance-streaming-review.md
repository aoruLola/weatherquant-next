# PR #11 最终审查记录

审查对象：`feature/performance-streaming` @ `7af667d`（PR #11，base `main`）
审查时状态：`OPEN` / `MERGEABLE` / `mergeStateStatus=CLEAN`，CI **20/20 通过**
本地复验：`uv run pytest -q` → **1004 passed, 6 skipped**；`dashboard/tests` → **93 passed**；
`npx tsc --noEmit` 无错误；`npm test` → **87 passed**

本文件只记录**实际读到或实际跑过**的东西。凡未验证的一律标注。

---

## 1. 审查范围与结论

| 模块 | 结论 |
|---|---|
| Streaming Reader（分片扫描/游标校验/逐行） | 通过 |
| `EpisodeFolder`（状态机、跨批次延续、五元组隔离） | 通过 |
| 持久化状态库（schema、事务、批量写入） | 通过（两处缺陷已修，见 §4） |
| 检查点事务（水位不越过分析） | 通过 |
| Edge 聚合 / 精确分位数（按秩） | 通过 |
| Win Rate / 聚类置信区间 | 通过 |
| Reference Capital（`PortfolioWalk`） | 通过（本轮新增"结果采样"，见 §5） |
| Settlement 状态（粘性） | **发现两处真实缺陷，已修** |
| Snapshot Publisher（世代原子切换） | 通过 |
| CLI | 通过 |

**结论：审查发现 2 处必须修复的缺陷（均在结算粘性层），已修复并补 6 个回归测试。
除此之外无阻断项。**

---

## 2. 状态恢复不变量（逐条核对）

| 不变量 | 证据 |
|---|---|
| 已处理行与检查点同事务提交 | `ingest_export` 每分片一个 `store.transaction()`；行、Episode 状态、计数器、分片记录、游标同提交（`streaming.py` ingest 循环） |
| 崩溃恢复不重复计数 | `test_an_interrupted_ingest_resumes_without_double_counting`：第 2 个分片注入异常 → 断言 `last_part_index == 1`、`rows_consumed == 200`、`recorded_parts() == {1}`；续跑 600 行；第三次 0 行 |
| 输入分片不能被静默跳过 | `scan_export_parts` 拒绝非连续索引；`test_a_missing_part_is_refused_and_the_watermark_does_not_move` |
| 已处理分片变更必须被检测 | 每分片记录 `rows` + `sha256`；边界分片每次复验，`--verify-parts` 全量复验；`test_a_part_that_changed_after_being_consumed_is_refused` |
| 断点恢复结果与全量结果一致 | 上条 + `test_any_number_of_parts_gives_the_same_snapshots[1,2,5,13]` |
| Episode 跨分片延续 | `test_a_part_boundary_does_not_split_an_episode`（1/3/7 分片结果逐字段相同） |
| LIVE / REPLAY 不混合 | `test_live_and_replay_never_share_an_episode`：6 条交错 LIVE/REPLAY 必须折叠成 **2 个** episode、各 3 次观测 |
| 算法参数变化必须拒绝复用旧状态 | `_require_compatible`（algorithm_version / input_source / gap_window / accepted_state）；`test_a_changed_fold_parameter_is_refused` |

补充核对（本轮的额外验证）：

- **乱序分片被拒绝**：`test_a_part_out_of_cursor_order_is_refused`（交换两个分片内容）。
- **重复游标被拒绝**：同一 `(decision_time_utc, decision_id)` 连续出现即拒绝（重复行会被重复计数）。
- **单写者**：`test_a_second_process_is_refused_rather_than_corrupting_the_store` 用**子进程**验证
  DuckDB 文件锁（同进程内的第二个连接共享实例，不是真实场景）。
- **内存不随历史增长**：`test_peak_memory_does_not_grow_with_the_history`（分片大小固定，
  2k vs 20k 行，`tracemalloc` 峰值 < 2 倍）。

---

## 3. 统计一致性（逐项，非整体比较）

`tests/test_performance_streaming.py` 用同一份固定数据集（900 行，含 NaN/inf edge、
null model、两种 direction/mode、4 个 target date、窗口内与跨窗口 gap、真实双侧盘口、
真实结算）**同时以"发布"方式跑两条路径**，再读回六个文件逐字段比较。

> 比较的是**发布产物**而不是构建器返回值：这样连"明细以分片发布"的部分也一并比较，
> 而且是读者真正看到的东西。

`test_every_edge_statistic_matches_item_by_item` 逐项断言（不是整体相等）：

| 项 | 覆盖 |
|---|---|
| ACCEPT observation count | `accept_observations` |
| Episode count | `opportunity_episodes` + 发布顺序逐个 `episode_id` |
| Observation Edge Sum | `observation_edge_sum`（`math.fsum`） |
| Unique Opportunity Edge Sum | `unique_opportunity_edge_sum`（内建 `sum` 左折叠） |
| Mean / Median / P05 / P25 / P50 / P75 / P95 / Min / Max / Std | `overall` 与 `by_model` / `by_direction` / `by_target_date` **每个分组每个统计量** |
| Win Rate | `counts` / `valid_samples` / `confidence_interval` / `method` |
| 日期聚类置信区间 | 同上（数据集保证 `method == cluster_bootstrap_by_target_date`） |
| Reference Capital | `allocated_total` / `peak_open` / `balance_remaining` / `executable_count` / `not_executable_reason_counts` / `results_sample` |
| 模型分组 | `models` 列表 + `by_model_win_rate` 全量比较 |
| 市场与目标日期统计 | `accept_opportunity_distinct_markets` / `..._target_dates` / `daily` / `by_edge_band` / `refused` |

**没有任何断言被放宽，没有任何期望值被改写。** 唯一被修改的既有断言是
`tests/test_performance_edge.py` 的两个分带测试：`by_edge_band` 的**形状**按本轮要求
从"原始数值数组"改为"分布"（见 §5），测试改为断言同样的边界行为（每个 edge 落在哪个带、
0.02/0.05/0.10 的边界语义），而不是断言数组内容。

---

## 4. 结算粘性：审查发现的两处缺陷（已修）

### 4.1 伪造的检索时间（**严重**）

`upsert_settlements` 写的是 `fetched_at or now`。当结算索引**没有**提供 `fetched_at_utc` 时，
状态库会盖上**写入时刻**，随后发布的 payload 报告了一个从未发生过的检索时间——
"沿用旧状态"因此与"刚刚验证过"完全无法区分。这正是审查要求第 2 条点名的失败。

修复：该列改为可空（迁移 `0002`），`NULL` 表示"未记录"，并在 payload 里显式说明：
`freshness.answers_without_retrieval_time` + 一条 warning。`updated_at_utc` 仍然记录
"何时写入"，与"何时取回"是两件事、两列。

### 4.2 没有撤销路径（**中**）

默认是 upsert（并集），这是对的：覆盖今天市场的索引不能抹掉昨天的答案。但并集
**无法表达撤销**——平台撤回的结算、或不应再计分的市场，会在之后每次运行中存活下来。

修复：`--settlements-replace` 明确声明"这份索引是完整的"，删除其中不存在的答案；
之后不带索引的运行保持撤销结果，不会从缓存里复活。

### 4.3 顺带修复：来源与结论被丢弃

`verification_status` 与 `resolution_source` 原本被折叠成一个 `verified` 布尔后丢弃，
结论无法在规则变化时被重新检视。现按原文存储并逐条发布，`freshness.as_of_utc` /
`oldest_utc` 汇总该层，`summary.settlement_freshness` 把同一份汇总带到首屏。

**两条路径共用同一个格式化函数**（`settlement_payload`），所以"逐字节相等"的等价性
测试仍然成立——它现在同时证明了两个引擎对来源信息的判断也一致。

### 4.4 回归测试（对应审查五条要求）

| 要求 | 测试 |
|---|---|
| 1. 不传 `--settlements` 沿用已保存状态 | `test_a_settlement_revision_updates_the_result_without_reingesting`（末段） |
| 2. 沿用旧状态不得伪装成刚完成验证 | `test_a_kept_settlement_answer_carries_its_retrieval_time`、`test_an_answer_with_no_retrieval_time_says_so` |
| 3. 结算修订必须重新计算受影响结果 | `test_only_the_revised_market_changes`（改一个市场的赔付 → 只有该市场的 outcome 变；episode 聚合不变） |
| 4. 明确撤销或失效的结算不能被旧缓存恢复 | `test_a_withdrawn_settlement_is_retracted_and_not_resurrected` |
| 5. 结果保留来源、检索时间、验证时间和数据版本 | `test_the_settlement_layer_records_where_it_came_from`（含 manifest 的 `algorithm_version` / `generated_at_utc`） |

---

## 5. 审查中确认、并在本轮修掉的相邻问题

审查过程中量化了"快照体积仍随历史线性增长"（原审计 §9 风险 3）。实测
（500 万行合成数据，真实落盘）：

| 文件 | 5M 行 | 全部体积来自 |
|---|---|---|
| `summary.json` | **131.3 MB** | `data.reference_capital.results[]`（242,570 条） |
| `opportunities.json` | **162.9 MB** | `data.episodes[]`（242,570 条） |
| `edge.json` | **22.6 MB** | `data.by_edge_band.*[]`（2,236,360 个浮点） |
| `settlements.json` | 0.16 MB | 有界（1,120 条） |
| `win-rate.json` | 0.03 MB | 有界 |
| `models.json` | 0.00 MB | 有界 |

Dashboard 因此会：`/summary` 返回 131 MB；`/opportunities` **解析 163 MB 只为返回 50 行**
并把它缓存下来。这是"把全量构建器省下的内存重新花在 Dashboard 上"的典型。

修复属于下一轮（Dashboard 分页 PR，见 `docs/performance-data-contract.md` §14），
不在 PR #11 的范围内——PR #11 的审查结论不依赖它。

---

## 6. 未解决风险（PR #11 合并前必须记录）

1. **导出游标只能前进**：REPLAY 写入历史时刻的行可能永不导出。本轮把它变成**可观测**
   （分片 sha256 + 游标非降校验），**未修**。
2. **分片无发布者签名**：能检出截断/改写，不能证明内容出自 Shadow。
3. **DuckDB 的 Python `execute()` 会物化结果集**：峰值里含"一个结果集"，与批同阶，
   不随历史线性增长（5M 行实测 477 MB，10M 行 613 MB）。
4. **单写者**：并发运行会被文件锁拒绝；运维需避免重叠执行。

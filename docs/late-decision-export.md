# 迟到决策导出（WQN-20）：写入序列游标的设计、迁移与回滚

状态：已实现，全部在本机以隔离测试验证。**NAS 上的生产迁移尚未执行**（见文末）。

## 1. 缺陷

历史导出（`shadow/export.py`）以 `(decision_time_utc, decision_id)` 为游标，排他前进：
`decision_time_utc > ? OR (= ? AND decision_id > ?)`。业务时间由引擎写入，但**可被数据倒拨**：
REPLAY 重读历史归档时，写入时刻是现在、业务时刻是过去。这样的行落在水位之后，
任何后续批次都选不中它——它存在于源数据库、永远缺席于导出，且**分片单调校验发现不了**：
分片自身是自洽的，只是不含这行。结果：迟到的 REPLAY 决策永远不会被统计。

## 2. 修复：定位用单调写入序列，业务时间只是数据

- 迁移 `0005_decision_export_seq.sql`：`shadow_decisions` 增加 `export_seq BIGINT`
  （可空），由 DuckDB 序列 `seq_shadow_decision_export` 在**插入时**取值一次；
  存量行按**旧游标顺序**（`(decision_time_utc, decision_id)` 的 row_number）回填；
  序列被推进到回填最大值之上（DuckDB 1.5.6 无 `setval`，用 `nextval FROM range(n)` 消费法）。
- `save_decisions` 的 UPSERT **不更新** `export_seq`：重写同一决策（重复写入）既不改变其导出位置，也不会二次导出。
- 导出定位改为 `WHERE export_seq > ? ORDER BY export_seq`：`export_seq` 唯一、
  只在插入时取值、重写不移动，因此该序是严格全序。**现在写入的行——无论业务时刻多老——
  序列值必然高于游标，永远可达**。迟到行由此从"结构上不可达"变为"下一批次的一部分"。
- 行内导出 `export_seq` 字段（`EXPORT_COLUMNS` 末尾），供下游做顺序校验与迟到行识别。

### 不变量（对账的依据）

**已导出集合 = { export_seq ≤ cursor_seq } 的前缀**。由三件事保证：
1. 每行序列唯一且取值一次（UPSERT 不动它）；
2. 游标只在分片落盘后推进（既有不变量，未改动）；
3. 崩溃恢复从分片末行读回 `export_seq`（`_end_seq_of`），分片仍是权威。

因此对账可以很便宜且精确（`DecisionExporter.reconcile()`）：
分片串联上 `export_seq` 严格递增（无重、无乱序）+ 源表 `export_seq ≤ cursor_seq` 的行数
等于分片行数（不漏不重）+ state 行数与磁盘一致。

## 3. 兼容已有导出：一次性过渡（不清空生产游标）

旧分片没有 `export_seq`，且 v1 按业务时间选取——被它跳过的行在序列空间里与已导出行
**交错**，不存在一个纯量下限能同时"排除已导出行、包含被跳过行"。所以过渡做三件事
（`DecisionExporter._transition_step`，每 tick 有界、可恢复、幂等）：

1. **扫描已发布分片**（每 tick 最多 8 个），把已导出的 `decision_id` 收进临时表——
   权威是分片里**实际有什么**，不是 v1 水位**声称覆盖了什么**（本缺陷恰恰是声称跳过了行）。
2. **定下限**：`floor = max(export_seq) over 已导出行`。
3. **重排序被跳过的行**：`export_seq ≤ floor 且未导出` 的行改取序列新值（必然 > floor），
   成为普通待导出行。分片一个字节都不改写；v1 水位字段保留（作为可观测的业务最远时刻）。

过渡有自己的阶段状态（`state.json` 的 `transition` 字段），引擎崩溃重启后临时表丢失则
从第 1 个分片重扫（幂等，只多花一次扫描）；序列头意外低于下限时先探测推进，杜绝自旋。

### 状态文件（v2）

```json
{
  "version": 2,
  "cursor_seq": 12345,              // 定位权威：写入序列游标
  "watermark": ["<ts>", "<id>"],    // 业务最远时刻（仪表盘新鲜度兼容；只增不减）
  "next_part": 36, "rows_exported": 700123,
  "transition": {"phase": "done", "floor_seq": 700000, "rows_resequenced": 4, ...}
}
```

`watermark[0]` 仍是时间戳，`dashboard/performance_freshness.py` 的解析不变。

## 4. 下游（performance 流式）适配

- **顺序校验换锚**：携带 `export_seq` 的行必须在全串联上**严格递增**（重复=重行，回退=乱序分片）；
  迟到行业务回退**允许**且计数（`late_rows_seen`，随 state 持久化）。仅旧格式行仍按业务时间非降校验。
- **折叠器不变量保持**：`EpisodeFolder` 的全局活跃索引只前进（迟到瞬间不移动索引）；
  迟到行对已越过其时刻的 episode 只记账（ACCEPT 计入 count/edge），不触发 gap 关闭、不倒拨
  `last_seen`。对**从不回退的流，行为与旧代码逐位一致**（既有全部折叠测试未改通过即为证），
  因此不 bump `PERFORMANCE_ALGORITHM_VERSION`，存量状态库可继续增量。
- 状态库迁移 `0003_ingest_export_seq.sql`：`performance_ingest` 增 `last_export_seq`（可空）
  与 `late_rows_seen`（默认 0），纯增量。

## 5. 回滚方案

- **引擎回滚到旧镜像**：安全。`export_seq` 对旧代码只是多出来的可空列（旧 INSERT 不写它，
  得 NULL）；v2 的 `_assign_missing_seq` 在升级回来后有界修复这些 NULL 行。
  旧镜像只读 `watermark` 字段（v2 保留且语义兼容），不会重导已导出行。
- **数据库回滚**：不需要。迁移是纯增量（序列 + 可空列 + 索引），前向迁移器只加不改；
  回滚数据库反而会丢掉回填成果。
- **研究侧（performance 状态库）**：v2 分片可能含业务回退行，**旧版消费代码会拒绝这些分片**
  （拒绝推进、可观测、不损坏状态库）——回滚研究侧应同时保持导出目录不再增长，或按
  `docs/performance-decouple-runbook.md` 重建状态库。

## 6. 验收证据（本机）

`tests/test_shadow_export_seq.py`（16 例，全过）逐条覆盖工单验收项：

| 验收项 | 测试 |
|---|---|
| 迟到 REPLAY/LIVE 混合 | `test_a_row_written_late_with_an_earlier_business_time_is_exported`、`test_a_replay_run_backfilling_history_is_exported_in_full` |
| 重复写入 | `test_rewriting_a_decision_does_not_re_export_it` |
| 崩溃恢复 | `test_a_lost_state_file_recovers_the_sequence_cursor_from_the_parts`、`test_recovery_after_a_crash_between_parts_publishes_the_rest_exactly_once`、`test_the_transition_is_bounded_and_resumable_across_restarts` |
| 连续分片 + 对账不漏不重 | `test_parts_are_consecutive_and_reconcile_is_clean_over_many_ticks`、`test_reconcile_reports_a_duplicated_line`、`test_reconcile_reports_rows_missing_from_the_parts` |
| v1 导出兼容迁移 | `test_the_transition_exports_rows_the_v1_cursor_skipped`（v1 跳过的行被补发、v1 行零重发） |
| NULL 修复（回滚后再升级） | `test_rows_written_without_a_sequence_are_repaired_in_write_order` |
| 下游消费 | `test_the_streaming_fold_accepts_late_rows_and_counts_them`、`test_legacy_parts_then_sequenced_parts_still_ingest`、`test_the_streaming_fold_refuses_a_repeated_sequence` |

全套 `uv run pytest -q`：1080 passed, 6 skipped（main 上本就失败的
`test_peak_memory_does_not_grow_with_the_history` 在本机通过；另一处因迁移清单变化的守卫断言已随 0003 更新）。

运维命令：`weatherquant-shadow export-verify [--json]`（引擎须停止）输出对账报告，
exit code 0/1 对应一致/不一致。

## 7. 需 NAS 人工验收的部分

- 生产 `shadow.duckdb` 上执行迁移 0005（约 70 万行回填）与一次性过渡扫描（约 35 个分片、
  数个 tick）的实际耗时观察；
- 过渡完成的质量事件（`decision_export_transition_done`）与 `export-verify` 在真实数据上的输出；
- 旧镜像→新镜像→旧镜像→新镜像 的回滚演练（本机已用故障注入等价模拟，见上表）。

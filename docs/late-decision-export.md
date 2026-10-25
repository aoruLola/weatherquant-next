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

1. **扫描已发布分片**，把已导出的 `decision_id` 收进**持久表** `_export_transition_ids`
   ——权威是分片里**实际有什么**，不是 v1 水位**声称覆盖了什么**（本缺陷恰恰是声称跳过了行）。
   扫描按批读入（`read_json_auto` 接受路径列表，一条语句一批分片）——逐行
   `executemany` 进带主键的表在验收机上约 33 秒/2 万行，是首轮 18.4 分钟的主要来源；
   持久表让重启**续扫**而非从 part-1 重扫。批间检查 `max_seconds`，每 tick 至少一批。
2. **定下限**：`floor = max(export_seq) over 已导出行`。
3. **重排序被跳过的行**：`export_seq ≤ floor 且未导出` 的行经**单条集合式 UPDATE** 改取
   序列新值（必然 > floor），成为普通待导出行。扫描耗尽预算时 finalize 顺延到独立 tick
   （fresh tick 必做工作，不会死循环）。分片一个字节都不改写；v1 水位字段保留。

过渡有自己的阶段状态（`state.json` 的 `transition` 字段）；序列头意外低于下限时先探测
推进，杜绝自旋。

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

## 5. 回滚方案（已按验收演练更正）

支持路径是「新 → 旧 → 新」：旧镜像在 v2 导出目录上继续用业务游标导出，把
NULL-seq 行写进**追加在 v2 分片之后的 v1 格式分片**（旧代码会原样保留它不认识的
`version`/`cursor_seq` 键）。新镜像重新接管时：

- **`_repair_rollback_tail`**（每个导出 tick 先行）：检测"末尾无 `export_seq` 的分片"，
  把其中 id 对应的 NULL-seq 行**钉在 cursor_seq**（≤ 游标 = 已导出前缀），它们不会再被
  `_assign_missing_seq` 分配新序列而二次导出。幂等（只碰 NULL 行），一次性记录在
  `state.json` 的 `last_rollback_repair`，runner 记 `decision_export_rollback_repair`
  质量事件。
- 旧镜像窗口里**写了但没来得及导出**的行（NULL 且不在任何分片里）由 `_assign_missing_seq`
  正常补序列并首次导出——旧镜像自身的迟到行缺陷也会在重新升级后被补发。
- `reconcile()` 对无 seq 的行改为**按源库核验**：id 必须存在于已导出前缀（seq ≤ 游标），
  位置本身不再是判据；对流式消费同理（旧格式行只在自己的连续区域内要求业务时间有序，
  seq 行之间仍严格递增）。

**双镜像演练证据**（old-seed 250 行 → new-upgrade → old-rollback 写 25 行并导出 →
new-again）：source=316，exported=316，missing=0，**duplicates=0**，reconcile
consistent=True，performance 流式消费 316 行 OK（修复前：duplicates=15、
reconcile INCONSISTENT、流式永久拒绝）。

- **引擎回滚到旧镜像**：安全。`export_seq` 对旧代码只是可空列；v2 state 的未知键被旧
  代码保留；重新升级后由上面的修复步骤收敛，无重复、无丢失。
- **数据库回滚**：不需要。迁移 0005/0006 纯增量（0005 列+回填+序列推进；0006 索引单独
  事务——同一事务内 UPDATE 后建索引在非空表上会报 "Cannot create index with outstanding
  updates"，这是拆分的原因）。
- **研究侧（performance 状态库）**：回滚窗口的 v1 分片现在可被消费（见上）；若分片在
  旧镜像运行期间已被旧版消费代码读过并留下分片 sha 记录，按
  `docs/performance-decouple-runbook.md` 重建状态库。

## 6. 验收证据（本机）

**单元/隔离测试**：`tests/test_shadow_export_seq.py`（20 例）与
`tests/test_shadow_export_seq_migration.py`（3 例，含旧 schema + 非空表 → 新版 open 的
真实迁移路径、250k 行下首取 `nextval == max+1`、迁移对拆分的静态守卫）全过；
全套 `uv run pytest -q` 1087 passed, 6 skipped；ruff 通过。

**双镜像演练**（旧=main@3757619，新=本分支；脚本与输出见 PR #37「本机验收证据」评论）：

| 验收项 | 结果 |
|---|---|
| 旧 schema + 非空表 → 新版 open（原 ①） | OK：0005/0006 两个事务分别应用，不再 "Cannot create index with outstanding updates" |
| v1 导出跳过的 30 行迟到 REPLAY | 过渡补发 30 行，v1 行零重发（source=250 exported=250 missing=0 dup=0） |
| 新→旧→新回滚（原 ③） | duplicates=0（修复前 15）；reconcile consistent=True（修复前 INCONSISTENT）；流式消费 316 行 OK（修复前永久拒绝）；旧镜像窗口漏掉的 10 行迟到 REPLAY 亦被补发 |
| kill -9 崩溃注入 ×3 轮 + 最终对账 | source=541 exported=541 missing=0 duplicates=0；无遗留 .tmp；reconcile consistent=True |
| 70 万行级过渡 tick 时长（原 ②） | 修复前：单 tick 240–267s、总 18.4min（逐行 executemany ≈33s/2 万行 + 无预算检查 + 重启重扫）。修复后（本机以旧镜像真实产物验证：6×2 万行分片、生产参数 batch=20000/max_seconds=5）：迁移+回填 open 7.6s，**过渡单 tick 0.74s**，补发 12,057 行一批 0.51s；每批分片一条 `read_json` 语句、批间查预算、id 表持久化免重扫 |

运维命令：`weatherquant-shadow export-verify [--json]`（引擎须停止）输出对账报告，
exit code 0/1 对应一致/不一致。

## 7. 需 NAS 人工验收的部分

- 生产 `shadow.duckdb` 上执行迁移 0005/0006（约 70 万行回填）与一次性过渡的实际耗时观察；
- 过渡完成的质量事件（`decision_export_transition_done`）与 `export-verify` 在真实数据上的输出；
- 旧镜像→新镜像→旧镜像→新镜像 的回滚演练（本机已用双镜像 + kill -9 注入等价复现，见上表）。

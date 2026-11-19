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
1. 新写入的决策取得单调递增序号，UPSERT 保留原序号。旧镜像回滚期间已导出的行可共享恢复时的游标值，见第 5 节。
2. 游标只在分片落盘后推进（既有不变量，未改动）；
3. 崩溃恢复从分片末行读回 `export_seq`（`_end_seq_of`），分片仍是权威。

`export-verify` 检查有序号的分片行是否严格递增，并核对 state、分片和源库游标范围内的行数。
行数相同时，工具还会读取全部分片的 `decision_id` 和 `export_seq`，按 ID 与源库比对。
每个 ID 在所有分片中须恰好出现一次，并且属于已导出范围。有序号的行还须匹配该 ID 在源库中的序号。
这样可以发现行数相同但 ID 被重复或替换的分片。完整扫描只在显式对账时执行。

### 0009 与 DuckDB 版本

- 本分支基于的 `origin/main` 只有 Shadow 迁移 0001–0006；新增 `0009_drop_mutable_export_seq_index.sql`
  没有编号冲突。迁移器按版本顺序执行并记录校验和，重复 open 不会重放；直接重复执行该 SQL 也因
  `DROP INDEX IF EXISTS` 而幂等。
- 0009 只删除 0006 的**非唯一**索引，不删除列、序列或约束。导出仍显式使用
  `WHERE export_seq > ? ORDER BY export_seq, decision_id`，结果不依赖索引是否存在。
- 删除索引是为了让旧行补序号及回滚修复在 DuckDB 1.1 上也能更新该列。本仓库当前 `uv.lock`
  锁定 **1.5.6**；本分支单独在该版本完成本轮测试，不需要合入 #44，也未改动版本锁。本轮没有切换
  DuckDB 版本，因此不把其他版本的结果写作本轮实测证据。

## 3. 兼容已有导出：一次性过渡（不清空生产游标）

旧分片没有 `export_seq`，且 v1 按业务时间选取——被它跳过的行在序列空间里与已导出行
**交错**，不存在一个纯量下限能同时"排除已导出行、包含被跳过行"。所以过渡做三件事
（`DecisionExporter._transition_step`，每 tick 有界、可恢复、幂等）：

1. **扫描已发布分片**，把已导出的 `decision_id` 收进**持久表** `_export_transition_ids`
   ——权威是分片里**实际有什么**，不是 v1 水位**声称覆盖了什么**（本缺陷恰恰是声称跳过了行）。
   每批最多读取 8 个分片。合计不超过 256 KiB 时，逐行提取 ID 后用一条 SQL 批量写入；超过该大小时，由 DuckDB 直接读取文件。
   两种路径共用去重表、扫描进度和时间预算。重启后从已记录的位置继续。批间检查 `max_seconds`，每 tick 至少处理一批。
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

## 5. 安全升级与回滚方案（已按验收演练更正）

### 升级步骤

1. 停止 Shadow 引擎，确认没有其他进程持有数据库；在同一持久卷保留数据库和整个导出目录的备份。
   不删除 `state.json`、不清空 `cursor_seq`，也不移动或改写任何 `parts/part-*.jsonl`。
2. 启动包含本分支的新镜像。数据库 open 会由唯一写者按事务依次应用尚未执行的 0005、0006、0009；
   已记录的迁移自动跳过。0009 不需要反向迁移。
3. 让导出 tick 持续运行，直到 `state.json` 为 `version: 2` 且 `transition.phase: done`，随后达到
   `caught_up`。大量旧分片会跨多个有界 tick 扫描，中途重启会从已保存的批次位置继续。
4. 停止引擎后运行 `weatherquant-shadow export-verify --json`。退出码必须为 0、`consistent` 必须为
   `true`，再恢复引擎和研究侧消费；非零时保留现场并按 `first_divergence` 排查，不要清空游标重来。

### 回滚步骤

支持路径是「新 → 旧 → 新」：先停止新镜像，再让旧镜像使用**同一个**数据库和导出目录，不能用旧备份
覆盖升级后的文件。旧镜像在 v2 导出目录上继续用业务游标导出，把 NULL-seq 行写进
**追加在 v2 分片之后的 v1 格式分片**（旧代码会原样保留它不认识的 `version`/`cursor_seq` 键）。
旧镜像不依赖已删除的非唯一索引，也不受新增可空列和额外迁移记录影响；回滚窗口仍存在旧版迟到行
缺陷，应尽快重新升级。新镜像重新接管时：

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
- **数据库回滚**无需反向迁移。0005 添加可空导出序号并回填已有决策，0006 在独立事务中创建索引。
  0009 删除该索引，以便 DuckDB 1.1.0 为旧镜像写入的决策补写序号。决策主键、已分配的序号、
  导出游标和既有分片均保留。回滚镜像后沿用原数据库和导出目录，再次升级时从持久化游标继续。
- **研究侧（performance 状态库）**：回滚窗口的 v1 分片现在可被消费（见上）；若分片在
  旧镜像运行期间已被旧版消费代码读过并留下分片 sha 记录，按
  `docs/performance-decouple-runbook.md` 重建状态库。

重新升级后必须运行到 `caught_up`，再次停止引擎并执行 `export-verify --json`；只有退出码为 0
才恢复研究侧消费。这样回滚窗口里已导出和未导出的行都经过源库对账，而不是仅依赖行数判断。

## 6. 验收证据（本机）

**WQN-20 复审单元/隔离测试**：`tests/test_shadow_export_seq.py`（35 例）与
`tests/test_shadow_export_seq_migration.py`（4 例，含旧 schema + 非空表 → 新版 open 的
真实迁移路径、250k 行下首取 `nextval == max+1`、0009 幂等与旧迁移集重开）共 39 例全过；
全套 `uv run pytest -q` **1521 passed, 6 skipped**；`uv run ruff check .` 通过，本次改动的四个
Python 文件均通过 `ruff format --check`。空分片恢复用例做了反向验证：临时恢复旧的空表判据后，
断言实际值 `next_part == 9`、预期值 `17`，测试如期失败；还原修复后通过。

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

如果已发布的分片缺号，`export-verify --json` 返回退出码 1，并在 `first_divergence`
中列出首个缺失文件。引擎保留导出游标，继续记录决策，通过 `decision_export_failed`
报告受阻的导出。恢复原分片后，下次导出从已有游标继续。若状态文件也已丢失，则从连续分片重建游标。
对账结果中的 `source_rows_total` 是源库决策总数，`source_rows_pending` 包括游标之后及尚未分配序号的决策。
生产追平时，已导出范围应一致且待导出数应为零。下游消费进度和已消费分片的 SHA-256 由 Performance 单独复核。

## 7. 需 NAS 人工验收的部分

- 生产 `shadow.duckdb` 上执行迁移 0005/0006/0009（约 70 万行回填）与一次性过渡的实际耗时观察；
- 过渡完成的质量事件（`decision_export_transition_done`）与 `export-verify` 在真实数据上的输出；
- 旧镜像→新镜像→旧镜像→新镜像 的回滚演练（本机已用双镜像 + kill -9 注入等价复现，见上表）。


### 评审修复验证

研究聚合升级为 performance-streaming-2，新增性能状态迁移 0004。导出定位仍以 export_seq 前进；迟到行将 episode 重建标记与游标一起提交，然后从已消费的不可变分片按业务时间重新聚合。DuckDB 在既有内存预算下排序，Python 分页读取。重建失败保留标记，下次 ingest 或 publish-only 必须先完成修复。既有状态首次升级会执行一次相同重建，保留结算层和导出位置；旧版研究镜像需要独立派生状态库或重建，不能继续写新版状态。

## 8. 部署顺序与回滚（Shadow 与 Performance 必须同批）

`export_seq` 是同一协议的两端：Shadow 侧改了导出格式与游标语义（0005/0006，`state.json`
升到 v2），Performance 侧改了摄入游标与顺序校验锚点（0003 的 `performance_ingest.last_export_seq`、
`late_rows_seen`）。只升级一侧会让两端对不上——新 Shadow 写出的 seq 行被旧消费者按业务时间
校验，迟到 REPLAY 仍会被判为回退而拒绝；旧 Shadow 的 v1 分片没有 seq，只有新消费者的旧格式
兼容路径能正确放行。因此**必须在同一维护窗口内成对升级，不要分两次上线**。

### 8.1 升级顺序

1. 记录两端镜像 digest 与当前版本；停 Shadow 后备份 `shadow.duckdb`、`state.json` 和
   `reports/decisions-export/` 的末两个分片（步骤见 `docs/shadow-unraid-deployment.md` §9）。
2. **先停摄入**：停 Shadow 容器；确认 Performance 的 5 分钟摄入 runner 未在运行
   （`flock -n /var/lock/weatherquant-performance.lock true`，返回 0 且无输出表示空闲；
   否则等本轮结束）。迁移与一次性过渡期间不应有半批消费。
3. **先升级 Performance（研究侧）**：换新 CLI/镜像后跑一次摄入（可先 `--no-publish`）。
   迁移 0003 是纯增量（可空列 + 默认值），旧格式分片仍按旧规则消费，此步不改变对外快照。
4. **再升级 Shadow**：启动新 Shadow。`migrate()` 先应用 0005（列 + 回填 + 序列推进），
   再在独立事务里应用 0006（建索引）；完成后立即 `CHECKPOINT`（见 8.2）。首 tick 进入
   一次性过渡，把 v1 跳过的行重排到下限之上，随后按 seq 正常导出。
5. **恢复 Performance 摄入**：确认 Shadow 已产出至少一个带 `export_seq` 的分片后，
   恢复 cron/流式摄入；观察它按 seq 严格递增消费、`late_rows_seen` 正常累加。
6. 验收：停引擎后 `weatherquant-shadow export-verify` 退出码 0、`reconcile()`
   consistent=True、Dashboard 新鲜度不变；再跑一次迁移确认无重复应用。

### 8.2 迁移后立即 CHECKPOINT（WQN-20 收尾）

`migrate()` 在应用完任一迁移后执行 `CHECKPOINT`，把 0006 的 `CREATE INDEX` 等 DDL 从
WAL 折进主库文件。否则"迁移已提交、首次 checkpoint 前进程被 kill -9"会留下 `.wal`，
只读打开无法回放（`Could not write file ... Bad file descriptor`），Shadow 启动时用于
识别外来库的只读探针会永久失败，引擎再也起不来。该 CHECKPOINT 与迁移在同一次启动中
完成，且幂等：已迁移库再启动一次不重放、不报错、不改变数据。所有 migrations 目录
（主库、shadow、performance、paper、research）共用同一个 `migrate()`，因此同时生效。

### 8.3 回滚

- **整体回滚（推荐，支持路径）**：按相反顺序成对回滚——先停摄入、停 Shadow；
  回滚 Shadow 镜像；回滚 Performance 镜像；启动旧 Shadow；恢复旧摄入。schema 不回滚：
  0003/0005/0006 全是纯增量（可空列、序列、索引），旧代码读新 schema 安全；
  v2 `state.json` 的未知键会被旧代码原样保留（详见 §5）。
- **只回滚一侧**只允许作为短窗口应急：只回滚 Performance，旧消费者按业务时间校验新
  seq 行，迟到 REPLAY 会再次被判回退；只回滚 Shadow，新消费者可以读 v1 分片，但迟到行
  缺陷重新出现。两者都不能作为长期状态，必须尽快补齐成对升级或成对回滚。
- 如果旧 Performance 在回滚窗口内读过新格式分片并在状态库留下分片 sha 记录，按
  `docs/performance-decouple-runbook.md` 重建状态库，不要手改状态。
- **不需要数据库回滚**。若必须回到旧 schema，唯一支持方式是从停机备份恢复
  `shadow.duckdb`，代价是丢失备份点之后的决策。


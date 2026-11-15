# 备份恢复演练与 DuckDB/WAL 生命周期台账（WQN-22）

2026-10-26。脚本：`scripts/backup_recovery_drill.py`；关联：
[`memory-acceptance.md`](memory-acceptance.md)（容器/OOM 门禁）、
`docs/unraid-collector-deployment.md` §9（备份由采集器自身执行）。

一个从未被恢复过的备份只是希望，不是能力。2026-10-17 的事故还证明备份
周边的管线会独立失效：备份挂起 3 小时、以 `.incomplete-…` 被放弃（磁盘上
残留 3.6 GB），15 分钟后服务劣化。本演练把备份的完整生命周期在**副本**上
跑一遍，逐阶段计时、逐阶段判定，全部结果进一份 JSONL 台账。

---

## 1. 演练的六个阶段

| 阶段 | 做什么 | 判定（全部实测） |
|---|---|---|
| `restore` | 源库字节复制 → 打开恢复副本 | 恢复点比对**全量**：schema 迁移版本、**所有表**的逐表行数、逐表内容哈希（`sum(hash(全列))`，与行序无关）、逐表列签名（列名/类型/可空，空表缺列也能发现）、决策水位（shadow 库取 `shadow_decisions.decision_time_utc`，采集库取 `price_history.ts` / `order_books.fetched_at_utc`）——恢复副本必须与源完全一致；缺表、多表、少行、内容变化、列变化分别报出表名与差异 |
| `backup_cycle` | 从已恢复的副本生成备份，再恢复到新目录 | 逐表校验备份前的恢复点，覆盖采集运行记录、事件和迁移台账。 |
| `clean_shutdown` | 优雅 `close()` | 关闭后 `.wal` 必须为零字节（被 checkpoint 折叠） |
| `wal_recovery` | 预算化写入子进程提交若干批后 SIGKILL（容器 OOM/断电留下的状态） | 重开重放：已提交前缀零丢失零撕裂；`CHECKPOINT` 后 WAL 折叠 |
| `slow_io` | 在 cgroup `io.max`（4 MiB/s）限速下重跑 restore 并比对恢复点 | **只有实际测得限速下的恢复写入字节（cgroup `io.stat` 的 `wbytes`）才算 pass**；字节为 0 记 fail。无 cgroup v2/root 记 `skip` 并写明原因。`--require-slow-io`（严格模式）下 skip 视为整体失败；NAS 的 FUSE 行为需单独观察 |
| `incomplete_files` | 种入 `.incomplete-*` 文件（含 2026-10-17 同名残留） | 备份发现**永不**把 `.incomplete-` 当恢复候选；残留文件按体积列出供清理 |

## 2. 生命周期台账

每次运行向 `--ledger`（默认在工作目录旁创建 `<work-name>-recovery-drills.jsonl`）追加一行：

```json
{
  "drill_id": "20261025T183008Z-8f3c2a",
  "tool_version": "recovery-drill-3",
  "strict": false,
  "started_at_utc": "…", "ended_at_utc": "…",
  "source": {"origin", "synthesized", "sha256", "bytes"},
  "recovery_point": {"tables": {"…": {"rows", "digest", "columns"}}, "schema_version",
                      "duckdb_version", "watermark": {"source", "value", "name"}},
  "stages": [{"name", "status", "duration_ms", "detail", "evidence": {…}}],
  "verdict": "PASSED"
}
```

要点：

- **恢复点哈希只在同一 duckdb 版本内可比**（`hash()` 的实现细节不跨版本
  承诺），所以 `duckdb_version` 随行记录——没人能把旧台账的数字拿来和新
  运行直接比；
- 水位在 UTC 钉死的会话里读取，同一时刻在任何机器上渲染成同一字符串；
- **`strict` 记录本次是否要求实测慢盘**，与 `verdict` 一起回答「这次通过
  是否包含慢盘证据」；
- **`evidence` 是结构化测量值**，不是从 detail 里反推：`restore` 记表数与
  耗时、`backup_cycle` 记备份文件名/字节数/差异、`clean_shutdown` 记关闭
  前后的 WAL 字节、`wal_recovery` 记已提交批次数、杀进程时 WAL 字节与
  checkpoint 后字节、`slow_io` 记限速设备、实测写入字节与耗时；
- 台账按次追加、永不覆盖，即 WQN-22 要求的生命周期记录。

## 3. 严格模式、退出码与如何读报告

| 运行方式 | `slow_io` 实测 pass | `slow_io` skip | 任一阶段 fail |
|---|---|---|---|
| 默认（非严格） | 整体 PASSED，退出码 0 | 阶段如实记 `skip`，整体 PASSED，退出码 0；报告不会把 skip 写成 pass | 整体 FAILED，退出码 1 |
| `--require-slow-io`（严格） | 整体 PASSED，退出码 0 | 整体 FAILED，退出码 1，并打印 `required slow_io stage did not pass (status: skip)` | 整体 FAILED，退出码 1 |

- **退出码与台账一致**：0 = `verdict: PASSED`，1 = `verdict: FAILED`，
  2 = 用法错误（例如 `--work` 已存在、外部 `--source` 未声明
  `--source-is-copy`）；人工验收以台账为准，文本报告只做速览。
- 判读顺序：先看 `verdict` 与 `strict`，再看每个阶段的 `status`，最后看
  `evidence`/`detail` 里的数字。`not_applicable` 表示该阶段对这类库不适用
  （shadow 库不跑采集器写路径），既不是 pass 也不是 skip。

## 4. 本机运行记录（2026-10-26，`recovery-drill-1` 时期）

**完整演练（sudo，含 slow_io）——RESULT: PASSED**

```
[pass] restore           131 ms  copy 0.0s, verify 0.1s, 14 tables, watermark=2026-10-26 02:30:07+00:00
[pass] backup_cycle     4349 ms  backup verified=True bytes=3420160; chain restore verified (12 tables; …excluded)
[pass] clean_shutdown   1785 ms  wal 101115 bytes folded away by graceful close
[pass] wal_recovery     1265 ms  kill left 6 journalled batches, max_batch=6, wal 265736 -> 0 bytes
[pass] slow_io          1218 ms  restore under 4MiB/s io.max completed in 1.2s, recovery point verified (14 tables)
[pass] incomplete_files    5 ms  discovery ignored 2 .incomplete files (143 bytes reported for cleanup)
source: 4.0 MiB, sha256=4ab185c0c06c663f…；recovery point: 14 tables
```

无 root 时 `slow_io` 如实记 `skip`（单测断言了这一点，不伪造）。

**测试**：`tests/test_backup_recovery_drill.py` 从 `main()`/CLI 覆盖完整演练
与台账形态、shadow 形状库的水位分支（真实 shadow 迁移构造）、外部 source
必须显式 `--source-is-copy`、`.incomplete` 发现规则；缺表/少行/内容变化/
多表/空表缺列分别经恢复阶段失败并报出差异；严格模式下 `slow_io` skip 导致
整体失败、非严格模式保持 skip 不冒充 pass；`io.stat` 实测字节为 0 时
`slow_io` 记 fail，只有伪造出实测字节的用例才 pass；恢复后的 DuckDB 与
WAL 回放证据可从台账读回。全套 `uv run pytest -q` 见 PR 描述。

## 5. 定期执行（NAS，需人工排程）

```bash
# PUBLISHED_BACKUP 指向已经发布的备份文件，DRILL_DISK 是隔离的测试磁盘目录。
run=$(mktemp -d "${DRILL_DISK:?}/wqn22-XXXXXXXX") || exit 1
cp "${PUBLISHED_BACKUP:?}" "$run/source.duckdb" || exit 1
sudo -E uv run python scripts/backup_recovery_drill.py \
    --work "$run/work" --source "$run/source.duckdb" --source-is-copy \
    --require-slow-io --ledger "$DRILL_DISK/recovery-drills.jsonl"
# 台账中的 slow_io 须为 pass，保留实际设备和运行结果。
```

观察项（事故先兆，顺带核对）：

```bash
ls -la /mnt/user/appdata/weatherquant-next/backups/ | grep .incomplete
# 任何 .incomplete- 残留 = 上一次备份挂起，立即上报（2026-10-17 的形态）
```

## 6. 边界（不要越过的三根线）

1. **演练只碰副本。** `--source` 若在 `--work` 之外必须显式
   `--source-is-copy`——对正在被写的库做字节复制得到的是撕裂文件，护栏
   的存在就是为了这个；对生产库的恢复写回**完全不在本脚本的能力范围内**，
   属单独授权动作。
2. **生产恢复必须单独授权**：从备份恢复进 `/mnt/user/appdata/weatherquant-next/`
   是一个需要停机窗口和明确批准的运维动作，演练通过不构成授权。
3. **`not_applicable` 不是 skip**：对 shadow 形状的库，采集器的写路径阶段
   如实记 `not_applicable` 并说明原因——一个对 shadow 库假装跑过 collector
   备份的演练，本身就是演练要消灭的那种假证据。


### 评审修复验证

默认生命周期台账位于工作目录旁的 <work-name>-recovery-drills.jsonl，成功清理及下一次演练均保留并追加它。显式 --ledger 也应位于 --work 之外。

演练只接受新的 `--work` 路径。正式慢 I/O 验收使用 `--require-slow-io`，缺少 Linux cgroup v2 或 root 时，阶段记为 skip，整体验收失败；非严格运行保留 skip 并如实说明，不把它说成 pass。目标设备必须在 `io.stat` 中实际记录恢复写入字节，字节为 0 时该阶段记 fail 而不是 pass。恢复点比对覆盖全部表的行数、内容哈希与列签名，差异写入 `detail` 与 `evidence.differences`。NAS 的 FUSE 挂载仍需现场验证。

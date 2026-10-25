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
| `restore` | 源库字节复制 → 打开恢复副本 | 恢复点比对：schema 迁移版本、逐表行数、逐表内容哈希（`sum(hash(全列))`，与行序无关）、决策水位（shadow 库取 `shadow_decisions.decision_time_utc`，采集库取 `price_history.ts` / `order_books.fetched_at_utc`）——恢复副本必须与源**完全一致** |
| `backup_cycle` | 在恢复副本上按生产节奏写入并执行 `Writer.backup()`，再把**那份备份**恢复到第三个目录、再次比对恢复点 | backup verified=True；backup→restore→verify 全链一致（`collector_runs`/`collector_events` 例外：`close()` 会在快照之后合法地写入收尾状态，比对时按名排除并记录原因） |
| `clean_shutdown` | 优雅 `close()` | 关闭后 `.wal` 必须为零字节（被 checkpoint 折叠） |
| `wal_recovery` | 预算化写入子进程提交若干批后 SIGKILL（容器 OOM/断电留下的状态） | 重开重放：已提交前缀零丢失零撕裂；`CHECKPOINT` 后 WAL 折叠 |
| `slow_io` | 在 cgroup `io.max`（4 MiB/s）限速下重跑 restore 并比对恢复点 | **正确性不因慢盘改变**；耗时如实记录。内核 io 控制器就是 NAS 慢盘的同类机制，无需 FUSE。需要 root；拿不到就记 `skip`，**绝不伪造** |
| `incomplete_files` | 种入 `.incomplete-*` 文件（含 2026-10-17 同名残留） | 备份发现**永不**把 `.incomplete-` 当恢复候选；残留文件按体积列出供清理 |

## 2. 生命周期台账

每次运行向 `--ledger`（默认 `<work>/recovery-drills.jsonl`）追加一行：

```json
{
  "drill_id": "20261025T183008Z-8f3c2a",
  "tool_version": "recovery-drill-1",
  "started_at_utc": "…", "ended_at_utc": "…",
  "source": {"origin", "synthesized", "sha256", "bytes"},
  "recovery_point": {"tables": {"…": {"rows", "digest"}}, "schema_version",
                      "duckdb_version", "watermark": {"source", "value", "name"}},
  "stages": [{"name", "status", "duration_ms", "detail"}],
  "verdict": "PASSED"
}
```

要点：

- **恢复点哈希只在同一 duckdb 版本内可比**（`hash()` 的实现细节不跨版本
  承诺），所以 `duckdb_version` 随行记录——没人能把旧台账的数字拿来和新
  运行直接比；
- 水位在 UTC 钉死的会话里读取，同一时刻在任何机器上渲染成同一字符串；
- 台账按次追加、永不覆盖，即 WQN-22 要求的生命周期记录。

## 3. 本机运行记录（2026-10-26）

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

**测试**：`tests/test_backup_recovery_drill.py` 5 项——完整演练 + 台账形态、
shadow 形状库的水位分支（真实 shadow 迁移构造）、外部 source 必须显式
`--source-is-copy`、`.incomplete` 发现规则、恢复点哈希能抓到行数之外的
内容变化。全套 `uv run pytest -q` 见 PR 描述。

## 4. 定期执行（NAS，需人工排程）

```bash
# 每周一次，对生产备份目录里最新一份 weatherquant-*.duckdb 的**副本**：
cp /mnt/user/appdata/weatherquant-next/backups/<latest>.duckdb /tmp/drill-src.duckdb
uv run python scripts/backup_recovery_drill.py \
    --work /tmp/wq-drill-$(date -u +%Y%m%d) \
    --source /tmp/drill-src.duckdb --source-is-copy \
    --ledger /mnt/user/appdata/weatherquant-next/recovery-drills.jsonl
rm /tmp/drill-src.duckdb
# 判定：RESULT: PASSED，台账新增一行 verdict=PASSED；
# 台账里 slow_io 必须 pass（NAS 上有 root）或明确 skip 并说明原因
```

观察项（事故先兆，顺带核对）：

```bash
ls -la /mnt/user/appdata/weatherquant-next/backups/ | grep .incomplete
# 任何 .incomplete- 残留 = 上一次备份挂起，立即上报（2026-10-17 的形态）
```

## 5. 边界（不要越过的三根线）

1. **演练只碰副本。** `--source` 若在 `--work` 之外必须显式
   `--source-is-copy`——对正在被写的库做字节复制得到的是撕裂文件，护栏
   的存在就是为了这个；对生产库的恢复写回**完全不在本脚本的能力范围内**，
   属单独授权动作。
2. **生产恢复必须单独授权**：从备份恢复进 `/mnt/user/appdata/weatherquant-next/`
   是一个需要停机窗口和明确批准的运维动作，演练通过不构成授权。
3. **`not_applicable` 不是 skip**：对 shadow 形状的库，采集器的写路径阶段
   如实记 `not_applicable` 并说明原因——一个对 shadow 库假装跑过 collector
   备份的演练，本身就是演练要消灭的那种假证据。

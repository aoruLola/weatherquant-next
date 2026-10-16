# Plan: 摄取/发布解耦 + 多层新鲜度语义

## 目标（用户指令）

1. **摄取/发布解耦**：5 分钟摄入、20 分钟发布，两者共享互斥机制，避免状态库并发写入。
2. **多层新鲜度**：区分 Shadow watermark、Performance consumed watermark、Snapshot published_at、数据实际年龄。
3. **结算修订触发重发**：即使没有新决策，结算修订也要能发布。
4. **避免无意义发布**：状态没变时不发布。
5. **测试先行**：隔离测试 `--no-publish` / `--no-ingest`，不改生产 cron。

## 现状分析

### 互斥机制
- **wq-performance-incremental.sh** 用 `flock -n -E 75 /var/lock/weatherquant-performance.lock` 包住整个 `docker run`。
- 锁在 host 上，容器内的两次调用**不共享锁** —— 要么改成 host 上调度两个脚本（共享锁），要么把锁移进容器（DuckDB 文件锁）。
- DuckDB 自己有文件锁，但同一进程打开两次会死锁 —— 现在的设计是"一个进程从头到尾"，解耦后要确保同一时间只有一个进程打开状态库。

### CLI 参数
- 已有 `--no-ingest`（line 460, cli.py），传给 `build_performance_streaming(ingest=not args.no_ingest)`。
- **缺 `--no-publish`**：函数接受 `publish: bool = True`（streaming.py:1397），但 CLI 未暴露。

### Watermark 记录
- `build_performance_streaming` 返回 `{"watermark": build.watermark, "ingest": ingest_result.as_dict(), ...}`（streaming.py:1558）。
- `build.watermark` 是**已消费的 export watermark**（`[timestamp, hash]`），写进 manifest.json。
- `published_at_utc` 是**发布时间戳**（streaming.py:1303 的 `now`），也写进 manifest。
- **缺失**：没有 "Shadow export 自己的最新 watermark"（即导出端发布了多少，而不是我消费了多少）。

### 新鲜度判断
- `performance.py:141` 调 `freshness_of(as_of, now, stale_after_seconds=stale + 60.0)`，其中 `stale` 从 `payload.stale_after_seconds` 读（默认 300），**再加 60 秒宽限**。
- `as_of` 是 manifest 的 `published_at_utc`（发布时间，不是数据时间）。
- **问题**：`published_at_utc` 只能说"这份快照是什么时候发的"，说不了"快照里的数据有多新"。解耦后，20 分钟一次发布，`published_at_utc` 会在前 19 分钟里显示"很新"，即使数据本身已经积压。

### 结算修订触发
- `--no-ingest` 会跳过摄入，但**仍会走 `build_streaming_payloads()` 和 `publish_performance()`**（streaming.py:1522-1553）。
- 结算修订传进来后，`store.upsert_settlements(payload)`（streaming.py:1520）更新状态库，然后重新计算统计。
- **已支持**：结算修订会触发 `--no-ingest` 的重发。

### 避免无意义发布
- `streaming_generation_id()` 对**输入**算哈希（游标、计数器、结算、参数），而不是对输出（streaming.py:1283-1343）。
- 两次运行如果输入相同，`generation_id` 的内容哈希相同（时间戳前缀不同）。
- **没有**"内容哈希和 current 相同就不发"的逻辑 —— 每次都发一个新世代。

## 方案设计

### 1. 添加 `--no-publish` CLI flag

**文件**：`src/weatherquant/cli.py`

在 `def _subparser_build_performance` 里加：

```python
p.add_argument(
    "--no-publish",
    action="store_true",
    help="consume what is new but do not publish a snapshot (摄入但不发布)",
)
```

在 `_build_performance_streaming` 里传：

```python
result = build_performance_streaming(
    ...
    publish=not args.no_publish,  # 新增
    ...
)
```

### 2. 多层 watermark 和新鲜度语义

**目标**：区分四个时间点 / 水位：

| 层 | 含义 | 来源 | 现在记录了吗 |
|---|---|---|---|
| **Shadow Export watermark** | 导出端最新发布的分片水位 | Shadow 的 `decisions-export/current.json` | ❌ 没读 |
| **Performance consumed watermark** | Performance 已消费到的水位 | 状态库 `state.last_end_cursor` | ✅ 写进 manifest |
| **Snapshot published_at** | 这份快照是什么时候发布的 | `build_performance_streaming` 的 `now` | ✅ manifest |
| **Data age** | 快照里最新数据的实际时间 | consumed watermark 的时间戳部分 | ✅ 在 watermark 里 |

**改动**：

**文件**：`src/weatherquant/performance/streaming.py`

在 `build_performance_streaming` 开头，读取 Shadow 的 `current.json`：

```python
export_path = Path(export)
export_current_path = export_path / "current.json"
export_watermark = None
if export_current_path.exists():
    try:
        export_current = json.loads(export_current_path.read_text())
        export_watermark = export_current.get("watermark")
    except Exception:
        pass
```

在返回值里加上：

```python
result = {
    ...
    "export_watermark": export_watermark,  # 新增
}
```

**文件**：`src/weatherquant/performance/snapshot.py`

`publish_performance()` 的 manifest 里加：

```python
manifest = {
    ...
    "export_watermark": export_watermark,  # 从调用者传进来
}
```

**文件**：`src/weatherquant/dashboard/performance.py`

`PerformanceState.snapshot()` 里，从 manifest 读 `export_watermark` 和 `watermark`，计算**数据积压**（export 比 consumed 新多少）和**发布延迟**（published_at 比 consumed 新多少）。

新鲜度判据改为：

- `as_of` 用 `consumed_watermark[0]`（数据时间），而不是 `published_at`（发布时间）。
- `stale_after_seconds` 不再硬编码 300 + 60，改成从 manifest 读，或根据调度间隔动态算（如 20 分钟发布间隔 → `stale_after_seconds=1200 + 60`）。

### 3. 避免无意义发布

**策略**：`--no-publish` 模式下，如果 `generation_id` 的内容哈希（12 位十六进制）与 `current.json` 指的世代相同，就不发布。

**文件**：`src/weatherquant/performance/streaming.py`

在 `build_performance_streaming` 里，发布前检查：

```python
if publish:
    # 读取 current generation 的内容哈希
    current_pointer_path = Path(out_dir) / "current.json"
    current_hash = None
    if current_pointer_path.exists():
        try:
            current_gen_id = json.loads(current_pointer_path.read_text()).get("generation_id")
            if current_gen_id and "-" in current_gen_id:
                current_hash = current_gen_id.split("-", 1)[1]
        except Exception:
            pass
    
    new_hash = generation_id.split("-", 1)[1] if "-" in generation_id else None
    
    if new_hash == current_hash and not settlements_replace:
        # 内容没变，跳过发布
        if progress is not None:
            progress(f"skipped: content unchanged (hash {new_hash})")
        written = []
    else:
        written = publish_performance(...)
```

**问题**：这个逻辑在 `--no-ingest + 结算修订` 时也会跳过，但结算修订**应该**触发重发。所以条件是 `if new_hash == current_hash and not settlements_replace and not (结算有变)`。

**更简单的方案**：在 `--no-publish` 时不做这个检查（摄入模式下每次都摄入，让发布模式决定要不要发），只在 `publish=True` 且 `ingest=False` 时检查（纯发布模式，如果内容没变就不发）。

### 4. 共享互斥机制

**现状**：host 上的 flock 只保护一个 runner。解耦后有两个 runner（摄入 + 发布），要么：

**方案 A**：两个 runner 共享同一个 host 锁文件。

```bash
# /boot/config/wq-performance-ingest.sh
flock -n -E 75 /var/lock/weatherquant-performance.lock \
  docker run ... --no-publish
  
# /boot/config/wq-performance-publish.sh
flock -n -E 75 /var/lock/weatherquant-performance.lock \
  docker run ... --no-ingest
```

**方案 B**：依赖 DuckDB 的文件锁（容器里挂载同一个状态库文件）。

DuckDB 会拒绝并发写入。但 `--no-publish` 也会打开状态库（摄入要写），`--no-ingest` 也会打开（读取状态重新计算）—— 所以无论如何都是互斥的，只要两个容器挂同一个文件。

**推荐**：方案 A（host 锁），因为它更明确、更快失败（exit 75），而不是等 DuckDB 超时。

### 5. 中断恢复与水位一致性

**现状**：状态库的 watermark 在**摄入成功后**才提交（streaming.py 的 `store.commit_ingest()`）。

**解耦后**：

- 摄入模式（`--no-publish`）：摄入 → 提交 watermark → 不发布。
- 发布模式（`--no-ingest`）：读状态库 → 重新计算 → 发布。

**中断场景**：

| 时刻 | 摄入 | 发布 | 状态 |
|---|---|---|---|
| T0 | 摄入 5 个分片，watermark 前进到 part-385 | 未跑 | 状态库已提交 385，快照还是 380 |
| T1 | 未跑 | 发布，读到 385 | 快照更新到 385 |
| 崩溃 | 摄入到一半，watermark 未提交 | — | **状态库回到上次提交，快照可能领先** |

**问题**：如果发布在"摄入未提交"时读状态库，会读到旧 watermark，发布一个**水位倒退**的快照。

**解决**：发布模式不能和摄入模式并发。两者必须互斥（已由 flock 保证）。

**测试点**：

1. 摄入 A 跑到一半被 kill（未提交），摄入 B 再跑 → watermark 从上次提交处恢复，不倒退。
2. 摄入提交后，发布读到新 watermark → 快照 watermark 前进。
3. 发布和摄入同时启动 → 一个拿到锁，另一个 exit 75。

### 6. 测试脚本设计

**文件**：`scripts/test_decouple_performance.sh`

在隔离环境（本机或 NAS 的测试目录）：

```bash
# 准备：复制生产导出的前 10 个分片到测试目录
# 准备：空的状态库

# Test 1: 摄入不发布
docker run ... --no-publish --max-parts-per-run 5
# 验证：状态库有 watermark，out/ 目录没有新世代

# Test 2: 发布不摄入
docker run ... --no-ingest
# 验证：out/ 有新世代，current.json 更新

# Test 3: 摄入 + 发布（传统模式）
docker run ... --max-parts-per-run 5
# 验证：状态库前进，out/ 有新世代

# Test 4: 互斥
(docker run ... --no-publish --max-parts-per-run 100 &) ; sleep 2 ; docker run ... --no-ingest
# 验证：第二个 exit 75（或 DuckDB 拒绝）

# Test 5: 结算修订触发重发
# 写一个新结算 JSON，跑 --no-ingest --settlements ...
# 验证：新世代的内容哈希变了（因为结算变了）

# Test 6: 内容没变不发布
# 跑两次 --no-ingest（没有结算修订）
# 验证：第二次没有新世代（或者有，但内容哈希相同）
```

**测试清单**（对应用户要求 §4、§5）：

| # | 测试 | 验证点 |
|---|---|---|
| 1 | `--no-publish` | 状态库前进、无新世代 |
| 2 | `--no-ingest` | 状态库不动、有新世代 |
| 3 | 传统模式 | 两者都做 |
| 4 | 互斥（flock） | 第二个进程 exit 75 |
| 5 | 结算修订 + `--no-ingest` | 触发重发，哈希变 |
| 6 | 无变化 + `--no-ingest` | 不发布（或哈希相同） |
| 7 | 摄入崩溃恢复 | kill 未提交的摄入，重跑不倒退 |
| 8 | 多层 watermark | manifest 里有 export / consumed / published_at |

## 交付物

### 代码改动

1. `src/weatherquant/cli.py` +2 lines: `--no-publish` flag
2. `src/weatherquant/performance/streaming.py` +30 lines: 读 export watermark、避免无意义发布逻辑
3. `src/weatherquant/performance/snapshot.py` +5 lines: manifest 加 `export_watermark`
4. `src/weatherquant/dashboard/performance.py` +20 lines: 多层新鲜度计算
5. `src/weatherquant/dashboard/contracts.py` +5 lines: Freshness schema 加字段

### 测试

1. `scripts/test_decouple_performance.sh`：8 个场景的隔离测试
2. `tests/test_performance_decouple.py`：单元测试（摄入 / 发布分离、watermark 记录、新鲜度计算）

### 文档

1. `docs/performance-streaming-rollout.md` §4：解耦调度方案，包括两个 runner 脚本、cron 条目、回滚步骤
2. `docs/performance-data-contract.md`：更新 manifest schema，加 `export_watermark`、多层新鲜度语义

### 生产 cron 变更方案（§6）

**新增两个 runner**：

```bash
# /boot/config/wq-performance-ingest.sh（每 5 分钟）
flock -n -E 75 /var/lock/weatherquant-performance.lock \
  nice -n 10 ionice -c2 -n7 \
  docker run ... --no-publish --max-parts-per-run 40

# /boot/config/wq-performance-publish.sh（每 20 分钟）
flock -n -E 75 /var/lock/weatherquant-performance.lock \
  nice -n 10 ionice -c2 -n7 \
  docker run ... --no-ingest
```

**cron 条目**（两个）：

```
# 摄入：每 5 分钟
*/5 * * * * /bin/bash /boot/config/wq-performance-ingest.sh

# 发布：每 20 分钟（0, 20, 40）
3,23,43 * * * * /bin/bash /boot/config/wq-performance-publish.sh
```

注意：`3,23,43` 是为了避开 `:00` 和 `:30`（全球流量高峰），且和摄入错开（摄入在 0/5/10/..., 发布在 3/23/43）。

**回滚步骤**：

1. 删除两个新 runner 的 cron 条目（`/etc/cron.d/weatherquant-performance-ingest` / `-publish`）
2. 恢复原来的单 runner 条目（5 分钟一次，不带 `--no-publish` / `--no-ingest`）
3. `crond reload`

**验证步骤**：

1. 部署前：在隔离环境跑完 8 个测试，全通过。
2. 部署时：先只部署摄入（5 分钟），观察 30 分钟，状态库前进、无新世代。
3. 再部署发布（20 分钟），观察 1 小时，世代按 20 分钟节奏产生。
4. 监控：世代产量从 288/天 → 72/天，freshness 在发布后 60s 内显示 fresh。

## 风险与开放问题

1. **`stale_after_seconds` 要改成多少**？现在 300 + 60，解耦后发布间隔 20 分钟 = 1200s。改成 1200 + 60 吗？还是根据调度间隔动态算？
2. **世代产量降到 72/天后，清理策略的 `--keep-recent 14` 要不要调**？72/天 = 14 个世代只覆盖约 4.7 小时，可能要调到 28（约 9.3 小时）或更多。
3. **摄入和发布的相位差**？如果摄入在 `:00/:05/:10/...`，发布在 `:03/:23/:43`，发布会读到"3 分钟前摄入的"数据，还是"23 分钟前的"？要不要让发布在摄入后立刻跑一次（如 `:05` 摄入完，`:06` 发布），然后再等 20 分钟？

## 时间估算

- 代码改动：2 小时
- 测试脚本：1 小时
- 隔离测试执行：1 小时
- 文档更新：30 分钟
- 总计：**4.5 小时**

不包括 CR / 部署 / 观察期。

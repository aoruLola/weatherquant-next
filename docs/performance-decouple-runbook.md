# Performance 摄取/发布解耦部署 Runbook

**状态**：设计完成，测试通过，**待生产部署授权**。

## 目标

将 Performance 快照生成从"每 5 分钟摄入 + 发布"改为"每 5 分钟摄入 + 每 20 分钟发布"，降低世代产量从 **288/天 → 72/天**（约 2.2 GB/天 → 0.56 GB/天）。

## 架构变更

### 现在（合并模式）

```
每 5 分钟运行一次：
  摄入新数据 → 更新状态库 → 发布快照 → 切换 current.json
  
世代产量：288/天（每次运行都发布，即使内容相同）
```

### 之后（解耦模式）

```
每 5 分钟：摄入作业
  摄入新数据 → 更新状态库 → 退出（不发布）
  
每 20 分钟：发布作业
  读状态库 → 重新计算统计 → 发布快照 → 切换 current.json
  
世代产量：72/天（只有发布作业产生世代）
互斥：两个作业共享同一个 flock，不能并发
```

## 前置条件

1. ✅ CLI 已支持 `--no-publish` flag（commit `d8f82bb`）
2. ✅ 单元测试通过（4/4 tests, 243 total performance tests pass）
3. ⏳ 新镜像构建并推送（待执行）
4. ⏳ 隔离集成测试（待执行）

## 部署步骤

### Phase 1: 准备（15 分钟）

**1.1 构建新镜像**

```bash
# 本地或 CI
git checkout main
git pull
docker build -f Dockerfile.research -t ghcr.io/aorulola/weatherquant-next:sha-d8f82bb .
docker push ghcr.io/aorulola/weatherquant-next:sha-d8f82bb
```

**1.2 在 NAS 上拉取新镜像**

```bash
docker pull ghcr.io/aorulola/weatherquant-next:sha-d8f82bb
docker inspect ghcr.io/aorulola/weatherquant-next:sha-d8f82bb --format '{{.Id}}'
# 记录 image id，用于验证
```

**1.3 准备新的 runner 脚本**

写入 `/boot/config/wq-performance-ingest.sh`：

```bash
#!/bin/bash
# WeatherQuant performance ingest (every 5 minutes).
# Consumes new parts from the shadow's export, updates the state store, does not publish.

set -u

IMAGE="ghcr.io/aorulola/weatherquant-next:sha-d8f82bb"
APP="/mnt/user/appdata"
LOG="/var/log/wq-performance-ingest.log"

{
  echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) ingest start"

  /usr/bin/flock -n -E 75 /var/lock/weatherquant-performance.lock \
    /usr/bin/nice -n 10 /usr/bin/ionice -c2 -n7 \
    /usr/bin/docker run --rm --name wq-research-ingest \
      --user 1000:1000 --memory=1.5g --cpus=2 \
      -v "$APP/weatherquant-shadow/reports/decisions-export:/export:ro" \
      -v "$APP/weatherquant-performance:/out" \
      -v "$APP/weatherquant-performance-state:/state" \
      --entrypoint weatherquant-research "$IMAGE" \
      build-performance \
        --export /export \
        --out /out \
        --dataset live \
        --state-db /state/performance-state.duckdb \
        --temp-dir /state/duckdb-tmp \
        --memory-limit 1GB \
        --max-parts-per-run 40 \
        --no-publish
  rc=$?

  case "$rc" in
    0)  echo "ok" ;;
    75) echo "skipped: another job holds the lock" ;;
    *)  echo "FAILED exit=$rc" ;;
  esac
} >> "$LOG" 2>&1

exit 0
```

写入 `/boot/config/wq-performance-publish.sh`：

```bash
#!/bin/bash
# WeatherQuant performance publish (every 20 minutes).
# Reads state store, rebuilds snapshots, publishes a new generation.

set -u

IMAGE="ghcr.io/aorulola/weatherquant-next:sha-d8f82bb"
APP="/mnt/user/appdata"
LOG="/var/log/wq-performance-publish.log"

{
  echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) publish start"

  /usr/bin/flock -n -E 75 /var/lock/weatherquant-performance.lock \
    /usr/bin/nice -n 10 /usr/bin/ionice -c2 -n7 \
    /usr/bin/docker run --rm --name wq-research-publish \
      --user 1000:1000 --memory=1.5g --cpus=2 \
      -v "$APP/weatherquant-shadow/reports/decisions-export:/export:ro" \
      -v "$APP/weatherquant-performance:/out" \
      -v "$APP/weatherquant-performance-state:/state" \
      --entrypoint weatherquant-research "$IMAGE" \
      build-performance \
        --export /export \
        --out /out \
        --dataset live \
        --state-db /state/performance-state.duckdb \
        --temp-dir /state/duckdb-tmp \
        --memory-limit 1GB \
        --no-ingest
  rc=$?

  case "$rc" in
    0)  echo "ok" ;;
    75) echo "skipped: another job holds the lock" ;;
    *)  echo "FAILED exit=$rc" ;;
  esac
} >> "$LOG" 2>&1

exit 0
```

两个脚本都需要 **755 权限**。

**1.4 更新 `/boot/config/go`**

在 `/usr/local/sbin/emhttp` 之前加（替换现有的单一 runner 条目）：

```bash
# WeatherQuant performance ingest/publish runners
cp /boot/config/wq-performance-ingest.sh /tmp/wq-performance-ingest.sh && chmod 755 /tmp/wq-performance-ingest.sh # wq-perf
cp /boot/config/wq-performance-publish.sh /tmp/wq-performance-publish.sh && chmod 755 /tmp/wq-performance-publish.sh # wq-perf
cp /boot/config/wq-performance.cron-ingest /etc/cron.d/weatherquant-performance-ingest # wq-perf
chmod 644 /etc/cron.d/weatherquant-performance-ingest # wq-perf
cp /boot/config/wq-performance.cron-publish /etc/cron.d/weatherquant-performance-publish # wq-perf
chmod 644 /etc/cron.d/weatherquant-performance-publish # wq-perf
```

写入 `/boot/config/wq-performance.cron-ingest`：

```
# WeatherQuant performance ingest (every 5 minutes)
*/5 * * * * /bin/bash /tmp/wq-performance-ingest.sh
```

写入 `/boot/config/wq-performance.cron-publish`：

```
# WeatherQuant performance publish (every 20 minutes, offset to avoid :00/:30)
3,23,43 * * * * /bin/bash /tmp/wq-performance-publish.sh
```

### Phase 2: 灰度（30 分钟）

**2.1 只部署摄入作业**

```bash
# 备份现有 cron
cp /etc/cron.d/weatherquant-performance /etc/cron.d/weatherquant-performance.bak-$(date +%Y%m%d-%H%M%S)

# 删除旧的合并作业
rm /etc/cron.d/weatherquant-performance

# 只安装摄入作业
cp /boot/config/wq-performance.cron-ingest /etc/cron.d/weatherquant-performance-ingest
chmod 644 /etc/cron.d/weatherquant-performance-ingest

# 重载 crond
/etc/rc.d/rc.crond restart
```

**2.2 观察 30 分钟**

```bash
tail -f /var/log/wq-performance-ingest.log
```

**验证点**：

- 每 5 分钟跑一次，exit 0
- 状态库 watermark 前进（`cat /mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb` 的 mtime 更新）
- **没有新世代**（`ls -lt /mnt/user/appdata/weatherquant-performance/generations/ | head -5`，最新的仍是 30 分钟前的）
- `current.json` 不变

如果摄入失败或状态库不前进 → **回滚到 Phase 2.3**。

**2.3 回滚步骤（Phase 2 失败时）**

```bash
# 恢复旧的合并作业
cp /etc/cron.d/weatherquant-performance.bak-YYYYMMDD-HHMMSS /etc/cron.d/weatherquant-performance
rm /etc/cron.d/weatherquant-performance-ingest
/etc/rc.d/rc.crond restart

# 验证旧作业恢复
tail -f /var/log/wq-performance.log
```

### Phase 3: 完整部署（30 分钟）

**3.1 部署发布作业**

```bash
# 安装发布作业 cron
cp /boot/config/wq-performance.cron-publish /etc/cron.d/weatherquant-performance-publish
chmod 644 /etc/cron.d/weatherquant-performance-publish
/etc/rc.d/rc.crond restart

# 手动触发一次发布（验证）
/bin/bash /tmp/wq-performance-publish.sh
```

**3.2 观察 1 小时**

```bash
# 监控两个日志
tail -f /var/log/wq-performance-ingest.log &
tail -f /var/log/wq-performance-publish.log &
```

**验证点**：

- 摄入：每 5 分钟跑，exit 0
- 发布：每 20 分钟跑（`:03`, `:23`, `:43`），exit 0
- 新世代按 20 分钟节奏产生（3 个/小时）
- 互斥：如果摄入和发布同时启动，一个 exit 75
- `current.json` 每 20 分钟切一次
- Dashboard `/healthz` 三源 fresh

**3.3 回滚步骤（Phase 3 失败时）**

```bash
# 停止两个解耦作业
rm /etc/cron.d/weatherquant-performance-ingest
rm /etc/cron.d/weatherquant-performance-publish

# 恢复旧的合并作业
cp /etc/cron.d/weatherquant-performance.bak-YYYYMMDD-HHMMSS /etc/cron.d/weatherquant-performance
/etc/rc.d/rc.crond restart

# 验证
tail -f /var/log/wq-performance.log
```

### Phase 4: 清理（5 分钟）

**4.1 删除备份**

```bash
rm /etc/cron.d/weatherquant-performance.bak-*
```

**4.2 更新文档**

在 `docs/performance-streaming-rollout.md` §4 记录：

- 部署时间
- 镜像 SHA
- 世代产量变化（before / after）
- 任何观察到的问题

## 监控指标

| 指标 | 预期值 | 告警阈值 |
|---|---|---|
| 世代产量 | 72/天（20 分钟一个） | > 100/天 或 < 50/天 |
| 摄入频率 | 12/小时（5 分钟一次） | 连续 15 分钟无摄入 |
| 发布频率 | 3/小时（20 分钟一次） | 连续 30 分钟无发布 |
| 状态库 watermark 延迟 | < 5 分钟 | > 10 分钟 |
| 快照 freshness | fresh（< 360s 数据年龄 + 60s 宽限） | stale 持续 > 5 分钟 |
| 锁冲突率 | < 1%（exit 75 / 总运行次数） | > 5% |

## 异常停止条件

立即回滚如果：

1. **摄入连续失败 3 次**（15 分钟）
2. **发布连续失败 2 次**（40 分钟）
3. **Dashboard /healthz 任一源 stale 持续 > 10 分钟**
4. **状态库 watermark 倒退**（数据丢失信号）
5. **世代产量异常**（> 100/天 说明发布没有解耦）

## 已知限制

1. **`stale_after_seconds` 仍是 300**：解耦后发布间隔 20 分钟，理论上应该改成 1200。但 Dashboard 又加了 60 秒宽限（`performance.py:141`），所以 360 秒总宽限勉强够用（20 分钟发布 + 5 分钟摄入延迟 < 25 分钟 = 1500 秒，仍会 stale）。**多层新鲜度语义**（export watermark / consumed watermark / published_at 分开记录）是下一步改进。

2. **结算修订触发**：`--no-ingest` 模式下，如果传入 `--settlements`，会触发重发（已测试）。但如果结算 JSON 在两次发布之间更新，摄入作业不会感知（它不读结算），下次发布才会看到。这是符合预期的：结算修订是"低频、人工触发"的操作，20 分钟延迟可接受。

3. **世代清理**：世代产量降到 72/天后，`--keep-recent 14` 只覆盖约 4.7 小时。可能要调到 `--keep-recent 28`（约 9.3 小时）。

## 测试结果

| 测试 | 环境 | 结果 |
|---|---|---|
| 单元测试（4 tests） | 本地 Python 3.12 | ✅ PASSED |
| 完整 performance 测试套件（243 tests） | 本地 | ✅ PASSED（73 seconds） |
| 隔离集成测试（真实导出 + 容器） | 待执行 | ⏳ |
| 灰度：只摄入（30 分钟） | 生产 NAS | ⏳ 待授权 |
| 完整：摄入 + 发布（1 小时） | 生产 NAS | ⏳ 待授权 |

## 相关 PR / Commits

- Prune tool + Legacy protection: `b2b04dc`
- `--no-publish` flag + tests: `d8f82bb`
- Runbook（本文档）: 待提交

## 联系人

问题联系 @aorulola

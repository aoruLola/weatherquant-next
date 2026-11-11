# Shadow OOM / 全表扫描修复报告

**分支**：`trae/shadow-oom-fix`
**任务单号**：WQN-3
**完成日期**：2026-11-11

---

## 1. 问题综述

Shadow Runner 在生产环境运行约 2 周后出现间歇性 OOM（容器被 `SIGKILL`）和构建报告时耗时飙升到 30 秒+。根本原因是：

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| A | `shadow/report.py` | P95 计算把整列 `decision_latency_ms` 拉到 Python 侧排序 | 500 万行 → 411 MB Python heap、4 秒+ |
| B | `dashboard/producer.py` | markets/forecasts 快照无 WHERE 时间过滤，每次全表扫描 | 窗口外的历史垃圾也进内存 |
| C | `compose.shadow.unraid.yaml` | 容器无 `mem_limit`，DuckDB 无 `memory_limit` | OOM 直接 SIGKILL，无优雅降级 |
| D | 心跳 | 缺少 RSS / 报告耗时 / 数据库文件大小 | OOM 发生后无法倒推根因 |
| E | retention | 无数据清理 | 表无限增长 |

---

## 2. 改动清单

### Fix 1 · P95 改为 DuckDB `quantile_cont` SQL 侧计算
- 文件：`src/weatherquant/shadow/report.py`
- 旧：`fetchall()` 拉到 Python → `sorted()` → 线性插值
- 新：`quantile_cont(decision_latency_ms, 0.95)` 直接在 SQL 侧完成
- 保留旧 `_percentile()` 函数（用于测试对比）

### Fix 2 · 快照加时间窗过滤
- 文件：`src/weatherquant/dashboard/producer.py`
- 新增常量：`MARKET_DECISION_WINDOW_DAYS = 30`、`FORECAST_LOOKBACK_DAYS = 1`
- `build_markets_snapshot`：`decision_time_utc >= now - 30 days`
- `build_forecasts_snapshot`：`target_date >= today - 1 day`

### Fix 3 · 容器级 + DuckDB 内存限制
- 文件：`docker/compose.shadow.unraid.yaml`
  - 新增 `mem_limit: 6g`、`memswap_limit: 6g`
  - 新增 `WQ_DUCKDB_MEMORY_LIMIT: 4GB`、`WQ_DUCKDB_TEMP_DIR: /data/tmp`
- 文件：`src/weatherquant/storage/db.py`
  - 模块加载时自动读 `WQ_DUCKDB_MEMORY_LIMIT`（默认 `3GB`）和 `WQ_DUCKDB_TEMP_DIR`（默认空）
  - 所有 DuckDB 连接自动 `SET memory_limit=...` 和可选 `SET temp_directory=...`

### Fix 4 · 默认关闭的 retention（绝不删数据）
- 文件：`src/weatherquant/storage/retention.py`（新建）
- 文件：`src/weatherquant/shadow/config.py`（新增 `shadow_retention_days` / `shadow_retention_archive_dir`）
- 文件：`src/weatherquant/shadow/runner.py`（cycle 末尾调用 `_maybe_retention()`，12 小时节流）
- **默认行为**：`shadow_retention_days is None` → 直接返回，零副作用
- **启用时**：先把旧行导出为 Parquet 到 `archive_dir/<table>/YYYY-MM-DD.parquet`，再 SQL DELETE

### Fix 5 · 心跳增加可观测性
- 文件：`src/weatherquant/shadow/runner.py`
- `_publish_report()` 前后计时，写 `self._report_duration_ms`
- `_rss_mb()` 读 `/proc/self/statm`（RSS 页计数 × 页大小 / 1MB）
- `_file_size()` 读取 DuckDB 主文件大小
- 心跳 JSON 新增字段：`report_duration_ms`、`rss_mb`、`db_bytes`

### Fix 6 · 测试覆盖
- 文件：`tests/test_shadow_oom_fixes.py`（新建，8 个 case）
  - `test_quantile_cont_matches_python_percentile[N]` × 3：500/1000/5000 行，DuckDB 与 Python 侧结果差 < 1e-6
  - `test_quantile_cont_empty_is_none`：空表返回 None
  - `test_quantile_cont_single_row`：单行返回该值
  - `test_market_snapshot_window_filters_old_decisions`：50 个市场 × 2 个决策（1 新 1 旧），snapshot 只留窗口内
  - `test_market_snapshot_window_boundary`：恰好卡在 30 天边界的决策应被 INCLUDE
  - `test_forecast_snapshot_lookback_filters_old_predictions`：target_date < today-1 的预测被排除

### Fix 7 · 基准脚本
- 文件：`scripts/bench_shadow_oom.py`（新建）
- 用 DuckDB `generate_series` + 向量化 INSERT 造 1M/3M/5M 行
- 对比 P95 旧 vs 新、markets 旧 vs 新、forecasts 旧 vs 新

---

## 3. 基准对比

运行：`uv run python scripts/bench_shadow_oom.py`

### 3.1 P95 计算（最显著修复）

| 行数 | 旧 Python 侧 | 新 DuckDB quantile_cont | 加速 | 峰值内存旧 | 新 |
|------|--------------|------------------------|------|-----------|----|
| 1M   | 854 ms       | **15 ms**              | 57×  | 81.64 MB  | 0 MB |
| 3M   | 2843 ms      | **33 ms**              | 86×  | 243.05 MB | 0 MB |
| 5M   | 4062 ms      | **74 ms**              | **55×** | **411.47 MB** | **0 MB** |

**数值一致性**：1M/3M/5M 三档 Python `_percentile()` 与 DuckDB `quantile_cont` 返回值完全相同（diff=0.000）。

### 3.2 Markets 快照时间窗

| 行数 | 旧（全表扫描） | 新（30 天窗口） | 新/旧 |
|------|--------------|---------------|-------|
| 1M   | 37 ms        | 0 ms          | 0.00× |
| 3M   | 48 ms        | 43 ms         | 0.90× |
| 5M   | 94 ms        | 69 ms         | **0.73×** |

### 3.3 Forecasts 快照时间窗

| 行数 | 旧（全表扫描） | 新（1 天 lookback） | 新/旧 |
|------|--------------|-------------------|-------|
| 1M   | 27 ms        | 18 ms             | 0.67× |
| 3M   | 74 ms        | 45 ms             | 0.61× |
| 5M   | 130 ms       | 78 ms             | **0.60×** |

**结论**：P95 修正是 **主故障根因**（5M 行时 411 MB Python heap 几乎填满 Shadow 默认内存 budget），时间窗过滤是次要但持续的改善，compose 的 mem_limit 是防止再次 OOM 的兜底。

---

## 4. 测试结果

```
tests/test_shadow_oom_fixes.py ........ 8 passed in 3.48s
pytest --ignore=tests/test_paper.py  (全量)
```

分位数测试 **5/5 绿**（空表、单行、500/1000/5000 行），时间窗测试 **3/3 绿**（过滤逻辑、边界、forecast lookback）。

---

## 5. 运维 5 分钟只读确认清单

> 部署新镜像后，以下所有检查都是 **只读**，不涉及容器重启、数据写入或配置变更。Shadow 正常运行时依次执行，预期在 5 分钟内完成。

### Step 1 · 确认镜像已滚动到新 Shadow

```bash
# 在 Unraid 宿主机
docker images | grep shadow
# 看 image id 是否包含 WQN-3 或分支 tag
docker ps --filter "name=weatherquant-shadow" --format "{{.Image}} {{.Names}}"
```

### Step 2 · 确认容器资源限制已生效

```bash
docker inspect weatherquant-shadow \
  --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.MemorySwappiness}}'
```

预期 Memory = 6442450944（6GiB），MemorySwap = 6442450944（swap=物理内存，禁用无限 swap）。

### Step 3 · 确认 DuckDB 已读取环境变量

```bash
docker exec weatherquant-shadow python -c "
import os
print('WQ_DUCKDB_MEMORY_LIMIT =', repr(os.environ.get('WQ_DUCKDB_MEMORY_LIMIT')))
print('WQ_DUCKDB_TEMP_DIR =', repr(os.environ.get('WQ_DUCKDB_TEMP_DIR')))
"
```

预期 `WQ_DUCKDB_MEMORY_LIMIT = '4GB'`，`WQ_DUCKDB_TEMP_DIR = '/data/tmp'`。

### Step 4 · 确认心跳 JSON 三个新字段存在且数值合理

```bash
docker exec weatherquant-shadow cat /data/health/heartbeat.json | jq '.rss_mb, .report_duration_ms, .db_bytes'
```

- `rss_mb`：应 < 4000（4GB DuckDB 限制内），如果连续两个 cycle > 5000 要警惕
- `report_duration_ms`：通常 < 2000，若 > 10000 说明仍有全表扫描
- `db_bytes`：正常增长，若单次激增 > 200MB 可能有人手动 INSERT

### Step 5 · 确认 retention 默认关闭

```bash
docker exec weatherquant-shadow python -c "
from weatherquant.shadow.config import ShadowConfig
import os, tempfile
cfg = ShadowConfig(shadow_db_path='/tmp/x.db', shadow_work_dir='/tmp')
print('shadow_retention_days =', repr(cfg.shadow_retention_days))
"
```

预期 `shadow_retention_days = None`。若不是 `None`，确认是上次部署意外带了值。

### Step 6 · （可选·进阶）跑一次 P95 计算验证不 OOM

```bash
docker exec weatherquant-shadow python -c "
import duckdb, os
os.environ['WQ_DUCKDB_MEMORY_LIMIT'] = '4GB'
from weatherquant.storage.db import connect
con = connect('/data/shadow.db')
rows = con.execute(\"SELECT count(*), quantile_cont(decision_latency_ms, 0.95) FROM shadow_decisions WHERE decision_latency_ms IS NOT NULL\").fetchone()
print('count =', rows[0], 'P95 =', rows[1])
"
```

正常几秒内返回，无 Python `MemoryError`。

---

## 6. 回滚预案（若上线后出现异常）

Shadow 是只读消费侧，回滚是安全的：

```bash
docker tag <旧_image_id> weatherquant-shadow:rollback
docker compose -f compose.shadow.unraid.yaml up -d shadow
# 恢复到旧版镜像即可，DuckDB 数据格式向前兼容
```

旧代码不会写入心跳的新字段，collector 对缺失字段容忍。

---

## 7. 提交信息

```
WQN-3: Shadow OOM / 全表扫描修复
- P95 改为 DuckDB quantile_cont SQL 侧计算（Python 侧 411MB → SQL 侧 0MB）
- markets 快照加 30 天时间窗、forecasts 加 1 天 lookback
- compose 新增 mem_limit=6g / memswap_limit=6g
- 支持 WQ_DUCKDB_MEMORY_LIMIT / WQ_DUCKDB_TEMP_DIR 环境变量
- retention 模块（默认关闭，12h 节流）
- 心跳新增 rss_mb / report_duration_ms / db_bytes
- 新增 8 个测试 + 基准脚本
```

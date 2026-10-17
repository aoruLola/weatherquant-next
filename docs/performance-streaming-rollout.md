# 流式 Performance 与分页明细：生产上线方案

本文件是方案，外加 §12 的一次执行记录。**2026-10-16 已按本方案部署（§12）**：
Dashboard 已切到 `be5ea9c`，首次回填已完成，增量已挂 5 分钟 cron。
未部署 MCP，未停 Collector / Shadow，未删除任何世代。**世代清理尚未有人负责，见 §4。**

适用对象：
- 研究进程（`weatherquant-research build-performance`，流式引擎 + 状态库）
- Dashboard（`weatherquant-next-dashboard`，分页明细）
- MCP（`weatherquant-next-mcp`，只转发 API）

前提：PR #11（流式引擎）与 #12（有界明细 + 分页）均已审查合并。

---

## 1. 镜像与版本

| 组件 | 镜像 | 说明 |
|---|---|---|
| Research Analytics | 无独立镜像 | CLI，随 `weatherquant-next`（`--group dev` 不需要，基础依赖即可） |
| Dashboard | `ghcr.io/aorulola/weatherquant-next-dashboard:sha-<commit>` | 本轮唯一需要更新的生产镜像 |
| Shadow / Collector | **不动** | 见 §10 |
| MCP | `…-mcp:sha-<commit>` | 可选，且必须单独批准（本轮不批准发布） |

**规则**：只部署带 `sha-<40 位 commit>` 的不可变标签；不使用 `latest`；部署前记录
`docker inspect --format '{{index .RepoDigests 0}}'` 得到的 digest，写入变更单。

**注意（本轮 CI 实况）**：`ci.yml` 的 `build-image` job 因 GitHub 账户
**账单/额度**（"recent account payments have failed or your spending limit needs to be
increased"）**未能启动**（3 秒失败、无任何 step）。其余 job（`lint-and-test`、
`shadow-tests`、`dashboard-*`）正常通过。**在账单恢复前不得依赖 CI 的镜像构建结论**；
本地已按同一 `Dockerfile` 复现构建与启动健康检查（见 §11 复验）。

---

## 2. 目录与挂载

```
/mnt/user/appdata/weatherquant-next/                     Collector（不动）
/mnt/user/appdata/weatherquant-shadow/                   Shadow（不动）
    reports/decisions-export/                            决策导出分片（流式引擎的输入，只读）
/mnt/user/appdata/weatherquant-performance/              快照世代（Dashboard 只读挂载）
    current.json
    generations/<id>/{summary,edge,win-rate,opportunities,settlements,models}.json
    generations/<id>/opportunities/part-*.json           明细分片（新增）
/mnt/user/appdata/weatherquant-performance-state/        【新增】状态库目录
    performance-state.duckdb                             可删可重建
    duckdb-tmp/                                          溢出目录（同卷）
```

Dashboard compose **不需要新增挂载**：分片在 `performance/` 之下，既有
`/sources/performance:ro` 已覆盖。**状态库不挂给 Dashboard**。

---

## 3. 旧快照保留

- 每次发布新建 `generations/<id>/`，`current.json` 一次 rename 切换；失败时旧世代仍是 current。
- **保留策略**：保留最近 14 个世代 + 每个自然月最后一个（约 6 GB 上限，按 1.5M 行
  13.8 MB/世代估算，5M 行约 40 MB/世代）。清理**只删 `generations/` 下非 current 的目录**，
  不删 `current.json`、不删状态库。
- 回滚判据见 §8：任何回滚都**不需要删除**当前世代。

---

## 4. 首次回填与增量

**首次回填**（一次性，离线窗口内执行）：

```bash
weatherquant-research build-performance \
  --export /mnt/user/appdata/weatherquant-shadow/reports/decisions-export \
  --out /mnt/user/appdata/weatherquant-performance \
  --dataset live \
  --state-db /mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb \
  --memory-limit 1GB \
  --max-parts-per-run 40        # 分批：见下方"实际分片数"
```

- 状态库为空即从头开始；`--max-parts-per-run` 让首次回填可分段、可中断。
- **不在 NAS 上跑旧版全内存构建器**（`--in-memory` 会打印警告，生产禁用）。

**实际分片数与耗时**（2026-10-16 首次回填实测，见 §12）：导出已是 **379 个分片 /
1,967,350 行**，不是本方案最初估的 79 个。分片大小极不均匀：追平时的分片是
20,000 行 / 约 32 MB，日常 tick 的分片只有约 1,500–1,800 行 / 约 2.6 MB。所以
"按分片数分批"不等于"按行数分批"：前 40 个分片就吃掉 800,000 行（41 s），
后 335 个分片合计只有 1,167,350 行。

**每次运行都会发布一个世代，即使没有新行。** 增量运行消费 0 行时仍会重写六个快照
并发布一个内容相同的世代（generation id 里的内容哈希不变）。因此世代数量等于运行
次数：5 分钟一次即 288 个/天，约 2.2 GB/天。**CLI 没有 prune/retention 参数**，
本方案 §3 的保留策略目前只能人工执行。在启用调度前必须先决定由谁清理。

**日常增量**（每 15–30 分钟一次，见 §6）：同一条命令**去掉** `--max-parts-per-run`。

**结算修订**：带新的 `--settlements` 重跑（upsert），不重读任何决策。

要**撤销**用 `--settlements-replace`（仅在确认索引完整时）。替换是完整语义：
索引里没有的答案会被删除。因此：

| 输入 | 结果 |
|---|---|
| `--settlements-replace` + 非空索引 | 完整替换（索引里没有的被撤销） |
| `--settlements-replace` + **空索引** | **拒绝**，返回退出码 2，不修改任何东西 |
| `--settlements-replace` + 空索引 + `--confirm-empty-retraction` | 允许全部撤销 |
| `--confirm-empty-retraction` 单独使用 | **拒绝** |

**为什么空索引默认拒绝**：空索引至少有同样多的概率来自**失败的回填**——平台不可达、
或没有任何市场匹配——而不是来自"确实全部撤销"。结算层是状态库里唯一**无法从导出重建**
的东西（重建要联网），所以宁可拒绝。拒绝发生在**状态库被打开之前**：水位不推进、
不发布世代、不写备份，即"拒绝"必须意味着什么都没动。

**全部撤销会先写备份**：`<state-db>.retracted-<UTC>.json`，含 `rows_retracted`、
`input_source`（`--settlements` 的路径）、`reason`（`--retraction-reason`，未给则为
`null`，不猜）以及被删除的每一行。这是唯一无法从别处重做的结算操作。

**定时任务不得添加 `--settlements-replace`、`--confirm-empty-retraction` 或
`--retraction-reason`。** 这三者是人工操作，不是日常增量的一部分：调度器无法判断
一次空索引是"平台故障"还是"真的全部撤销"。

---

## 5. 资源限制

| 资源 | 设置 | 理由 |
|---|---|---|
| 研究进程内存 | `--memory-limit 1GB`（DuckDB 缓冲池上限）+ 容器 `--memory=1.5g` | 实测 5M 行峰值 477 MB、10M 行 613 MB；上限是**上限**，不是期望值 |
| 溢出目录 | `--temp-dir` 指向状态库同卷 | 溢出不得落在 tmpfs |
| CPU | 容器 `--cpus=2`（NAS 4 核） | 排序/聚合是唯一的 CPU 密集段；留出余量给 Collector/Shadow |
| I/O | 无显式限制，但：分片顺序读、无随机写、无 swap | 见 §9 监控 |
| Dashboard 内存 | 容器 `--memory=512m` | 最坏一条端点（按需本金）峰值 49 MB，其余 < 5 MB |
| 并发 | **同时只允许一个研究进程**（DuckDB 文件锁会拒绝第二个） | §6 |

---

## 6. 定时任务与互斥

- 调度：cron，每 **5 分钟**一次，`nice -n 10 ionice -c2 -n7`。
  - 落地位置：runner 在 flash 上 `/boot/config/wq-performance-incremental.sh`；
    cron 条目是 `/etc/cron.d/weatherquant-performance`，由 `/boot/config/go` 在每次开机时重建
    （`/etc` 在 RAM rootfs 上，重启即丢）。
  - **不是本方案最初写的 20 分钟。** 快照自带 `stale_after_seconds=300`，Dashboard 再加
    60 s 宽限（`performance.py` 的 `stale + 60.0`），所以 20 分钟一次会让 Performance 页在
    大部分时间里显示 stale —— 实测发布后 397 s 即报 stale（上限 360 s）。导出本身约每
    5 分钟落一个分片，因此 5 分钟一次正好每次消费约一个分片。
  - Unraid 的 user.scripts 插件只提供 hourly/weekly/daily/monthly/boot 粒度，做不到 5 分钟，
    所以走 `/etc/cron.d` 而不是插件。
- **互斥**：状态库是单写者，第二个进程会被 DuckDB 文件锁拒绝（已由子进程测试验证）。
  为让失败**可见**，脚本用 `flock -n` 包裹并记录退出码：

```bash
flock -n /var/lock/weatherquant-performance.lock \
  weatherquant-research build-performance ... || echo "skipped: another run holds the lock"
```

- 运行时间远小于间隔（增量通常 < 10 秒；只有新分片落盘时才接近分钟级）。
- **定时任务的命令里不得出现 `--settlements-replace`、`--confirm-empty-retraction`
  或 `--retraction-reason`。** 日常增量只做 upsert：一个自动化脚本没有能力判断
  一次空索引是"平台故障"还是"确实全部撤销"，而这两者的后果不对称——猜错的代价是
  清空整个结算层，且无法从导出重建。撤销是人工操作，见 §4。

---

## 7. 健康检查与新鲜度

| 检查 | 期望 |
|---|---|
| `GET /healthz` | 三源 `fresh` |
| `GET /api/v1/performance/summary` | `freshness.state == "fresh"`；`data.settlement_freshness.as_of_utc` 非空 |
| `GET /api/v1/performance/opportunities?limit=50` | 200；`data.window.episodes > 0`；`items` 非空 |
| `GET /api/v1/performance/reference-capital` | 200；`window` 存在；`results_total > 0` |
| 状态库 | `performance_parts` 的 `part_index` 最大值 == 导出目录分片数 |
| 新鲜度判据 | 快照 `stale_after_seconds`（默认 300 s）+ Dashboard 的 60 s 宽限 = **360 s**；研究进程每 5 分钟一次，故正常不会 stale |

**告警**：连续 2 个周期没有新世代 → 检查状态库是否被锁、导出是否停摆。

---

## 8. 失败恢复与回滚

### 8.1 数据层恢复（不需要回滚镜像）

| 情况 | 处置 |
|---|---|
| 研究进程崩溃/断电 | 直接重跑。同事务提交行与水位，未提交的分片会重读且**不重复计数** |
| 分片缺失/被改写/乱序 | 拒绝并报错，水位不动 → 人工确认导出，必要时 `--reset` 重建状态库 |
| 换导出目录或升级算法 | 会因 `input_source` / `algorithm_version` 不符而拒绝 → 确认后 `--reset` |
| 状态库损坏 | **删除状态库目录**后重跑（它全部是派生物，删除它不丢任何原始数据） |
| 本次发布可疑 | 用 §8.2 的指针回退；**不删除世代目录** |

### 8.2 快照回退：移动指针（**不是删除**）

新研究构建任务 → 禁用：

```bash
# 1) 停掉调度（cron / Unraid User Scripts 里禁用那一条），确认没有研究进程在跑
flock -n /var/lock/weatherquant-performance.lock true && echo "no run in flight"
```

**保留**状态库与故障日志（不要删——调查需要它，而且下一次回退可能还要用它）：

```bash
cp -a /mnt/user/appdata/weatherquant-performance-state/ \
      /mnt/user/appdata/weatherquant-performance-state.faulty-$(date +%Y%m%dT%H%M%SZ)/
```

查看可选世代与它们的**格式族**，然后**先 dry run** 再移动指针：

```bash
uv run python scripts/pin_performance_generation.py \
  --performance /mnt/user/appdata/weatherquant-performance --list

uv run python scripts/pin_performance_generation.py \
  --performance /mnt/user/appdata/weatherquant-performance \
  --generation <上一个已验证的世代 id>            # 默认 dry run

uv run python scripts/pin_performance_generation.py \
  --performance /mnt/user/appdata/weatherquant-performance \
  --generation <上一个已验证的世代 id> --apply
```

工具会拒绝：世代不完整（缺任一快照文件）、格式无法识别（没有 Dashboard 能与之匹配）。
它**只**原子改写 `current.json`；`generations/`、状态库、导出目录一个字节都不动。

### 8.3 ⚠️ 镜像与快照必须成对回退

v0.1.5 改变了 JSON 契约：`opportunities.episodes` 由**内联数组**变为**分片描述符**，
`summary.reference_capital.results` 被 `results_sample` 取代。因此：

| 方向 | 是否可读 | 证据 |
|---|---|---|
| **新** Dashboard + **旧** 快照 | ✅ 可读 | `dashboard/tests/test_performance_rollback.py::test_the_new_reader_still_reads_a_legacy_snapshot` —— 因此**首次上线是安全的**（新 Dashboard 先上线，读旧世代） |
| **旧** Dashboard + **新** 快照 | ❌ **不可读** | 同文件 `test_a_new_snapshot_breaks_the_old_reader`：旧读取逻辑对描述符执行 `list()` 得到键、再当行取字段 —— 这正是旧 `performance.py` 的原表达式 |

**所以回退顺序是：**

1. 停掉研究构建任务（§8.2 第一步）。
2. **先**把 Dashboard 切回与目标世代格式匹配的镜像（`DASHBOARD_SHA=<上一个 sha>` → Apply）。
3. **再**按 §8.2 移动指针到该镜像能读的世代。
4. 验证（§8.4）。
5. 不删除 Shadow / Collector / 历史导出 / 任何世代目录。
6. 把问题世代**隔离保留**（留在 `generations/` 里即可——指针不指向它，读者就不会读它）。

反向（先移动指针、后回退镜像）会在两者都完成之前留出一个旧 Dashboard 读新快照的窗口，
那段时间 `/api/v1/performance/opportunities` 与 `/reference-capital` 会 500。

### 8.4 回退后的验证

```bash
curl -sf localhost:8787/healthz | jq '.sources'                       # 三源 fresh
curl -sf localhost:8787/api/v1/performance/summary | jq '.data.generation_id, .data.data.opportunity_episodes'
curl -sf "localhost:8787/api/v1/performance/opportunities?limit=1" | jq '.data.data.window, .items|length'
curl -sf "localhost:8787/api/v1/performance/reference-capital" | jq '.data.scope, .data.window_completeness'
```

判据：`generation_id` == 指针指向的世代；`items` 非空；`freshness.state != "missing"`；
`reference-capital` 的 `scope == "window"` 且带 `window_start`/`window_end`。

**回滚判据（何时执行 §8.2）**：`/api/v1/performance/*` 持续 5xx 或非 fresh 且无法解释；
MCP 数值与 Dashboard API 不一致；出现任何下单/钱包/私钥路径（立即回滚并保留日志）。

### 8.5 回滚演练结果（隔离环境）

在隔离目录用两个已发布世代实测（`dashboard/tests/test_performance_rollback.py`）：

| 步骤 | 结果 |
|---|---|
| 发布两代（5 个 / 40 个 episode，market 前缀不同） | 指针在第二代 |
| `--list` | 两代均 `bounded-detail complete`，`detail_parts` 正确 |
| `--generation <第一代>`（dry run） | 指针未变 |
| `--apply` | 指针移动；**`generations/` 目录列表逐项不变**，第二代文件仍在 |
| 移动后取 `/opportunities?limit=200` | `generation_id` == 第一代；items 为第一代的 5 条；market 前缀全为 `old` |
| 移动后取 `/summary` | 同一世代；`opportunity_episodes == 5` |
| 用**第二代**的游标请求 | **409**（拒绝跨世代偏移） |
| 缺文件 / 格式不明的世代 | 拒绝，指针不动 |
| 旧读取逻辑对新快照 | `TypeError`（即"必须成对回退"的证据） |
| 新读取逻辑对旧格式快照 | 正常返回分页与按需本金 |


---

## 9. NAS 宿主资源监控

- **内存**：Unraid Dashboard 的 Memory 面板 + `docker stats`。预期研究进程峰值 < 700 MB，
  Dashboard < 200 MB。
- **宿主可响应性**：压力测试期间并行采样空命令延迟，实测中位 **6 ms** / p95 **17 ms** /
  最大 **32 ms**（对比旧构建期间 HTTP/SSH 全部超时 35 分钟）。
- **磁盘**：`/mnt/user/appdata/weatherquant-performance/` 与状态库目录容量告警（80%）。
  导出目录只读，不清理。
- **I/O**：观察 `iowait`；研究进程只顺序读导出与写状态库，不触碰生产 DuckDB。
- **不做**：不在 NAS 上跑 `scripts/bench_performance_streaming.py`（它会写 GB 级合成数据）。

---

## 10. 不影响 Collector / Shadow 的证明

| 声明 | 依据 |
|---|---|
| 不打开生产 Shadow 库 | 研究进程只读 `reports/decisions-export/`；`--export` 指向目录时走流式引擎，代码路径中没有任何指向 `shadow.duckdb` 的连接 |
| 不写生产库 | 状态库是**独立文件**（`weatherquant-performance-state/`），schema 由 `migrations_performance/` 管理 |
| 不重启任何容器 | 本方案不要求重启 Collector/Shadow；Dashboard 的 Apply 只影响 Dashboard 容器 |
| 不修改导出 | 研究进程对导出目录**只读**；分片指纹校验在检测到改写时**拒绝**而不是修复 |
| 不抢占资源 | 增量运行 < 10 s、峰值 < 700 MB、`nice`/`ionice` 降级、`--cpus=2` |
| 不共享文件锁 | 状态库与 Shadow 库是两个文件，锁互不相干 |

**唯一的生产写动作**是创建 `weatherquant-performance-state/` 目录与写入快照世代目录。

---

## 11. 上线前复验清单

| # | 检查 | 本轮状态 |
|---|---|---|
| 1 | `uv run pytest -q` | **1020 passed, 6 skipped**（2026-10-16 复跑，含本轮新增的撤回语义与导出测试） |
| 2 | `uv run pytest dashboard/tests -q` | **109 passed** |
| 3 | `npx tsc --noEmit` + `npm test` | 干净 / **89 passed** |
| 4 | `uv run ruff check .` | 干净 |
| 5 | 隔离压力测试 1.5M/3M/5M/10M | 见 `performance-deployment.md` §11 |
| 6 | 端点延迟/体积/峰值 | 见 §12 |
| 7 | CI | `lint-and-test` / `shadow-tests` / `dashboard-*` **通过**；`build-image` 因**账户账单**未启动，改为在 NAS 上按同一 `Dockerfile.dashboard` 本地构建（见 §12） |
| 8 | 生产部署 | **已执行 2026-10-16**，见 §12 |

---

## 12. 生产部署记录（2026-10-16）

| 项目 | 值 |
|---|---|
| 部署提交 | `be5ea9c0d24dd04aa36192ccf9d37e0cf5efae98`（PR #11 + #12 合并后的 main） |
| Dashboard 镜像 | `ghcr.io/aorulola/weatherquant-next-dashboard:sha-be5ea9c…`，image id `sha256:f67662641a52…`，485 MB，在 NAS 上构建 |
| 研究镜像 | `ghcr.io/aorulola/weatherquant-next:sha-be5ea9c…`，image id `sha256:848f6e2b99ee…`（只作为 CLI，不替换 Collector 的 `:stable`） |
| 回滚目标 | Dashboard `sha-e6698dfd…`（digest `sha256:838b5af27de1…`），stack.env 备份 `stack.env.bak-20261016T103220Z` |
| 部署方式 | Portainer 当时未运行（`Exited (255)`），改为在 stack 目录用 `docker compose -p weatherquant-next-dashboard --env-file stack.env up -d --pull never dashboard`；只改 `stack.env` 的 `DASHBOARD_SHA`，挂载与端口逐项未变 |
| 首次回填 | 379 分片 / 1,967,350 行，分 3 次（40 / 190 / 145 分片），41 s + 60 s + 27 s，峰值可用内存始终 > 6.5 GB |
| 状态库 | `/mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb`，14.4 MB |
| 当前世代 | `20261016T110659Z-02108d731722`，10,480 episode，`open_episodes=30`，watermark `2026-10-16T11:02:46Z` |
| 增量调度 | `/etc/cron.d/weatherquant-performance`，`*/5 * * * *`，runner 在 flash |
| 两轮增量验证 | 第 1 轮消费 4 个分片 / 6,248 行（13 s）；第 2 轮消费 0 行；锁被占用时输出 `skipped: another run holds the lock` |
| 端点 | 13 个端点全部 200；六个 SPA 路由 200；`/healthz` 三源 fresh |
| 跨世代游标 | 用旧世代的游标请求 `/opportunities` → **409**，指针不动 |
| 未做 | 未停 Collector / Shadow；未部署 MCP；未删除任何世代；未清理世代（见 §4 的 churn 说明） |

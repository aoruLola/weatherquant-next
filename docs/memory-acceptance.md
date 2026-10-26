# DuckDB 内存与 OOM 验收门禁（WQN-3）

2026-10-26。配套代码：PR #14（引擎预算）+ 本门禁的容器上限、验收脚本与测试。

这份文档回答一个问题：**凭什么可以说"采集器不会再把宿主拖进直接回收"**，
以及这个判断目前处在四个状态（代码合并 / 镜像发布 / 生产部署 / 验收完成）
中的哪一个。每一层的证据必须分开存放、分开判定——一个绿了的 CI 不能代替
一台 NAS 上的一个夜晚。

---

## 1. 故障回顾（为什么要这套门禁）

2026-10-17 的生产事故链：

1. 采集器容器**没有任何内存上限**（`Memory=0`），DuckDB 也没有
   `memory_limit`（引擎默认 = 物理内存的 80%，实测 12.2 GiB）。
2. 每日备份是**独立的水位尖峰**（正常 ~1 分钟、写入 5+ GB），当天它挂起
   3 小时后被放弃（`.incomplete-…` 2.17 GB）。
3. 15 分钟后全宿主服务劣化（nginx / php-fpm / sshd / smbd 一起卡）——
   内核被迫直接回收，16 GB 无 swap 的机器上所有用户态一起付账。

结论：**引擎预算不是进程上限**。DuckDB 超过 `memory_limit` 会溢写而不是
失败，Python 堆与原生分配根本不在这个预算里。因此需要三层各自独立的闸：

| 层 | 闸 | 失效方式 |
|---|---|---|
| 引擎 | `WQ_DUCKDB_MEMORY_LIMIT=3GB`（写连接），溢写到 `WQ_DUCKDB_TEMP_DIR` | 大查询变慢，不变死 |
| 容器 | `mem_limit: 6g` + `memswap_limit: 6g` | 进程被内核 OOM 杀掉，**只有这个容器** |
| 宿主 | 内存 PSI 监控（`/proc/pressure/memory`） | 告警在用户态劣化**之前** |

读连接（legacy 库、dashboard）**刻意不设预算**：它们无法溢写，设了上限
只会把大查询变成失败而不是变慢。

## 2. 预算数字的依据

- **引擎 3GB**：采集器的工作集（发现 + 盘口 + 价格批量提交）实测远低于此；
  溢写路径有真实磁盘可去（`/data/duckdb-spill`，NAS 阵列，**不是 tmpfs**——
  tmpfs 溢写本身就是内存分配，会让预算形同虚设）。
- **容器 6GB** = 引擎 3GB × 2：给 Python 堆、原生分配和溢写缓存留一倍余量。
  `memswap_limit == mem_limit` 即不借 swap：超限就是被杀，而不是把整台
  无 swap 的机器拖进页回收。
- **预算有下限**：本机实测（duckdb 1.5.6），这条插入路径在 **256MB 预算下
  失败**（"failed to pin block"——行组内的追加无法溢出，小分配直接失败）、
  **384MB 通过**。运维调低 `WQ_DUCKDB_MEMORY_LIMIT` 时不要低于 ~512MB
  （`tests/test_storage_memory_gate.py` 记录了这次标定）。

## 3. 本机已验证（代码合并所需的证据）

全部离线或打向临时目录，未触碰任何生产数据。

| 验证 | 方式 | 位置 |
|---|---|---|
| 引擎预算生效、可覆盖、拒绝注入值 | 单测 19 项 | `tests/test_storage_memory_budget.py`（PR #14） |
| 容器 6GB / 无 swap / 溢写目录在 /data | compose 断言测试 | `tests/test_storage_memory_gate.py` |
| 溢写目录在连接时创建、路径错误启动即报错 | 同上 | 同上 |
| **SIGKILL 后零丢失**：硬杀等价于容器 OOM 杀；flush 事务原子；WAL 重放；checkpoint 折叠 WAL | 子进程硬杀测试 | 同上 |
| 备份在 512MB 预算下完成并通过读回校验 | 子进程备份测试 | 同上 |
| **内核级 OOM 杀 + 恢复 + 重启续写**（cgroup v2，与 Docker `mem_limit` 同一机制） | `scripts/oom_recovery_accept.py` | 本文档 §5 的运行记录 |
| 30 分钟真实采集：RSS 有界、无泄漏斜率、PSI 无停顿、备份周期通过 | `scripts/soak_memory.py` | PR 描述与 §5 |

### 验收命令（两台机器上各自运行）

```bash
# 内核级 OOM：cgroup memory.max=1GB，子进程增长直到被内核杀死，
# 然后验证已提交前缀完整、WAL 重放、重启续写
sudo -E uv run python scripts/oom_recovery_accept.py --memory-max 1GB --data /tmp/wq-oom-accept

# 内存 soak：真实采集 30 分钟（NAS 上建议 --minutes 720 做 12 小时版本）
# + 备份周期，全程采样 RSS / PSI
uv run python scripts/soak_memory.py --minutes 30 --data /tmp/wq-mem-soak

# 单测
uv run pytest tests/test_storage_memory_budget.py tests/test_storage_memory_gate.py -q
```

（本机运行输出粘贴在 PR 描述；两条脚本都是**测量后判定**，不预设结论。）

### 本机运行记录（2026-10-26，Debian / duckdb 1.5.6 / 8 核 15GB）

**内核级 OOM（`--memory-max 1GB`）——RESULT: PASSED**

```
kill:        process gone with return code -9; kernel oom_kill=3; peak=1024 MiB; wal=732121 KiB
recovered:   max batch=36, rows=720000, journalled=36     # 720000 = 36 × 20000，零丢失零撕裂
wal replay:  732121 KiB -> 0 KiB after checkpoint         # WAL 全量重放并折叠
restart:     batches 36 -> 39 (+3), rows=780000           # 重启续写，行集连续
```

峰值恰好 1024 MiB = cgroup 上限：内核在闸口处击杀，与 Docker `mem_limit`
同一机制（Docker 的内存限制就是 cgroup v2 `memory.max`）。

**30 分钟内存 soak（真实采集 + 备份周期）——RESULT: PASSED**

```
collection     1803s，360 样本：peak RSS 304.8 MiB；斜率 −0.6 MiB/min（无泄漏）；
               PSI some/full 峰值 0.54% / 0.36%；采到 4490 盘口 / 34300 价点
backup-cycle   200k 盘口 + 200k 价点按生产 500 行/flush 节奏写入：
               peak RSS 472.8 MiB；backup verified=True（62.9 MB，1.6s）
final health   stopped (exit 0)
```

> 如实记录：backup-cycle 阶段的 PSI 读数（some/full 峰值 86.7/68.4%）被
> **同期并行的 OOM 验收**污染（同一台机上 cgroup 回收风暴），不构成采集器
> 的账——采集期的 PSI 才是"采集器不饿死宿主"的门，峰值为 0.54/0.36%。
> NAS 上的 12h soak 应独占机器运行。

---

## 4. 生产验收清单（需在 NAS 上人工执行）

机器上没有 Docker、没有生产数据库；以下每一条都必须在 NAS 上执行并留下
输出，**任何一条没有证据，验收就没有完成**。

### 4.1 镜像发布后（部署前）

```bash
# 镜像里的 db.py 与本分支一致（md5 对比）
docker run --rm ghcr.io/aorulola/weatherquant-next:stable \
  md5sum /app/.venv/lib/python3.12/site-packages/weatherquant/storage/db.py
```

### 4.2 部署（Compose Manager Plus Apply 后）

```bash
docker inspect weatherquant-collector \
  --format 'mem={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} oom={{.HostConfig.OomKillDisable}}'
# 期望：mem=6442450944 swap=6442450944（相等 = 不借 swap）

docker exec weatherquant-collector \
  python -c "from weatherquant.storage import db; print(db.DEFAULT_MEMORY_LIMIT, db.DEFAULT_THREADS)"
# 期望：3GB 4

docker exec weatherquant-collector ls -la /data/duckdb-spill
# 期望：目录存在（首次连接即创建）
```

### 4.3 容器级 OOM 演练（一次性，验证"被杀后完好"）

```bash
# 把运行中容器的上限临时压到 512MB，制造真实容器 OOM
docker update --memory 512m --memory-swap 512m weatherquant-collector
# 等待被杀（docker events 里看 OOM），然后恢复：
docker update --memory 6g --memory-swap 6g weatherquant-collector
docker start weatherquant-collector
# 重启后：
#   - healthcheck 回到 healthy
#   - 数据库完好：backups/ 上一份备份仍可读
#   - collection_gaps 记录了这次中断并随后关闭
docker logs weatherquant-collector 2>&1 | grep -i "gap\|replay\|lock"
```

### 4.4 12 小时 soak（验收的核心一条）

```bash
# 在 NAS 上（或任何能跑本仓库的常驻机器）：
uv run python scripts/soak_memory.py --minutes 720 --data /mnt/user/appdata/soak-mem --keep
# 判定：RESULT: PASSED，且峰值 RSS < 3072 MiB、斜率 < 8 MiB/min、PSI full avg10 < 2.0%
```

退而求其次（不引入新进程、直接观察生产容器）：

```bash
for i in $(seq 1 4320); do
  ts=$(date -u +%FT%TZ)
  rss=$(docker stats --no-stream --format '{{.MemUsage}}' weatherquant-collector)
  psi=$(grep some /proc/pressure/memory | head -1)
  echo "$ts rss=$rss psi=$psi" >> /var/log/wq-mem-soak.log
  sleep 10
done
# 12 小时后检查：RSS 无持续上升斜率；PSI full avg10 峰值 < 2%；
# 期间 backups/ 恰好新增 1 份当日备份且无 .incomplete- 残留
```

### 4.5 备份周期观察（12h soak 期间顺带完成）

```bash
ls -la /mnt/user/appdata/weatherquant-next/backups/
# 期望：每日 UTC 00:00 后一份新 weatherquant-<stamp>.duckdb，
#       无 .incomplete-* 残留（有 = 备份挂起，事故先兆，立即上报）
du -sh /mnt/user/appdata/weatherquant-next/backups/
# 对照：事故时该目录 37 GB，含两份废弃 .incomplete（合计 3.6 GB）
```

### 4.6 PSI 常驻观察

```bash
cat /proc/pressure/memory
# full avg10 长期 > 2% 即为异常：用户态在等内存，采集器之外的服务会先受害
```

---

## 5. 门禁状态表

| 状态 | 内容 | 当前 | 判定证据 |
|---|---|---|---|
| 代码合并 | 引擎预算（PR #14）+ 容器上限与验收工具（本 PR） | 🟡 评审中 | CI 全绿 + 本文档 §3 |
| 镜像发布 | `release.yml` 打 tag → `:stable` 前进 | ⚪ 未做 | GHCR sha-<commit> tag |
| 生产部署 | NAS compose Apply（新 env + mem_limit 生效） | ⚪ 未做 | §4.2 的 inspect 输出 |
| 验收完成 | OOM 演练 + 12h soak + 备份周期 + PSI | ⚪ 未做 | §4.3–4.6 的输出留档 |

**规则：四格全绿之前，不得在故障复盘里引用"已修复"三个字；只说"已缓解，
验收进行中"。**

## 6. 已知限制（不要当成已解决）

1. **Shadow / Dashboard 容器没有内存上限。** 它们同样会开 DuckDB 连接
   （Shadow 可写、Dashboard 只读）。本工单的范围是采集器；给其余两个容器
   定上限需要各自的水位数据，是独立跟进项。
2. **备份挂起本身未修**（2026-10-17 那次 3 小时的备份）。本门禁让它的
   后果可观测（§4.5 的 `.incomplete` 检查）并限制了爆炸半径（容器上限），
   但挂起根因是独立问题。
3. **预算下限是本机标定**（256–384MB 之间）。不同 DuckDB 版本 / 列数下
   该值会移动；调低预算前先在目标环境重跑
   `tests/test_storage_memory_gate.py::test_the_daily_backup_verifies_under_a_tight_budget`。
4. `mem_limit` / `memswap_limit` 是 compose v2 的非 swarm 字段。Unraid
   Compose Manager 使用 docker compose v2，支持它们；若换部署方式，需把
   限制迁移成 `deploy.resources.limits` 并重跑 §4.2 的检查。


### 评审修复验证

资源门禁覆盖采集与备份两个阶段。备份阶段采样 RSS、进程 VmHWM 和 PSI 任一超限都导致非零退出，备份校验成功不会覆盖资源超限。

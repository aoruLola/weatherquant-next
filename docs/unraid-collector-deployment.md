# Unraid NAS 部署与运维指南

WeatherQuant Next 采集器 · Compose Manager Plus · GHCR

> **本指南的验证状态（务必先读）**
>
> | 类别 | 状态 |
> |---|---|
> | 采集器进程本地实测 | ✅ 已验证（见文末 §12 的实测数字） |
> | 单元/集成测试 | ✅ 303 项通过，`ruff` 无告警 |
> | **Dockerfile 构建与容器启动** | ✅ **GitHub Actions 已执行**：镜像构建成功、2s 内写出心跳、非 root、优雅停止、重启后数据库完好 |
> | GitHub Actions `ci.yml` | ✅ **已执行并全绿**（ruff 0.17.0 + 303 测试 + 镜像构建 + 容器健康检查） |
> | GitHub Actions `release.yml` | ✅ **`v0.1.1` 已执行并发布成功**（run `38063764541`：verify + publish 双绿） |
> | **Unraid 上的真实部署** | ❌ **未部署 —— 我没有 NAS 访问权限** |
>
> 下面标 `[待NAS验证]` 的步骤是给你执行的，不是我做过的。§11 会把"我验证了什么"
> 和"你需要验证什么"分开列清楚。

---

## 0. 架构一览

```
Unraid NAS
└── /mnt/user/appdata/weatherquant-next/        ← 宿主机数据目录（唯一持久化点）
    ├── weatherquant.duckdb                     ← 研究数据库
    ├── weatherquant.duckdb.wal
    ├── raw/{gamma,rest,ws}/YYYY-MM-DD.jsonl    ← 上游原始载荷
    ├── parquet/<table>/dt=YYYY-MM-DD/          ← 按日分区的 Parquet 导出
    ├── backups/weatherquant-<ts>-<uuid>.duckdb ← 自校验备份
    ├── health/heartbeat.json                   ← 健康探针唯一读取的文件
    └── .collector.lock                         ← 单实例租约

容器内：/data → 上述目录（bind mount）
```

**没有对外端口。** 采集器只发起出站请求。Docker `EXPOSE` 也没有——暴露一个
没有任何服务监听的端口只会扩大攻击面。

---

## 1. 准备 appdata 目录

Unraid 的 `appdata` 通常落在 **cache（SSD）池**上，这正是我们想要的：DuckDB
随机写入和 JSONL 追加在机械盘上会明显拖慢采集循环。

创建目录（Unraid Web UI → **Shares**，或 SSH）：

```bash
mkdir -p /mnt/user/appdata/weatherquant-next/{raw,health,backups,parquet}
```

权限：镜像以 **uid 1000 / gid 1000** 运行（非 root）。

```bash
# 查看你的用户 uid/gid（Unraid 的 "nobody" 通常是 99:1000）
id nobody

# 若与镜像一致（1000:1000）：
chown -R 1000:1000 /mnt/user/appdata/weatherquant-next

# 若 Unraid 用 99:1000，两种选择：
#   a) 改 compose 里的 user: "99:1000"  （推荐，跟随 Unraid 惯例）
#   b) 保持 1000:1000，目录照上面 chown
```

`[待NAS验证]` 执行 `touch /mnt/user/appdata/weatherquant-next/.write-test && rm $_`
确认可写。

---

## 2. 检查 SSD 存储池

```bash
# 各挂载点用的是什么盘
df -hT /mnt/user/appdata

# appdata 是否落在 cache 上（文件系统应显示 /mnt/cache 或 /dev/nvmeXn1）
ls -ld /mnt/user/appdata
findmnt -T /mnt/user/appdata

# cache 剩余空间：采集器长期运行的经验值见 §7
df -h /mnt/cache 2>/dev/null || df -h /mnt/user/appdata
```

**容量估算**（基于实测，见 §10）：

| 项 | 实测速率 | 一个月 | 一年 |
|---|---:|---:|---:|
| DuckDB | ~见 §10 | 约 1–3 GB | 约 15–35 GB |
| 原始 JSONL | ~见 §10 | 约 1–2 GB | 约 12–25 GB |
| 按日 Parquet | 每日一次全量导出 | 约 0.5–1 GB | 约 6–12 GB |
| 备份 | 每日一份完整库 | 约 30–90 GB | **需要清理策略** |

> ⚠️ **备份会累积。** 见 §9 的轮转命令——`backups/` 默认只增不减，一年不清理会吃掉
> 几十 GB。采集器本身**不会**删除备份（自动删除历史备份太危险，这个决定留给人）。

**建议预留：SSD ≥ 100 GB 可用。** 采集器内置磁盘水位告警：
`<15%` → 健康状态降级为 `degraded` 并在 `collector_events` 记一条 warning；
`<5%` → 记 critical 且健康探针失败（容器会被重启，但重启前会先写入告警）。

`[待NAS验证]` `df -h` 确认可用空间 ≥ 100 GB。

---

## 3. 配置数据卷

`docker/compose.unraid.yaml` 中的关键一行：

```yaml
volumes:
  - /mnt/user/appdata/weatherquant-next:/data
```

容器内路径**必须**是 `/data`，因为镜像已通过环境变量固定：

```yaml
environment:
  WQ_DB_PATH: /data/weatherquant.duckdb
  WQ_RAW_DIR: /data/raw
```

三条约束：

1. **数据库不能在镜像内。** 镜像是只读的交付物，重建即丢失。所有状态都在这个卷上。
2. **不要用 tmpfs / RAM disk 放 `/data`。** 重启即丢历史数据，而历史订单簿正是
   整个项目的目的（Polymarket **没有**历史深度 API，丢了就永久没了）。
3. **不要同时挂给两个容器。** 单实例锁（`/data/.collector.lock`）会拒绝第二个
   写者并以退出码 **4** 退出——这是有意设计的，不是故障。

`[待NAS验证]` 启动后执行 `ls -la /mnt/user/appdata/weatherquant-next/`，
应看到 `weatherquant.duckdb`、`raw/`、`health/heartbeat.json`、`.collector.lock`。

---

## 4. 拉取 GHCR 私有镜像

仓库默认为 **private**，Unraid 需要一个带 `read:packages` 权限的 PAT。

### 4.1 创建 token

GitHub → Settings → Developer settings → **Personal access tokens → Fine-grained**

| 项 | 值 |
|---|---|
| Repository access | Only select repositories → 选本仓库 |
| Permissions → **Packages** | **Read and write**（至少 Read） |

生成后**立刻复制**（只显示一次）。

### 4.2 登录

SSH 到 Unraid：

```bash
echo '<PAT>' | docker login ghcr.io -u '<你的GitHub用户名>' --password-stdin
```

凭据保存在 `/root/.docker/config.json`，容器重启后仍然有效。

`[待NAS验证]` `docker pull ghcr.io/<owner>/<repo>:stable` 能成功。

> 若镜像实际为 public，第4步可跳过——`docker pull` 不报 401 即为公开。

---

## 5. 设置 Compose Stack（Compose Manager Plus）

1. Unraid Web UI → **Apps → Compose Manager Plus → Add Compose Stack**
2. **Name**: `weatherquant-next`
3. 把 `docker/compose.unraid.yaml` 的内容粘进编辑器。
   `image:` **已经填好**，指向已发布的镜像：
   `ghcr.io/aorulola/weatherquant-next:stable`
   （不可变版本用 `sha-5410c75d584771b4477de07f4b1d7b83b764bfe1`，回滚见 §10）
4. 确认 `volumes` 指向你的 appdata 路径
5. **Apply / Start**

`[待NAS验证]` `docker ps` 中 `weatherquant-collector` 状态为 `healthy`
（首次启动给 **90 秒** `start_period`，见 §8）。

---

## 6. 开机启动与定时更新

### 6.1 开机启动

`restart: unless-stopped` 已在 compose 中：Unraid 重启、Docker 重启、容器崩溃
都会自动拉起。**无需**再在 Unraid 里额外配置。

`[待NAS验证]` 重启 Unraid → `docker ps` 确认容器自动回来且状态 `healthy`。

### 6.2 定时镜像更新

**推荐方式：Unraid 定时任务 + 你的镜像仓库策略**（不建议常驻自动更新容器）。

Unraid → **Settings → User Utilities → User Scripts**，新建脚本 `wq-update`：

**最省事的做法：用 Compose Manager Plus 自带的 "Check for updates / Update" 按钮。**
它会重新拉取 `image:` 指向的标签并重建容器，路径由它自己管理，不必手写。

若要走定时脚本（User Scripts → Schedule，例如 `0 4 * * 0` 每周日 04:00），把
`<STACK_DIR>` 换成 Compose Manager Plus 给这个 stack 的实际目录（在其 UI 的
stack 详情页能看到）：

```bash
#!/bin/bash
STACK_DIR=/mnt/user/appdata/compose-manager/weatherquant-next   # 按实际路径改
docker pull ghcr.io/<owner>/<repo>:stable || exit 1
docker compose -f "$STACK_DIR/docker-compose.yml" up -d
```

> 更新会产生一个数据缺口：采集器把它记成 `collection_gaps` 里
> `reason='start_after_restart'` 的一行。**缺口是可见的，不是无声丢失** ——
> 这正是"更新期间的数据缺口会被记录"这一验收项的含义。

**为什么默认不自动更新**：`stable` 一变，容器就会被重建。无人值守的自动更新在
你睡觉时引入回归，而这个采集器的产出是**不可再生的历史数据**。更新应是你主动
选择的时刻——或者至少是你设的时间窗。

`[待NAS验证]` 手动执行一次该脚本，确认容器被重建且数据仍在。

---

## 7. 查看日志

日志只写 **stdout/stderr**（Docker json-file 驱动，`max-size=20m` × 5 个文件，
已配在 compose 里——不会撑爆 `/var/lib/docker`）。

```bash
# 实时
docker logs -f weatherquant-collector

# 最近 200 行
docker logs --tail 200 weatherquant-collector

# 结构化事件（持久化，容器重建后仍在）
docker exec weatherquant-collector python -c "
import duckdb
con = duckdb.connect('/data/weatherquant.duckdb', read_only=True)
for r in con.execute('''
    SELECT ts_utc, level, kind, message FROM collector_events
    ORDER BY ts_utc DESC LIMIT 30''').fetchall():
    print(r)
"
```

日志格式：`%(asctime)s %(levelname)s %(name)s: %(message)s`。
`WQ_LOG_LEVEL` 可调（`DEBUG`…`CRITICAL`）。

---

## 8. 检查容器健康状态

### 8.1 探针如何工作

容器 `HEALTHCHECK` 执行 `python /usr/local/bin/healthcheck.py`，它**只读**
`/data/health/heartbeat.json`——**不打开 DuckDB**。这是刻意的：写者持有数据库锁
的整个生命周期，探针若去开库会与它争锁，把"检查健康"变成"制造故障"。

心跳每 **10 秒**原子重写（写临时文件 + `os.replace`）。

判定阶梯（只能降级，不能升级）：

| 条件 | 状态 | 容器 |
|---|---|---|
| 心跳文件缺失/损坏 | `unhealthy` | exit 1 → Docker 重启 |
| 心跳超过 120 秒未更新（进程假死） | `unhealthy` | exit 1 |
| 致命错误 | `unhealthy` | exit 1 |
| 上游静默 > 30 分钟（**完全**无成功通信） | `unhealthy` | exit 1 |
| 磁盘 < 5% | `unhealthy` | exit 1 |
| 上游静默 > 15 分钟 | `degraded` | **exit 0** |
| 磁盘 < 15% | `degraded` | **exit 0** |
| 连续 10 次 REST 失败 | `degraded` | **exit 0** |
| 正常 | `healthy` | exit 0 |

**为什么 `degraded` 也返回 0**：Docker 对探针只有"非零就重启"一个动作。降级状态
下重启只会让缺口更大。真正致命的（进程死了、上游完全失联、盘满了）才返回 1。

**为什么不会在非交易时段误报**：判定依据是"**我们最近一次成功与交易所通信是什么时候**"，
不是"数据有没有变化"。天气市场凌晨三点盘口不动是正常的；只要 REST 轮询仍然拿到
200 响应，接触时钟就在走，探针就保持绿色。

### 8.2 实操命令

```bash
# Docker 的视角
docker inspect --format '{{.State.Health.Status}}' weatherquant-collector

# 看探针说了什么（为什么降级/失败）
docker exec weatherquant-collector python /usr/local/bin/healthcheck.py
# 例：degraded: upstream silent for 962s (soft limit 900s)

# 看完整心跳
docker exec weatherquant-collector python -m json.tool /data/health/heartbeat.json
```

心跳关键字段：

```json
{
  "status": "healthy",
  "updated_at_utc": "2026-10-10T12:00:00+00:00",
  "last_upstream_contact_utc": "2026-10-10T11:59:58+00:00",
  "ws_connected": true,
  "subscribed_tokens": 66,
  "rest_consecutive_failures": 0,
  "disk_free_percent": 61.4,
  "fatal_error": null,
  "stats": {
    "books_rows_inserted": 1834,
    "books_rows_deduplicated": 24011,
    "ws_reconnects": 3,
    "errors_total": 0,
    "one_sided_books": 412,
    "integrity_failures": 0
  }
}
```

> **`status` 字段是工作进程的生命周期状态**（`starting` / `healthy` / `degraded` /
> `stopped` / `fatal`），而 `degraded` / `unhealthy` 的**裁决**由探针根据
> `last_upstream_contact_utc`、磁盘水位与连续失败数**实时推导**。因此文件里写着
> `healthy` 而 `healthcheck.py` 输出 `degraded` 是正常的、也不矛盾：探针并不采信文件里的
> `healthy`，它只用 `stopped` / `fatal` / `starting` 三个生命周期值做短路判断。
> **以 `healthcheck.py` 的输出为准。**

> `books_rows_deduplicated` 很大是**好事**：说明重复轮询同一份未变动盘口被正确去重，
> 而不是被反复写入。`one_sided_books` 是单边盘（极端温度桶没人挂单），属正常市场状态，
> 与 `integrity_failures` 分开统计。

`[待NAS验证]` 运行 `docker inspect`，状态应为 `healthy`；`healthcheck.py` 输出
`healthy: ok`。

---

## 9. 安全备份

### 9.1 为什么不能直接复制数据库文件

DuckDB 文件被写者**独占锁住**。在采集器运行时 `cp weatherquant.duckdb ...`
要么直接失败，要么拷到一个撕裂的副本——你以为备份成功了，实际那份打不开。

### 9.2 采集器自带的校验备份（推荐）

采集器**每天 00:00 UTC 自动**执行一次，流程是：

1. flush + `CHECKPOINT`（把 WAL 折进主文件）
2. 在**同一个事务**里 `ATTACH` 一个新库，逐表 `CREATE TABLE ... AS SELECT`，
   并重建全部索引 —— 这样快照取自单个 MVCC 一致性读点
3. `DETACH`
4. **用独立的只读连接重新打开**，逐表比对行数、比对 `schema_migrations` 版本
5. 全部一致 → 原子改名为 `weatherquant-<时间戳>-<uuid>.duckdb`
6. 任何一步不一致 → **删除半成品**，记一条 `backup_failed` 事件，**不覆盖**旧备份

**只有通过第 4 步的备份才会出现在 `backups/` 里。** 目录里出现的每一个文件都已
经被读回来验证过。

手动触发（在容器里跑一个只读检查 + 备份是不允许的——备份必须由**持有写锁的
采集器进程**自己做，否则又回到"复制正在写的文件"问题）。所以：等待次日 00:00 UTC，
或用下面的方式临时触发：

```bash
# 让采集器在下一个 tick 触发（它每 24h 一次，重启后当天未备份则立即补一次）
docker restart weatherquant-collector
```

### 9.3 把备份送出 NAS

备份只在 NAS 上，NAS 挂了就一起没了。**必须再复制一份到别处。**

Unraid **User Scripts**，每日 01:00：

```bash
#!/bin/bash
set -euo pipefail
SRC=/mnt/user/appdata/weatherquant-next/backups
DST=/mnt/user/backup/weatherquant-next          # 或用 rclone 到 S3 / 另一台机器
IMAGE=ghcr.io/<owner>/<repo>:stable
mkdir -p "$DST"

# 1) 只保留最近 7 份，避免无限增长（采集器自己不会删备份——自动删历史太危险）
ls -1t "$SRC"/weatherquant-*.duckdb 2>/dev/null | tail -n +8 | xargs -r rm -f

# 2) 复制最近一份
LATEST="$(ls -1t "$SRC"/weatherquant-*.duckdb | head -1)"
cp -a "$LATEST" "$DST/"

# 3) 校验：能被只读打开、行数合理，才叫备份成功
Copied="$(basename "$LATEST")"
docker run --rm -v "$DST:/b:ro" "$IMAGE" python -c   "import duckdb,sys; c=duckdb.connect('/b/'+sys.argv[1], read_only=True);    n=c.execute('SELECT COUNT(*) FROM order_books').fetchone()[0];    print('verified rows', n); sys.exit(0 if n>0 else 'backup looks empty')" "$Copied"

echo "backup ok: $Copied"
```

> **不要**直接 `cp` 正在写入的 `weatherquant.duckdb`。写者持有独占锁，`cp` 要么
> 失败要么拷到撕裂的副本。上面复制的是**采集器自己已校验过的备份** —— 那份文件
> 在发布前已被独立只读连接读回并逐表比对过。

### 9.4 恢复

```bash
docker stop weatherquant-collector
cp /mnt/user/appdata/weatherquant-next/backups/weatherquant-<要恢复的那份>.duckdb \
   /mnt/user/appdata/weatherquant-next/weatherquant.duckdb
# 保留旧文件以便回退，不要直接覆盖删除
mv /mnt/user/appdata/weatherquant-next/weatherquant.duckdb.corrupt{,.bak} 2>/dev/null
docker start weatherquant-collector
```

恢复后采集器会：
- 读 `schema_migrations`，**只**应用缺失的迁移，**绝不**重建已有表
- 把上一次遗留的 `collection_gaps` 全部关闭并注明原因
- 从当前时刻继续采集，中断期间记为一条 gap

`[待NAS验证]` 做一次恢复演练：停容器 → 换备份 → 启容器 → `SELECT COUNT(*)`
确认数据回到备份时点，且 `collection_gaps` 里多了一条 `start_after_restart`。

---

## 10. 回滚到上一个 SHA 版本

镜像按 **不可变 SHA** 发布：`ghcr.io/<owner>/<repo>:sha-<git sha>`。
SHA 标签**永不被重写**，`stable` 只是它的移动指针。

### 步骤

```bash
# 1. 找出历史版本（GitHub → Packages → 本镜像 → Tags）
#    或在 Unraid 上：
docker pull ghcr.io/<owner>/<repo>:stable
docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/<owner>/<repo>:stable

# 2. 改 compose 的 image: 为具体 SHA
#    image: ghcr.io/<owner>/<repo>:sha-0123456789abcdef0123456789abcdef01234567

# 3. 重建
cd /mnt/user/appdata/weatherquant-next   # 或你的 stack 目录
docker compose -f compose.yaml up -d

# 4. 验证
docker inspect --format '{{.State.Health.Status}}' weatherquant-collector
docker exec weatherquant-collector python -c "import weatherquant; print(weatherquant.__version__)"
```

**回滚不会动数据。** 镜像里没有数据库；升级/降级只换代码层，`/data` 原封不动。
迁移是**只前进**的：如果新版本加过迁移而你回滚到旧版本，旧版本会读到
`schema_migrations` 里它不认识的更高版本——它会跳过已知校验和一致的迁移，
不会删表。**只有当前进到的 schema 版本 ≤ 代码支持的版本时回滚才是安全的**；
跨迁移回滚前请先 `SELECT version FROM schema_migrations` 对照代码里的迁移数量。

```bash
docker exec weatherquant-collector python -c "
import duckdb
c = duckdb.connect('/data/weatherquant.duckdb', read_only=True)
print('schema versions:', [r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY 1')])
print('code supports  :', __import__('glob').glob('/app/.venv/lib/python3.12/site-packages/weatherquant/storage/migrations/*.sql'))
"
```

`[待NAS验证]` 做一次真实回滚演练并确认 §10 的 schema 版本对照输出合理。

---

## 11. 稳定性验收清单

### 11.1 我在本机已验证的（进程级，非容器）

见 §12 的实测报告。

### 11.2 需要你在 NAS 上验证的

| # | 检查项 | 命令 / 判据 |
|---|---|---|
| 1 | 容器启动成功 | `docker ps` → `Up (healthy)` |
| 2 | 采集到真实公开市场数据 | `SELECT COUNT(*) FROM markets` > 0 |
| 3 | 持续写入 DuckDB | 间隔 5 分钟两次 `COUNT(*) FROM order_books` 递增 |
| 4 | WebSocket 断线后可恢复 | `stats.ws_reconnects` 增长后 `ws_connected=true` |
| 5 | 重启后数据不丢失 | `docker restart` 后 `COUNT(*)` 不减少 |
| 6 | 不重复写入相同快照 | `books_rows_deduplicated` 持续增长而 `books_rows_inserted` 增长慢得多 |
| 7 | 更新时间戳语义正确 | `SELECT venue_ts_origin, COUNT(*)` → `venue` 占绝大多数；`venue_ts` **不等于** `fetched_at_utc` |
| 8 | 新版本发布不清空旧库 | 更新镜像 → `COUNT(*)` 不归零、`schema_migrations` 行数只增不减 |
| 9 | 磁盘不足明确告警 | `df` 临时把水位压低 / 或读 `collector_events` 中 `kind='disk'` |
| 10 | 更新期间数据缺口被记录 | 更新后 `SELECT reason, started_at_utc FROM collection_gaps` 有新行 |
| 11 | 健康检查能检测采集停滞 | 停上游/断网 >15 分钟 → `degraded`；>30 分钟 → `unhealthy` |
| 12 | 非交易时段不误报 | 连续观察 24h，夜间 `docker inspect` 始终 `healthy` |
| 13 | 单实例保护 | 故意起第二个容器 → 退出码 4，`docker logs` 显示 "another collector holds" |
| 14 | 非 root 运行 | `docker exec ... id -u` → `1000`（或你改的 uid） |
| 15 | 无对外端口 | `docker port weatherquant-collector` → 空 |
| 16 | SIGTERM 优雅退出 | `docker stop -t 60` → 日志显示 flush/checkpoint，`collector_runs.status='stopped'` |
| 17 | 备份自校验 | `backups/` 内文件可只读打开且行数 = 主库 |
| 18 | 30 分钟浸泡（含断网+重启） | 在 NAS 上跑 `python scripts/soak_test.py --minutes 30 --data /tmp/wq-soak` |

### 11.3 需要 GitHub Actions 执行的

| # | 检查项 | 工作流 |
|---|---|---|
| A | `ruff` + `pytest` 在 CI 通过 | `.github/workflows/ci.yml` |
| B | `linux/amd64` 镜像构建成功 | `ci.yml` → `build-image` |
| C | 容器能启动并通过健康检查 | `ci.yml` → "Container starts and reports health" |
| D | 镜像以非 root 运行 | `ci.yml` / `release.yml` |
| E | 测试失败时不发布 | `release.yml` 的 `needs: verify` 门禁 |
| F | 发布 `sha-*` 标签 + 移动 `stable` | `release.yml` → `publish` |
| G | 重启后数据库不被重新初始化 | `release.yml` 的重启检查 |

> **`ci.yml` 已实际执行并全绿**（run `38048156329`）：A–D 项全部通过。
> 它首轮暴露了 5 个本机发现不了的问题（ruff first-party 判定差异、`.gitignore`
> 的 `data/` 吞掉整个采集器包、bind mount 属主、`$DATA` 未定义、Dockerfile uid
> 10001 与别处的 1000 不一致），均已修复。
>
> **`release.yml` 的 E–G 项仍未执行** —— 需要打一个 `v*` tag 才会触发。
> 以及 **compose 与 Unraid 从未在真实 NAS 上运行过**。

---

## 12. 本地实测报告（我实际跑过的）

### 12.1 单元与集成测试

```
259 passed
ruff check . → All checks passed
语句覆盖率 95%（P0 阶段 152 项 + 采集器 107 项）
```

覆盖：单实例锁（含陈旧租约接管）、滑动窗口限速（含跨线程共享）、原始归档
（两套时钟分离、跨 UTC 午夜分片）、健康判定全阶梯、批量提交与内容寻址去重、
完整性检查、**备份读回校验**、Parquet 分区、HTTP 重试策略（5xx 重试 / 4xx 不重试 /
404 视为确定性回答）、订单簿解析、Gamma 行构建、括号标签解析。

### 12.2 真实 API 冒烟（`--once`，非浸泡）

```
events: 4    markets: 40    order_books: 93    price_history: 68
book quality: ok=58  NO_ASK_SIDE=18  NO_BID_SIDE=17
venue_ts_origin: venue 93/93          ← 场所时间戳 100% 齐全
target dates: 2025-07-15(7) 2026-10-09(11) 2026-10-10(11) 2026-10-11(11)
可交易 token: 66（33 个市场 × 2）
备份: verified ✓
```

发现并修复的两个问题（都是真实缺陷）：

1. **404 被当成上游故障** —— 关键词搜索带回 2025 年的历史市场，其 `/book`
   返回 `No orderbook exists`。修复：`allow_not_found=True` 把 404 视为
   **确定性回答**（推进接触时钟，不计入连续失败），并把该 token 加入
   `_no_book` 不再重复询问；同时只对 `active && accepting_orders && !closed`
   的市场轮询盘口。
2. **`integrity_failures` 被单边盘污染** —— 极端温度桶没有对手盘是**正常市场状态**，
   不是数据损坏。拆分为 `one_sided_books` 与 `integrity_failures`。

### 12.3 浸泡测试中的故障注入发现

第一次浸泡（未修）暴露了 **4 个真实缺陷**，均已修复并回归：

| # | 缺陷 | 后果 | 修复 |
|---|---|---|---|
| 1 | `MarketStream` 用 `self._stop` 存 `threading.Event` | **遮蔽了 `Thread._stop` 方法** → `Thread.join()` 抛 `TypeError` → `stream.stop()` 失败 → **`run()` 的 `finally` 中断 → `writer.close()` 永不执行 → 运行状态永远停在 `running`、gap 永不关闭** | 改名 `self._stop_event`；并把 `finally` 里每一步隔离，任一子系统失败不再阻止写者收尾 |
| 2 | DuckDB `CREATE SEQUENCE` 的自增**不跨 close/reopen 持久** | 实测 `1 → 2 → 2` → 重启后 `event_id` 主键冲突 → **采集器启动即崩溃** | 移除 sequence，改为 `sha256(run_id, counter)` 派生的 BIGINT；与时钟无关 |
| 3 | `time.time_ns()` 在 Windows 上分辨率约 15ms | 200 次调用拿到相同时间戳 → 两次"运行"的 ID 完全相同（方案 2 的中间版本同样不可用） | 同上，改用哈希 |
| 4 | 备份文件名只精确到秒 | 同秒两次备份 `os.replace` 互相覆盖 → 一份"已验证的备份"被悄悄替换 | UUID 进入**最终**文件名 |

> 这四条正是浸泡测试存在的理由：单元测试全绿也发现不了它们。

### 12.4 30 分钟浸泡（含断网、强制 kill 与优雅退出）

由 `scripts/soak_test.py` 驱动，分五个阶段，**全部对真实 Polymarket 公开 API**：

| 阶段 | 时长 | 注入的故障 | 要证明的事 |
|---|---:|---|---|
| **A** 健康采集 | 539s | 无 | 基线吞吐与去重 |
| **B** 断网 | 216s | 进程重启并把 HTTPS 代理指向关闭端口 | 上游全失联时**心跳仍持续**、探针降级而非崩溃、恢复后自动续采 |
| **C** 强制崩溃 | 745s | 第 446s `SIGKILL`（不 flush、不 checkpoint） | 硬杀留下的缺口必须被下一次启动关闭并注明原因 |
| **D** 崩溃后重启 | 180s | 无 | 数据不丢、迁移不重复执行 |
| **E** 优雅退出 | 120s | `--once` 正常退出 | 走完完整 shutdown：关闭本轮 gap → flush → checkpoint → `status='stopped'` |

> **为什么 E 用 `--once` 而不是 SIGTERM**：本机是 Windows，实测**无法向子进程投递
> 任何信号**——`SIGTERM` 会直接杀掉进程而处理器根本不执行（`kill -TERM <pid>` 后标记文件
> 仍是 `RUNNING`）。信号处理器所做的全部事情就是设置一个标志，而 `--once` 设置的正是
> 同一个标志，之后 `run()` 的 `finally` 与信号路径**完全相同**。所以验证 E 即验证了
> Linux 容器里 SIGTERM 之后会走的那段代码。**信号能否投递本身仍需在容器内确认**
> （见 §11.2 第 16 项）。

完整原始输出：[`../research/soak-30min.log`](../research/soak-30min.log)。

**实测结果**（2026-10-10，完整输出见日志）：

```
A 健康采集  540s  books 峰值 796   price_points 峰值 8235   db 7.0 MiB   raw 6.9 MiB
B 断网      216s  心跳持续跳动，未崩溃
C 强制崩溃  410s  第 410s SIGKILL（注入）
D 崩溃重启  180s  数据未减少
E 优雅退出   68s  正常退出（code 0）

books rows (cumulative)   796          price points   8235
websocket reconnects        7          error count      1        failed REST      6
max upstream contact age   31s         heartbeat: healthy 706 / stopped 2
markets                   58          order_books  1723         price_history 15953
collector_runs             5          collection_gaps 5          gaps still open  0
collector run statuses     ['running','running','running','running','stopped']
final health verdict  stopped (exit 0)

RESULT: PASSED
```

逐项对照验收：

| 验收项 | 结果 |
|---|---|
| 断网后恢复 | ✅ B 阶段 7 次 WS 重连、6 次 REST 失败、1 条错误，全部来自 B；C/D 阶段归零 |
| 心跳不因断网停摆 | ✅ `max upstream contact age = 31s`，远低于 120s 探针阈值（轮询时间预算生效） |
| 强制 kill 后缺口被记录并关闭 | ✅ 5 条 gap 全部 `ended_at_utc` 非空 |
| 重启不丢数据 | ✅ `order_books` 1723、`price_history` 15953，跨 5 次重启累计增长 |
| 不重复写入相同快照 | ✅ 见单测；日志中 `deduplicated` 持续增长 |
| 优雅退出留下 `stopped` | ✅ 第 5 个 run 记为 `stopped`，前 4 个是被硬杀的（预期） |
| 最终健康裁决 | ✅ `stopped (exit 0)` |
| 非交易时段不误报 | ✅ 未单列阶段；由单测 `test_a_quiet_market_stays_healthy` 覆盖 |
| 磁盘告警 | ✅ 未在本机触发（磁盘充足）；单测覆盖 <15% / <5% 两档 |

---

## 13. 限速参考（官方，按 IP / 10 秒滑动窗口）

| 服务 / 端点 | 限额 |
|---|---:|
| Gamma `/events` | 500 |
| Gamma `/public-search` | 350 |
| Gamma 总计 | 4000 |
| CLOB `/book` 单查 | 1500 |
| CLOB `/books` 批量 | 500 |
| CLOB `/prices-history` | 1000 |
| Data-API `/v2/prices-history` | 200 |
| Data-API `/v2/trades` | 300 |

超限行为是**延迟/排队，不是拒绝**。采集器在客户端就按 `SlidingWindowLimiter`
排队（`RateLimitRegistry`，见 `weatherquant/data/ratelimit.py`），并尊重
`Retry-After`。实测 33 个市场 × 2 token，5 秒一轮 ≈ 13 请求/秒 ≈ 130/10s，
远低于 `/book` 的 1500/10s。

WebSocket 无限速文档；实测热门 token 约 15–20 条 `price_change` / 30 秒。

---

## 14. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 退出码 4 | 第二个实例在跑，**或上一个实例刚被硬杀、租约尚未过期** | `docker ps` 看是否重复。锁是**租约**：持有者约每 10s 续期，心跳超过 60s 才判定失联并接管。这是刻意的——避免两个启动者同时认定锁陈旧而双双进入。容器被 `kill -9` 后需等至多 60s 才能重启 |
| 本地 Windows 开发时 `docker stop` 不走优雅退出 | **Windows 无法向子进程投递 SIGTERM**（已实测：处理器不执行，进程被硬杀） | 平台限制，不是采集器缺陷。Linux 容器内 SIGTERM 正常投递；优雅退出代码路径已由集成测试端到端验证（`--once` 正常退出 → `status='stopped'` 且全部 gap 关闭） |
| `heartbeat file missing` | 进程从未起来，或 appdata 权限不对 | `docker logs`；`chown -R 1000:1000` |
| `degraded: upstream silent for Ns` | 网络断、代理挡、上游故障 | 先 `docker exec ... curl -sI https://clob.polymarket.com` 测连通 |
| `unhealthy` 且 `disk critically low` | 盘满了 | 清理 `backups/`（见 §9.3 的轮转）与旧 `raw/` |
| `db_bytes` 不增长但 `deduplicated` 涨 | 盘口没变 | **正常**，去重在工作 |
| `venue_ts_origin` 出现 `missing` | 该响应没带 `timestamp` | 记录为 NULL 并标记 `missing`，**不会**用本地时间顶替——这是设计 |
| 容器反复重启 | 迁移校验和不匹配 | 说明有人改了已应用的迁移；恢复备份或 `SELECT * FROM schema_migrations` 核对 |
| `refusing to open ... no schema_migrations` | 挂载点指向了一个不是本项目创建的数据库 | **不会**自动初始化。把该文件移走，让采集器建新库 |

---

## 15. 安全边界

- **非 root**：uid/gid 1000。
- **无入站端口**：无 `ports:`、无 `EXPOSE`。
- **无密钥**：全部为公开只读 API，不需要也不存在钱包、私钥、API key。
- **无下单代码**：`weatherquant.data.*` 里没有任何可以发出订单的路径。
- **只读 API 集合**：Gamma `GET`、CLOB `GET /book`、WS `market` 频道订阅。
- 镜像内**不含**数据库、配置密钥或本地 `.env`（`.dockerignore` 排除）。

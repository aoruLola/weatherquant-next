# 影子引擎在 Unraid NAS（Oreochromis）上的部署

2026-10-11。P2-0。
本文件与 [`docker/compose.shadow.unraid.yaml`](../docker/compose.shadow.unraid.yaml)
配套使用；两者的路径、变量、端口必须一致。

采集器本身的部署见 [`unraid-collector-deployment.md`](unraid-collector-deployment.md)，
**本文不重复它的内容，也不修改它**。

---

## 0. 与采集器的关系（先读这一节）

| | 采集器 | 影子 |
|---|---|---|
| 镜像 | `ghcr.io/aorulola/weatherquant-next:stable` | `ghcr.io/aorulola/weatherquant-next-shadow:shadow-stable` |
| 容器名 | `weatherquant-collector` | `weatherquant-shadow` |
| 主机目录 | `/mnt/user/appdata/weatherquant-next` | `/mnt/user/appdata/weatherquant-shadow` |
| 数据库 | `weatherquant.duckdb` | `shadow/shadow.duckdb` |
| 单实例锁 | `.collector.lock` → 退出码 4 | `.shadow.lock` → 退出码 4 |
| 发布工作流 | `release.yml`（tag `v*`） | `shadow-release.yml`（tag `shadow-v*`） |
| 端口 | 无 | 无 |

**禁止事项**（每一条都会在某处被结构或测试挡住）：

- ❌ 把两个容器都指向 `/mnt/user/appdata/weatherquant-next`
  —— 采集器对那个目录的锁持有其整个生命周期；影子的 `WQ_DB_PATH`
  指过去只会被拒绝，而两个不同引擎共用一个目录则会让谁的布局都不对。
- ❌ 让影子打开 `weatherquant.duckdb`
  —— 影子代码里没有任何指向它的路径，它只读挂载目录里的 JSONL。
- ❌ 用影子的发布工作流去动 `:stable`
  —— 那是**另一个镜像名、另一个 tag 命名空间**，物理上写不到采集器的包。
- ❌ 给任一容器加 `ports:`
  —— 两者都只出站。

---

## 1. 前提

- 采集器已在运行，且 `/mnt/user/appdata/weatherquant-next/raw/` 里有归档
  （`raw/gamma/`、`raw/rest/`、`raw/ws/` 各自按日分文件）。
- `models/manifest.json` 存在且固定了 `shadow_model_names` 里的每个模型
  （随镜像一起发布，正常情况下无需操作）。
- Compose Manager Plus 已安装。

先在开发机或 CI 上确认工件与阈值：

```bash
uv run python -c "from weatherquant.models.artifacts import load_manifest; print(load_manifest('models'))"
uv run python -c "from weatherquant.shadow.config import load_thresholds; print(load_thresholds('configs/shadow-thresholds.json').version)"
```

---

## 2. 准备目录

```bash
mkdir -p /mnt/user/appdata/weatherquant-shadow/{shadow,health}
chown -R 1000:1000 /mnt/user/appdata/weatherquant-shadow
```

镜像内以 uid 1000 运行；bind mount 会遮掉镜像里的 `chown`，所以主机目录
必须先匹配。若你的 Unraid 用户偏好 nobody（`99:1000`），在 compose 里加
`user: "99:1000"`，并把上面的 `chown` 改成 `99:1000`。

**采集器的 raw 目录不需要也不应该被改动** —— 它以 `:ro` 挂载。

---

## 3. 获取镜像

发布由 `.github/workflows/shadow-release.yml` 完成，tag 为 `shadow-vX.Y.Z`：

```bash
docker pull ghcr.io/aorulola/weatherquant-next-shadow:shadow-stable
```

| tag | 含义 |
|---|---|
| `shadow-stable` | 最近一次通过全部验证的版本（可变指针） |
| `shadow-vX.Y.Z` | 某次具体发布 |
| `sha-<commit>` | 不可变；**回滚目标** |

**首次部署建议先用 `sha-<commit>` 钉死**，跑满 24 小时验收后再切到
`shadow-stable`。镜像 tag 同时固定了模型工件与阈值文件，因此"哪组参数
做的这个决定"由 tag 单独回答。

---

## 4. 部署 stack

把 `docker/compose.shadow.unraid.yaml` 放进 Compose Manager Plus 的 stack
目录（**与采集器同一个 stack 目录即可，文件名不同不冲突**），然后 Update。

确认三处：

```yaml
volumes:
  - /mnt/user/appdata/weatherquant-shadow:/data
  - /mnt/user/appdata/weatherquant-next/raw:/collector-raw:ro   # ← 只读
environment:
  WQ_COLLECTOR_RAW_DIR: /collector-raw
  WQ_SHADOW_DB_PATH: /data/shadow/shadow.duckdb
```

### 4.1 需要代理吗

采集器当前没有任何代理配置（见采集器部署文档）。影子的出站只有
**Open-Meteo**（`single-runs-api.open-meteo.com`），不直接访问 Polymarket
——— 行情全部来自只读挂载的归档。

若 NAS 需要代理才能访问 Open-Meteo，在 compose 的 `environment` 里取消注释：

```yaml
    HTTPS_PROXY: http://proxy:3128
    NO_PROXY: localhost,127.0.0.1
```

httpx 认这几条标准变量。代理不可用时影子**不会崩**：预报抓取失败会被记录
为 `forecast_fetch_failed` 并指数退避，行情侧照常评估，相关市场给出
`NO_FORECAST`。

---

## 5. 首次启动与验收

启动后 1–2 分钟内：

```bash
# 1. 容器在跑且健康
docker ps --filter name=weatherquant-shadow
docker logs weatherquant-shadow | tail -40

# 2. 健康文件（探针读的就是它，不需要打开数据库）
cat /mnt/user/appdata/weatherquant-shadow/health/heartbeat.json | jq .

# 3. 报告
docker exec weatherquant-shadow weatherquant-shadow report
docker exec weatherquant-shadow weatherquant-shadow report --json | jq '.decisions'
```

`report` **不打开数据库** —— 引擎用它自己已有的连接把报告算好，原子发布在
`/data/reports/report.json`（`WQ_SHADOW_REPORT_PATH`），CLI 只读那个文件。
引擎运行期间直接开 DuckDB 会被文件锁拒绝，这是设计而非故障；
确实要直接读库（引擎已停）时用 `report --offline`。

宿主侧直接看快照：

```bash
jq '{generated_at_utc, expires_at_utc, source, evaluated: .decisions.evaluated}' \
  /mnt/user/appdata/weatherquant-shadow/reports/report.json
```

`generated_at_utc` 超过 `expires_at_utc` 后，文本输出标 `EXPIRED`、JSON 里
`stale: true` —— 过期报告仍然打印，但必须带着这个标记读。

### 5.1 24 小时验收清单

部署后**先连续运行至少 24 小时**，逐项确认：

| # | 检查 | 命令 / 位置 |
|---|---|---|
| 1 | 模型预测次数 | `report` → `prediction_quality.by_model` |
| 2 | 行情评估次数 | `report` → `decisions.evaluated` |
| 3 | 拒绝原因分布合理 | `report` → `decisions.primary_reasons` |
| 4 | **未污染采集器数据** | `sha256sum` 采集器 `weatherquant.duckdb` 前后对比；`raw/` 目录 mtime 不因影子运行而变化 |
| 5 | **重启可恢复** | `docker restart weatherquant-shadow`，之后 `report` 的表行数不回退 |
| 6 | **代理断线不制造机会** | 临时把 `HTTPS_PROXY` 指向关闭端口 → 重启；确认出现 `forecast_fetch_failed`，相关市场是 `NO_FORECAST` 而**不是** ACCEPT |
| 7 | **行情陈旧时不输出 ACCEPT** | 停采集器 > 15 分钟 → 确认 `COLLECTOR_DISCONNECTED`，ACCEPT 归零 |
| 8 | 单实例 | `docker run --rm -v .../weatherquant-shadow:/data ... run --once` → 退出码 **4** |
| 9 | SIGTERM 优雅退出 | `docker stop -t 60 weatherquant-shadow` → 心跳 `status` 为 `stopped`，`shadow.duckdb` 存在 |
| 10 | 非 root | `docker exec weatherquant-shadow id -u` → `1000` |
| 11 | 只读挂载 | `docker exec weatherquant-shadow sh -c 'echo x >> /collector-raw/gamma/'$(date -u +%F)'.jsonl'` → 必须失败 |
| 12 | 无端口 | `docker ps` 该容器无 `PORTS` |
| 13 | **报告不碰库** | 引擎运行中连续调用 `report --json` **10 次全部成功**，且期间心跳 `updated_at_utc` 仍在前进 |
| 14 | **模式分离** | `report` 里 `ACCEPT live / replay` 的 **live 必须 > 0**；若采集器已停，`live_accepted` 必须为 **0**（见 §5.3） |

验收清单 6 与 7 是**最容易被忽略也最重要**的两条：一个静默的输入故障
如果被记成"发现机会"，整个系统的结论就都不可信了。

### 5.2 全部 NO_TRADE 也是通过

当前模型在样本外**劣于市场**（Brier 1.49 vs 1.03）。因此
`ACCEPT = 0`、连续数日 `NO_TRADE` 是**预期结果**，不是故障。
验收看的是：理由正确、数据可靠、时间一致、成本一致。

### 5.3 LIVE / REPLAY 与"采集器停机不得输出 ACCEPT"

影子既能尾随正在写入的归档，也能重读历史。默认 `WQ_SHADOW_MODE=auto`：
启动时按最新归档行的年龄判定一次，**整个运行期不再改变**（理由见
[`shadow-engine.md`](shadow-engine.md) §3.6）。

| 场景 | 期望结果 |
|---|---|
| 采集器在跑 | `mode=LIVE`，`ACCEPT live / replay = N / 0` |
| **采集器停机，影子一直在跑** | `mode=LIVE` 保持不变，`COLLECTOR_DISCONNECTED`，**`live_accepted = 0`** |
| 采集器停机后影子才重启 | `mode=REPLAY`，可对历史作判断，但 **`live_accepted = 0`**，且 warnings 点名这些是 REPLAY |
| 明确指定 `WQ_SHADOW_MODE=live` | 无论归档多旧都按实时处理，静默一律报 `COLLECTOR_DISCONNECTED` |

**验收方式**：

```bash
docker exec weatherquant-shadow weatherquant-shadow report --json   | jq '{live: .decisions.live_accepted, replay: .decisions.replay_accepted, mode: .decisions.by_mode}'
```

第 3 行是关键：**采集器离线时 `live` 必须为 0**。历史重放可以得出结论，
但那些结论永远带 `mode=REPLAY`，且不计入 live 计数 —— 一个数据源已经死掉、
日志却还在说 ACCEPT 的服务，是不能部署的。

---

## 6. 健康检查

探针：`/usr/local/bin/healthcheck.py` → 只读 `/data/health/heartbeat.json`。

| 状态 | 条件 | 退出码 |
|---|---|---|
| `starting` | 心跳还没写出来 | 0 |
| `healthy` | 心跳新鲜、磁盘充足、近期有决策 | 0 |
| `degraded` | 磁盘 <15%、或 30 分钟内没有决策 | 0 |
| `unhealthy` | 心跳 >120s 未更新、`fatal_error`、或磁盘 <5% | 1 |

**degraded 退出 0 是刻意的**：`没有新决策` 与 `采集器静默` 往往是同一件事
的两次陈述，重启引擎只会把正在报告的缺口拉得更宽。

磁盘阈值可用 `WQ_DISK_WARN_PERCENT` / `WQ_DISK_CRITICAL_PERCENT` 覆盖
（CI 用 10 / 3，NAS 默认 15 / 5 —— 覆盖不等于放松）。

---

## 7. 日志

compose 已设 `json-file`、`max-size: 20m`、`max-file: 5`，不会撑爆
`/var/lib/docker`。结构化的持久记录在 `shadow.duckdb`：

```sql
SELECT kind, severity, COUNT(*), MAX(ts_utc)
FROM data_quality_events GROUP BY 1, 2 ORDER BY 3 DESC;
```

常见 kind：`shadow_started`、`archive_gap`、`archive_file_truncated`、
`archive_line_malformed`、`forecast_fetch_failed`、`prediction_refused`、
`gamma_parse_problem`。

---

## 8. 单实例、重启与恢复

- 锁文件 `/mnt/user/appdata/weatherquant-shadow/shadow/.shadow.lock`，
  `O_CREAT|O_EXCL` 创建，含 `run_id / pid / hostname / 心跳时刻`。
- **第二个实例被拒绝时退出码 4** —— 那是成功的拒绝。
- 租约是**租借**的：持有进程死亡或心跳超时即可接管。影子每个周期都会
  `heartbeat()` 刷新它，正常运行时不会被抢。
- `kill -9` 后最多等 `max(60s, tick×4)` 再启动。
- 重启后：偏移、目录、盘口来自库，**不回放、不重复、不重新抓取已持有的
  上游运行**。

---

## 9. 备份与回滚

### 9.1 备份

`shadow.duckdb` 是**衍生数据** —— 归档还在，就能重放重建。真正不可再生的是
**模型工件与阈值**，它们随镜像与仓库走，不在这里备份。

若仍要备份数据库：**必须先停容器**（`docker stop`），再复制
`shadow/shadow.duckdb`。运行中的 DuckDB 文件外部 `cp` 会拿到不一致的副本
—— 与采集器同一条规则。

```bash
docker stop weatherquant-shadow
cp /mnt/user/appdata/weatherquant-shadow/shadow/shadow.duckdb \
   /mnt/user/appdata/weatherquant-shadow/backups/shadow-$(date -u +%Y%m%dT%H%M%SZ).duckdb
docker start weatherquant-shadow
```

### 9.2 回滚

```yaml
image: ghcr.io/aorulola/weatherquant-next-shadow:sha-<commit>
```

改这一行 → Update。**数据库 schema 是前向迁移的**，回滚镜像不会回滚 schema；
跨版本回滚前请确认该版本仍能打开现有库（迁移器会用 checksum 拒绝不兼容的
历史，而不是静默跳过）。

回滚**不影响采集器** —— 两个镜像、两个 tag 命名空间。

---

## 10. 升级

1. `git pull`，确认 `models/manifest.json` 与 `configs/shadow-thresholds.json`
   的变更已评审（阈值 `version` 有变动必须看）。
2. `uv run pytest -q`
3. 推 tag `shadow-vX.Y.Z` → `shadow-release.yml` 验证通过后发布。
4. 改 compose 的 `image:` 为新 tag（或 `shadow-stable`）→ Update。
5. 跑一遍 §5.1 的 1、2、4、5。

升级期间的缺口会以 `archive_gap` 记在**影子自己的**库里；采集器不受影响
（它一直在跑）。

---

## 11. 故障排查

| 症状 | 原因 | 处置 |
|---|---|---|
| 容器反复重启，日志 `refusing to start a second writer` | 第二个实例，或上一个进程未释放 | 退出码 4 是正常的；等租约过期，确认没有第二个 stack |
| `decisions.primary_reasons` 全是 `COLLECTOR_DISCONNECTED` | `raw/` 没挂上或采集器停了 | `docker inspect` 看挂载；`ls /mnt/user/appdata/weatherquant-next/raw/` |
| 全是 `NO_FORECAST` | 上游不可达 / 未发布 | 看 `data_quality_events` 里的 `forecast_fetch_failed`；试 §4.1 代理 |
| 全是 `NO_VALID_MODEL` | manifest 缺项或工件未随镜像打入 | `docker exec ... python -c "from weatherquant.models.artifacts import load_manifest; print(load_manifest('/app/models'))"` |
| 心跳 `unhealthy: heartbeat is Ns old` | 进程卡住 | `docker logs`；必要时 restart（重启会恢复，不会丢数据） |
| `STALE_ORDERBOOK` 持续 | 采集器归档不再增长 | 先确认采集器健康，再看影子 |
| 磁盘 `degraded` | 空间不足 | 清理；阈值见 §6 |

---

## 12. 完整性声明

本部署**不包含**：真实下单、钱包、私钥、持仓账本、Dashboard、
自动更新采集器镜像。这些要么是后续阶段，要么明确禁止。

# 流式研究任务生产调度与长期运行 Runbook（WQN-28）

2026-10-26。调度器：`scripts/streaming_schedule.sh`；长跑 harness：
`scripts/streaming_soak.py`；回滚工具：`scripts/pin_performance_generation.py`；
保留策略：`scripts/prune_performance_generations.py`。上游实现：PR #11/#12/#13
（已合并）。

---

## 1. 调度模型：cron 拥有节奏，flock 保证互斥，状态库保证崩溃恢复

两个角色共享一个状态库（DuckDB 单写者），节奏不同：

| 角色 | cron 节奏 | CLI 形态 | 作用 |
|---|---|---|---|
| `ingest` | 每 5 分钟 | `build-performance --no-publish` | 消费新的决策分片进状态库，不发布 |
| `publish` | 每 20 分钟 | `build-performance --no-ingest` | 从状态库重建并发布六个快照 |

刻意的选择：

- **节奏归 cron**（Unraid User Scripts 或 NAS crontab），不做一个常驻守护
  进程——守护进程多一件要监控的事，多一种静默死亡的方式；每次调用都是
  新进程，`flock -n` 让迟到者**跳过而不是排队**（丢掉的 tick 由下一拍补上，
  一次跳过的 publish 代价是 20 分钟，不是正确性）；
- **崩溃恢复不需要代码**：状态库在一个事务里提交已消费行与水位，SIGKILL
  留下的是上一个完整水位，下一拍重做（`test_decouple_processes.sh` 与
  soak 均已证明）；
- **冷启动**：publish 在第一次 ingest 之前触发时，状态库尚不存在——记
  `skipped-empty-state`，是调度伪影而非管线故障（由调度器预检，CLI 的
  空库拒绝是第二道闸）。

## 2. 部署（NAS）

```bash
# /boot/config/plugins/user.scripts 或 crontab：
*/5  * * * *  WQ_SCHEDULE_ROLE=ingest  WQ_EXPORT_DIR=/mnt/user/appdata/weatherquant-shadow/reports/decisions-export \
  WQ_PERFORMANCE_OUT=/mnt/user/appdata/weatherquant-performance \
  WQ_STATE_DB=/mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb \
  WQ_SETTLEMENTS_JSON=/mnt/user/appdata/weatherquant-settlements/settlements.json \
  PY=/mnt/user/appdata/wq-venv/bin/python /mnt/user/appdata/wq/scripts/streaming_schedule.sh >> /var/log/wq-schedule.log 2>&1
# publish 错开分钟（2-59/20 而不是 */20）：*/5 与 */20 在 :00/:20/:40 同时
# 触发时两角色抢同一把 flock，本机验收实测 75% 的 publish 被 skipped-locked。
# 错峰后同一分钟内不再有第二个角色；调度器另支持 WQ_LOCK_WAIT_SECONDS（flock
# -w 有界等待）与 WQ_RUN_TIMEOUT_SECONDS（默认 900s，挂死的运行被杀并记
# timeout——否则一次挂起会让后续每一拍都 skipped-locked）。
2-59/20 * * * *  WQ_SCHEDULE_ROLE=publish <同上环境变量> /mnt/user/appdata/wq/scripts/streaming_schedule.sh >> /var/log/wq-schedule.log 2>&1
```

每次调用向 `<state db 目录>/schedule.jsonl` 追加一行 JSON：
`{ts, role, status: ok|failed|skipped-locked|skipped-empty-state, exit,
duration_s, rss_kb（装了 GNU time 时）, watermark}` —— **这份台账就是验收
证据的主体**。

### 部署镜像 SHA 核验

```bash
docker inspect weatherquant-collector --format '{{.Config.Labels}}' | tr ',' '\n' | grep -i sha
# 与 git log origin/main -1 的 sha 一致；research CLI 与镜像同仓同 sha（pip 安装源为仓库）
docker exec weatherquant-collector python -c "import weatherquant, importlib.metadata as m; print(weatherquant.__version__ if hasattr(weatherquant,'__version__') else m.version('weatherquant-next'))"
```

## 3. 验收清单（WQN-28）

### 3.1 本机已验证（加速节奏，真实调度脚本）

```bash
uv run python scripts/streaming_soak.py --minutes 12 --work /tmp/wq-soak \
    --ingest-seconds 45 --publish-seconds 150 --feeder-seconds 30 \
    --crash-after-seconds 260 --keep
```

判定（全部实测）：水位随新分片前进；flock 跳过被记录；**SIGKILL 中途
ingest 后下一拍恢复且水位越过崩溃前**；每次调用峰值 RSS 低于 1.5 GB 门
（装 GNU time 时）；发布树 `current.json` 恒指向完整世代。结果见 PR 描述。

### 3.2 需 NAS（≥24 小时，未验证前不标完成）

```bash
# ① 水位持续前进：24h 内 schedule.jsonl 的 ingest 水位严格递增、
#    publish 每 20min 出新世代
tail -1 /mnt/user/appdata/weatherquant-performance-state/schedule.jsonl
# ② 无错误重叠：没有连续两次 failed；failed 行的 stderr 尾巴已人工过目
grep -c '"status":"failed"' .../schedule.jsonl
# ③ SSH/WebUI 可用：调度运行期间（尤其 publish 的 60s 窗口）
#    ssh 延迟与 WebUI 响应采样——对比 docs/performance-deployment.md §11.2 的基线
for i in $(seq 1 288); do s=$(date +%s%N); ssh -o ConnectTimeout=5 nas true; e=$(date +%s%N); echo "$(( (e-s)/1000000 ))ms" >> /tmp/ssh-latency.log; sleep 300; done
# ④ 响应稳定：Dashboard /api/v1/performance/summary 的 P95 与单请求分配
uv run python scripts/bench_performance_api.py --generation /mnt/user/appdata/weatherquant-performance
# ⑤ 资源预算：每行 rss_kb（装 GNU time 后）< 1.5GB；容器内存上限见 memory-acceptance.md
# ⑥ 可回滚（演习一次，不真回滚）：
uv run python scripts/pin_performance_generation.py --performance /mnt/user/appdata/weatherquant-performance --list
uv run python scripts/pin_performance_generation.py --performance ... --generation <上一世代>   # dry run 打印
# ⑦ 保留策略在跑（防 288 世代/天）：
uv run python scripts/prune_performance_generations.py --performance ... --dry-run
```

**回滚（真实执行需单独授权）**：坏发布 ≠ 删目录。`--pin` 移动指针到上一
完整世代；Dashboard 与 MCP 各自按 `DASHBOARD_SHA` / `MCP_SHA` 回退镜像；
状态库不需要动（水位只前进）。

## 4. 边界

- 调度器只触碰研究侧三路径（export 只读、state DB、performance out），
  **不打开 shadow 活库、不重启容器、不写生产 collector 数据**；
- 本机 soak 用 fixture 数据集标签；生产 publish 的 `WQ_DATASET=live`
  由部署环境变量决定，**不是脚本默认**；
- 24h/SSH/WebUI 项只能在 NAS 上观察，本机无法模拟，未验证即不标完成。

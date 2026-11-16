# Performance 快照体积、API 延迟与 retention 监控（WQN-31）

2026-10-26。监控器：`scripts/performance_monitor.py`。上游：契约 §14
（分片窗口）、PR #11–#13（流式引擎）、`bench_performance_api.py`（延迟基准）、
`prune_performance_generations.py`（保留策略）。

---

## 1. 监控什么、为什么

流式管线的内存故事全部押在三件**结构性事实**上，此前没有任何东西持续
核对它们：

1. **页面就是页面**。聚合文件（summary/win-rate/edge/models/settlements）
   与 opportunities **指针**的设计体积是 KB 级，与历史长度无关；明细按
   分片发布，单片由明细窗口决定。任何一个聚合文件随历史长大，就是
   §14 之前的形状回来了——Dashboard 的 `json.load` 会先死，任何人工
   巡检都来不及发现。门：聚合文件 ≤ 5 MiB、单片 ≤ 64 MiB（均可调）。
2. **世代树在保留饮食上**。每次发布产生一个世代；5 分钟节奏 = 288/天
   ≈ 2.2 GB/天（PR #13 的实测）。监控器报告世代数与总体积，超 8 GiB
   门即 FAIL 并指向 `prune_performance_generations.py`。**动作仍在
   prune 工具**，监控器只判定、不删任何东西。
3. **端点保持便宜**（可选 `--dashboard`）：核心端点 p95 延迟 ≤ 1s、响应体
   ≤ 32 MiB。机会分页请求 `limit=200` 须成功且最多返回 200 条。当前 API 对超限请求返回 422，监控器核对错误是否明确指出查询参数 `limit` 的上限为 200。

判定全部实测：退出码 0 = 所有门通过；1 = 任何门失守；`--report` 输出
机器可读 JSON。

## 2. 用法

```bash
# 离线画像（只读 published 目录，不写任何东西）
uv run python scripts/performance_monitor.py --performance /mnt/user/appdata/weatherquant-performance

# 连上 Dashboard 一起探（token 与部署同源）
uv run python scripts/performance_monitor.py --performance ... \
    --dashboard http://127.0.0.1:8787 --dashboard-token "$WQ_MCP_DASHBOARD_TOKEN" \
    --probes 20 --report /tmp/perf-monitor.json

# 保留策略现状（监控器只报告；真正删除用 prune 的 dry-run → apply）
uv run python scripts/prune_performance_generations.py --performance ... --dry-run
```

建议纳入 NAS 的每日 cron，失败即告警（与 WQN-28 的 schedule.jsonl 台账
互补：那边看"跑没跑、水位动没动"，这边看"发布物长没长歪"）。

## 本机复验与生产采样

2026-10-31 的 Windows 隔离测试从 150 万条合成决策构建快照，流式引擎实际摄取 79 个分片，生成 73,188 个 episode。通过本机 HTTP 对每个端点发起 20 次请求，按最近秩计算 P95。参考本金重算为 698.2 ms，其他五个端点为 2.4 至 10.3 ms。Dashboard 单进程峰值工作集为 118.1 MiB，本轮测试门槛为 512 MiB。该数值只涵盖 Dashboard 进程。

测试先从另一份已发布快照取得游标，再切换到百万行快照。旧游标返回 409，合法分页最多返回 200 条，超限请求返回指向 `limit` 的 422。

在 NAS 上测量时，在发布前保存机会分页响应中的 `data.next_cursor`。正常发布下一代快照后，给上述监控命令加上 `--previous-cursor "$PREVIOUS_CURSOR"`。测量 RSS 时，同时指定 `--dashboard-pid "$DASHBOARD_PID" --max-dashboard-rss-mib 512`。PID 应是实际服务该端点的单个工作进程，监控器需能读取同一 PID 命名空间的 Linux `/proc`。报告记录进程生命周期的峰值 RSS，无法读取时检查失败。仅检查快照文件的报告不包含 HTTP 或内存测量。

有可清理世代时，`--apply` 要求 `current.json` 指向清单中的世代。删除失败会返回非零状态，回收量只计入成功移除的目录。

## 3. 验收对照与证据（本机，2026-10-26）

| 工单验收 | 证据 |
|---|---|
| 百万级规模下 P95/RSS 有上限 | 1,500,000 行合成历史（真实流式引擎构建，1GB 引擎预算）→ 监控器全门通过（见下方数字）；构建期 RSS 上限由 `bench_performance_streaming.py` 的既有记录覆盖（§11：1.5M 行 299 MB） |
| 分页 409 世代游标与 limit≤200 正常 | limit 钳制探针：`limit=1000000` → 服务端最多 200（`test_the_limit_clamp_gate_over_a_stand_in_transport` 同时断言钳制消失会被抓住）；409 世代游标语义由仓库既有单测覆盖（`dashboard/tests/test_performance_detail_api.py` 的跨代测试），监控器可用发布前保存的游标复验 |
| 磁盘使用量受保留策略控制 | 世代树体积门 + 超门时 FAIL 并指向 prune 工具（`test_the_generations_tree_gate_demands_a_prune`）；prune 本体已有独立工具与测试 |

**1.5M 行实测**（构建 ~90s，引擎预算 1GB）：

```
performance monitor performance-monitor-1 over /tmp/wq-31-bench/performance
  current generation: 20261026T120000Z-ec9e1a8f33ae
    summary.json            32.0 KiB     # 修复前同文件 39.6 MB
    edge.json               17.9 KiB     # 修复前 6.8 MB
    win-rate.json           33.5 KiB
    models.json              4.0 KiB
    settlements.json       273.9 KiB
    opportunities.json       5.5 KiB     # 指针——修复前 49.1 MB 单文件
    opportunities/       13115.1 KiB (20 parts)  # 分片明细，单片 < 64 MiB
  generations: 1 total, 13.2 MiB（世代合计；修复前 95.8 MB）
RESULT: PASSED

prune 干跑（1 个世代，保留策略正常）：
  Will keep: 1 generations (13.2 MB) / Will delete: 0 generations (0.0 B)
```

（具体数字见 PR 描述；旧形状对照：§12.1——修复前 summary.json 39.6 MB、
opportunities.json 49.1 MB、/opportunities 解析 163 MB 只为返回 50 行。）

**测试 5 项**：真实发布树全门通过；6 MiB 聚合文件被抓（"growing with the
history"）；世代树超门指向 prune；钳制消失被抓住（替身 transport，无 socket）；
不可达 Dashboard 是 failure 不是 crash。全套 `uv run pytest -q` 见 PR。

## 4. 边界

- 监控器**只读**：不写 published 目录、不触发 prune 的删除、不重启任何服务；
- `--dashboard` 探针需要能访问 Dashboard 端口（本机验证用 ASGI/替身，
  生产探活走 NAS 本机回环）；
- 409 世代游标的**生产**行为（发布中途游标跨代）需 NAS 观察，本机由单测
  覆盖语义。


### 评审修复验证

每次探针请求的非 200 响应均记录失败，后续成功响应不能清除先前 503 等错误。

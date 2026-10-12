# WeatherQuant Dashboard — Architecture (P2-1)

一个只读 Web 监控台，第三个独立容器。Collector 与 Shadow 的架构、数据语义、
采样行为**完全不变**；Dashboard 只是它们已发布文件的读者。

```text
                    Local Network
                          |
                          v
                WeatherQuant Dashboard      (weatherquant-dashboard)
                   Port 8787 / HTTP          FastAPI + built React SPA
                   1 container, 1 process    uid 1000, read-only mounts
                          |
        +-----------------+-------------------+
        | (JSON, read-only, atomic renames)   |
        v                                     v
   Collector 快照                       Shadow 快照
   health/heartbeat.json                health/heartbeat.json
   raw/{gamma,rest,ws}/*.jsonl          reports/report.json
        |                               reports/*-snapshot.json (待集成)
        v                                     v
   Collector DuckDB                  Shadow DuckDB
   (Dashboard 永远看不到这个文件)      (Dashboard 永远看不到这个文件)
```

## 1. 三条不可协商的规则

1. **绝不打开 DuckDB。** 两个引擎各自持有独占租约，DuckDB 会拒绝任何第二个进程
   （read-only 也一样）。Dashboard 只读小 JSON 文件 + 归档文件的**有界尾部**。
   `dashboard/tests/test_api.py` 用两层测试钉死这一点：静态扫描（包内不得出现
   `import duckdb` / `duckdb.connect`）+ 运行时把 `duckdb.connect` 换成抛异常的
   函数后所有端点仍然 200。
2. **绝不编造数据。** 缺失的生产者 → `unavailable` + 集成指针；损坏的文件 →
   `corrupt` + 原因；过期 → `stale` + 年龄。永远不用 0 冒充"没有发生"。
3. **新鲜度与健康是两个独立判断。** Dashboard 返回 200 只说明这个进程在服务；
   每个 payload 自带 `freshness`，Docker healthcheck 只看 `/healthz` 的存活，
   因为 Collector 挂掉时重启 Dashboard 毫无意义——它正是解释这次故障的组件。

## 2. 代码布局

```text
src/weatherquant/dashboard/      后端包（复用 weatherquant 现有解析器）
  config.py        WQ_* 路径与阈值设置（无凭据字段）
  contracts.py     Pydantic 响应契约（Envelope[T] 统一信封）
  sources.py       JSON 快照读取：mtime 缓存 + 每请求重算年龄 + 缓存不缓存年龄
  archive.py       归档有界 tail -> 市场/盘口视图（复用 shadow.events 的解析函数）
  builders.py      端点数据组装（同步、可测、不含 HTTP）
  trends.py        本进程观测窗口内的趋势环形缓冲（不伪造历史）
  api.py           FastAPI：路由 / 限流 / 静态 SPA / 后台采样
  producer.py      Shadow 侧三个明细快照的生产者（契约实现，待集成，见
                   docs/dashboard-data-contract.md §4）
dashboard/
  frontend/        Vite + React 19 + TS + Tailwind v4 + Recharts + Lucide
  tests/           后端 API 测试（42 项；独立目录，root CI 不装 FastAPI）
  fixtures/        generate.py -> data/（fixture 数据集，UI 显著标记 FIXTURE）
docker/
  compose.dashboard.unraid.yaml
  healthcheck_dashboard.py
Dockerfile.dashboard
scripts/dashboard_docker_accept.sh   CI 与 Release 共用的容器验收
.github/workflows/dashboard-ci.yml / dashboard-release.yml
```

**为什么后端测试不在 `tests/`**：root CI 只装 `dev` 组（无 FastAPI），
`pyproject.toml` 把 FastAPI 放进新的 `dashboard` 依赖组——因此 collector/shadow
镜像与它们的 CI **不会安装任何新依赖**。dashboard 测试放在自己的目录，由自己的
工作流运行；`tests/test_dashboard_ci.py` 则是 root 套件的一部分（纯文本不变量，
无 FastAPI 依赖），保证两个方向都被看住。

## 3. 请求生命周期

1. 前端按页面节奏轮询（健康 5s / 市场 10s / 预测 30s / 决策 5s / 系统 5s）。
   `usePoll` 带 AbortController、在途去重、标签页隐藏暂停、错误不打断循环。
2. 路由 → `asyncio.to_thread(builders.*)`（归档解析不阻塞事件循环）。
3. `sources.py` 用 mtime+size 判断缓存命中；**年龄永远按当前时刻重算**，
   否则缓存会把"3 秒前"冻成谎言。
4. 归档 tail：每源只读最新 `1 + archive_days_back` 个文件的最后
   `archive_tail_bytes`（默认 512 KiB），丢弃半行，TTL 4s。GB 级归档与
   页面刷新次数无关。
5. 响应统一 `Envelope`：`schema_version / api / generated_at_utc / dataset /
   freshness / data`。任何响应都不含宿主机绝对路径（有测试钉死）。

## 4. 页面与数据源映射

| 页面 | 真实数据源 | 缺失时 |
|---|---|---|
| Overview | collector 心跳 + shadow 心跳 + report + 归档 tail | 显式 missing/unavailable 卡片 |
| Markets | market-snapshot.json（待集成）→ 否则归档 tail（bid/ask/深度/新鲜度） | 模型概率列显示"暂未接入"+原因 |
| Forecasts | report.prediction_quality/coverage + forecast-snapshot.json（待集成） | 分布图 unavailable，观测/结算恒为未接入 |
| Decisions | report 聚合（实时可用）+ recent-decisions.json 行窗口（待集成） | 聚合与行明细**独立**显示状态 |
| System | 两份心跳 + report + 本进程趋势环 | 每个未接入指标单独 N/A，附原因 |

## 5. 实时性与诚实性

- LIVE / REPLAY 永不合并：`by_mode` 拆分展示，REPLAY 徽章单独着色。
- ACCEPT 免责声明常驻 Decisions 页与 Overview 决策卡（不可被筛选隐藏）。
- 连续同市场同方向的 ACCEPT 渲染为一个"连续 ×N"分组，防止单一观点被数成 N 个机会。
- `equity_curve` 在 report 中恒为 `null`，Dashboard 原样传递并标注原因。
- `WQ_DATASET=fixture` 时所有响应打上 `dataset:"fixture"`，UI 顶部常驻横幅——
  样例数据无法冒充生产数据。

## 6. 依赖与镜像隔离

| 镜像 | sync 命令 | 含 FastAPI? |
|---|---|---|
| collector (`Dockerfile`) | `uv sync --frozen --no-dev` | 否 |
| shadow (`Dockerfile.shadow`) | `uv sync --frozen --no-dev` | 否 |
| dashboard (`Dockerfile.dashboard`) | `uv sync --frozen --no-dev --group dashboard` | 是 |

`uv.lock` 由 `uv lock` 统一锁定全部组；三个镜像按组取用，互不污染。

## 7. 与另一会话的边界

本分支**没有修改** Collector / Shadow 的任何既有文件。唯一共享文件的改动是
`pyproject.toml`（追加 `dashboard` 依赖组与 `weatherquant-dashboard` 入口）与
`uv.lock`（重锁）。Shadow 侧明细快照的生产者逻辑在新文件
`src/weatherquant/dashboard/producer.py` 中完成并有契约测试，**等待集成**
（约 10 行，见数据契约文档 §4），不与任何进行中的修复争夺同一文件。

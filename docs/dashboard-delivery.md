# P2-1 Unified Dashboard — 交付报告

日期：2026-10-12 · 分支：`worktree-p2-1-dashboard`（自 `main@8bea431`，未提交，等待确认）

---

## 1. 最终架构

**一个 WebUI、两个数据引擎、三个独立容器。**

- `weatherquant-dashboard`（新增）：单容器 = FastAPI 只读 API + 构建后的 React SPA，
  端口 8787，uid 1000，`no-new-privileges`，无 Docker Socket、无凭据、无出站需求。
- 只读挂载**子目录**：`collector/{health,raw}`、`shadow/{health,reports}` —— 两个
  `.duckdb` 文件对 Dashboard 结构性不可见（容器验收断言容器视野内没有任何 .duckdb）。
- 数据通路：**Producer 原子发布 JSON → Dashboard 适配器 → Envelope HTTP → 轮询 UI**。
  归档使用有界 tail（每源最新 2 个文件 × 末 512 KiB，TTL 4s），任何刷新都不会触发
  GB 级扫描。
- 新鲜度与健康分离：`/healthz` 只回答"这个进程在服务"（Docker 重启判据）；每个
  payload 携带 `freshness`（fresh/stale/missing/corrupt/unavailable + 年龄 + 原因）。
- 详见 `docs/dashboard-architecture.md`。

## 2. 新增文件

**改动的既有文件仅 3 个（全部为追加，零 Collector/Shadow 代码改动）：**

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | +`dashboard` 依赖组（fastapi/uvicorn）、+`weatherquant-dashboard` 入口 |
| `uv.lock` | `uv lock` 重锁（含新组） |
| `.dockerignore` | +node_modules/dist/screenshots 等前端构建产物 |

**新增：**

```text
src/weatherquant/dashboard/     config / contracts / sources / archive /
                                builders / trends / api / producer / __main__
dashboard/frontend/             Vite+React19+TS+Tailwind v4+Recharts+Lucide
                                （5 页面 + 组件 + 52 单测 + Playwright e2e）
dashboard/tests/                后端 45 项测试（conftest/helpers/test_api/test_producer）
dashboard/fixtures/generate.py  fixture 数据集生成器（UI 强制 FIXTURE 横幅）
dashboard/screenshots/          15 张验收截图（1440/768/375 × 5 页）
Dockerfile.dashboard            三阶段（node build → uv sync --group dashboard → slim runtime）
docker/compose.dashboard.unraid.yaml
docker/healthcheck_dashboard.py 只探自身 /healthz
scripts/dashboard_docker_accept.sh   CI 与 Release 共用的 8 项容器验收
.github/workflows/dashboard-ci.yml
.github/workflows/dashboard-release.yml
tests/test_dashboard_ci.py      13 项工作流/Dockerfile/compose/脚本不变量
docs/dashboard-{architecture,data-contract,unraid-deployment,delivery}.md
```

## 3. 实际可用的 API（全部只读 GET，Pydantic 响应模型）

```text
GET /healthz                        存活 + 三源状态（信封包裹）
GET /api/v1/overview                总览六卡 + 决策分布 + Model vs Market + 系统健康
GET /api/v1/collector/health        心跳全量 + Docker 探针同源判定
GET /api/v1/collector/metrics       计数器 + 本进程趋势
GET /api/v1/shadow/health           心跳（mode/LIVE/REPLAY/last_cycle…）
GET /api/v1/shadow/report           report.json 透传 + 独立新鲜度
GET /api/v1/markets                 市场列表（date/status/book/decision/q + limit/cursor）
GET /api/v1/markets/{id}            详情（盘口/深度/最近决策/分布）
GET /api/v1/forecasts               模型、覆盖率、温度桶分布（生产者就绪时）
GET /api/v1/decisions               聚合 + 行窗口（state/mode/model/market_id/since + 分页）
GET /api/v1/system                  Collector/Shadow 指标 + 趋势 + N/A 说明
```

约束已测：枚举/范围非法 → 422；限流 600/min/IP → 429；无数据 → 合法空状态；
文件损坏 → `corrupt` + 原因；响应无宿主绝对路径；无查询/写入/Shell 接口；
不提供 DuckDB/JSONL 下载；包内无 `import duckdb`/`duckdb.connect`（静态+运行时双重断言）。

## 4. 完成的页面（5/5 + 1 详情页）

| 页面 | 核心内容 |
|---|---|
| 总览 Overview | 六张状态卡、ACCEPT/REJECT/NO_TRADE 卡+比例图+LIVE/REPLAY 拆分、Model vs Market 表+并列柱图、System Health、常驻免责 |
| 市场行情 Markets | 卡片网格（报价/价差/深度/模型概率/Edge/STALE）、5 个筛选、点击进详情 |
| 市场详情 | 报价盘口+缺陷、概率分布图、最近决策、元数据 |
| 天气预测 Forecasts | 4 模型卡、温度桶概率分布（多模型切换）、run/训练截止/预测最大温、覆盖率、观测缺失说明 |
| Shadow 决策 Decisions | 统计卡、5 类筛选（含时间窗）、行表 + **连续 ACCEPT ×N 分组**、LIVE/REPLAY 分色、常驻免责声明、拒绝原因榜 |
| 系统监控 System | Collector/Shadow 分区指标、趋势 sparkline、"暂未接入"清单、数据质量 |

响应式：375 / 768 / 1440 三档 Playwright 项目全通过；移动端抽屉导航可用。

## 5. 使用的真实数据源

| 来源 | 用途 |
|---|---|
| `collector/health/heartbeat.json` | Collector 状态/计数/磁盘/WS/REST（探针同源函数判定） |
| `shadow/health/heartbeat.json` | Shadow 模式/周期/阈值版本/最近决策 |
| `shadow/reports/report.json` | 决策聚合、LIVE/REPLAY 拆分、覆盖率、liveness、数据质量、警告 |
| `collector/raw/{gamma,rest,ws}/*.jsonl`（有界 tail） | 市场目录 + 每 token 最新盘口（复用 `shadow.events` 解析器） |
| 归档直读实测（主仓库 data/） | 58 市场 / 33 有盘口 / 报价与 STALE 判定 / 无路径泄漏 |

## 6. 明确暂未接入的字段（UI 显示 N/A 或 unavailable + 原因，绝不编造）

- **逐市场模型概率 / 理论 Edge、决策行明细、温度桶分布** → 等待 Shadow 集成
  producer（契约与就绪实现见 `docs/dashboard-data-contract.md` §2/§4）；
- **实际观测 / 官方结算** → 属研究管线回溯评分，结构性不接入（页面常驻说明）；
- **WS 队列长度/高水位/丢帧** → Collector ≥ v0.1.2 心跳 `telemetry` 已接入
  （System/Overview 实时展示 + 趋势采样）；旧版心跳无字段时显示"暂未接入"；
  **Shadow 数据库大小** → 心跳没有该字段（"暂未接入"）；
- **收益曲线** → report 恒为 `null`，原样传递并注明原因；
- **shadow report 面板** → 生产 shadow-v0.1.0 尚无 report 快照机制，升级前显示 missing（设计内降级）。

## 7. 测试通过情况

| 套件 | 结果 |
|---|---|
| 仓库全量 `pytest`（含新增 13 项工作流不变量） | **645 passed, 5 skipped** |
| `pytest dashboard/tests`（API + producer 契约 + 端到端契约一致性） | **45 passed** |
| `ruff check .`（全仓库） | **通过** |
| 前端 `tsc --noEmit` | **0 错误** |
| 前端 `vitest`（7 文件：hook/组件/5 页面） | **52 passed** |
| Playwright 浏览器验收（3 视口 × 5 页 + 导航/详情/深链/FIXTURE 横幅） | **25 passed, 2 skipped**（抽屉用例仅移动端执行，by design） |
| 本机真实数据集成脚本 | 全端点 200、诚实降级、无路径泄漏 |

## 8. 实际浏览器 UI 验证

Playwright 真实 Chromium 驱动 FastAPI + 构建产物，`dashboard/screenshots/` 共 15 张：

- `desktop-{overview,markets,forecasts,decisions,system}.png`（1440）
- `tablet-*.png`（768）、`mobile-*.png`（375）

验收断言包含：五页关键元素、FIXTURE 横幅、决策免责声明、市场卡片含真实报价、
概率图与模型切换、WS 队列"暂未接入"、移动端抽屉、卡片→详情跳转、SPA 深链回退。
本地可复现：`python dashboard/fixtures/generate.py` → 启动服务 → `npx playwright test`。

## 9. Docker 构建情况（如实说明）

- `Dockerfile.dashboard`（三阶段）、`compose.dashboard.unraid.yaml`、
  `healthcheck_dashboard.py`、`scripts/dashboard_docker_accept.sh`（bash -n 通过）
  均已完成，并由 `tests/test_dashboard_ci.py` 做文本级不变量校验
  （非 root、HEALTHCHECK、8787、:ro 子目录挂载、无 sock/CLI、组隔离、验收项齐全）。
- **本机（Windows）没有 Docker**，镜像构建与 8 项容器验收**未在本机执行**；
  两者被编排进 `dashboard-ci.yml`（build + 验收，无发布能力）与
  `dashboard-release.yml`（全部门禁通过后才 push），推送后由 CI 实际执行。
- CI 红绿以 Actions 为准；发布标签 `dashboard-v*` → `sha-<commit>` +
  `dashboard-stable`，与 collector `stable`、shadow `shadow-stable` 分属不同镜像仓库，
  有测试断言不可互移。

## 10. NAS 部署步骤

见 `docs/dashboard-unraid-deployment.md`（首次部署、只读挂载、绑定 LAN IP、
验证清单、升级/回滚、故障排查）。要点：

```bash
mkdir -p /mnt/user/appdata/weatherquant-shadow/reports
# compose 贴入 docker/compose.dashboard.unraid.yaml → Apply
# （推荐）ports 改为 "192.168.x.x:8787:8787" 绑定 NAS LAN IP
open http://<NAS-LAN-IP>:8787
```

**本版本无认证：仅限可信局域网，禁止公网端口映射。** 未擅自部署——
按任务要求，提交 / 发布 / 部署全部等待你的确认。

## 11. 与 Collector / Shadow 的兼容性

- **零改动**：未修改任何 Collector / Shadow 源文件、镜像、工作流或 compose；
  另一会话的未提交工作（`src/weatherquant/data/http.py`、
  `scripts/collector_vitals.py`）未被触碰。
- 共享文件仅 `pyproject.toml` / `uv.lock` / `.dockerignore` 的追加项，
  collector/shadow 的 `uv sync --frozen --no-dev` 不会安装 dashboard 组，
  两个生产镜像内容不变。
- Dashboard 崩溃不影响生产服务；停止生产服务不影响 Dashboard（降级为 missing）。
- Shadow 明细 producer 完成于**新文件** `src/weatherquant/dashboard/producer.py`
  并有契约测试，按约定**不在本分支改 `shadow/runner.py`**，集成仅需约 10 行
  （契约文档 §4 给出精确代码），合并时机由你与 Shadow 会话协调。

## 12. 尚未解决 / 待协调的问题

1. **Shadow producer 集成**（最高优先）：决策行明细、逐市场模型概率、概率分布
   三块 UI 已就绪，等 `runner._publish_report` 追加 10 行调用（§4 契约）。
2. **生产 Shadow 版本**：shadow-v0.1.0 无 report 快照；需升级到含 1849187 的
   构建，否则 report 面板长期 missing。
3. **CI 首跑**：dashboard-ci / dashboard-release 为新增工作流，需推送后由
   Actions 验证（本机无 Docker / 无 act）；`environment: dashboard-release`
   首次运行时需在 GitHub 创建并按需加审阅者。
4. **认证**：无认证设计如上；如需公网暴露须先加认证层（超出本阶段范围）。
5. **趋势历史**：仅覆盖 Dashboard 进程自身观测窗口（页面已注明），如需长期
   历史需生产者落盘指标——未擅自引入数据库。
6. Bundle 715KB（gzip 216KB，Recharts 为主）：局域网可接受；如需优化可做路由级
   代码分割（未做）。

---

### 提交说明

按任务要求**未执行任何 git 提交**。工作树：3 个追加式修改 + 新文件清单见 §2；
`git status` 干净（生成物均被 ignore）。确认后我可以按逻辑分批提交
（契约/后端 → 前端 → Docker/CI → 文档 → 截图），并按你的指令发布与部署。

# WeatherQuant Next

从零重建的 Polymarket 天气预测市场量化研究核心。

**当前处于 P1-0：NAS 无人值守数据采集器。** P0（架构、数据模型、迁移、数学不变量）
已完成并通过测试；本轮交付真实采集器、Docker 化、CI/CD 与部署文档。

进度见 [`docs/roadmap.md`](docs/roadmap.md) 与文末状态表；
部署见 [`docs/unraid-collector-deployment.md`](docs/unraid-collector-deployment.md)。

---

## 硬性边界

以下内容**在当前代码库中不存在**，并且在被明确要求之前不会被创建：

- 真实资金下单 / 交易机器人 / 任何可发出订单的代码路径
- Dashboard / Web 界面
- 钱包、私钥、交易所凭证、任何 API key
- 对旧数据库的写入、覆盖或清理

系统只做三件事：**读取公开数据 → 估计概率 → 在研究与模拟环境中评估**。
当前阶段只做第一件。

## 量化原则

这些原则约束代码结构，而不只是文档措辞：

1. 所有概率估计必须经过历史**样本外**验证。
2. 不假设市场必然存在错误定价。
3. 不将理论 Edge 直接等同于实际利润。
4. 不将回测收益作为未来收益保证。
5. 不使用 LLM 直接猜测天气结果。
6. 同一目标日期的多次预报运行是**相关样本**，不能当作独立事件计算统计置信度。
7. 市场概率比较必须保证**时间点一致**（预报快照与订单簿快照属于同一时刻）。
8. 交易信号与模拟成交**共享同一套定价计算逻辑**（`weatherquant.execution.pricing`）。
9. 不允许使用 midpoint 冒充成交价格。
10. 交易费用不能重复计算。

## 目录结构

```
weatherquant-next/
├── src/weatherquant/
│   ├── data/        上游采集、原始归档、健康与持久化    [P1-0 已实现]
│   ├── markets/     Polymarket 市场、事件、订单簿模型    [P0 + P1-0 解析]
│   ├── weather/     预报与气象站观测模型                 [P0]
│   ├── models/      概率模型与评估指标                   [P0：metrics]
│   ├── strategies/  信号逻辑                             [P1，当前为空]
│   ├── backtest/    研究引擎                             [P1，当前为空]
│   ├── paper/       模拟交易引擎                         [P2，当前为空]
│   ├── risk/        风险限制                             [P2，当前为空]
│   ├── storage/     DuckDB 连接、schema、迁移            [P0 + P1-0 采集迁移]
│   ├── execution/   共享定价 / 成本 / Edge 计算（见下）   [P0]
│   ├── legacy/      旧库只读导入                         [P0]
│   └── config.py    运行时配置（WQ_* 环境变量）
├── tests/          P0 数学不变量 + P1-0 采集器测试
├── scripts/        legacy_plan.py / soak_test.py
├── docker/         compose.unraid.yaml + healthcheck.py
├── Dockerfile      多阶段构建，非 root，HEALTHCHECK
├── .github/        ci.yml（仅测试构建）+ release.yml（发布门禁）
├── research/       研究笔记与外部审计报告
├── configs/
├── docs/
└── data/           运行期产物（git 忽略）
```

> **关于 `execution/` 的说明**：推荐目录列表中没有它。定价计算必须同时被
> `strategies`（信号）与 `paper`（模拟成交）调用；放进任何一方都会造成反向依赖。
> 因此新增一个中立的 `execution` 包作为唯一的成本计算来源。

> **关于空包**：`strategies/`、`backtest/`、`paper/`、`risk/` **只有 `.gitkeep`，没有任何代码**。
> 按规范"不要一次性生成全部未来阶段的占位代码"，这里不放 stub——空桩比没有更糟，
> 它会假装某个能力已经存在。`data/` 已在 P1-0 实现，不再是空包。

## 安装

需要 Python 3.12+ 与 [`uv`](https://docs.astral.sh/uv/)：

```sh
cd weatherquant-next
uv sync --group dev
```

没有 `uv` 时：

```sh
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e . && pip install pytest pytest-cov ruff
```

## 测试

```sh
uv run pytest          # 完全离线，不打开任何 socket
uv run ruff check .
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/open-source-review.md`](docs/open-source-review.md) | 四个开源项目 + 官方 SDK 审计与选型结论 |
| [`docs/architecture.md`](docs/architecture.md) | 新系统架构设计 |
| [`docs/legacy-data-migration.md`](docs/legacy-data-migration.md) | 旧库只读迁移方案与待核实判定规则 |
| [`docs/roadmap.md`](docs/roadmap.md) | P1–P4 详细开发任务 |

## P0 状态（已完成）

| 项 | 状态 |
|---|---|
| 工作目录检查 | ✅ 已完成 |
| 四个开源项目审计 | ✅ 已完成，见 `docs/open-source-review.md` |
| 官方 SDK / API 核实 | ✅ 已完成，见 `docs/open-source-review.md` §2 |
| 项目架构 | ✅ 已完成，见 `docs/architecture.md` |
| 依赖管理与基础配置 | ✅ 已完成（`pyproject.toml` / ruff / pytest） |
| 统一数据模型 | ✅ 已完成（市场 / 预报 / 观测 / 订单簿） |
| 数据库初始化与迁移机制 | ✅ 已完成（版本化迁移，DuckDB） |
| 关键数据类型与数学不变量测试 | ✅ 已完成，测试通过 |
| 旧数据只读导入接口 | ✅ 已设计并实现（默认 dry-run） |
| P1–P4 任务清单 | ✅ 已完成，见 `docs/roadmap.md` |

## P1-0 状态（本轮）

| 项 | 状态 | 验证方式 |
|---|---|---|
| 真实 Collector Worker | ✅ | 本机实跑，采到 40 个市场 / 93 份盘口 / 68 个价点 |
| Polymarket Gamma 发现 + CLOB `/book` 轮询 | ✅ | 真实 API 冒烟 |
| WebSocket 实时行情（含协议 PING、断线重连） | ✅ | 真实 WS 连接；`ws_connected=true` |
| 原始载荷归档（来源 / 采集时间 / 质量三要素） | ✅ | `raw/{gamma,rest,ws}/*.jsonl` |
| 断线重连、超时、限速重试、优雅退出 | ✅ | 单测 + 浸泡故障注入 |
| 单实例保护 / 批量提交 / 定期 checkpoint | ✅ | 单测 + 浸泡 |
| 按日分区 Parquet 导出 | ✅ | 单测 + 实跑 |
| 自校验一致性备份 | ✅ | 读回比对行数、schema 版本与索引 |
| 健康检查（独立于 DuckDB 写者） | ✅ | 全阶梯单测 |
| 磁盘水位告警 | ✅ | 单测（<15% degraded，<5% unhealthy） |
| 数据缺口记录 | ✅ | 浸泡确认重启/崩溃后 gap 被记录并关闭 |
| `Dockerfile`（多阶段 / uv / 非 root / HEALTHCHECK） | ⚠️ 已写 | **本机无 Docker，未构建** |
| `docker/compose.unraid.yaml` | ⚠️ 已写 | YAML 校验通过，**未在 Unraid 上运行** |
| GitHub Actions（ci + release） | ⚠️ 已写 | YAML 校验通过，**未在 CI 中执行** |
| 部署文档 | ✅ | `docs/unraid-collector-deployment.md` |
| **Unraid NAS 真实部署** | ❌ **未做** | **无 NAS 访问权限** |
| 概率模型（ECMWF/GFS/融合/校准） | ❌ 未实现 — P1 续 | |
| 回测引擎 | ❌ 未实现 — P1 续 | |
| Paper trading 引擎 | ❌ 未实现 — P2 | |
| Dashboard / 交易机器人 | ❌ **禁止** | |

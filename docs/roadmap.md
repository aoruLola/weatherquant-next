# P1–P4 开发任务

2026-10-10。P0 状态与边界见 [`../README.md`](../README.md)；架构约定见
[`architecture.md`](architecture.md)；选型依据见 [`open-source-review.md`](open-source-review.md)。

---

## 0. 优先级最高的一件事：数据正在永久流失

审计结论（[`open-source-review.md`](open-source-review.md) §6.2）：

| 数据 | 可回溯窗口 |
|---|---|
| 1 分钟价格 | **约 7 天**（10 天前返回 0 点） |
| 5 分钟价格 | ≥40 天 |
| 30 分钟价格 | ≥40 天 |
| 12 小时价格 | 永久全史 |
| **历史订单簿 L2 深度** | **不存在任何 API** —— 只能自己边收边存 |

12 小时价格以后能补，**1 分钟价格和盘口深度补不了**。旧版 WeatherQuant 只攒了一天的深度
（693 行，2026-10-09T17:18Z → 2026-10-10T02:42Z），就几乎没有了。

**因此 P1 的第 0 项是启动持续采集器，其余 P1 任务都排在它后面。** 这一项每晚一天，
就永久少一天的数据，且直接决定未来交易回测可用的时间窗有多长。

约束：采集器是**只读拉取 + 落库**，不是交易机器人，不违反"禁止开发真实下单功能"。

---

## P1 — 研究引擎与概率基线

**目标**：产出 §五 要求的五条 KLGA 基线，并给出样本外评估。**退出标准见本节末。**

### 第 0 项：持续采集器（必须先做）

| # | 任务 | 说明 | 验收 |
|---|---|---|---|
| C-0 | **1 分钟价格流采集** | `/v2/prices-history`（60s bucket）+ WS 价格流；写 `price_history` | 连续运行 24h 无缺口；缺口有记录 |
| C-1 | **订单簿快照轮询** | CLOB `/book`（限速 1500/10s），写 `order_books`，**必须填 `venue_ts`**（P0 迁移的教训） | 每市场每分钟一帧；`venue_ts` 非空率 = 100% |
| C-2 | **成交流水** | `/v2/trades`（限速 300/10s） | 幂等，可重放 |
| C-3 | **原始载荷归档** | 上游原始 JSON 按采集时刻落 `data/raw/` | 无网络可重放；校验和可核 |
| C-4 | **限速与退避** | 尊重 §6.3 表格；超限延迟而非报错 | 无 429 导致的数据洞 |
| C-5 | **单写者租约** | 沿用旧版 `process_locks` 模式 | 两个进程同时起只会有一个写 |

### 模型与概率（§五 的 5 条基线）

| # | 任务 | 说明 | 验收 |
|---|---|---|---|
| V-1 | **核实各市场的取整与解析站** | 逐市场读 `rules_text`，确认取整规则（`NEAREST_INTEGER_F` 是否普遍成立）与解析站（是 `KLGA` 而非 `KNYC`）。`Bracket.rounding` 的默认值目前是**假设** | 用真实 `rules_text` 覆盖 ≥95% 市场；发现例外即单独建模 |
| V-2 | **实测手续费口径** | Polymarket 对跨多档 taker 单按 VWAP 还是逐笔 match 计费，文档未明说。当前实现取 VWAP（较保守），见 `architecture.md` §6.3 | 小额实测或查合约/源码，结论写进 `open-source-review.md` |
| B-1 | **ECMWF 单模型基线** | `single-runs-api`，`models=ecmwf_ifs025`；4 runs/day | 见下方指标 |
| B-2 | **GFS 单模型基线** | 同一 API，`models=gfs` | 同上 |
| B-3 | **ECMWF+GFS 简单融合** | 先做等权/按历史 skill 加权，**不做**复杂 BMA | 融合在样本外**不劣于**两条单模型 |
| B-4 | **历史偏差校准模型** | 单站残差：`bias` + `σ(lead_time)`；**σ 必须按 lead time 分桶**，不得用单常数 | 见下方指标 |
| B-5 | **市场概率基线** | 直接取 `price_history` 的市场隐含概率作对照 | 与模型同表比较 |

**σ 的三条硬要求**（审计发现的错误全在这里）：

1. 若由 MAE 推 σ，必须 `σ = MAE · √(π/2) ≈ 1.2533 · MAE`，**不能 `σ = MAE`**。
2. 桶概率必须走 `Bracket.probability_interval_f()`（±0.5 换算），**不得**自行写区间。
3. 禁止硬编码 σ 常数表；σ 必须由训练数据估计并按 lead time 分桶。

### 概率生成方式

| # | 任务 | 说明 |
|---|---|
| P-1 | **集合方法（优先）** | 若能取到 GEFS / ECMWF ENS 成员，直接用经验成员频率算桶概率 —— 绕开"硬编码 σ + CDF"整类错误。审计确认 polymarket-weather-bot 有此路径但**默认关闭**且写错，需重写 |
| P-2 | **误差分布方法（回退）** | 历史残差经验分布 / 正态 + 按 lead time 的 σ；显式声明假设 |
| P-3 | **校准层** | 样本外可靠性校准（isotonic 或分桶重标定）；**训练/校准/测试按时间顺序三分** |

**禁止**：把确定性预报当概率；用 LLM 猜结果；不加说明地假设正态。

### 评估与回测

| # | 任务 | 说明 | 验收 |
|---|---|---|---|
| E-1 | **五项指标 + 日期级 CI** | MAE、RMSE、`brier_multiclass`、`log_loss`、`calibration_bins`/ECE；**全部用 `date_level_summary` 取置信区间** | 已有实现，接入基线即可 |
| E-2 | **样本外协议** | 严格按**时间顺序**切分；同一 `target_date` 的多次 run **不得跨切分** | 有测试断言切分不交叉 |
| E-3 | **时间点一致性检查** | 比较模型概率与市场概率时，二者必须取自**同一时刻**；预报须 `t >= publishable_at_utc` | 不一致即报错，不静默降级 |
| E-4 | **市场 vs 模型的 Brier 对照** | 旧库实测**市场 Brier 0.615 优于模型 0.829、top-1 命中 0.59 vs 0.30** —— 新模型必须跑赢这条既有事实，否则没有交易理由 | 结论如实写入研究报告 |
| E-5 | **研究回测（仅 mid 价）** | 用 `price_history` 评"市场定价是否更准"，**不得**假设可成交 | 明确标注 `NOT_EXECUTABLE` |
| E-6 | **交易回测（仅自采深度窗）** | 用 C-1 采集的深度做 `build_fill_plan` 式成交模拟 | 窗口外一律报 `HISTORICAL_BOOK_UNAVAILABLE`，**不降级为 mid** |

### 旧数据迁移（[`legacy-data-migration.md`](legacy-data-migration.md) §5）

| # | 任务 |
|---|---|
| M-1 | 就 `books.venue_ts` 缺失作出**被记录的**决定（迁移文档 §3.1 三个选项） |
| M-2 | 10 张表的逐列映射 + `bids_json` 键名转换与排序断言 |
| M-3 | 幂等写入 + 计数核对（对不上即失败） |
| M-4 | 决定 R1（`daily_observations` 须与官方结算相等才可信；实测 1,100 天仅 45 天通过）的取舍并记录 |
| M-5 | 迁移后复核分类分布与迁移文档 §3 一致 |
| M-6 | `data/raw/` 原始载荷单独归档 |
| M-7 | 全程只读；完成后验证一次写入被拒 |

### P1 退出标准

1. 五条基线全部产出，**按日期聚类**的 95% CI 齐备。
2. 每条基线给出样本外 MAE / RMSE / Brier / LogLoss / ECE。
3. 明确回答：**新模型在样本外是否优于市场隐含概率？** 不允许含糊。
4. 采集器连续运行 ≥7 天，无未解释的数据洞。
5. 全部测试通过，`ruff` 无告警，研究报告落 `research/`。

---

## P2-0 — Shadow Trading Engine（决策观察，本轮）

**与 P2 的关系**：P2-0 是 P2 的**前置观察阶段**。P2 假设"模型样本外有效"，
而 P1-2 的结论恰恰是**尚未证明**（模型 Brier 1.49 vs 市场 1.03）。在那之前
直接上模拟账本，等于把一个未经证明的信号变成持仓记录。P2-0 回答的是更前面的
问题：**在真实实时数据上，这套链路能否做出时间一致、成本一致、可审计的决策？**

**前提**：P1-0/1-1/1-2 已完成。

| # | 任务 | 说明 | 验收 |
|---|---|---|---|
| S-1 | 只读行情输入适配器 | 尾随采集器 `raw/*.jsonl`；半行不读、偏移与效果同事务、损坏留痕 | `tests/test_shadow_ingest.py` |
| S-2 | 事件分类 | 快照 / 增量 / 成交 / 元数据四类，**增量永不成为盘口** | 有断言 |
| S-3 | 实时天气推断 | 复用 `SingleRunsClient` + `aggregate_daily_max`；`publishable_at = run+12h` | `tests/test_shadow_forecast.py` |
| S-4 | 模型工件 | 拟合 → 冻结 → manifest 固定；截止日期防未来泄漏 | `tests/test_shadow_models.py` |
| S-5 | Edge 计算 | **必须**复用 `execution/pricing.py`；卖出侧从 bid 走 | 手算值比对 |
| S-6 | 决策状态机 | 5 态 + 16 原因 + 因果序主原因；缺 `generic_error` | `tests/test_shadow_decision.py` |
| S-7 | 风险闸门 | 版本化 JSON，先验设定，**不拟合样本** | `configs/shadow-thresholds.json` |
| S-8 | 独立 Shadow 数据库 | 第三个文件 + 第三个迁移目录；按名拒绝他库 | `tests/test_shadow_storage.py` |
| S-9 | CLI 报告 | 四区块分离；`equity_curve` 恒为 `null` | `weatherquant-shadow report` |
| S-10 | Docker / CI | `Dockerfile.shadow` + 两份 workflow，**不动采集器发布** | ⚠️ 本机无 Docker，未执行 |
| S-11 | 文档 | `shadow-engine` / `shadow-risk-rules` / `shadow-unraid-deployment` | ✅ |
| S-12 | 验证报告 | 区分**已执行**与**未执行** | `research/shadow-validation.md` |

**S-6 与 R-2 的关系**：P2-0 只做**观察**，不建账本。闸门、流动性拒绝、
阈值不做同样本拟合这三条，在 P2-0 就已经落地并可测试 —— P2 复用它们。

**本阶段仍禁止**：真实下单、钱包、私钥、Dashboard、**自动虚拟下单与持仓账本**。

---

## P2 — Paper Trading Engine 与风险

**前提**：P1 已证明模型样本外有效，且**不以回测收益作未来保证**。
（**当前不满足** —— 见 `research/baseline-comparison.md`。P2-0 的 24 小时与
7 天影子观察是进入 P2 的运行稳定性依据，但**它不会让模型变好**：模型是否
优于市场仍是 P1 未回答的问题。）

| # | 任务 | 说明 | 验收 |
|---|---|---|---|
| T-1 | 虚拟订单与成交 | **必须**复用 `execution.build_fill_plan`，禁止另写一套 | 定价只有一处实现 |
| T-2 | 持仓与结算 | 结算**只**认 `/v2/resolutions` 已决结果；无 `resolved_at` 不记收益 | 禁止资本前视（旧版缺陷 6） |
| T-3 | 账本恒等式 | `cash + Σ(position value) = equity`；费用只进一次 | 每周期断言，残差 ~1e-13 |
| T-4 | 单写者租约 | 沿用 C-5 | 两个进程只能有一个写 |
| R-1 | 风险限制 | 单笔 / 单事件 / 总敞口上限；**阈值不得为制造交易而下调** | 每次评估落库附 reason |
| R-2 | 流动性闸门 | 最小深度、最大账龄、交叉盘/空盘拒绝 | `NO_TRADE` 是常态 |
| R-3 | 阈值不做同样本拟合 | 审计发现三个项目都在 n=4–18 样本上拟合闸门又评估 | 阈值须有样本外依据或先验声明 |
| T-5 | Research / Paper 表级隔离 | `account`/`orders`/`fills`/`positions`/`equity_snapshots` 只有 `paper/` 可写 | 有测试断言 `backtest/` 写被拒 |

**本阶段仍禁止**：真实下单、钱包、私钥、Dashboard。

### P2 退出标准

- 模拟盘连续运行 ≥14 天，账本恒等式始终成立。
- 每笔 `NO_TRADE` 都有可读 reason；**没有**为提高交易频率而调整阈值的记录。
- 研究侧任何操作都未能改动模拟账本（有测试证明）。

---

## P3 — 多城市扩展

| # | 任务 | 说明 |
|---|---|---|
| X-1 | 参数化站点 | 把 `KLGA` 硬编码全部改为配置项；验证 `station` 贯穿 market / forecast / observation / settlement 四处一致 |
| X-2 | 站点偏差各自独立校准 | 每站独立拟合 `bias`/`σ`，**禁止跨站共享** |
| X-3 | 逐站复现 P1 退出标准 | 每个城市独立跑完五条基线与样本外评估 |
| X-4 | 跨市场敞口上限 | 新增事件级与全组合级上限 |
| X-5 | 采集器多站扩展 | 限速预算按总 IP 摊分（§6.3） |

**只有 P1、P2 的退出标准全部满足后才开始 P3。** 规范 §一第 9 条：先证明模型与策略有效，再考虑多城市扩展。

---

## P4 — 加固与研究深化

| # | 任务 | 说明 |
|---|---|---|
| H-1 | 概率模型深化 | 从"正态 + 按 lead time 的 σ"走向完整的 skill 校准与集合融合 |
| H-2 | 成本模型细化 | 实测并细化 spread / 深度冲击 / 跨档手续费口径（依赖 V-2） |
| H-3 | 样本外稳健性 | 滚动时间窗回测；报告**多个不重叠窗口**的分布，而非单一累计收益 |
| H-4 | 不确定性量化 | 明确区分"模型期望为正"与"实现利润"；报告实现率与实际分布 |
| H-5 | 数据质量自动稽核 | 每日核对：溯源列非空率、`venue_ts` 覆盖率、分类分布漂移 |
| H-6 | 文档与复现 | 一键从原始载荷重放全部研究结论 |

**P4 之后**：Dashboard 与任何真实下单，均须在明确的单独批准后才开始。当前被规范禁止。

---

## 附：各阶段状态

| 阶段 | 状态 |
|---|---|
| **P0** 数据模型 / 存储 / 定价不变量 / 只读迁移接口 / 审计 | ✅ **已完成** |
| **P1-0** 真实采集器 / Docker / CI-CD / 部署文档 | ✅ **已完成**（303 测试通过，85% 覆盖，ruff 无告警；`ci.yml` 与 `release.yml` 均已在 GitHub Actions 执行并全绿，`v0.1.1` 镜像已发布至 GHCR；仅 Unraid 真实部署待执行，见下） |
| **P1-1** 气象数据与结算真值管线 | ✅ **已完成**（355 测试通过，lint 全绿；独立研究库、60 天回填 485/488 成功） |
| **P1-2** ECMWF/GFS 概率模型 + 样本外评估 + 市场基线 | ✅ **已完成**（420 测试通过，lint 全绿）；行级旧数据迁移 ⬜ 未开始 |
| **P2-0** Shadow 决策观察引擎 | ✅ **已完成**（570 测试通过，lint 全绿；checkpoint 热修复后 671 passed）；Docker 构建已在 `shadow-release` 执行，NAS 上已部署并运行（版本无法从心跳核对，见 `docs/p2-0.1-audit.md` §2） |
| **P2-1** 统一只读 Dashboard | ✅ **已完成**并部署（`dashboard-v0.1.0` 已发布）；三个明细快照的生产者在 **P2-0.1** 接入 |
| **P2-0.1** 数据质量监控 + Dashboard 增强 + 七天观察 | 🔶 **镜像已发布、生产未切换**：PR #3 已合并，`shadow-v0.1.3` / `dashboard-v0.1.3` 已构建（`sha-1d66e4da`）；`ci` 间歇性挂起已**定位并修复**（测试真实访问 Open-Meteo）；七天观察窗口**尚未开始**，计划见 `docs/p2-0.1-deployment-plan.md` |
| **P2** Paper trading + 风险限制 | ⬜ 未开始（前提"模型样本外有效"**当前不满足**） |
| **P3** 多城市 | ⬜ 未开始 |
| **P4** 加固与深化 | ⬜ 未开始 |
| Dashboard | ✅ 只读观察已交付（P2-1 / P2-0.1）；**无认证、无下单能力**，仅限可信局域网 |
| 真实下单 | ⛔ 规范禁止，本轮未做任何相关工作 |

### P1-0 交付明细

| 任务 | 交付物 |
|---|---|
| 真实 Collector Worker | `src/weatherquant/data/`（worker / discovery / books / stream / archive / writer / heartbeat / lock / ratelimit / http） |
| Docker | `Dockerfile`（多阶段 + uv + 非 root + HEALTHCHECK）、`docker/healthcheck.py`、`.dockerignore` |
| Unraid | `docker/compose.unraid.yaml`、`docs/unraid-collector-deployment.md` |
| CI/CD | `.github/workflows/ci.yml`（仅测试构建）、`release.yml`（测试→构建→启动验证→发布 SHA→移动 stable） |
| 持久化 | 单实例租约、批量事务、定期 checkpoint、按日 Parquet、**自校验一致性备份** |
| 恢复 | 断线重连、优雅退出、异常隔离、**数据缺口记录**（启动/崩溃/更新/关停） |
| 测试 | 303 项（P0 152 + 采集器 151），完全离线；`scripts/soak_test.py` 做故障注入浸泡 |

**已由 GitHub Actions 实际验证**（`ci.yml`，run `38048156329`，全绿）：

- `ruff 0.17.0` + **303 项 pytest** 在 Linux 上通过
- `linux/amd64` 镜像**构建成功**
- 容器**启动成功**，2 秒内写出心跳；健康检查、非 root（uid 1000）、
  `docker stop -t 60` 优雅停止、**重启后数据库完好**，全部通过

CI 首轮暴露并修复了 **5 个本机发现不了的问题**（本机无 Docker，`.gitignore` 的
副作用也只在全新 checkout 上才显现）：

| # | CI 暴露的问题 | 修复 |
|---|---|---|
| 1 | ruff 在 CI 上把 `weatherquant` 判为第三方，`I001` 失败（本地同版本同字节却通过） | 显式 `known-first-party = ["weatherquant"]` |
| 2 | `.gitignore` 的 `data/` 无锚点，**11 个采集器模块整个没被提交** | 改为 `/data/` |
| 3 | bind mount 覆盖镜像内的 chown，容器以 uid 10000 之外的用户写 `/data` 失败 | CI 侧 `chown` + 启动前置可写性预检（给出可操作的错误信息） |
| 4 | 该补丁把 release 专用的 `$DATA` 塞进 `ci.yml`，`set -u` 直接中止 | 各工作流各用各的变量 |
| 5 | Dockerfile 用 uid **10001**，而 compose/文档/CI 全是 **1000** | 统一为 1000；错误信息改为读取真实 uid，不再硬编码 |

**尚未验证的部分**（无 NAS 权限）：

- ~~`release.yml` 未触发过~~ → **`v0.1.1` 已执行成功**：`needs: verify` 门禁通过，
  GHCR 收到 `sha-*` / `v0.1.1` 两个标签，`stable` 已移动。
- **`docker/compose.unraid.yaml` 未在 Unraid 上运行**。
- **Unraid 未部署** —— 见 `docs/unraid-collector-deployment.md` §11.2 的 18 项验收清单。



### P1-2 交付明细

| 目标 | 交付 |
|---|---|
| 残差模型 | `models/residual.py` — 按日期折叠后估计 σ、`sigma_from_mae` 用 √(π/2) |
| 概率生成 | `models/probability.py` — 积分括号自身半度区间；阶梯不完整即拒绝 |
| 四个模型基线 | `models/baselines.py` — `ecmwf_raw` / `gfs_raw` / `fusion` / `calibrated` 共用一条概率通路 |
| 市场基线 | `models/market_baseline.py` — 决策时点 = 目标日**开始**，与预报同规则 |
| 样本外评估 | `models/evaluation.py` — **按日期**切分、**按日期**算置信区间 |
| 报告生成 | `scripts/baseline_report.py` → `research/baseline-comparison.md` |

**CI 抓到、本地漏掉的一个问题**：偏置方向测试把点预报放在括号正中心，正态对称使
±bias 概率**完全相等**（`0.13590512198327787 > 0.13590512198327787`），本地只靠浮点噪声
侥幸通过。已改到中心之上并用手工推导的 Φ 差值断言，对称性本身另立一条测试记录在案。

**另两处由"跑真实数据"暴露的问题**：

- **单位被假设**：Open-Meteo 默认返回 **°C**，首轮回填把 104,089 个摄氏度值存进了
  `*_f` 列。数据看着合理，只有 `mae=53°F`、`brier=2.0`（最差值）暴露了它。现已显式请求
  °F **并断言 `hourly_units.temperature_2m` 回显**，不合格即拒绝入库；坏数据已删除重取。
- **模型别名不一致**：回填存 `gfs`，查询用 `gfs_seamless` → GFS 基线显示"样本不足"而非评分。
  已改为存储 API 实际 id。

### P1-1 交付明细（2026-10-11）

| 目标 | 交付 |
|---|---|
| A 结算规则与观测 | `markets/settlement_rules.py`（408/408 解析）、`truth/`（交叉核对）、`research/klga-settlement-audit.md` |
| B ECMWF/GFS 历史运行 | `weather/single_runs.py`、`weather/backfill.py`、`scripts/backfill_forecasts.py` |
| C 每日最高温转换 | `weather/daily_max.py`（DST 感知） |
| D 独立研究库 | `storage/research.py` + `migrations_research/`（与采集器 schema 完全隔离） |
| E 测试验证 | 54 项新增测试；405 事件交叉核对；2 ECMWF + 2 GFS 实跑；60 天回填 |
| F 交付 | CLI `weatherquant-research`、3 份 docs、2 份 research 报告 |

**实测结果**：60 天回填 488 请求 → **485 成功 / 3 unavailable / 0 失败**；
104,956 小时点、4,641 日最高温、3,916 合格 / 725 不合格；
**62/63 个目标日可进入严格样本外评估**（两模型同）。

**在线核实已补齐**：Polymarket 规则在线取得，与旧库快照 SHA **逐字节一致**（未漂移）；
NCEI global-hourly 已冻结于 2025-08-27，结算期日最高温主源改为 **GHCN-Daily TMAX**（实测到 2026-10-07）。

### P1-0 浸泡测试发现并修复的缺陷

单元测试全绿仍未发现，只有故障注入浸泡才暴露：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | `MarketStream` 以 `self._stop` 存 `threading.Event` | 遮蔽 `Thread._stop` 方法 → `join()` 抛 `TypeError` → `stream.stop()` 失败 → **`run()` 的 `finally` 中断，`writer.close()` 永不执行** |
| 2 | DuckDB `CREATE SEQUENCE` 自增不跨 close/reopen 持久（实测 `1→2→2`） | 重启后 `event_id` 主键冲突 → **采集器启动即崩溃** |
| 3 | `time.time_ns()` 在 Windows 上分辨率约 15ms | 方案 2 的中间版本同样不可用：两次运行 ID 完全相同 |
| 4 | 备份文件名只精确到秒 | 同秒两次备份 `os.replace` 互相覆盖，"已验证的备份"被悄悄替换 |
| 5 | 干净退出时本轮的 `collection_gap` 从未关闭 | **每次正常停机都留下一个幽灵缺口**，被误读为停机中断 |
| 6 | `_publish(final=True)` 强制把状态覆盖为 `STOPPED` | **致命错误的最终心跳被报告为正常停机**，Docker 不会重启崩溃的采集器 |
| 7 | 404 被计为上游故障 | 历史市场的 `/book` 返回 404 → 连续失败计数飙升 → 健康探针误报 |
| 8 | 行尾注释在 `;` 之后变成下一条语句的前缀 | 备份的索引/序列 DDL 提取静默失败 → 备份可能不带索引 |
| 9 | `_pid_alive` 用 `os.kill(pid, 0)`，而 Windows 上对已死进程不抛错、对不存在的 pid 抛 `OSError(22)` | 判定恒为"存活" → **陈旧锁永不回收，后续启动全部退出码 4**；改用 `OpenProcess` |
| 10 | 单轮盘口轮询没有时间预算 | 死网下 66 个 token × 重试可阻塞主循环十余分钟 → 心跳停摆 → 健康探针把活着的进程判为假死 |

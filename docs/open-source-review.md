# 开源项目审计与技术选型

审计日期：2026-10-10（UTC）

审计对象：四个 GitHub 项目 + Polymarket 官方 SDK/API。

审计方法：全部仓库均**实际克隆并阅读源码**，不以 README 自述为准。每一个关键数学公式均独立
重新推导并判定正误；每一条结论均附 `path:line` 证据。**不因作者声称策略盈利而接受其结论**，
而是逐一检验该结论能否被时间泄漏或忽略成本所解释。

完整逐条审计（含全部证据）归档于：

| 文件 | 对象 |
|---|---|
| [`research/external/review-weatherbot.md`](../research/external/review-weatherbot.md) | technosheen/weatherbot |
| [`research/external/review-polymarket-backtest.md`](../research/external/review-polymarket-backtest.md) | luuhongyii/polymarket-backtest |
| [`research/external/review-polymarket-weather-bot.md`](../research/external/review-polymarket-weather-bot.md) | yannbellec/polymarket-weather-bot |
| [`research/external/review-py-sdk.md`](../research/external/review-py-sdk.md) | Polymarket 官方 SDK 与 API |
| [`research/legacy-quant-review.md`](../research/legacy-quant-review.md) | 本项目旧版量化核心 |

---

## 1. 四个项目速览

| 项目 | 许可证（已亲自核验） | 天气代码 | 可直接采用 | 一句话结论 |
|---|---|---|---|---|
| technosheen/weatherbot | **MIT**（Copyright 2026 alteregoeth-ai） | 有 | 否 | 边数推导正确但输入的 σ 系统性偏低；回测用合成价格，edge 是 σ 比值的恒等式 |
| luuhongyii/polymarket-backtest | **MIT**（2026 Polymarket Backtest Contributors） | **无**（零命中） | 否 | **仓库本身损坏**（核心 PIT 模块被 .gitignore 排除）；是加密涨跌做市回测器，与天气无关 |
| yannbellec/polymarket-weather-bot | **无 LICENSE 文件** | 有 | **依法不可** | 回测直接拿结算实况当预报；README 的 MIT 徽章是死链 |
| Polymarket/py-sdk | **MIT**（Copyright 2026 Polymarket） | 不适用 | **是** | 唯一存活的官方 SDK，只读能力实测可用 |

许可证核验方式：直接读取各仓库根目录 `LICENSE` 文件首行，并核对 `git log -1`。三个 MIT 项目均
已核对版权行。`polymarket-weather-bot` 经 `git ls-files` 确认**不存在任何 LICENSE/COPYING 文件**
——README:15 的 MIT badge 是死链，README:302-304 仅有一行 `[MIT](LICENSE)` 指向不存在的路径。
**默认版权即无授权**，故该项目代码在获得作者书面确认前不得复用。

---

## 2. 关键数学公式：独立推导与正误判定

### 2.1 天气桶概率：半度修正是决定性的

Polymarket 温度市场报价为整数华氏度，而底层温度是连续量。四舍五入取整
（`reported = floor(true + 0.5)`）意味着整数桶 `{46, 47}` 对应的**真实连续温度**是
`[45.5, 47.5)`，而不是 `[46, 47]`。

以 `T ~ N(46.5, 2)` 独立复算（`statistics.NormalDist`，stdlib）：

| 积分区间 | P |
|---|---|
| 正确 `[45.5, 47.5)` | **0.382924** |
| 漏掉半度 `[46, 47]` | **0.197412** |

低估约 **48%**，或者说正确值是错误值的 1.94 倍。这直接乘进 edge。

- **weatherbot**：`bot_v3.py:585` 对 `between 46-47°F` 桶**未加 ±0.5**，而同一文件
  `bot_v3.py:583-584` 的 exact-match 分支却加了——自相矛盾。→ **错误**
- **polymarket-weather-bot**：`signals/probability_engine.py:65-71,107-109` 在 °F→°C 换算后
  仍按 °C 使用 ±0.5 容差，而 °F 市场桶宽是 °C 的 1.8 倍。独立复算 P=0.1974 vs 正确 0.1105，
  **高估 79%**。→ **错误**
- 本项目：`weatherquant.markets.models.Bracket.probability_interval_f()` 是该换算的**唯一**
  实现，并有 `tests/test_bracket_math.py` 用独立正态 CDF 锁死上述 0.382924 / 0.197412。

### 2.2 由 MAE 推 σ：weatherbot 低估 20%

正态分布下 `MAE = σ · √(2/π)`，故 `σ = MAE · √(π/2) ≈ 1.2533 · MAE`。

weatherbot 直接令 `σ = MAE`（`train_v3.py:120-124`、`bot_v3.py:890-893`），把 σ 低估
约 20%，分布过窄 → 概率虚高 → EV/Kelly 虚高。作者自己观察到"EV>2 的单子胜率 0–12%"
（`bot_v3.py:1935`），却用 MAX_EV 上限掩盖而非修正根因。→ **错误**

polymarket-weather-bot 同样使用硬编码 σ（`probability_engine.py:45` 的 σ 表 `{0:1.0 … 5:3.5}` °C
纯硬编码；`run_nomads_live_test.py:83-93` 的 σ 基础默认 1.5，`MODELSCIENCE.md:7` 自认遗留）。

### 2.3 比较算子符号错误

polymarket-weather-bot `run_nomads_live_test.py:1335-1340` 计算 `δ = val − thr`，**不含算子翻转**：
对 `lte`/`lt` 概率完全颠倒，对 `eq` 语义错误，**只有 `gte` 恰好正确**。→ **错误**
（同文件 `:1175-1188` 的 GEFS 经验频率路径反而正确，且默认关闭。）

### 2.4 手续费公式（已向官方文档核实）

```
fee = C × feeRate × p × (1 − p)
```

- 来源：`docs.polymarket.com/trading/fees`
- `C` = 股数，`p` = 成交价，`feeRate` 按品类：**Weather = 0.05（taker）/ 0（maker）**，Crypto 0.07，
  Sports 0.05,Finance/Politics/Tech 0.04，Geopolitics 0。
- **只有 taker 付手续费，maker 永不付**；成交时收取。
- 保留 5 位小数。

官方文档四组算例全部复算通过：100 股 @ $0.50、rate 0.05 → **$1.25**；@ $0.01 → 0.0495（显示 $0.05）；
@ $0.30 与 @ $0.70 同为 **$1.05**（rate 0.05 时）；rate 0.07 时 $0.50 → **$1.75**、$0.30/$0.70 → **$1.47**。
费率为 0 时必须**抛异常**而非返回 0 —— 把"费率未知"当成"免费"会虚增每一笔 edge。

本项目实现见 `weatherquant.execution.pricing.taker_fee_usd`，测试见
`tests/test_pricing_invariants.py::test_fee_matches_polymarket_documented_example_sports` 等。

### 2.5 期望净收益（规范 §六）

```
cost           = fill_cost_usd + fees_usd + execution_buffer_usd        [USD]
expected_net_profit        = q × filled_shares − cost                   [USD]
expected_net_profit/share  = expected_net_profit / filled_shares         [USD/share]
                            = q − cost/filled_shares                     （代数恒等，已断言）
```

`q × filled_shares` 是赔付期望（赢时每股兑付 $1）。**这是模型自身概率下的期望，不是利润预测。**

实现于 `weatherquant.execution.pricing`，恒等式由
`test_per_share_profit_equals_q_minus_cost_per_share` 断言，费用不重复计账由
`test_fee_is_counted_once_and_only_once` 断言（fee 变动 Δ 时 total 恰好变动 Δ，而非 2Δ 或 0Δ）。

---

## 3. 时间信息泄漏：具体证据

这是本次审计最重要的发现类别。四个项目**全部**存在泄漏。

### 3.1 weatherbot — 回测 edge 是恒等式

`backtest.py:44,152-186`：44 场价格中 **39 场不是观测值**，而是用 `MARKET_SIGMA=3.5` 从"市场预报"
合成的。独立重推：若双方预报相同，仅因 σ_b=2 < σ_m=3.5 即**恒产生 +73% 的 "EV"** —— edge 是 σ
比值的恒等式，与定价错误无关。

预报取 Open-Meteo Historical Forecast API（`backtest.py:50,99-118`），该 API 官方文档说明是"拼接
序列"，用的是**当天/数小时前初始化的批次**，晚于最长 72h 的决策时点；`train_v3.py` 用同一 API
训练 σ，同病。另：回测用 HRRR-first 美国策略 ≠ 生产的 ECMWF-first；blacklist/danger zone/EV cap
等闸门参数在 n=4–18 的同一样本上拟合又评估。数据文件全被 gitignore，回测不可复现。

**线上入场路径本身无 forecast-after-decision、无用实测选桶、无用结算价入场**——选桶
`bot_v3.py:1879-1883`，实际温度仅结算后取证 `:2277-2278`，结算委托 Gamma API `:1433-1448`
（站点/取整规则与 Polymarket 一致，是全项目最正确的一环）。但校准取"结算前最后快照"
（`:882-887`）属决策后信息；ghost 结算 `pnl` 变量跨单污染（`:2339`）。

### 3.2 polymarket-weather-bot — 回测直接吃结果变量

**致命**：`notebooks/backtest.py:144-149` — `simulate_trade` 直接把 `actual_temp_max`
（结算实况）当预报算概率；`:258` 兜底链同样回落到实况；入选条件要求市场已 resolved（`:230-239`）。
回测的 our_prob / edge / stake **全部吃到结果变量**。

- **严重**：`backtest.py:76-78` — 气候池 `end_yr = now.year − 1` 在回测运行时切片，包含目标市场
  自身月份/未来年份（权重 0.35）。
- **严重**：时点错配 — 价格为 `[D−48h, D−24h]` 均值（`history_scraper.py:177-181`）vs 预报代理含到
  `D−1` 实况（`backtest.py:59-60`）；回测非时序（`backtest.py:252`）。
- **中**：`signals/sources/open_meteo.py:121-126` — 预报缺失时回落 archive 实况，目标日已过而市场
  未闭合时模型即见结果。

结论：其 "PROFITABLE"（`backtest.py:209`）可完全由泄漏 + 互补价买 NO（`:143`）+ 魔数成本
（`:158,171`）产生，**不可信**。

另：该仓库默认站点表是 `KNYC`（`contract_parser.py:31`），而真实 NYC 市场解析站是 `KLGA`
（`temp/llm_diag_dump.txt`）——**站都不对**。

### 3.3 polymarket-backtest — 同 tick 成交 + 未来价格记账

- **同 tick 成交**：`orderbook_replay.py:271-298` taker 在**决策同一 tick** 成交，零延迟，
  违反其自身 README.md:166 的原则。
- **非因果权益**：`engine.py:100-111` — 按未来 bar 的价格成交，却在决策 tick 记账后再 mark；
  恰好复现 README 的 `initial_equity 99.8 = 100 − 10×0.42 + 10×0.40`。
- **PIT 是约定不是强制**：`context.py:21-22` 把含未来的价格/成交全量面板暴露给策略；
  `LookaheadAuditor` 只做结构检查（`lookahead.py:16-35`），其快照告警 `:47-61` **从未被调用**。
- **无训练/切分/OOS**：硬编码"已校准"尺寸阶梯（`complete_set.py:161-225`），`rg` 全仓
  train/split/oos 命中 0。
- **成交模型不真实**：`execution.py:47-76` 按点位价格序列全额成交 ± 统一 10bps 滑点，无深度、
  无成交量、无价差 —— p≈0.5 时一个 tick 就是 200bps，成本被低估约 **10 倍**。
  `orderbook_replay.py:117` 的 `max(queue_participation, 0.25)` 使配置的 0.12 失效，虚增 maker 成交。

### 3.4 本项目旧版 — 独立样本假设导致 SE 低估约 5 倍

见 [`research/legacy-quant-review.md`](../research/legacy-quant-review.md)。核心缺陷：
`se = σ/√n` 把约 30 个**同目标日**的运行当独立样本，SE 低估约 5 倍；"保守概率"因此只缩小
0.007–0.009（**纯装饰性**）。且 train/val 按 `run_time` 切分，同一 `target_date` 的观测跨切分泄漏。

**本项目对策**（已实现并测试）：`weatherquant.models.metrics.date_level_summary()` 先把同目标日的
运行折叠成单日均值，再在 G 个日期间取 SE。`test_treating_runs_as_independent_would_understate_uncertainty`
直接断言 `clustered.standard_error > naive_se`。

---

## 4. 四项必答

### 4.1 推荐直接使用的依赖

| 依赖 | 版本 | 许可证 | 用途 | 依据 |
|---|---|---|---|---|
| `polymarket-client`（PyPI 包名；仓库 `Polymarket/py-sdk`） | **0.13.0**（2026-10-09 发布） | **MIT** | 官方只读客户端 | 13 类只读能力**零密钥实测通过**；依赖 `httpx[http2]`+`pydantic`，与本栈零冲突 |
| `httpx` | `>=0.27,<1` | BSD-3 | 补 SDK 未封装的端点 | 与 SDK 同底层，避免双栈 |
| `pydantic` / `pydantic-settings` | `>=2.7,<3` / `>=2.2,<3` | MIT | 数据模型与配置 | 官方 SDK 同为 v2 |
| `duckdb` | `>=1.1,<2` | MIT | 研究数据库 | 旧版已验证可用 |
| `numpy` / `scipy` / `pandas` | 见 `pyproject.toml` | BSD 系 | 数值与评估 | 规范指定 |
| `pytest` / `pytest-cov` / `ruff` | 见 `pyproject.toml` | MIT/Apache-2 | 测试与 lint | 规范指定 |

**不要**使用 `py-clob-client`：已于 **2026-05-25 被 GitHub 归档**，README 声明
"no longer functional and should not be used"，末版 0.34.6（2026-02-19）。`Polymarket/agents`
同样于 2026-05-11 归档；`py-order-utils` 已 404（改名 `python-order-utils`，是旧客户端的签名依赖，
与 py-sdk 无关）。

### 4.2 值得参考的算法（思路，非代码）

1. **weatherbot 的 CLOB fail-closed 安全模式**：下单前重报价验证、原子写。
2. **weatherbot 的结算委托**：交由 Gamma API 按 `condition_id` 取官方结果，站点/取整规则与
   Polymarket 一致 —— 四个项目里做得最对的一环。
3. **weatherbot 的分位数 Kelly 与 14 道仓位闸门结构**（`bot_v3.py`）：闸门**结构**可参考，
   **参数**不可（见 4.4）。
4. **polymarket-weather-bot 的 GEFS 经验频率路径**（`:1175-1188`）：直接用集合成员经验分布算桶
   概率，绕开"硬编码 σ + CDF"这一整类错误 —— 但它是**默认关闭**的，需重写后才能用。
5. **polymarket-backtest 的 `1 − (up + down)` 完整集合 edge**（`complete_set.py:284-285`）：
   代数正确。
6. **四项目共同的"每次评估都落库并附 reason"模式**（旧版 WeatherQuant 也已实现）：`NO_TRADE`
   是常态，阈值不为制造交易而下调。

### 4.3 可以依法复用的代码模块

| 项目 | 可复用范围 | 法律依据 |
|---|---|---|
| `Polymarket/py-sdk` | **整个只读客户端** | MIT，保留版权声明即可 |
| `technosheen/weatherbot` | 安全模式、结算委托、Kelly 闸门结构 | MIT，保留版权声明即可 |
| `luuhongyii/polymarket-backtest` | 完整集合 edge 代数 | MIT，保留版权声明即可 |
| `yannbellec/polymarket-weather-bot` | **无** | **无 LICENSE 文件 = 默认版权无授权** |

**实际建议：本项目不直接粘贴上述任何仓库的代码。** 三个 MIT 项目在法律上可复用，但在数学上都
有已证实的错误，抄一段就得连它的假设一起抄。唯一真正值得"直接使用"的是 `polymarket-client`
这个**依赖**，而非代码片段。

### 4.4 不应该复用的模块

- `weatherbot` — `backtest.py` 全部（合成价格）、`train_v3.py` 的 `σ = MAE`、`bot_v3.py:583-596`
  的桶概率与边数分支、以及在同样本上拟合又评估的各闸门参数。
- `polymarket-backtest` — **整个仓库**（核心 PIT 模块缺失，`import` 与 9/9 测试均失败）；
  即使修复也不复用 `execution.py`（10bps 统一滑点、无深度）与 `orderbook_replay.py` 的
  同 tick 成交。
- `polymarket-weather-bot` — **依法不可复用任何代码**；技术上亦不应复用 `notebooks/backtest.py`、
  硬编码 σ 表、比较算子分支、`contract_parser.py` 的站点表、以及 `py_clob_client==0.34.6`。
- 三者的**仓位参数、EV 门槛、成本魔数**一律不复用 —— 它们都未经过样本外验证。

---

## 5. 每个项目的潜在设计缺陷

### 5.1 technosheen/weatherbot

1. `σ = MAE` 而非 `MAE · √(π/2)`，σ 低估 ~20%，全链概率虚高（`train_v3.py:120-124`）。
2. 桶概率漏 ±0.5 半度（`bot_v3.py:585`），且与同文件 exact 分支不一致。
3. 回测 39/44 场价格为合成值，edge 恒为 σ 比值的恒等式（`backtest.py:44,152-186`）。
4. 训练与回测共用 Open-Meteo Historical Forecast"拼接序列"，存在预报晚于决策时点。
5. 机场偏差**零显式修正**（仅机场坐标 `bot_v3.py:478`；METAR 抓了不用 `:1620`）。
6. 无多模型融合，仅模型选择 + 平方和加宽（`:1642-1675`）；无校准层、无 Brier 检验。
7. gas 不入 PnL；spread 用 `ask+3¢` 闸门粗略覆盖。
8. 测试为**自引用循环验证**（`test_bot_v3_probability.py:6-14`），非独立期望值。
9. `import py_clob_client_v2`（`clob_trader.py:12`）但 README:91 指示装旧版 `py-clob-client`
   —— 按 README 安装必然 `ImportError`；无任何版本 pin。
10. `config_v2.json` 把 Visual Crossing API key **提交进了仓库**。
11. 审计期间**因权限未执行测试**（测试质量结论基于静态阅读）。

### 5.2 luuhongyii/polymarket-backtest

1. **仓库损坏**：`.gitignore:10` 的 `data/`（本意是数据集）同时排除了
   `src/polymarket_backtest/data/`，即整个 PIT 核心（calendar/pit/schema/ingest/store/orderbook）。
   全部模块与 9/9 测试 import 它，`import polymarket_backtest` 与全部测试在收集阶段即失败。
   README.md:52 仍宣称该目录存在。
2. **零天气代码**：`rg weather|nws|noaa|forecast|airport|ensemble|brier` 在代码与文档中命中 **0**。
   实为加密涨跌完整集合做市回测器。
3. 回测按点位价格序列全额成交 ±10bps，无深度/成交量/价差，成本低估 ~10×
   （`execution.py:47-76`）。
4. `min(latency, len(future))` 的延迟回退可导致**提前成交**（`execution.py:52`）。
5. taker 零延迟同 tick 成交（`orderbook_replay.py:271-298`）。
6. 未来价格在决策 tick 记账，权益非因果（`engine.py:100-111`）。
7. 策略可访问含未来的全量面板，PIT 靠自觉不靠强制（`context.py:21-22`）；
   `LookaheadAuditor` 的快照告警从未被调用（`lookahead.py:47-61`）。
8. `max(queue_participation, 0.25)` 使配置的 0.12 失效，虚增 maker 成交（`:117`）。
9. `complete_set_edge` 用 bid 而非 ask（应为 ask），且为死代码（`:288-292`）。
10. 无任何 Kelly；`bankroll` 上限**默认关闭**（`complete_set.py:36-38`）。
11. 结算模块缺失（PIT 部分被 gitignore），无 UMA/链上/天气站规则。

**正面**：手续费 `size × rate × p × (1−p)`（taker-only，rate 0.07）与当前官方文档一致 —— 公式正确。

### 5.3 yannbellec/polymarket-weather-bot

1. **无 LICENSE** —— 无复用授权。
2. 回测直接以 `actual_temp_max` 作为预报（`backtest.py:144-149,258`），入选条件要求已 resolved。
3. 气候池在未来年份切片（`backtest.py:76-78`）。
4. 价格与预报时点错配；回测非时序（`backtest.py:252`）。
5. 比较算子缺符号翻转，`lte/lt` 概率颠倒（`run_nomads_live_test.py:1335-1340`）。
6. 硬编码 σ 表与 σ=1.5 默认值，无 skill 校准。
7. °F 桶用 °C 容差，概率高估 79%。
8. 层权 0.55/0.30/0.15 无依据（`probability_engine.py:48-50`）；年份硬编码 2026（`:125,177`）。
9. **站点错误**：代码用 `KNYC`，真实解析站是 `KLGA`；且无站点偏差修正，偏差参考系是
   Open-Meteo 城市网格实况而非解析站（`model_calibrator.py:103-116`）。
10. README 宣称的 ECMWF 51 成员集合**不在交易路径**（`cdsapi` 仅用于 notebooks）。
11. 预报缺失时回落 archive 实况（`open_meteo.py:121-126`），可能在决策时点后取到结果。
12. `ev > 0` 门槛**无成本 haircut**（`finance_engine.py:902`）。
13. 137 个测试仅 collect-only（审计时因权限未执行）；Kelly 测试测的是本地复制品
    （`tests/test_all.py:457-470`）；核心 CDF 公式**无数值测试**。

### 5.4 Polymarket/py-sdk（官方 SDK）

1. **0.x 版本允许 minor 破坏** —— 必须 pin 精确版本。
2. **issue #321**（分页 cursor 重复，2026-09-29 截至审计时**未修**）—— 需自行加护栏。
3. `list_markets` **会静默丢弃非二元市场** —— 对只做二元温度市场无碍，但须知晓。
4. Gamma 扁平字段被重组为 `m.state` / `m.resolution` 嵌套，直接访问 `.uma_resolution_status`
   会 `AttributeError`（实测踩坑）。
5. 仓库名与 PyPI 包名不一致：`py-sdk` 在 PyPI 上 **404**，真实包名是 **`polymarket-client`**
   —— 按仓库名安装会失败。
6. `/v2/resolutions` 未决时 `price=69` 为哨兵值（SDK 自动归一为 `None`）—— 调用方必须知晓，
   否则会把 69 当价格。

### 5.5 本项目旧版 WeatherQuant

16 项缺陷见 [`research/legacy-quant-review.md`](../research/legacy-quant-review.md) §12，要点：

1. `se = σ/√n` 把同目标日运行当独立样本，SE 低估 ~5×，"保守概率"仅缩小 0.007–0.009（装饰性）。
2. train/val 按 `run_time` 切分，同 `target_date` 观测跨切分泄漏；val bias −2.3°F。
3. 无在线校准门控 —— 可靠性/Brier 仅离线；且实测**市场 Brier 0.615 优于模型 0.829**，
   top-1 命中市场 0.59 vs 模型 0.30。
4. 信号溯源损坏：`book_id` 87.5% 为 NULL、`snapshot_id` 91.7% 为 NULL；存在公式版本漂移
   （flat buffer vs 0.5% notional）。
5. 溯源列缺口：`forecast_runs.raw_json` 100% NULL、`forecast_hourly` 无 `fetched_at`、
   `venue_ts` 100% NULL、`settlements.resolved_at` 实为导入时间、`fetched_at` 被 upsert 覆盖。
6. 回放在无 `resolved_at` 检查的情况下记入结算收益（**资本前视**）。
7. 气候基线无日期上界（未来泄漏）。
8. `backtest/` 模块**零测试**；概率数学无独立期望值测试（仅属性测试 + `normal_cdf(1.96)`）。

**正面**（本项目已核对确认）：手续费公式与官方 `fee = C·feeRate·p·(1−p)` **完全一致，无重复计费**；
**midpoint 从未被当作成交价**；`publishable_at` 守卫在 signal 与 predict **两个入口均已生效**；
账本恒等式在真实数据上全部成立（残差 1e-13）。

---

## 6. 最终技术选型与原因

### 6.1 Polymarket 客户端

**选 `polymarket-client==0.13.0`（仓库 `Polymarket/py-sdk`）作默认只读客户端，外包一层薄
Repository；`httpx` 仅用于 SDK 未封装的端点。**

理由：
- `py-clob-client` 已归档且官方声明不可用，`py-order-utils` 已 404 —— 旧客户端路线已死。
- 新 SDK 是**唯一存活的官方封装**，MIT，13 类只读能力零密钥实测通过（Gamma 元数据、`/book`
  盘口、`/v2/prices-history`、`/v2/trades`、`/v2/resolutions`、持仓/holders/OI/排行榜）。
- 依赖 `httpx[http2]>=0.27,<1` + `pydantic>=2,<3`，与本项目技术栈**零冲突**；不含 `requests`、
  不含完整 `web3.py`；`requires-python>=3.11`（我们用 3.12）。
- 已在 Python 3.12.10 venv 中**实际安装并真实调通**。

**为什么不只用裸 HTTP**：SDK 自带分页、重试与 Pydantic 模型，且随 Polymarket API 演进维护。
但必须**外包薄 Repository 层**，原因有二：其一，0.x 允许 minor 破坏，Repository 把 SDK 类型
挡在领域模型之外；其二，SDK 的字段重组（`m.state` / `m.resolution`）与 `list_markets` 静默丢弃
非二元市场，需要在边界处显式处理。

**风险与护栏**：pin 精确版本；对分页 cursor 去重（issue #321）；只走 `PublicClient`
（免认证只读），**绝不触碰 `SecureClient`**（私钥交易）—— 本项目 P0–P2 禁止真实下单。

### 6.2 只读数据能力边界（决定回测设计）

| 数据 | 可获得性 | 细节 |
|---|---|---|
| 价格序列 | ✅ | `/v2/prices-history`，单窗 ≤15 天，bucket 60–86400 秒 |
| 　└ 1 分钟 | ⚠️ | **仅存约 7 天**（10 天前返回 0 点） |
| 　└ 5 分钟 | ✅ | ≥40 天实测可用（文档称 ≥60 天） |
| 　└ 30 分钟 | ✅ | ≥40 天（文档 ≥90 天） |
| 　└ 12 小时 | ✅ | **永久全史** |
| 成交历史 | ✅ | `/v2/trades` 全生命周期全量，公开 |
| 结算结果 | ✅ | `/v2/resolutions`（未决时 `price=69` 哨兵）+ Gamma `umaResolutionStatus` / `outcomePrices` |
| **历史订单簿 L2 深度** | ❌ | **三份官方 OpenAPI 全路径清点确认无任何端点** |
| 官方预报 API | ❌ | 不存在；隐含概率即价格序列 |

**两个由此而来的硬约束：**

1. **交易回测只能覆盖本系统自行采集到深度的时间窗。** 历史深度不可重建，`prices-history`
   只有 mid，而 mid 不可成交（规范 §六）。旧版已正确地把这种情况报为
   `HISTORICAL_BOOK_UNAVAILABLE` 而非假装可回测。
2. **1 分钟价格与盘口快照正在永久流失。** 5 分钟以上的历史能事后补，1 分钟的补不了。
   这把"启动持续采集器"提到了 **P1 第 0 项**（见 [`roadmap.md`](roadmap.md)），
   而不是按原设想放到后面。

### 6.3 速率限制（文档数字，按 IP / 10 秒）

| 服务 | 限额 |
|---|---|
| Gamma 总 / `/markets` | 4000 / 300 |
| Data v2 总 / `/v2/trades` / `/v2/prices-history` | 800 / 300 / 200 |
| CLOB `/book` / `/prices-history` | 1500 / 1000 |

超限**延迟而非拒绝**；SDK 内建 Retry-After 退避。

### 6.4 天气数据源

沿用旧版已验证的路径，P0 不改动（属 P1 采集任务）：

- **预报**：Open-Meteo `single-runs-api.open-meteo.com`（注意：`single-run-api` 单数形式 TLS 失败），
  `models=ecmwf_ifs025` 与 `models=gfs` 两条并行管线 —— §五 的 ECMWF/GFS 双基线都需要。
  **已知约束**：ECMWF 存档滞后实时，须按"最新可用 run"而非"当前 run"处理，且
  **绝不能把 run 的初始化时间当成发布时间**（本项目 `ForecastRun.publishable_at_utc` 强制此约束）。
- **观测**：IEM ASOS archive（`station=LGA`，回溯至 2026-07 及更早）作交叉核对；
  NOAA AWC METAR（`aviationweather.gov/api/data/metar`）作现势。
  **观测永远不是官方结果** —— 本项目 `weatherquant.weather.models.Observation`
  **刻意不含 `is_official` 字段**，官方结果只存在于 `Settlement`。
- **结算**：Polymarket 自身已决结果（`/v2/resolutions`，`status=="resolved"` 时 `price∈{0,1}`）。

### 6.5 明确排除

- ❌ `py-clob-client`（已归档）
- ❌ `polymarket-weather-bot` 的任何代码（无授权）
- ❌ 任何把理论 edge 当作利润、或把回测收益当未来保证的实现
- ❌ 真实资金下单、Dashboard（本阶段禁止）
- ❌ LLM 直接猜测天气结果

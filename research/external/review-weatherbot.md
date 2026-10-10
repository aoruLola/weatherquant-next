# technosheen/weatherbot 审计

> 审计对象：https://github.com/technosheen/weatherbot
> 方法：`git clone --depth 1` 后补全历史，通读全部 Python 源码（29 个 .py 文件），逐条给出 `path:line` 证据；关键公式独立重推。测试套件**未执行**（权限系统禁止对克隆仓库运行 pytest），测试质量部分为静态审查结论。

## 0. 元信息

| 项 | 值 |
|---|---|
| Clone commit | `f2668af6db8e0366f993d680c43879368c18cf9a` |
| 最后提交时间 | 2026-05-02 01:46:15 +0000 |
| 提交历史 | 45 个 commit（2026-03-03 首提交 → 2026-05-02） |
| 文件数 | 38 个 tracked 文件 |
| 语言构成 | Python 29 文件 / 8113 行；Markdown 3 文件 / 1169 行；HTML 1 文件 / 303 行；JSON 3；LICENSE、.gitignore 各 1 |
| 作者 | technosheen（唯一提交者） |
| 主体 | `bot_v3.py` 2758 行单体 + `clob_trader.py`、`backtest.py`、`train_v3.py`、14 个测试文件 |
| 数据 | `data/`（calibration、training_data、all_trade_data、markets、state）**全部被 .gitignore 排除，仓库内不存在**——回测与校准在 fresh clone 上不可复现 |

## 1. 许可证

**存在 LICENSE 文件，为 MIT License**（`LICENSE:1-21`），原文关键段落：

```
MIT License
Copyright (c) 2026 alteregoeth-ai
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software...
```

- **许可证**：MIT。**OSI-approved：是**（MIT 在 OSI 批准清单内）。
- **复用义务**：只需在所有副本/实质性部分中保留版权声明与许可声明（`LICENSE:12-13`）。无 copyleft、无源码开放义务、无专利条款。
- **商业/衍生使用**：**明确允许**（"use, copy, modify, merge, publish, distribute, sublicense, and/or sell"）。再分发、闭源商用、修改后销售均可。
- **注意点**：版权人写的是 `alteregoeth-ai` 而非仓库 owner `technosheen`（`LICENSE:3`），暗示代码移植自另一实体/fork（代码注释亦提及 "ported from nicolastinkl fork"，`bot_v3.py:425`）。MIT 下这不构成障碍，但衍生复用时应保留原版权声明。
- **无 setup/pyproject 元数据**：仓库没有 setup.py / pyproject.toml / requirements.txt，许可证信息仅存在于 LICENSE 文件。
- 另注意 `config_v2.json` 中**提交了真实 Visual Crossing API key**（`config_v2.json` `"vc_key": "P6J9XTNMZL8WHEELNVJ3WZGRE"`），虽与许可无关，但属于泄漏的第三方凭证。

## 2. 天气模型

- **数据源**：全部经 **Open-Meteo** API 代理，非直接 NWS/ECMWF 官方接口：
  - `get_ecmwf` → `models=ecmwf_ifs025`（`bot_v3.py:1353-1355`）
  - `get_icon` → `icon_seamless`（`bot_v3.py:1357-1359`）
  - `get_hrrr` → `models=gfs_seamless`（实际是 GFS/HRRR blend，命名有误导，`bot_v3.py:1361-1367`）
  - `get_gem` → `gem_seamless`，仅美洲（`bot_v3.py:1369-1374`）
  - METAR 实况来自 aviationweather.gov（`bot_v3.py:1376-1393`），D+0 才取（`bot_v3.py:1616`）
  - 历史实测温度用 Visual Crossing（`bot_v3.py:1411-1431`），仅结算后取证用
- **确定性 vs ensemble**：取的是各模型**单一确定性预报**（`daily=temperature_2m_max`，`bot_v3.py:1331`），**没有 ECMWF ENS/GFS 集合成员、没有分位数、没有概率预报产品**。README 所称 "5-model ensemble" 实为 4 个独立 NWP 的**跨模型集合**（METAR 实际未参与：见 `bot_v3.py:1620`，`model_temps` 只含 ecmwf/icon/hrrr/gem）。
- **概率机制**：唯一概率机器是"点预报 + 正态误差假设 + σ"（见第 3 节）。**没有** logistic/softmax、没有校准层（isotonic/Platt）、没有对市场隐含分布的拟合。
- **偏差修正**：`bias_correction=true` 传给 Open-Meteo（`bot_v3.py:1333`），是 Open-Meteo 的网格级历史订正，与站点无关。

## 3. 概率分布构建

核心实现 `bot_v3.py:564-585`：

```python
def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    s = sigma or 2.0
    half = 0.5
    if t_low == -999:
        return round(norm_cdf((t_high + half - forecast) / s), 4)
    if t_high == 999:
        return round(1.0 - norm_cdf((t_low - half - forecast) / s), 4)
    if t_low == t_high:
        return round(norm_cdf((t_high + half - forecast) / s) - norm_cdf((t_low - half - forecast) / s), 4)
    return round(norm_cdf((t_high - forecast) / s) - norm_cdf((t_low - forecast) / s), 4)
```

即 **P(bucket) = Φ((t_high−f)/σ) − Φ((t_low−f)/σ)**，固定 σ 正态假设。

σ 的来源：
1. 默认常数 `SIGMA_F = 2.0` / `SIGMA_C = 1.2`（`bot_v3.py:381-382`）。
2. 校准：`run_calibration` 对每个 city×source 取 |预测−实际| 的均值写入 sigma（`bot_v3.py:890-894`）：
   ```python
   mae  = sum(errors) / len(errors)
   new  = round(mae, 3)          # MAE 直接当 sigma 用
   ```
3. `train_v3.py:120-124` 同样：
   ```python
   def compute_sigma(errors: list) -> float:
       """Mean absolute error → sigma."""
       return round(sum(errors) / len(errors), 3)
   ```
4. 模型分歧按平方和并入（`bot_v3.py:1873-1875`）：
   ```python
   if _ens_std_sig is not None and _ens_std_sig > sigma:
       sigma = round(math.sqrt(sigma * sigma + _ens_std_sig * _ens_std_sig), 3)
   ```

**正误判定——错误**：
- **MAE≠σ**。正态误差下 `E|e| = σ√(2/π) = 0.7979σ`，故正确换算 `σ = MAE × √(π/2) ≈ 1.2533 × MAE`。代码直接令 `σ = MAE`，**把 σ 系统性低估约 20%**（分布过窄 → 中心桶概率虚高 → EV/Kelly 虚高）。正确形式：`sigma = mae * math.sqrt(math.pi / 2)`。
- 作者自己观察到过拟合证据并用补丁掩盖：`bot_v3.py:1935` 注释 "bets with EV > MAX_EV had 0–12% win-rate" —— 声称 EV>2 的单子实际胜率 0–12%，正是 p 系统性虚高的直接证据，但修复方式是加 `MAX_EV` 上限（`bot_v3.py:1936-1939`）而非修正 σ。
- **有限区间桶缺半度连续修正**：`t_low==t_high`（整数桶）与开放桶用 ±0.5，但 Polymarket "between 46-47°F" 解析为 `(46,47)`（`bot_v3.py:1485-1486`）后走最后一行，**不加半度**。若市场按整数高温结算（这正是代码自己在 `in_bucket`/exact-match 处理中隐含的假设，`bot_v3.py:1507-1508`），正确应为 `Φ((47.5−f)/σ) − Φ((45.5−f)/σ)`。数值例：f=46.5、σ=2 时，代码得 `Φ(0.25)−Φ(−0.25)=0.197`，正确值 `Φ(0.5)−Φ(−0.5)=0.383`——**差约一倍**。两类错误方向相反（窄 σ 抬高 p、丢半度压低 p），净效应不可控且从未做可靠性检验（无 Brier/reliability 图代码）。
- **无任何后校准层**：不存在 logistic/softmax/isotonic/calibration layer——"未发现"。

## 4. 机场观测偏差修正

问题：Polymarket 按特定机场站（KLGA/KJFK/KDAL…）结算，网格预报与站点观测存在站网偏差。

发现：
1. **唯一措施 = 使用机场精确经纬度**（`bot_v3.py:476-516`），如 `"nyc": {"lat": 40.7772, "lon": -73.8726, "station": "KLGA"}`（`bot_v3.py:478`）。README:59-61 自述 "City-center vs airport can differ 3–8°F. The bot uses exact airport lat/lon"。
2. **没有任何显式的站点−网格偏差项**。全库 `rg -i bias` 仅命中 Open-Meteo 的 `bias_correction` 参数（`bot_v3.py:1333`、`train_v3.py:104`、`backtest.py:110`），**量化结论：显式偏置修正代码 0 行**。
3. METAR 站点实况被抓取（`bot_v3.py:1395-1409`）但**被排除在集合与 best 之外**（`bot_v3.py:1620`：`model_temps` 只含 4 个 NWP），只进快照与打印（`bot_v3.py:751`），以及校准循环里的一个死分支（`bot_v3.py:877`，metar 的 sigma 永远不会被 `get_sigma` 用于交易，因为 `best_source` 不可能是 "metar"）。
4. 实盘校准的 ground truth 用 Visual Crossing 站点坐标日最高温（`bot_v3.py:1414,1427-1428`），**站点偏差被"吸收"进 σ（表现为方差），而非作为均值偏差修正**；而 `train_v3.py` 的 ground truth 是 ERA5 网格值（`train_v3.py:63-87`，同一坐标网格点），**网格 vs 网格，完全不含站点偏差** → 由 train_v3 产生的 σ 进一步低估结算相关误差。

## 5. 多模型融合

**不是** Bayesian model averaging / ensemble-of-ensembles，而是"选择 + 平方和加宽"：

1. ≥2 模型时算算术平均与总体标准差（`bot_v3.py:1623-1635`，`ensemble_stddev` `bot_v3.py:839-843`）。
2. **选择规则**（`bot_v3.py:1642-1655`）：≥3 模型且 std < 1.0°F/0.7°C → 用 ensemble mean；否则 **ECMWF 优先**，再次 ICON；GFS/HRRR 被有意降权（`bot_v3.py:1640-1641` 注释：GFS 美国城市 12% vs ECMWF 33% 胜率）。
3. 无模型权重、无基于历史 skill 的加权、无贝叶斯后验合并。模型分歧仅以平方和并入 σ（`bot_v3.py:1873-1875`）。
4. **内部不一致**：`train_v3.py:271` 生成 ensemble sigma 时乘 0.8（`ens_sigma = mean(sigmas) * 0.80`），而线上 `ENSEM_Sigma_REDUCTION` 已改为 1.0 并注释 "disabled — tight agreement was the worst-performing slice (7% win-rate)"（`bot_v3.py:387`）。但 `get_sigma` 优先读 `_cal["{city}_ensemble"]`（`bot_v3.py:861-868`），train_v3 写入的 ×0.8 值在被 run_calibration 覆盖前仍会生效——**"收紧 σ"的旧逻辑部分残留**。这也再次说明：模型高度一致时 σ 被进一步收紧，而实测该切片胜率仅 7%——集合一致度**低估**真实误差（共享系统性误差），正态独立误差假设不成立。
5. README 宣称 "Model disagreement is blended into sigma in quadrature"（README:49）与代码一致，但这只是启发式，不是融合。

## 6. Edge 计算（含独立推导与正误判定）

### 实盘公式（`bot_v3.py:587-595`）

```python
def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)
```

**独立推导**：
- 每 $1 以价格 `price` 买入得 `1/price` 股，赢每股付 $1：`EV = p·(1/price) − 1 = p/price − 1`。
- 代码：`p(1/price − 1) − (1−p) = p/price − p − 1 + p = p/price − 1`。**代数正确** ✓。
- Kelly：二元合约净赔率 `b = (1−price)/price = 1/price − 1`，全 Kelly `f* = (bp − q)/b`，代码逐字一致，再乘 `KELLY_FRACTION`（分数 Kelly）并截断到 [0,1]。**代数正确** ✓。
- 调用点：`ev = calc_ev(p, ask)`（`bot_v3.py:1927-1928`），**确实用模型概率对比市场 ask**；下单前重新拉取 `bestAsk/bestBid` 再算（`bot_v3.py:1999-2011` + `validate_repriced_signal` `bot_v3.py:656-692`，`signal["ev"] = calc_ev(p, real_ask)` `bot_v3.py:683`）。
- 附加调整：EV 门槛乘 win-rate 因子、时间溢价、learned premium（`bot_v3.py:1931-1934`）；仓位 = Kelly × ens_scale（0.5–1.5，`bot_v3.py:1945-1955`）× Nicolas 调整（`bot_v3.py:1942,1082-1086`）。

**判定**：EV/Kelly 代数正确，但**输入 p 不可信**（第 3 节：σ=MAE 低估 20%、有限区间桶丢半度、无校准层），因此 edge 数值本身不可信。另外 `calc_kelly` 的 p 虚高会直接放大仓位（分数 Kelly 只是缓冲）。

### 回测公式（`backtest.py:15,221`）

```
ev = p_bot / price - 1   # price = 市场价
```

与实盘一致，**但 39/44 场的 price 不是市场观测值**，而是 `bucket_prob_cont(market_forecast, …, MARKET_SIGMA)` 合成（`backtest.py:44-45,152-186`）。独立重推其"edge"：

- 合成价：`price = P_mkt(bucket | market_forecast, σ_m=3.5)`（`backtest.py:44`）
- 机器人：`p_bot = P_bot(bucket | forecast, σ_b≈2.0 或 MAE 校准值)`（`backtest.py:47,192-200`）
- 若两者**预报完全相同** f=46.5、桶 (46,47)：`p_bot = 0.197 (σ=2)`，`price = 0.114 (σ=3.5)` → **EV = +0.73，纯由 σ 比值制造**，与信息优势无关。σ_b=2.5 时仍有 +0.40。

**结论：回测的 "edge" 是"我的 σ 比我假设的市场 σ 窄"这一设定的恒等式产物，不是可交易的定价错误。** EV 公式本身没错，错在 p 与 price 的生成方式（price 是合成的、p 是过窄分布算的）。

## 7. 手续费与滑点

- **显性交易费**：**未发现任何 fee 字段**——这与现状一致（Polymarket 多数天气市场无 taker fee），可接受。
- **价差（spread）**：
  - 初筛价由 Gamma `outcomePrices` ±1¢ 近似（`bot_v3.py:1747-1750`），是有意的粗代理。
  - 下单前用真实 `bestAsk` 重算 EV，并拒绝 `real_spread > MAX_SLIPPAGE (0.03)`（`bot_v3.py:2006-2009`，常数 `bot_v3.py:56`）。**以 ask 成交本身就隐含吃掉半价差**，这一项算被覆盖。
  - 结算/退出按 `bestBid` 估值（`bot_v3.py:2620-2627`、`bot_v3.py:1827`）——卖侧也在 bid，方向保守合理。
- **滑点**：没有队列/冲击模型，只有 spread 上限闸门；GTC 限价单可能不成交，靠 `pending_buy`+reconciliation 兜底（`bot_v3.py:2065-2071`）。
- **gas/relayer**：赎回 gas 只在 `weatherbot_redeem.py` 里做余额下限告警（`GAS_FLOOR_USD = 2.0`，`weatherbot_redeem.py:76,273-283`），**不进入 PnL 记账**：`pnl = shares*(1-price)` 或 `−size`（`bot_v3.py:2259`）、`calculate_exit_pnl`（`bot_v3.py:374-378`）均无成本项。小额仓位（$1–20）每次兑换的 gas 占比可能达数个百分点，**被系统性忽略**。
- 回测则**完全无成本**：以 `early_avg_price`（仅 5 场）或合成 CDF 价成交（`backtest.py:146,168,183`），无价差、无 gas、无未成交。

## 8. 回测机制

`backtest.py`（44 个历史事件，`backtest.py:3,272`）：

1. **不是订单簿回放**。价格：5 场用 `early_avg_price`（某种均价，源文件 `data/all_trade_data.json` 不在仓库），**其余 39 场用 `MARKET_SIGMA` 正态 CDF 合成**（`backtest.py:6-8,152-186`）。成交假设 = 名义价，无 bid/ask、无部分成交、无未成交情形。
2. **前瞻泄漏（lookahead）确凿**：
   - **预报取自 Open-Meteo Historical Forecast API**（`backtest.py:50,99-118`）。该 API 按官方文档返回**拼接序列（stitched timeseries）**："Each run's first few hours are stitched into a continuous hourly timeseries"——即某日的温度取自**当天（甚至几小时前）初始化的预报批次**，而非决策时点（最长 72h 前）可得的批次。要取固定提前量需用 Previous Runs API（`run`/lead-time 参数），代码没有用。**回测授予了模型决策时不存在的近实时信息**。
   - `bias_correction=true`（`backtest.py:110`）对历史预报做基于观测的订正，进一步向真值靠拢。
   - `train_v3.py` 用同一 API 训 σ（`train_v3.py:29,90-117`）——**σ 是在近零提前量下测得的**，却被实盘用于 2–72h 决策（`MIN_HOURS/MAX_HOURS`，`bot_v3.py:52-53`），系统性偏小。
   - **策略不一致**：回测 v3 对美国城市优先 HRRR（`backtest.py:320-324`），线上 v3 ECMWF 优先、GFS 降权（`bot_v3.py:1647-1649,1640-1641`）——回测的不是生产策略。
   - **规则在同一小样本上拟合并评估**：CITY_BLACKLIST、danger zone、EV cap、min_ensemble_std 全部由注释里标明的 n=4–18 的实盘样本反推（`bot_v3.py:390-412,1907-1914,1935`），再用同源样本报告效果 → 选择偏差。
   - 胜负判定用 `actual`（`backtest.py:165,170,343-344`）仅作结算，未用于选桶——**桶选择本身未见用真值**（见第 9 节）。
3. **不可复现**：依赖 `data/training_data.json` 与 `data/all_trade_data.json`（`backtest.py:251-252`），均不在仓库中；fresh clone 直接 `FileNotFoundError`。
4. 回测无风险闸门对齐（MIN_EV=0.05 vs 线上自适应门槛、无 spread 检查、无 5 股下限），结果与实盘行为不可比。

**结论：该回测是"合成价格 + 零提前量预报 + 同样本选规则"的三重泄漏结构，其盈利/胜率输出不可作为任何收益证据。**

## 9. 时间信息泄漏（具体证据 file:line）

已证实的泄漏/时序问题：

1. **回测预报为"当天拼接批次"，晚于决策时点** — `backtest.py:50,105-111`（Historical Forecast API，见第 8 节文档依据）。
2. **σ 校准用近零提前量误差** — `train_v3.py:90-117`（同 API）→ 实盘 2–72h 决策用过小 σ。
3. **线上校准取"结算前最后一张快照"而非入场时快照** — `bot_v3.py:882-887`：
   ```python
   snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                if s.get("best_source") == source or s.get(source) is not None), None)
   ```
   `reversed(...)` 取最新（最靠近结算）的预报误差来估计 σ——用**决策时点之后**的信息校准决策时模型。
4. **同一样本内拟合 + 报告**：`adapt_thresholds`（`bot_v3.py:1229-1296`，min 15 笔）、win-rate EV 乘子（`bot_v3.py:933-953`）、blacklist（`bot_v3.py:390-396`，commit message 直接写 "0% WR, combined -$7.24"）、EV cap（`bot_v3.py:1935`）——全部从已结算单子学习，而这些单子又是收益展示来源，构成 in-sample 选择偏差（不是严格 lookahead，但同样使"有效性"声明失效）。
5. **ghost 结算把上一笔的 PnL 记入当前笔学习日志** — `bot_v3.py:2334-2343`：ghost 循环内 `nicolas_record_trade(..., pnl=pnl, ...)` 引用的 `pnl` 在该循环中从未赋值，它要么是本函数早期其他市场残留的旧值（`bot_v3.py:2151,2259,1833`），要么在本扫描从未赋值时抛 `UnboundLocalError`。**学习数据被跨单污染**（`cost/kelly/ev` 取自当前 pos，`pnl` 却是上一单的——`bot_v3.py:2339`）。

**未发现**（明确排除项）：
- 实盘入场路径无"预报时间戳晚于决策"：预报与决策在同一次 `scan_and_update` 内，`ts = now`（`bot_v3.py:1592,1611`），`opened_at = snap.ts`（`bot_v3.py:1979`）。
- **未发现用观测温度选桶**：桶由点预报匹配（`bot_v3.py:1879-1883` `in_bucket(forecast_temp, ...)`）；`get_actual_temp` 只在结算/取证时调用（`bot_v3.py:2277-2278,2325-2326`）。回测中 `actual` 也只用于标 `is_winner`（`backtest.py:165,170`）。
- **未发现用结算价入场**：入场价来自 Gamma 实时 `outcomePrices`/`bestAsk`（`bot_v3.py:1745-1750,2004`）；`check_market_resolved` 只出现在结算循环（`bot_v3.py:1433-1448,2252`）。
- 无跨时间 shuffle/重采样类操作。

## 10. 资金与仓位管理

- **分数 Kelly**：`f* = (bp−q)/b × KELLY_FRACTION`（`bot_v3.py:591-595`）；默认 `kelly_fraction: 0.25`（`config.json`），README 示例 0.1。
- **多重缩放**：× ens_scale（0.5–1.5，共识甜区还 ×1.15，`bot_v3.py:1945-1955`）× Nicolas 自适应（0.25–1.0，`bot_v3.py:1082-1086`）。
- **硬帽**：`MAX_BET`（默认 $20，`bot_v3.py:41`）；但 `bet_size` 会**向上突破 MAX_BET** 以满足 5 股最小可卖量（`bot_v3.py:695-715`），突破上限被 `min_cost = ceil(5×price×100)/100` 与 Kelly 余额双重约束。
- **组合层闸门**：单城一仓（`bot_v3.py:1854-1857`）、最大并发仓（`bot_v3.py:816-821`）、余额地板（`bot_v3.py:778-783,2022-2025`）、回撤暂停（`bot_v3.py:799-812`）、价格地板/上限、桶排名、danger zone 等 14 道闸（`analyze_signal` `bot_v3.py:723-837` + 扫描内联闸）。
- **判定**：结构完整，但仓位的输入 p 与 EV 不可信（第 3/6 节），分数 Kelly 只能部分吸收这一过估；且 Kelly 假设可按报价连续成交（实际 CLOB 薄）。

## 11. 结算方式

- **胜负判定 = 直接查询 Polymarket Gamma API 的市场自身结算结果**（`bot_v3.py:1433-1448`）：
  ```python
  r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", ...)
  if not data.get("closed"): return None
  yes_price = float(json.loads(data["outcomePrices"])[0])
  return True if yes_price >= 0.95 else (False if yes_price <= 0.05 else None)
  ```
  **这是本仓库最正确的一环**：站点、取整、四舍五入全部委托给 Polymarket 的实际 resolution 规则，本地规则即使写错也不影响盈亏入账。
- **PnL**：赢 `shares × (1 − entry_price)`，输 `−cost`（`bot_v3.py:2259`）；提前退出按 `(current_bid − entry) × shares`（`bot_v3.py:374-378`）；现金簿记 `balance += cost + pnl`（`bot_v3.py:2261`）——账务自洽，但**不含 gas**。
- **actual_temp（Visual Crossing）仅取证**（`bot_v3.py:1411-1431,2276-2278`），可能与 Polymarket 采用的 NWS 气候报告来源不同，**不影响 PnL**，但会影响校准/forensics 的一致性（VC vs NWS vs ERA5 三套 ground truth 混用：`bot_v3.py:1411` / `train_v3.py:64`）。
- 链上赎回独立于账务（`bot_v3.py:2101-2136`），幂等；已发生过一次人工对账冲销（`docs/weatherbot_v3_code_analysis.md` 记录 `reconciliation_adjustment ... writeoff_post_negrisk_redemption`），说明 paper 账本与链上现实会漂移。

## 12. 测试质量

- **数量**：14 个测试文件（另 `__init__.py`），**68 个 test 函数，约 150 条 assert，1073 行**（`rg -c "def test_"` / `rg -c "assert" tests/`）。
- **覆盖面（静态分类）**：
  - CLOB/实盘安全（对账、退出、僵尸、解析、gate）：~45 个测试，占 2/3——**质量高**，monkeypatch 打桩、断言 fail-closed 行为（如 `test_bot_v3_live_reconciliation.py`、`test_bot_v3_live_exit_safety.py` 13 个测试）。
  - 状态/持久化/审计分类：~15 个，中规中矩。
  - **概率与 edge 数学：仅 3 个**（`tests/test_bot_v3_probability.py`），且是**自引用验证**——测试内重写同一个 erf 公式再断言实现与之相等（`test_bot_v3_probability.py:6-14`），只能证明"代码按自己的定义实现"，**不检验定义本身是否符合结算规则或 σ 是否是真 σ**。
  - EV/Kelly：`test_bot_v3_signal_validation.py:49-50` 断言 `signal["ev"] == bot_v3.calc_ev(0.75,0.30)`——**同一函数两边，循环论证**；`bet_size` 测试（`test_bot_v3_bet_size_sellable.py`）有真实数值断言，是数学类里唯一过硬的。
  - **完全没有**：backtest 正确性、MAE→σ 换算、`scan_and_update` 决策路径、有限区间桶 vs 整数结算语义、成本模型。
- **未执行**（环境权限禁止对克隆仓库跑 pytest），以上为静态审查。
- **覆盖率判断**：运维安全面良好（估 ~70% 的安全分支有断言）；**金融数学面 ~10–20%，且多为自洽性测试而非独立真值测试**。README:251-256 声称 "Probability / EV math consistency" 覆盖——措辞准确（consistency），但容易被读成正确性验证。

## 13. SDK 与版本兼容性

- **客户端**：`clob_trader.py:12-15` `from py_clob_client_v2 import (ClobClient, ApiCreds, BalanceAllowanceParams, AssetType, OrderArgs, OrderType, OrderPayload, Side)`。
  - `py-clob-client-v2` **确实存在**（PyPI，Polymarket 官方组织维护，最新 **1.2.0，2026-09-25**），是 CLOB 的 v2 客户端；官方同时建议新项目用统一的 `Polymarket/py-sdk`。
- **版本钉死：无**。仓库**没有 requirements.txt / pyproject.toml / setup.py / lock 文件**——任何依赖都没有 pin。
- **README 安装指引与代码不符**：`README.md:91` 让用户 `pip install requests python-dotenv web3 py-clob-client`，装的是**旧版 `py-clob-client`**（最新 0.34.6，2026-02-19），其模块名是 `py_clob_client`，**没有 `py_clob_client_v2`**——按 README 配置的环境 `import clob_trader` 直接失败。docs 也只提 `py_clob_client`（`docs/weatherbot_v3_code_analysis.md:25`）。
- 其余接口：Gamma API（`gamma-api.polymarket.com`，`bot_v3.py:1435,1457`）、CLOB host `https://clob.polymarket.com` chain 137（`clob_trader.py:17-18`）、Open-Meteo、aviationweather、Polygon RPC（`weatherbot_redeem.py`）、web3（无 pin）。端点与当前 Polymarket API 形态一致，**API 兼容性大概率 OK，安装路径不兼容**。
- 安全附注：`config_v2.json` 提交了 Visual Crossing key；`config.json` 虽在 `.gitignore`（`.gitignore:2-3`）却已被 tracked 提交。

## 14. 设计缺陷清单

按严重度排序：

1. **σ = MAE（低估 ~20%）导致概率系统性虚高** — `train_v3.py:120-124`、`bot_v3.py:890-893`；正确应 `×√(π/2)`。作者以 `MAX_EV` cap、danger zone、city blacklist 等补丁掩盖 miscalibration（`bot_v3.py:1935-1939`），而非修正根因。
2. **回测三重泄漏**：拼接"当天"预报（决策时不可得）、合成市场价（σ_m=3.5 vs σ_b≈2 制造恒等式 edge）、同样本选规则再评估 — `backtest.py:44-45,99-118,152-186,330-338`。回测输出不构成任何收益证据。
3. **有限区间桶缺半度连续修正** — `bot_v3.py:585` vs 自身 exact-match 分支 `bot_v3.py:583-584`；与 `in_bucket` 隐含的整数结算假设（`bot_v3.py:1507-1508`）矛盾。
4. **机场站偏差零显式修正** — 仅靠坐标（`bot_v3.py:478`）；train_v3 用 ERA5 网格真值进一步剔除站点项（`train_v3.py:63-87`）；METAR 抓了不用（`bot_v3.py:1620`）。
5. **校准用结算前最后快照**（决策后信息）— `bot_v3.py:882-887`。
6. **ghost 结算 `pnl` 变量跨单污染 / 潜在 UnboundLocalError** — `bot_v3.py:2339`（`pnl` 在 ghost 循环内未赋值）。
7. **成本模型缺 gas**：赎回 gas 不入 PnL — `bot_v3.py:2259`、`bot_v3.py:374-378`；小额仓位影响显著。
8. **收益声明的统计效力为零**：所有闸门参数来自 n=4–18 的同一样本（`bot_v3.py:390-412`），无 held-out、无 walk-forward。
9. **依赖不可安装**：README 装 `py-clob-client` 而代码 import `py_clob_client_v2`（`README.md:91` vs `clob_trader.py:12`）；无任何版本 pin。
10. **数据缺失不可复现**：`data/` 全部 gitignored，回测/校准无法从 clone 复跑（`backtest.py:251-252`）。
11. **回测策略 ≠ 生产策略**（HRRR vs ECMWF，`backtest.py:320-324` vs `bot_v3.py:1647-1649`）。
12. **凭证入库**：`config_v2.json` 的 VC key。
13. 单体 2758 行、全局可变状态（`_cal` 等，`bot_v3.py:849`）、forecast_snapshots 每轮 append 无上限（`bot_v3.py:1789`）、forensics 把 `snaps[0]`（发现时快照）当"入场快照"用（`bot_v3.py:1161-1163`）。
14. 集合 σ 逻辑残留不一致：train_v3 ×0.8 vs 线上 REDUCTION=1.0（`train_v3.py:271` vs `bot_v3.py:387`，经 `bot_v3.py:861-868` 仍可能生效）。

## 15. 复用建议

| 类别 | 内容 |
|---|---|
| **推荐直接使用** | **无**。理由：依赖装不上（README 指错包）、数据文件缺失不可复现、σ/桶概率存在已证实的数学错误、回测泄漏。若要用其思路，需先修 MAE→σ、半度连续修正、去掉回测合成价格。 |
| **值得参考的算法/工程** | ① CLOB 安全模式：fail-closed 订单状态机、未知状态不入账、pending/reconciliation/zombie/ghost 四重保护（`bot_v3.py:137-263,2053-2071`）；② `validate_repriced_signal` 下单前重报价复验 EV + 5 股可卖下限（`bot_v3.py:656-692`）；③ `atomic_json_write`（fsync+rename，`bot_v3.py:433-468`）；④ 结算委托 Gamma API 的设计（`bot_v3.py:1433-1448`）；⑤ 分数 Kelly + 组合层闸门的骨架（公式本身正确）。 |
| **可依法复用** | **MIT 许可，允许商用、修改、再分发**，条件是保留 `LICENSE` 的版权声明（`Copyright (c) 2026 alteregoeth-ai`）与许可文本于副本中。无其他义务。 |
| **不应复用** | ① `backtest.py` 整体（泄漏+合成价格）；② `train_v3.compute_sigma` / `run_calibration` 的 MAE→σ；③ `bucket_prob` 有限区间分支（缺半度）；④ `adapt_thresholds`/blacklist/EV-cap/danger-zone 等同样本规则（`bot_v3.py:1229-1296,390-412`）；⑤ 以 paper 账本、回测胜率、"EV>2 曾盈利"类注释作为收益依据；⑥ `config_v2.json`（含泄漏 key）。 |

---

### 附：证据索引（高频引用）

- 概率：`bot_v3.py:564-585`、`backtest.py:79-93`
- EV/Kelly：`bot_v3.py:587-595`、`backtest.py:221,225-228`
- σ 校准：`train_v3.py:120-124,271`、`bot_v3.py:872-901,1873-1875`
- 预报融合：`bot_v3.py:1589-1658`
- 入场决策：`bot_v3.py:1844-2091`
- 结算：`bot_v3.py:1433-1448,2252-2300`
- 回测：`backtest.py:33-48,99-118,137-186,204-241,280-338`
- SDK：`clob_trader.py:12-15` vs `README.md:91`

Sources:
- [PyPI: py-clob-client-v2](https://pypi.org/project/py-clob-client-v2/)
- [PyPI: py-clob-client](https://pypi.org/project/py-clob-client/)
- [Open-Meteo Historical Forecast API docs](https://open-meteo.com/en/docs/historical-forecast-api)

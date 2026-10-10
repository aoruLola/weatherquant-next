# yannbellec/polymarket-weather-bot 审计

> 审计对象：`https://github.com/yannbellec/polymarket-weather-bot`，克隆于 `E:/Project/.wqn-audit/polymarket-weather-bot`（`git clone --depth 1`）。
> 所有结论均基于实际读取的源码；README 声明一律不作为证据。
> 说明：测试套件仅执行了 `pytest --collect-only`（收集到 137 个用例），**未实际运行**（环境权限拒绝执行外部克隆代码），测试质量结论基于静态阅读。

---

## 0. 元信息（commit, 文件数, 语言, 最后提交）

- commit：`1f635a245595788728a95ad71c60b37a8f6b9214`（depth-1 浅克隆，仅见最新提交）
- 最后提交：2026-05-20 18:08:00 -0700，作者 Yann V. Bellec
- 跟踪文件数：174（`git ls-files | wc -l`）
- 语言分布：`.py` 125、`.sh` 13、`.md` 6、`.json` 6、service/timer 等若干；核心为 Python
- 结构：`signals/`（扫描器机器人概率与边缘）、`bots/bot1/`（NOAA GFS/GRIB 主力机器人 + 执行）、`bots/bot1_realtime/`、`bots/bot2_realtime/`（METAR 实时狙击）、`execution/`、`core/`、`notebooks/`（回测）、`tests/`（唯一测试文件）
- 依赖：`requirements.txt` 全量 pin（`py_clob_client==0.34.6`、`httpx==0.28.1`、`pytest==9.0.2`；`web3>=7.0.0` 无上限）

## 1. 许可证

- **仓库中不存在 LICENSE 文件**：`git ls-files | grep -i licen` 无结果；根目录 `ls` 无任何 LICENSE/COPYING/NOTICE。
- README 声称 MIT：`README.md:15`（badge 链接指向不存在的 `LICENSE` 文件，死链）、`README.md:302-304`（"## 📜 License / [MIT](LICENSE) © yannbellec"）。
- 无 `pyproject.toml` / `setup.py` 等包元数据可提供替代的许可证声明。
- **判定**：许可证文本缺失。README 中一行 "MIT" 属于**模糊声明**——无版权许可正文（无 permission grant 条款、无 disclaimer），严格按默认版权处理 = **未授予复用许可**；即便按最宽松解释视其为 MIT 要约，MIT 是 OSI 批准许可证、允许商业与派生使用，义务仅为保留版权声明——但正式复用前应向作者索取 LICENSE 文件或书面确认。**结论：当前状态不建议在未获授权前复用其代码。**

## 2. 天气模型

代码里实际存在 **三条不同的天气数据管线**：

1. **Scanner 管线（`main.py` 驱动）**：
   - 多源共识：Open-Meteo forecast API、NWS API（`signals/sources/noaa.py:69-92`，注意这是 **NWS 城市网格点预报**，坐标硬编码如 `noaa.py:20-33` 的 `new york: (40.7128,-74.0060)` 即曼哈顿下城，而非机场）、WeatherAPI、Tomorrow.io、OWM —— `signals/sources/aggregator.py:142-186`。
   - 可选模型路由 `FORECAST_MODEL_ROUTING=1`：HK→HKO、美国≤2天→`gfs_hrrr`、≥3天→`ecmwf_ifs04`（`core/forecast_model_router.py:104-130`）。全部是**确定性单值预报**（Open-Meteo `ecmwf_ifs04` 是 IFS 确定性 0.25° 版本，不是集合）。
   - **README 宣称的 "ECMWF IFS 51-member ensemble via Copernicus CDS" 不在交易路径中**：`rg cdsapi -g '*.py'` 只命中 `notebooks/fetch_era5.py`（ERA5 再分析，非预报集合）。`cdsapi==0.7.7` 在 requirements 里但交易链路从未调用。
2. **Bot1 管线（主力）**：从 NOMADS/S3 下载原始 GFS GRIB，`wgrib2` 在**站点经纬度**取值（`bots/bot1/noaa_nomads_live/run_nomads_live_test.py:1234-1235, 1278-1279`；站点库 `data/master_stations.sqlite3`，`market_manager.py:149-150, 340-385`）。确定性 GFS 为主；**GEFS 31 成员集合是可选项**（`run_nomads_live_test.py:1175-1188`，`BOT1_USE_GEFS_ENSEMBLE` 默认关闭；开启时 `BOT1_GEFS_BLEND_ALPHA` 默认 1.0 = 纯集合经验频率）。有效时间对齐到合同本地日已实现（`bots/bot1/noaa_nomads_live/gfs_horizons.py:40-86`，gate 在 `run_nomads_live_test.py:1242-1248`）。
3. **实时管线**：NOAA AWC METAR 直接读 resolving 站点（`bots/bot1_realtime/metar_live/realtime_metar_registry.py`、`produce_rt_signals.py`）。

**是否按 Polymarket 变量定义解析（站点日高温、整度取整）**：
- 取整：`core/contract_parser.py:146-150` `_ORACLE_ROUNDING["temp_max"]=0`（整数），与 Polymarket "whole degrees" 规则一致（`contract_parser.py:14` 注释；`temp/resolution_rules_dump.txt` 中真实规则确认 "measures temperatures to whole degrees"）。
- 站点：**不一致**。真实规则显示 NYC 市场在 **KLGA（Wunderground）** 解析（`temp/llm_diag_dump.txt`："LaGuardia Airport Station"、URL `/new-york-city/KLGA`），而 `core/contract_parser.py:31-32` 硬编码 `"new york" → ("KNYC","noaa_ghcnd",0.95)`；`data/active_markets.json` 样例又把 NYC 映射到 `KJFK`。解析器只从 rules URL 提取**来源域名**（`contract_parser.py:229-253`），**不从 URL 提取站点代码**；站点来自问题文本的城市名查表（`_detect_station`, `contract_parser.py:177-180`）。
- 年份硬编码 bug：`probability_engine.py:125,177` 把任意日期 `strptime(..., 2026)`——2026 年以外的市场日期解析全部错误。
- 实时 bot 的"日界"防错是认真做的：`core/rt_day_guard.py:1-45`（按合同地点 IANA 时区判断 premature/past），且 `MODEL_SCIENCE.md:26-37` 明确记录了 GFS 窗口与合同日错配的风险与 gate 方案（已落地于 `run_nomads_live_test.py:1242-1248`）。

## 3. 概率分布构建（含独立推导）

### 3.1 Scanner 路径（`signals/probability_engine.py`）

- 固定 σ 表（**硬编码、无任何实证依据**）：`TEMP_SIGMA_BY_DAYS = {0:1.0, 1:1.5, 2:2.0, 3:2.5, 4:3.0, 5:3.5}`（°C，`probability_engine.py:45`），再按置信度放大 `σ = base × (1 + (1−conf)×0.4)`（`probability_engine.py:79-83`）。
- CDF：`_normal_cdf`（`probability_engine.py:59-62`）：`z=(x−μ)/(σ√2); Φ=0.5(1+erf(z))` —— **独立推导：正确**，即标准正态 CDF。
- 分位桶（exact）：`_prob_exact`（`probability_engine.py:65-71`）`P = Φ(target+tol)−Φ(target−tol)`，`tolerance=0.5`。
- **独立复核发现的错误**：问题解析时 °F→°C（`probability_engine.py:107-109`），但容差仍以 **±0.5 °C** 使用。对 "35°F" 市场，正确桶宽应为 ±0.5°F = ±0.2778°C；代码用了 ±0.5°C，**窗口宽 1.8 倍**。数值复算（σ=2°C、μ=阈值）：代码 P = 2Φ(0.25)−1 = **0.1974**；正确 P = 2Φ(0.1389)−1 = **0.1105**，**高估 79%**。对 °C 市场（Wunderground 整 °C 规则）±0.5°C 恰好正确；对 °F 市场系统性高估 exact 概率。气候层同样 `climate_prob_exact(tol=0.5)`（`climate_history.py:273-283`）存在相同问题。
- 层融合：`P = 0.55·P_forecast + 0.30·P_climate + 0.15·P_trend`（`probability_engine.py:48-50, 327-329`）——**固定线性意见池，权重无任何校准依据**（注释自证 "signal le plus fort à court terme"）。趋势层把 7 日线性回归斜率乘 0.3 平移均值（`probability_engine.py:229-250`），`days_ahead=1` 硬编码（line 241），与目标日期无关。
- `p_final` 最终被 `model_registry.predict` 覆盖（`probability_engine.py:370-374`）；默认 `StatisticalModel` 只是用相同权重重算一遍（`model_interface.py:110-135`），等于冗余；若配置 `MODEL_PATH`，则换成 sklearn/ONNX 模型——训练示例**无时间切分**（`model_interface.py:157-165` 文档代码）。
- 降水：`sigma_mm = 2.0 + min(days,5)×0.8` 再乘置信度因子（`probability_engine.py:446-447`），同样硬编码。
- 可选的 "skill 校准" 钩子存在但默认关闭且键不匹配：`BOT1_USE_BIAS_STD_FOR_SIGMA`（`run_nomads_live_test.py:103-108` → `model_calibrator.get_sigma_hint_for_key`，`model_calibrator.py:158-186`，按 `bias_std` 截断到 [0.5, 8.0]）；`MODEL_SCIENCE.md:10-12` 自认 "bot1 管线不按站点自动填充这些条目"。
- **未发现** logistic smoothing / isotonic / Platt 校准；**未发现**按预报时效的经验技能衰减曲线（bot1 的线性乘子除外，见下）。

### 3.2 Bot1 路径（`run_nomads_live_test.py`）

- `calculate_probability(delta, σ) = Φ(delta/σ)`（`run_nomads_live_test.py:83-93`），公式本身正确。
- σ 基础值：环境变量默认 **1.5**（°F/合同单位；`run_nomads_live_test.py:100-112`，`MODEL_SCIENCE.md:7` 直言 "σ = 1,5 … même formule qu'avant"——**历史遗留硬编码，无推导**）。
- 时效放大：`mult = 1 + 0.5·(fh−24)/24`（fh>24，`gfs_horizons.py:106-125`）→ f120 ≈ ×3、f384 ≈ ×8.5。线性启发式，非技能拟合。
- **算子符号 bug（独立推导）**：`delta = val_converted − target.threshold`（`run_nomads_live_test.py:1335`），`pwin = Φ(delta/σ)`（line 1340），**全程无按 operator 翻转符号**（全文件 operator 仅出现在 474/898/923/1344 行）。设预报误差 e = forecast − true ~ N(0,σ)：
  - `gte/gt`（高于阈值赢）：P(true≥thr) = Φ((forecast−thr)/σ) = Φ(+delta/σ) ✓ 正确；
  - `lte/lt`（低于阈值赢）：正确应为 Φ(−delta/σ)，代码给 Φ(+delta/σ) —— **概率完全颠倒**；
  - `eq`：正确应为桶概率 Φ((thr+0.5−f)/σ)−Φ((thr−0.5−f)/σ)，代码给 Φ(delta/σ) = P(X≤thr) —— **语义错误**。
  - GEFS 路径反而正确处理算子（`core/ensemble_prob.py:29-45`）。当前 `data/active_markets.json` 仅含 `gte` 样例（掩盖了该 bug），但市场宇宙存在 "or below" 合同（`probability_engine.py:112-117` 就是为此写的）。
- 集合概率：`pwin = α·p_gefs + (1−α)·p_sigma`（`run_nomads_live_test.py:1343-1347`，`blend_prob` 定义 `ensemble_prob.py:63-66`）——α 默认 1.0 即纯成员经验频率，方法本身统计上正当，但**默认关闭**。
- RT 确定性三态：`p_win ∈ {1.0, 0.5, 0.0}`（`produce_rt_signals.py:78-112`）。**错误**：`above=False`（日高温 ≤ 阈值）时，只要当前 METAR ≤ 阈值就返回 `1.0/attack`（line 100-101）——日高温尚未定型，午后仍可升破阈值；单调性只对 "above" 成立。这是过度自信错误（非泄漏）。

### 3.3 总体判断

概率构建 = **硬编码 σ 的高斯 CDF + 固定权重层平均**（scanner）/ **硬编码 σ=1.5 的高斯 CDF + 时效线性放大 + 可选集合**（bot1）。σ、权重、2.5% spread、0.90/0.95 alignment 均无历史预报技巧（skill）或可靠性（reliability）数据支撑。未做任何概率校准（reliability diagram / Brier 分解 / Platt）。

## 4. 机场观测偏差修正

- **Scanner**：`bias_corr = get_bias_correction(city) or 0.0`（`probability_engine.py:310-311`）。偏差定义 = 预报 − **Open-Meteo Archive 城市网格实况**（`model_calibrator.py:103-116`），按城市名键控（line 195-203），`MIN_OBSERVATIONS=5`、窗口 50 条（line 28-30）。**这是"模式 vs 再分析网格"的偏差，不是"网格 vs 解析站点"的偏差**；且参考系（Open-Meteo 2m 网格）本身就不是 Polymarket 的解析站点（Wunderground 机场站）。数据不足时修正 = 0.0。
- **Bot1**：GFS 在**站点坐标**取值（见 §2），部分缓解网格-站点距离偏差（GFS ~25 km 网格），但**没有模型-站点气候偏差订正**——`MODEL_SCIENCE.md:12` 明确承认按站点的偏差校准是"后续工作"。可选的 `bias_std→σ` 也只是改 σ，不改均值。
- **实时管线**：直接读 resolving 站点的 METAR（`realtime_metar_registry.py`），站点对齐本身即修正——但前提是 `icao_mapping.json` 里的站点正确（样例里 NYC 用 KJFK，与真实解析站 KLGA 不符，`data/icao_mapping.json`）。
- **量化**：默认路径下修正量恒为 0（`or 0.0`）；启用后为城市级 mean-bias（典型亚摄氏度），**与机场站无关**。**未发现**任何针对 KLGA/KNYC 等具体站的 grid-vs-station 订正。

## 5. 多模型融合

- Scanner 默认：**固定权重加权平均** `SOURCE_WEIGHTS = {tomorrow_io:0.35, open_meteo:0.30, noaa:0.20, weatherapi:0.10, owm:0.05}`（`aggregator.py:18-24`），按可用性归一化（line 102-139）。注释声称 "Meilleure précision technique" 等理由——**无历史技巧加权、无数据支撑**。置信度 = `1 − std/3` + 源数量 bonus（line 123-137），启发式。
- Scanner 路由模式：**单模型选择**（按地区/时效选 HKO / gfs_hrrr / ecmwf_ifs04，`forecast_model_router.py:104-130`），不是融合；合同对齐模式 `get_max_temp_contract_aligned`（`aggregator.py:189-208`）单一 connector，且 `noaa_ghcnd` connector 实际是 **NWS 城市网格点预报**（`aggregator.py:39` → `noaa.py:69-92`）而非 GHCND 站点观测，`base_confidence=0.90` 亦为硬编码。
- Bot1：**单源确定性 GFS** + 可选 GEFS 混合（见 §3.2）。
- **结论**：没有按技能加权的多模型融合；README 的 "ensemble" 卖点在默认配置下不成立。

## 6. Edge 计算（独立推导 + 正误判定）

### Scanner（`signals/edge_detector.py`）

```
edge_yes = our_prob − market_yes_prob          # edge_detector.py:128
net_edge = (raw_edge × alignment) − TOTAL_COST  # edge_detector.py:133
TOTAL_COST = FEE_BPS/1e4 + 0.025               # edge_detector.py:31-35
```
- 判定：**结构正确**（概率差 − 成本 − 置信折减），但各分量质量差：
  - `FEE_BPS=0`（`config/settings.py:47-49`）——符合 Polymarket 现行 0 taker fee ✓；
  - `SPREAD_COST=0.025` **拍脑袋常数**（`edge_detector.py:32-34` 注释自证 "estimé à 2.5%"），不看实际盘口；
  - `alignment` 来自 `CITY_STATION` 硬编码表 0.80–0.95（`contract_parser.py:31-118`）或默认 1.0（`edge_detector.py:71-81`）——**非实证**；
  - **不包含**：盘口深度（只用一个 `market_yes_prob`——WS `price_change` BUY 侧缓存 `scanners/ws_price_feed.py:55-67` 或 Gamma `outcomePrices` 快照 `market_scanner.py:161-168`）、gas/relayer（Polymarket 免 gas，缺省可接受）、模型误差 haircut（仅有 alignment 线性折减）、资金占用成本。
- Kelly（`edge_detector.py:40-68`）：`k = max(0, (p·odds − q)/odds)`，`odds = 1/price − 1`（line 154）。**独立推导**：二元合约 b=(1−c)/c，f* = (pb−q)/b = p − (1−p)c/(1−c) —— 代码与标准分数 Kelly **一致 ✓**。随后 `×KELLY_FRACTION(0.25)`、alignment<0.85 再乘 alignment（line 58-59）、UMA 再乘 (1−0.15)（line 62-63）、cap `MAX_BET_USD=20`（line 68，`settings.py:42,104`）。**缺陷**：Kelly 输入的 p 未先扣除 2.5% 成本（门槛扣了、下注量没扣）。

### Bot1（`finance_engine.py`）

```
ev = effective_p_win − best_ask                 # finance_engine.py:898
gate: ev > FINANCE_NOAA_MIN_EV，默认 0.0        # finance_engine.py:902-905
net_edge (barbell 用) = raw_edge − (0.025 + fee) # finance_engine.py:590-600
```
- **判定：默认交易门槛 `ev > 0`，即按盘口 ask 计的裸 EV 为正就下单，没有任何成本/模型误差 haircut**（2.5% 只在 barbell 分腿里用，line 918）。相对 ask 的 EV 在 p 校准正确时已隐含付了价差，但同时完全不防范模型误差——对一个 σ 硬编码、站点错配的模型而言这是危险的默认值。
- 尺寸：固定 `ORDER_SIZE_USDC=2.5`（cap `MAX_BET_USD`，`finance_engine.py:834-841`），**bot1 无 Kelly**。

## 7. 手续费与滑点

- 手续费：`FEE_BPS=0`（`settings.py:47-49`，注释 "Polymarket ne prélève pas de taker fee"）——事实正确；下单带 `fee_rate_bps: FEE_BPS`（`finance_engine.py:646`）。
- 滑点/成交建模：
  - Bot1：LIMIT BUY 挂 best ask，数量 `min(budget/ask, best_ask_size)`（`finance_engine.py:963-965`）——**只触达盘口第一档**，不 walk the book；承认 partial/pending fill（line 1019-1036），成交价即请求价。
  - Scanner：`execution/order_manager.py:111` 同样 `fee_rate_bps=FEE_BPS`，entry 价 = `token.price`（Gamma/WS 快照），**无盘口遍历**。
  - 回测：`no_p = round(1 − yes_p, 4)`（`backtest.py:143`）——**用互补价冒充 NO 买价，完全忽略 NO 侧价差**；成本为魔数 `−0.045`（`backtest.py:158,277`）和**只在赢时**扣 `stake×0.02`（`backtest.py:171`）——不对称、无依据。
  - gas/relayer：全库未建模（Polymarket 免 gas，可接受；`rg gas` 无交易成本语义命中）。
- 结论：**没有真实成交/盘口模拟**；live 路径至少用了真实 ask 并限深，回测最弱（互补价 + 魔数）。

## 8. 回测机制

唯一回测：`notebooks/backtest.py`（301 行）+ 数据管道 `history_scraper.py` / `build_master.py`。

- **非时序**：`for i, market in enumerate(resolved)`（`backtest.py:252`）按 master 文件顺序交易，bankroll 在该顺序上演化（line 285）；仅展示时按日期排序（line 202）。
- **无实际盘口快照**：价格 = CLOB `prices-history` 在 `[end−48h, end−24h]`（fallback 72/24、96/48）的**小时均值**（`history_scraper.py:177-203`），或 Gamma `outcomePrices` 抓取时刻值（`build_master.py:249-254`），或 HF 数据集 VWAP（`build_master.py:273-291`，as-of 时间未标注）。无 bid/ask、无深度、无成交量约束。
- **决策时点预报**：默认用"D−3..D−1 实况均值"当预报代理（`backtest.py:53-66`），不是决策时刻真实存在的预报；`notebooks/fetch_forecast_at_trade.py` 存在（标题 "Récupère les prévisions … 24-48h AVANT"）但**回测未使用它**。
- 统计：**无置信区间、无显著性检验**；`backtest.py:208-213` 仅按 `pnl>0` 打印 "PROFITABLE"，且 n≥10 即下结论。
- 明确的 lookahead 见 §9。
- **判定：结果可完全由泄漏 + 忽略成本产生**（见 §9 尾部评估）。

## 9. 时间信息泄漏（具体 file:line 证据）

1. **【致命】用结算实况当预报**：`notebooks/backtest.py:144-149` —— `simulate_trade` 中只要 `market["actual_temp_max"] is not None` 就以**实际最高温**作为 forecast 计算 `_compute_prob_with_forecast(...)`；`backtest.py:258` 的 fallback 链 `forecast_tmax_j2 or tmax_j1 or actual_temp_max` 同样兜底到实况。而入选条件要求 `resolved=True` 且 `yes_won` 已知（`backtest.py:230-239`），`actual_temp_max` 由 `build_master.py:306-307` 合并。**每一笔回测交易的 our_prob/edge/stake 都吃到了结果变量**。
2. **【严重】气候统计"全历史算完再切片"**：`backtest.py:76-78` —— `end_yr = datetime.now().year − 1`，即回测**运行时刻**取到去年为止的 15 年整月池。2026 年运行回测会把 2025 年（含目标市场自身月份、甚至目标日之后的天）计入气候层（权重 0.35，`backtest.py:110,134`）。对更早的市场，池包含交易日**之后**的年份——纯 lookahead。
3. **【严重】价格快照与预报快照时点不匹配**：价格 = [D−48h, D−24h] 均值（`history_scraper.py:177-181`），预报代理含到 D−1（`backtest.py:59-60`，即 −24h 的信息）——价格平均窗更早，预报更新；若 `yes_price_clob` 来自 Gamma 实时抓取（`build_master.py:249-254`），其 as-of 时刻与预报构造时刻完全无关联约束。
4. **【中】live 路径的"实况回退"**：`signals/sources/open_meteo.py:121-126` —— forecast API 无数据时**无条件回退 archive 实况**。当目标日在当地时间已"过去"而市场仍在交易（Polymarket 天气市场常次日才终局），模型直接拿到已实现温度 → 概率塌缩到 0/1 而市场尚未闭合。
5. **【中】非时序切分**：`backtest.py:252`（见 §8）；ML 训练示例无时间切分（`model_interface.py:157-165`）。
6. **【低-中】RT 过度自信**：`produce_rt_signals.py:100-101`（见 §3.2，把未定型的日极值当已定）。
7. **多个预报 run 当独立样本算 CI**：全库 `rg`（bootstrap/stderr/sem/confidence interval）**未发现任何置信区间计算**——即该类泄漏不存在，但也意味着**完全没有不确定性量化**。
8. **正面证据**（作者意识到了部分风险）：`MODEL_SCIENCE.md:26-69`（curation 风险与 gate 计划）、`run_nomads_live_test.py:1242-1248`（valid-time gate 已实现）、`core/rt_day_guard.py`（本地日界防错）、`model_calibrator.py:97-99`（actual 只在日期过后回填，时序正确）。

**能否仅凭泄漏/忽略成本解释报告结果？——回测：能。** 第 1 条让模型"看见"结果后给出接近 0/1 的概率（σ=2~3.5°C 下，实况越过阈值 5°C → p≈0.99），对仍按 D−48h..D−24h 均值定价的市场产生巨额"edge"，扣除 0.045 魔数后仍轻松通过 `min_edge=0.05`（`backtest.py:158-159,277-281`）；再叠加互补价买 NO（`backtest.py:143`）与只赢扣费（line 171），**"PROFITABLE" 打印结果（line 209）不可信**。Live 路径无此泄漏（预报为当时 API 实时值），但其盈亏主要取决于 §3-§6 的校准缺陷，未经本审计验证（未执行、无实盘数据）。

## 10. 资金与仓位管理

- Scanner：
  - 分数 Kelly 0.25 + 硬顶 $20（`settings.py:42`；`edge_detector.py:66-68`）；
  - `MAX_OPEN_POSITIONS=6`、`MAX_EXPOSURE_PCT=0.30`（新增敞口 ≤ bankroll×30%，`edge_detector.py:176-177,261`）、`MAX_PER_CITY=1`、`MAX_PER_EXPIRY=3`（line 178-179）；
  - 总止损：相对 `INITIAL_BANKROLL=150` 回撤 >20% 停机（`edge_detector.py:180-195`）；
  - barbell 桶预算 85/15（`core/barbell_strategy.py:88-104`）。
- Bot1：**固定注额** `ORDER_SIZE_USDC=2.5`（`finance_engine.py:834-841`），barbell 开启时按桶限额（line 917-944）；`_already_traded_for_cycle` 防同 cycle 重复（line 861）。
- 抵押/锁定：`get_current_bankroll = initial + 已实现 PnL`（`pnl_tracker.py:210-219`）——**未扣除在场持仓成本**（在场资金仍计入可用 bankroll），靠 30% 敞口上限间接约束；UMA 争议冻结单列 `get_dvm_blocked_capital`（`pnl_tracker.py:135-154`），`get_bankroll_detail` 区分 disponible/bloquee/bond（line 224-256）。`compute_yes_prob` 的 `kelly_stake_usd` 入参表明记录闭环存在（`probability_engine.py:261`）。
- **未发现**：对资金赎回前锁定周期（UMA 2h liveness + 7-14 天争议）的机会成本建模——仅以 `UMA_DISPUTE_PENALTY=0.15` 一刀切折减 Kelly（`edge_detector.py:62-63`，`settings.py:104`）。

## 11. 结算方式

- **镜像真实解析**：`settlement_watcher.py` + `exit_engine.py` 轮询 Gamma `outcomePrices`，`≥0.99 → RESOLVED_YES`、`≤0.01 → RESOLVED_NO`、`closed 且非0/1 → CANCELLED`（`exit_engine.py:526-557`）——以 Polymarket/UMA 的最终结果为准，而非自己重算天气 ✓。结算链 `run_settlement_chain.sh:17-18`（watcher → exit_engine）+ redeem 流程（`exit_engine.py:276,366` 的 `_mark_settled_winner/loser`）。
- UMA 监控：`execution/uma_oracle.py:38-41`（OO 地址 `0xee3Afe...87c24`，liveness 7200s）、阶段状态机（line 116-165）；disputer 功能存在（`uma_disputer.py:359,543`）。
- **争议评估的数据源错误**：`uma_disputer.py:306-317` 用 **Open-Meteo 城市网格**温度代入 `spec.evaluate` 判断是否与 UMA 提案分歧，`sigma=1.5` 硬编码计算置信度（line 331-338）——**不是合同指定的解析源/站点**（如 Wunderground KLGA）。以 $500 bond（`settings.py:144`）对赌城市网格值，存在错误争议风险。
- **对照官方站点观测二次验证**：**未发现**（无 Wunderground/NWS CLImate 的独立复核）。

## 12. 测试质量

- 规模：**137 个测试**（`pytest --collect-only` 实测）、23 个 `Test*` 类、275 行 `assert`，全部在 `tests/test_all.py`（1763 行；文件头 "Ne pas créer d'autres scripts de test"）。**未实际运行**（权限拒绝执行外部代码）。
- 断言什么：
  - 好的独立期望值：`TestEnsembleProb` —— `empirical_prob_from_operator` 3/5、2/5；`quantile([1..4],0.5)=2.5`；`blend_prob(0.8,0.2,α=0.25)=0.35`（`tests/test_all.py:536-554`）；`sigma_horizon_multiplier(120)=3.0`（line 1402，与公式 1+0.5×(120−24)/24 复算一致 ✓）；`ContractSpec.evaluate` 各算子与 rounding（line 137-157）；PnL 情形（line 433-449）。
  - **Kelly 测的是本地复制品**：`TestEdgeDetector` 自带 `_kelly` 静态方法（`tests/test_all.py:457-470`，docstring "Réplique kelly_stake localement"），**不 import `signals.edge_detector.kelly_stake`**——被测代码改坏也不会红；断言多为不等式（>0、≤MAX），无标准情形的精确数值。
  - `test_total_cost_formula`（line 494-500）自证恒等式（`0.025 + taker_fee == 0.025 + taker_fee`），无信息量。
  - **缺失**：`calculate_probability` / `_normal_cdf` 对已知 erf 值的数值测试（rg 无命中）；`probability_engine` exact 桶宽、°F 换算的回归测试；bot1 operator 符号（`lte` 颠倒）测试——**所以 §3.2 的符号 bug 测不出来**；回测 PnL/edge 公式无独立期望值对照；`edge_detector` 实函数无单测。
  - 另有 live 测试（`--live`，网络 + DRY_RUN），**作为脚本直接运行时默认也跑 live**（`tests/test_all.py:1745` `run_live = not args.live`）。

## 13. SDK 与版本兼容性

- 客户端：`py_clob_client==0.34.6`（`requirements.txt:68`）。经 PyPI 实查 `pypi.org/pypi/py-clob-client/json`：**latest = 0.34.6，pin 即最新版** ✓。未用 py-slumber；大部分行情/Gamma 用裸 `urllib`/`httpx`；链上用 `web3>=7.0.0`（`requirements.txt:105`，**无上限 pin**，风险点）。
- 兼容性处理：`finance_engine.py:649-731` 对 `create_order(dict)` / `OrderArgs` / `SimpleNamespace` 三代签名做回退，`post_order` vs `create_and_post_order` 分支（line 698-727）；`order_manager.py:25-43` 类似。说明作者跟进过 SDK 变更，但多级 fallback 也意味着**没有单一可信的下单成功语义**（用 `_clob_order_accepted` 事后判断，`finance_engine.py:604-627`）。
- 与现行 CLOB API：GTC 限价单、`fee_rate_bps=0`、`prices-history`、Gamma `outcomePrices` 端点均为现行接口；**未发现**已废弃端点（静态判断）。

## 14. 设计缺陷清单

1. 无 LICENSE 文件（README MIT 为死链）——法律层面不可复用。
2. 回测三重泄漏：结算实况当预报（`backtest.py:144-149,258`）、气候池含未来（line 76-78）、价格/预报时点错配（line 53-66 vs `history_scraper.py:177-181`）。
3. 回测非时序、魔数成本（−0.045）、互补价买 NO（`backtest.py:143,158,171,252`）。
4. bot1 `lte/lt/eq` 概率符号/语义错误（`run_nomads_live_test.py:1335-1340` 无算子分支）。
5. exact 桶宽 °F/°C 混用，°F 市场 P(exact) 高估 ~80%（`probability_engine.py:65-71,107-109`）。
6. σ 全线硬编码（1.5°F / 表格 °C / σ_mm），无 skill 依据（`probability_engine.py:45`、`run_nomads_live_test.py:111-112`、`MODEL_SCIENCE.md:7`）。
7. 融合权重、source weights、alignment 分数全部拍脑袋（`probability_engine.py:48-50`、`aggregator.py:18-24`、`contract_parser.py:31-118`）。
8. 解析站点错配：NYC→KNYC 而真实为 KLGA（`contract_parser.py:31` vs `temp/llm_diag_dump.txt`）；不从 resolution URL 提取站点。
9. README 宣称 ECMWF 51 成员集合，交易路径实际没有（`cdsapi` 仅 `notebooks/fetch_era5.py`）。
10. bot1 默认门槛 `ev>0` 无成本/误差 haircut（`finance_engine.py:902`）；2.5% spread 为常数且只用于 barbell。
11. `probability_engine.py:125,177` 年份硬编码 2026。
12. RT sniper `below` 侧过早宣告 `p_win=1.0`（`produce_rt_signals.py:100-101`）。
13. UMA 争议评估用城市网格而非合同解析源（`uma_disputer.py:306-317`）。
14. Kelly 测试测本地复制品；核心概率公式无数值测试（`tests/test_all.py:457-470`）。
15. bankroll 未扣在场持仓成本（`pnl_tracker.py:210-219`）；`web3` 无版本上限。

## 15. 复用建议：推荐直接使用 / 值得参考的算法 / 可依法复用 / 不应复用

- **推荐直接使用**：**无**。许可证缺失 + 回测泄漏 + bot1 算子概率 bug + 站点错配，任何一条都足以否决"直接拿来跑"。
- **值得参考的算法/工程**（阅读学习，见 §15 版权注记）：
  - `bots/bot1/noaa_nomads_live/gfs_horizons.py` —— `calculate_gfs_horizons` 把 GFS valid time 换算到合同本地日再选 fh，是防"curation 错日"的正确做法；
  - `core/rt_day_guard.py` —— 按合同地点时区判断 premature/past_target 的日界防错；
  - `bots/bot1/noaa_nomads_live/run_nomads_live_test.py:1242-1248` —— 有效时间 gate；
  - `core/ensemble_prob.py` —— 经验集合概率/quantile/blend（含正确算子处理）；
  - `bots/bot1/Long Term/noaa_engine.py` 的 GEFS 分批成员管道（`iter_gefs_member_batches` / `run_gefs_members_batched`, line 1624-1684）与 NOMADS/S3 idx+range+wgrib2 提取；
  - 结算链（Gamma 解析轮询 → exit/redeem）与 `execution/uma_oracle.py` 的 OO 阶段状态机；
  - `signals/edge_detector.py:40-68` 的 Kelly 公式（本身正确，注意先扣成本）。
- **可依法复用**：**无**。仓库无 LICENSE 文件 = 默认版权，未经作者明确许可不得复制代码；README 单行 "MIT" 不足以作为安全的授权依据（需作者补发 LICENSE 或书面确认；若确认为 MIT，则需保留版权声明，允许商业/派生）。
- **不应复用**（明确有害或错误）：
  - `notebooks/backtest.py` 全部（泄漏 + 非时序 + 魔数成本）；
  - `signals/probability_engine.py` 的 exact 容差与 2026 硬编码年份；
  - `run_nomads_live_test.py:1335-1340` 的无算子 p_win；
  - `core/contract_parser.py` 的 `CITY_STATION`/alignment 表（站点错配、分数无据）；
  - `aggregator.py` 的固定 `SOURCE_WEIGHTS`；
  - `produce_rt_signals.py:100-101` 的 below 侧 attack 逻辑；
  - `execution/uma_disputer.py` 的城市网格争议判据；
  - `SPREAD_COST=0.025` / `FINANCE_NOAA_MIN_EV=0` 这类未经校准的成本默认值。

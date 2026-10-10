# 旧版 WeatherQuant 量化核心审计

> 审计时间：2026-10-10。范围：`E:/Project/WeatherQuant/` 内 `backend/quant/` 代码与 `backend/data/` 数据，**只读**。
> 数据库检查方式：原库 `backend/data/weatherquant.duckdb` 被一进程占用期间未做任何读写；待其退出（干净 checkpoint，WAL 已合并）后，按协调方指示复制快照至 `.audit-out/legacy-snapshot.duckdb`，**全部** DuckDB 查询均以 `duckdb.connect(snapshot, read_only=True)` 对快照执行。未以写模式运行过任何 legacy CLI。
> 所有时间戳换算为 UTC 标注（库内为 `TIMESTAMPTZ` 绝对时刻；会话显示为 +08:00 本地时区）。
> pytest 实测：`PYTHONPATH=backend python -m pytest backend/tests -q` → **144 passed in 189.51s**。

---

## 1. 数据清单与时间覆盖（每表：列/行数/时间范围/样例/来源列）

共 22 张表（`main` schema）。以下每表给出行数、全部时间列 MIN/MAX（UTC）、2 行样例（长 JSON 截断）、来源（provenance）列与关键数值列空值率。

### 1.1 account（1 行）
- 列：`account_id VARCHAR, initial_capital DOUBLE, cash DOUBLE, realized_pnl DOUBLE, fees_paid DOUBLE, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ`
- 时间：`created_at = updated_at = 2026-10-09T19:43:36Z`（单行，创建后仅一次成交即冻结）
- 样例：`paper-klga | 1000.0 | cash=970.0982449999999 | realized_pnl=0.0 | fees_paid=1.32355636`
- 来源列：无 `source`；仅时间戳。空值：无。

### 1.2 audit_log（761 行）
- 列：`id INTEGER PK, ts TIMESTAMPTZ, kind VARCHAR, message VARCHAR, details_json VARCHAR`
- 时间：`ts` 2026-10-09T17:18:29Z → 2026-10-10T02:50:37Z（**单日运维日志**）
- kind 分布：`collect 724, predict 9, integrity 7, settle 5, book 4, paper_order 3, calibrate 2, benchmark 2, signals 2, paper_run 2, account 1`
- 样例：`1 | 2026-10-09T17:18:29Z | collect | "event 1147154 stored" | {"slug": "highest-temperature-in-nyc-on-october-9-2026", ...}`
- 来源：自身即审计流；无外部来源列。空值：`details_json` 部分为 NULL。

### 1.3 backtest_runs（10 行）
- 列：`backtest_id VARCHAR PK, kind VARCHAR, created_at TIMESTAMPTZ, params_json, status, metrics_json, reason`
- 时间：`created_at` 2026-10-09T18:49:17Z → 2026-10-10T02:34:33Z
- kind/status：`all/OK×4, weather/OK×3, trade/OK×1, benchmark/OK×2`（全部成功）
- 样例：`bt-1791571757 | weather | params={"start":null,"end":null} | status=OK | metrics_json={"weather":{"method":"walk_forward_refit_per_target_date",...}}`
- 来源：无 `source` 列；可由 `kind+params_json+created_at` 复现重跑。空值：`reason=NULL`（全部）。

### 1.4 books（660 行 = 33 market × 20 快照）
- 列：`book_id PK, market_id, token_id, outcome, best_bid, best_ask, spread, bid_depth_usd, ask_depth_usd, top_ask_size, venue_ts TIMESTAMPTZ, fetched_at TIMESTAMPTZ, bids_json, asks_json, status, reason`
- 时间：`fetched_at` **2026-10-09T17:18:33Z → 2026-10-10T02:42:20Z（全部集中在同一天内）**；`venue_ts` **MIN=MAX=NULL（660/660 全空）**
- status/reason：`ok 656`（其中 **247 行 best_bid=NULL，单边盘口**）、`no_asks 4`（"order book has no sell side"）；无 `crossed`/`empty`
- 样例：`0eeb3aec… | 5400221 | YES | best_bid=None, best_ask=0.001, ask_depth_usd=310.36 | fetched_at=2026-10-09T17:18:33Z | asks_json=[{"price":0.001,"size":13591.3},…] | status=ok`
- 来源：无 `source` 列（隐含 Polymarket CLOB）；`fetched_at` = 抓取时刻 ✓；`book_id=sha1(token|fetched_at)` 每次抓取新行（历史保留）。空值率：`best_bid` 37.4%、`best_ask` 0.6%、`venue_ts` **100%**、asks 空 4 行。venue_ts 全空的原因：`parse_book` 读 `raw.get("timestamp")`（`backend/quant/polymarket/client.py:346-349`），而 CLOB book 载荷中该键不存在/非数值。

### 1.5 daily_observations（2200 行 = 1100 天 × 2 规则）
- 列：`station, target_date DATE, source, max_temp_f, max_temp_c, n_readings, rule, computed_at TIMESTAMPTZ, is_official BOOLEAN`
- 时间：`target_date` **2023-10-06 → 2026-10-09（约 3 年）**；`computed_at` 2026-10-10T02:48:51Z → 02:50:37Z（最终一次重算批量写入）
- 分布：`hourly_metar/iem_asos/False ×1100`、`all_readings/iem_asos/False ×1100` → **`is_official=TRUE 行数 = 0**
- 样例：`KLGA | 2026-10-01 | iem_asos | max_temp_f=78.0 | n_readings=24 | rule=hourly_metar | is_official=False`
- 来源列：`source` ✓、`rule` ✓、`computed_at` ✓、`is_official` ✓（但语义失效，见 §5）。`max_temp_f` 空值 0。`hourly_metar` 中 `n_readings<24` 共 **37 天**（20~23 个读数，日覆盖不完整）。

### 1.6 equity_snapshots（8 行）
- 列：`snapshot_id PK, ts, cash, open_position_value, equity, realized_pnl, unrealized_pnl, open_risk_fraction, reason`
- 时间：`ts` 2026-10-09T19:43:36Z → 19:53:49Z（10 分钟内）
- 样例：`… | 19:43:36Z | cash=1000.0 | equity=1000.0 | reason=account_opened`；末行 `cycle_end | cash=970.0982449999999 | equity=979.8109032462197 | unrealized=-18.723360753780184`
- 来源：自生成；无外部来源列。

### 1.7 events（408 行）
- 列：`event_id PK, slug, title, series_slug, event_date DATE, resolution_source, resolution_source_family, rules_text, active, closed, start_date, end_date VARCHAR, fetched_at TIMESTAMPTZ, raw_json`
- 时间：`event_date` **2025-06-20 → 2026-10-11**；`fetched_at` 2026-10-09T18:58:46Z → 2026-10-10T02:42:07Z
- 分布：`closed=True 405 / False 3`；`resolution_source_family: WEATHER_UNDERGROUND 358, NOAA 50`
- 样例：`1147154 | highest-temperature-in-nyc-on-october-9-2026 | event_date=2026-10-09 | resolution_source=https://www.weather.gov/wrh/timeseries?site=klga | family=NOAA | rules_text=<2365 chars> | raw_json=<63526 chars>`
- 来源列：`resolution_source(+family)` ✓、`fetched_at` ✓、`raw_json`（平均 40.6 KB，29 KB–65 KB）✓。**注意：无 `source` 列（数据来源隐含 gamma-api）；`fetched_at`/`raw_json` 每次 upsert 被最新抓取覆盖**（`db.py` upsert 语义），历史抓取不保留。`event_date` 空值 0。

### 1.8 fills（3 行）
- 列：`fill_id PK, order_id, market_id, token_id, price, shares, fee_usd, seq, created_at`
- 时间：2026-10-09T19:43:36Z（同一秒级三笔）
- 样例：`… | 5424786 | price=0.04184463751420711 | shares=226.52061408259988 | fee_usd=0.45410207 | seq=0`
- 来源：派生自 orders；每 order ≤1 fill（已验证）。

### 1.9 forecast_daily（2017 行）
- 列：`model, run_time TIMESTAMPTZ, target_date DATE, predicted_max_c/f, peak_valid_time, n_hours, lead_hours, lead_bucket, fetched_at`
- 时间：`run_time` 2026-08-22T00:00Z → 2026-10-09T18:00Z；`target_date` **2026-08-26 → 2026-10-11（47 天）**；`fetched_at` 2026-10-09T17:30:48Z → 19:52:06Z
- 空值：`predicted_max_f` 0、`lead_hours` 0；**`n_hours<24` 共 341 行（16.9%，不完整日）**；195 个 run_time、47 个 target_date；PK 无重复
- 样例：`ecmwf_ifs025 | run=2026-08-22T00:00Z | target=2026-08-26 | predicted_max_f=80.24 | lead_hours=100.0 | lead_bucket=>72h`
- 来源列：`model` ✓、`fetched_at` ✓（但 upsert 会覆盖首次抓取时刻）；**无 raw 载荷列**（原始 payload 在 `raw/forecast_runs/`）。

### 1.10 forecast_hourly（49662 行）
- 列：`model, run_time, valid_time, temp_c, temp_f`（**无 fetched_at、无 source 列**）
- 时间：`run_time` 2026-08-22T00:00Z → 2026-10-09T18:00Z；`valid_time` 2026-08-22T00:00Z → 2026-10-24T02:00Z（含预报未来）
- 样例：`ecmwf_ifs025 | 2026-08-22T00:00Z | 2026-08-22T00:00Z | 22.1°C / 71.78°F`
- 来源：仅靠 `(model, run_time)` 外键回溯 `forecast_runs`；**原始抓取时间缺失（列级缺陷）**。

### 1.11 forecast_runs（196 行）
- 列：`model, run_time PK-part, fetched_at, publishable_at, status, reason, n_hours, raw_json`
- 时间：`run_time` 2026-08-22T00:00Z → 2026-10-10T02:00Z；`publishable_at` 2026-08-22T06:00Z → 2026-10-10T00:00Z；`fetched_at` 2026-10-09T17:18:46Z → 19:51:37Z
- status：`ok 195`、`unavailable 1（reason="model run not archived yet"）`
- **`raw_json` NULL = 196/196（100%，该列从未写入**——`collect.py:193` 恒置 `None`）
- 样例：`ecmwf_ifs025 | run=2026-08-22T00:00Z | publishable_at=run+6h | status=ok | n_hours=363`
- 来源：`model` ✓、`fetched_at` ✓、`publishable_at` ✓（**注意：= run_time + 配置 `model_lag_hours`（默认 6h）的计算值，非观测到的真实发布时间；表中无 lag 快照列**，见 §5 R9）。

### 1.12 markets（3756 行）
- 列：35 列（bracket 解析五列、token、outcomes/outcome_prices JSON、fees_enabled/fee_rate/fee_exponent/taker_base_fee/tick_size/min_order_size、`parse_status/parse_reason/tradeable`、`updated_at/fetched_at/raw_json` 等，全列见 DDL `db.py:54-89`）
- 时间：`target_date` 2025-06-20 → 2026-10-11；`updated_at`（场馆时间）2025-06-22T00:24:39Z → 2026-10-10T02:39:30Z；`fetched_at` 2026-10-09T18:58:46Z → 2026-10-10T02:42:07Z
- 分布：**`parse_status='ok'` 3756/3756（0 失败）**；`tradeable=True 33 / False 3723`（closed=3723）；bracket：`closed 2940, open_low 408, open_high 408`；`fees_enabled=True 2156`；`target_date` 空 0、`fee_rate` 空 0
- 样例：`5400221 | event=1147154 | bracket="59°F or below" open_low high=59 | fee_rate=0.05 | fee_exponent=1.0 | taker_base_fee=1000.0 | min_order_size=5.0 | target=2026-10-09 | parse_status=ok | tradeable=True | raw_json=<5030 chars>`
- 来源列：`fetched_at/updated_at/raw_json/resolution_source` ✓（同 events：最新抓取覆盖）。

### 1.13 meta（2 行）
- `schema_version=1`；`active_model_version=normres-ecmwf_ifs025-20261009T1833Z-n1256`

### 1.14 model_runs（1 行）
- 列：`model_version PK, created_at, model, n_samples, train/val 起止, params_json, train/val_metrics_json, status`
- 时间：`created_at` 2026-10-09T18:33:18Z；train 2026-08-26→2026-10-05（n_train=1256），val 2026-10-06→2026-10-08（n_val=314）
- 样例 metrics：train `{bias:-0.3288, mae:3.7189, rmse:5.1578}`；**val `{bias:-2.302, mae:3.1554}`（验证段偏差 -2.3°F，漂移明显）**；status=OK
- 来源：派生表；`params_json` 内含全局/分桶 bias、sigma。

### 1.15 observations（30267 行）
- 列：`station, obs_time TIMESTAMPTZ, temp_c, temp_f, source, report_type, fetched_at`
- 时间：`obs_time` **2023-10-06T04:32Z → 2026-10-10T01:51Z（1101 个日历日）**；`fetched_at` 仅 **4 个批次**（2026-10-09T17:31:21Z … 2026-10-10T02:42:23Z）
- 分布：`iem_asos` report_type `3`(例行 METAR) 26357、`4`(SPECI) 3910
- 样例：`KLGA | 2026-08-25T04:51Z | 72.0°F | iem_asos | report_type=3`
- 来源列：`source` ✓、`report_type` ✓、`fetched_at` ✓（insert_or_ignore：保留**首次**抓取时刻）。`temp_f` 空 0。**语义注意：`report_type` 是按"分钟==51"惯例推断的，不是上游字段**（`weather/observations.py:98-126`）。

### 1.16 orders（3 行）
- 时间：created/updated 2026-10-09T19:43:36Z
- 样例：`1414fd8a… | BUY | status=partial | requested=3333.33 | filled=226.52 | avg=0.0418446375 | fee=0.45410207 | buy=9.478673 | buffer=0.047393 | total=9.980168 | reason="risk cap limited the fill before the intended size"`
- 来源：`signal_id` 回链 signals ✓（但多数 signals 行缺 book_id，见 §4）。

### 1.17 positions（3 行）
- 时间：`opened_at` 2026-10-09T19:43:36Z；`marked_at` 19:53:49Z；`closed_at` NULL（全部 open）
- 样例：`… | shares=226.52 | cost_basis=9.478673 | fees_paid=0.45410207 | loaded_cost_usd=9.980168 | status=open | mark_price=0.001`
- 来源：`settlement_source` 列存在但当前 NULL（未结算）。

### 1.18 predictions（3 行）
- 时间：`target_date` 2026-10-09/10/11；`run_time` 2026-10-09T00:00Z/06:00Z/10:00Z；`created_at` 2026-10-09T18:33:24Z、19:15:56Z
- 样例：`target=2026-10-09 | predicted=71.6 | bias=-0.9287 | sigma=2.6735 | n=30 | status=OK | probs_json={"59°F or below":1.5e-05,…} (6 位小数舍入)`
- 来源：`model_version` ✓、`run_time` ✓、`created_at` ✓。**`probs_json` 数值为 6dp 舍入（实测 sum=1.000001），与现行 `distribution_table` 全精度语义不一致（旧代码版本写入）**。

### 1.19 price_history（22996 行）
- 列：`market_id, token_id, ts, mid, fetched_at`（PK: market_id+ts；**无 source 列**）
- 时间：`ts`（场馆报价时刻）**2026-09-09T20:00:25Z → 2026-10-10T02:23:24Z**；`fetched_at` 2026-10-09T18:51:28Z → 2026-10-10T02:23:51Z
- 覆盖：363 个 market、**33 个 target_date**；mid 空 0
- 样例：`5368840 | 2026-10-06T07:00:20Z | mid=0.015`
- 来源：mid 价来自 CLOB prices-history（60 分钟 fidelity）；仅 mid，无深度。

### 1.20 process_locks（0 行）
- 空表（审计时无持有者）。

### 1.21 settlements（3723 行）
- 列：`market_id PK, event_id, target_date, is_official, yes_price, source, daily_max_temp_f, daily_max_temp_f_all, observed_rule, resolved_at, notes`
- 时间：`target_date` **2025-06-20 → 2026-10-08**；`resolved_at` 2026-10-09T19:52:14Z → 19:53:49Z（**= 导入时刻，非场馆解决时刻**）
- 分布：**`is_official=True 3723（100%），source='polymarket_gamma.outcomePrices'`；yes_price 空 0**；`daily_max_temp_f` **空 3228/3723（86.7%）**；带观测交叉核对的 495 行仅覆盖 2026-08-25→2026-10-08 共 45 天
- 交叉核对结果：`agrees 493`、**`DISAGREES 2`**（均为 2026-09-20，routine hourly 71°F vs 场馆按含 SPECI 的 72°F 解析——`observations.py:69-71` 注释记录了该案例）；`daily_max_temp_f_all` 在 settlements 中全部 NULL（该列为后补迁移列，导入时未回填）
- 事件家族 × 核对：**NOAA 家族 495/517 有核对；WEATHER_UNDERGROUND 家族 0/3206 有核对**
- 样例：`5368840 | 2026-10-08 | official=True | yes=0.0 | daily_max=76.0 | notes="official YES price…observation cross-check…agrees…"`

### 1.22 signals（264 行）
- 列：32 列（decision/reasons/edge/probability/cost 全链 + `book_id/snapshot_id/forecast_run_time/details_json`）
- 时间：`created_at` 2026-10-09T18:33:38Z → 2026-10-10T01:44:29Z；`target_date` 2026-10-09/10/11；`forecast_run_time` 2026-10-09T00:00Z → 12:00Z
- 决策分布：`NEGATIVE_EDGE 142, TARGET_DAY_PARTIALLY_OBSERVED 66, EDGE_BELOW_MIN 19, PASS 14, DUPLICATE_POSITION 12, BOOK_STALE 11`
- 空值率：`edge/fee_usd 89/264（33.7%，无 plan 的决策）`、**`book_id NULL 231/264（87.5%）`、`snapshot_id NULL 242/264（91.7%）`**、`forecast_run_time NULL 11（4.2%）`
- 样例（PASS 复算见 §7）：`… | PASS | cons=0.0683991954998136 | cost_per_share=0.00314108 | total=28.902097 | shares=9201.32 | edge=0.06525811393760296`
- **口径漂移**：02:33Z 批次（33 行）`execution_buffer_usd/buy_cost_usd ∈ [0.00235, 2.0]`，符合旧版"0.002 USDC/股"平摊 buffer（=0.002/price）；03:15Z 起恒为 0.005（现行"名义 0.5%"公式）。即 **signals 表混有 ≥2 个代码版本的公式口径**（另见 `db.py:444-461` notional→新列回填迁移）。

---

## 2. raw/ 原始载荷清单

`backend/data/raw/`（3 个子目录 + 顶层文件）：

| 类别 | 命名约定 | 数量 | 内容 / 日期范围 |
|---|---|---|---|
| 事件 | `event_{event_id}.json` | 408 | Gamma `/events/{id}` 全量载荷（含 description/markets/venue 时间字段）；event_id 27589…1158124。**每次抓取覆盖同名文件（只留最新）** |
| 订单簿 | `books/{market_id}.json`、`books/{market_id}_error.json` | 33（0 error） | CLOB `/book` 原始 bids/asks；**每 market 仅保留最新一份**（DB 的 books 行才含历史 20 快照/市场） |
| 预报 | `forecast_runs/{model}_{run:%Y%m%dT%H%M}Z.json` | 195 | Open-Meteo single-runs 原始 payload（`ecmwf_ifs025_20260822T0000Z` … `_20261009T1200Z`，即 2026-08-22 → 2026-10-09 每 6h cycle） |
| 观测 | `observations/iem_LGA_{start}_{end}.csv` | 3 | IEM ASOS CSV：**`iem_LGA_20231006_20261010.csv`（3 年回填）**、`20260825_20261009`、`20260925_20261009` |

`backend/data/*.json` 报告清单：
- `report.json` / `report_v2.json`（1.17 MB）：`data_sources/research/trading/backtest_coverage/integrity` 全量报告；`backtest_all.json`（1.17 MB）：`weather/trading/coverage`；`benchmark.json`（207 KB）：配对基准（§6 引用其校准数字）；`predict.json`/`predict_v2.json`：单次概率估值+执行计划；`signals.json`：信号汇总；`integrity.json`：`{tables, orphan_positions:0, ok:true, backup}`；`recoll_obs.json`：观测重收集汇总。
- **`paper_run_final.json`（547 KB）与 `worker_cycle.json`（547 KB）实际内容是日志文本（`2026-10-09T19:42:26Z INFO …`），并非 JSON——扩展名误导，迁移时按文本日志处理。**
- 配套 `*.log` 各 134–1485 B；`report.txt` 为文本摘要（记录"528/528 usable books"等历史快照）。
- `backend/data/backup/`：5 个时间戳备份 `weatherquant-20261009T1946…Z.duckdb`（44.3–46.4 MB，2026-10-09T19:46–20:02Z），为完整库副本，可作迁移的第二数据源。

---

## 3. 迁移价值分级（高价值 / 可再生成 / 低价值）

### 高价值（不可再生的研究资产，必须原样迁移）
1. **`observations` + `daily_observations`**（站点观测 3 年，1101 天；双规则日最高）——唯一长期真值来源，支撑模型训练与结算核对。
2. **`settlements`**（3723 官方结算）——场馆真值；虽 86.7% 无观测核对，`is_official+yes_price+source` 本身可信（来自 Gamma outcomePrices 且 0/1 校验通过）。
3. **`events` + `markets`**（408/3756，含 `rules_text`、bracket 解析、费用条款、token、`raw_json`）——市场结构与规则文本，重建历史 bracket 集必需。
4. **`forecast_runs` + `forecast_daily` + `forecast_hourly`**（195 个 ECMWF 单次 run、47 个目标日、49662 小时点）+ `raw/forecast_runs/` 195 份 payload——**历史 single-run 预报不可再从上游取回全部组合**，是训练集本体。
5. **`books`**（660 快照，仅 1 天 × 33 market）——**全库唯一带深度的盘口数据**；量小但不可替代（Polymarket 无历史盘口 API，代码自己也承认 `backtest/trading.py:1-9`）。
6. **`price_history`**（22996 mid 点，33 个目标日）——市场概率基准（明确不可执行）。
7. **`raw/` 全部**（408 事件 + 195 run + 3 CSV + 33 book）——一手证据，防解析逻辑变更后无法回溯。
8. （次级）`backtest_runs` 的 `metrics_json`——历史评估结果快照，留档即可。

### 可再生成（保留亦可，迁移优先级低；应记录生成参数）
- `signals`（264）：可由 markets+books+forecasts+model 重放，但**因公式口径漂移与 book_id 缺失，重放结果可能与旧行不一致**——仅作历史对照迁移，不作为研究输入。
- `predictions`、`model_runs`、`backtest_runs`、`audit_log`：派生/运维数据。
- `equity_snapshots`：paper 曲线，可由 orders/positions 重建。

### 低价值（建议仅归档或不迁）
- `account` / `orders` / `fills` / `positions`：paper 账本，零研究价值（仅证明实验发生过）；3 行量级，随快照归档即可。
- `process_locks`、`meta`：运行时状态，不迁（`active_model_version` 随 `model_runs` 重建）。

---

## 4. 来源与原始时间信息的保留情况（逐列）

| 表 | 来源列 | 原始/抓取时间列 | 缺失或歧义 |
|---|---|---|---|
| events | `resolution_source`（场馆声明的结算源）、`resolution_source_family`；**无 `source`（上游=gamma-api，隐含）** | `fetched_at`（=最近一次，被 upsert 覆盖）；`start_date/end_date` 为场馆字符串 | 历史抓取时刻与旧 raw_json **被覆盖丢失**（raw/ 同名文件同样只留最新）；`event_date` 解析自 venue 字段+标题回退（`client.py:48-72`），来源可信但规则需记录 |
| markets | `resolution_source`；无 `source` | `fetched_at`（覆盖式）、`updated_at`（场馆 updatedAt，**保留历史值** ✓） | 同上：多轮抓取只留最后态；`parse_status` 语义随解析器版本可能变化（当前全 ok） |
| books | **无 source 列**（隐含 clob） | `fetched_at` ✓（逐行保留）；`venue_ts` **660/660 全 NULL** → 场馆侧时间戳从未捕获 | 无法用场馆时钟校验快照时刻；raw/ 每市场只留最新 |
| forecast_runs | `model`（隐含 single-runs-api） | `fetched_at` ✓；`publishable_at` = run_time+**当时配置的** `model_lag_hours` | **`raw_json` 恒 NULL**；**无 lag 快照列**——若历史 lag 配置变过，新旧行的 publishable 语义不一致（未发现 lag 变更证据，但列级上无法证明） |
| forecast_daily | `model` | `fetched_at`（upsert 覆盖） | 无 raw 列（回溯 raw/forecast_runs 文件名 `{model}_{run}Z.json`） |
| forecast_hourly | `model` | **无任何时间来源列** | 只能靠 `(model,run_time)` 回链 |
| observations | `source=iem_asos` ✓、`report_type` ✓ | `obs_time`（观测时刻）、`fetched_at`（**首次**抓取，insert_or_ignore 保留） ✓ | `report_type` 为 :51 惯例**推断**而非上游字段（`observations.py:101-123`） |
| daily_observations | `source` ✓、`rule` ✓ | `computed_at`（重算即覆盖） | **`is_official` 恒 FALSE，无置真代码路径**（`observations.py:225-237` 默认 False，rg 未发现写 True 处）；与 settlements 的核对结果只写在 `settlements.notes`，**观测行本身无回链列** |
| settlements | `source=polymarket_gamma.outcomePrices` ✓、`observed_rule` ✓ | `resolved_at` = **导入时刻，非场馆 resolve 时刻**（场馆 resolve 时间未捕获） | `daily_max_temp_f_all` 恒 NULL；观测核对结果仅存于自由文本 `notes`，不利结构化查询 |
| price_history | **无 source 列**（隐含 CLOB prices-history） | `ts`（场馆点位）✓、`fetched_at` ✓ | 仅 mid；无深度、无 venue 源标注 |
| signals | `model_version` ✓ | `created_at` ✓、`forecast_run_time` ✓（11 行 NULL，details 回填尽力而为） | **`book_id` 87.5% NULL、`snapshot_id` 91.7% NULL**（列后加+旧行走 details，回填未覆盖）→ 多数决策无法钉死当时盘口；公式口径混版本（§1.22） |
| orders/fills/positions | `settlement_source` 列（未用） | `created_at/opened_at/marked_at` ✓ | `positions.execution_buffer_usd` 由迁移回填（`db.py:483-496`），与 orders 值有 ~1e-8 浮点差 |

**会丢/会歧义的列总结**：① 各表 `fetched_at`（events/markets/forecast_daily）只保留最近一次；② `books.venue_ts` 全空；③ `forecast_runs.raw_json` 全空、publishable 的 lag 无快照；④ `forecast_hourly` 无抓取时间；⑤ `daily_observations.is_official` 名存实亡；⑥ `settlements.resolved_at` 是导入时间；⑦ signals 的 book/snapshot 绑定大部分缺失。

---

## 5. 可靠性不足 → 待核实的判定规则（可直接实现的规则清单）

以下规则可直接翻译为迁移 ETL 的 SQL/谓词；命中即打 `pending_verification`，未命中且来源明确方可 `trusted`。

**R1（观测官方性）** — `daily_observations` 全表 2200 行 `is_official=FALSE`，且**代码中不存在置 TRUE 的路径**（`observations.py:225-237` 固定默认 False；rg 全库未发现写入 True）。因此 **`is_official` 列不可作为信任依据**。
```
trusted(d) ⟺ EXISTS s ∈ settlements
    WHERE s.target_date = d.target_date AND s.is_official AND s.yes_price ∈ {0,1}
      AND s.daily_max_temp_f = d.max_temp_f AND s.observed_rule = d.rule
ELSE pending_verification
```
当前仅 45 天（2026-08-25…2026-10-08）可通过；其余 1055 天 → `pending_verification`。

**R2（结算缺核对）** — `settlements.is_official = TRUE AND daily_max_temp_f IS NULL` → `pending_verification`（**3228/3723 = 86.7%**）。`yes_price IS NULL` 或 `yes_price NOT IN (0,1)` → `unusable`（当前 0 行）。

**R3（核对冲突）** — `notes LIKE '%DISAGREES%'` → `pending_verification`（2 行，均为 2026-09-20，routine-71F vs 场馆-72F，即 hourly 与 all_readings 规则冲突）；`notes LIKE '%rule_disagreement%'` 类（本库 0 行，因 `daily_max_temp_f_all` 全 NULL，导入时未比对）→ 同样规则备用。

**R4（结算源家族不匹配）** — join `events.resolution_source_family`：`family='WEATHER_UNDERGROUND' AND settlements.daily_max_temp_f IS NULL` → `pending_verification`（**3206 行**：观测是 NOAA/IEM 数据，场馆按 WU 解析，二者结构上不可互证；有核对的 495 行全部属于 NOAA 家族）。

**R5（盘口质量）** 对 `books` 行：
- `status ∈ {'empty','no_asks','crossed'} OR json_array_length(asks_json)=0` → `unusable`（4 行 no_asks）；
- `status='ok' AND best_bid IS NULL` → 对 **mark/估值用途** 打 `pending_verification`（**247/660 = 37.4% 单边盘口**，`status` 列本身不感知此缺陷）；
- `venue_ts IS NULL`（660/660）→ 全部标注 `venue_ts_missing`（无法对场馆时钟核验）；
- 决策配对规则：`signals.created_at − books.fetched_at > 300s`（`max_book_age_seconds`，`config.py:95`）→ 该信号 `pending_verification`（线上已挡 11 条 BOOK_STALE，迁移时按历史差值复算）。

**R6（预报完整性）** — `forecast_runs.status <> 'ok'` → `unusable`（1 行 unavailable）；`forecast_daily.n_hours < 24` → `pending_verification`（**341/2017**，不完整日不可用于训练/评分）；`forecast_daily JOIN forecast_runs` 后 `publishable_at IS NULL` → `pending_verification`（当前 0）。

**R7（市场解析）** — `markets.parse_status <> 'ok' OR bracket_kind = ''` → `unusable`（当前 0 行，保留规则防历史污染）；`tradeable=FALSE AND closed=FALSE` → `pending_verification`（生命周期异常，当前 0）。

**R8（信号可复现性）** — `signals` 行满足任一：`book_id IS NULL OR snapshot_id IS NULL`（231/242 行）或 `created_at < 2026-10-10T03:00:00+08:00`（旧 buffer 公式批次，`execution_buffer_usd/buy_cost_usd ≠ 0.005` 实测）→ `pending_verification`（决策无法钉死数据快照 / 口径不一致）。**旧批次还违反现行"总支出≤1% cap"语义**（02:33Z 批次单笔 `total_execution_cost≈28–30 USDC` > `equity×1%=10`，当时 cap 只约束 buy），迁移时不得当合规样本。

**R9（publishable 语义）** — `forecast_runs.publishable_at = run_time + model_lag_hours(当时配置)`，无 lag 快照列。迁移规则：`assumption='lag=6h'` 随行记录；若日后发现配置历史变更，则该时段行降级 `pending_verification`（本次审计**未发现** lag 变更证据 → 当前按 trusted 假设标注）。

**R10（观测日完整性）** — `rule='hourly_metar' AND n_readings < 24` → `pending_verification`（**37 天**，读数 20–23）；`obs_time` 非 :51 推断的 SPECI（report_type='4'，3910 行）在用于 `all_readings` 规则时标注 `rule=includes_speci`（与 hourly 严格分离，代码已分离 `RULES`，`observations.py:37-39`）。

**R11（报告文件）** — `backend/data/paper_run_final.json`、`worker_cycle.json` 实为日志文本 → 迁移清单中标 `format_mismatch`，勿按 JSON 解析。

---

## 6. 概率模型审计（公式 + 独立推导 + 正误）

### 6.1 代码实际做什么（path:line）
实现全部在 `backend/quant/model/baseline.py`：

1. **残差构造**：`residual = predicted_max_f − observed_max_f`（`baseline.py:130`），观测限定 `rule='hourly_metar' AND source='iem_asos' AND is_official=FALSE`（`baseline.py:104-105`——因全表 FALSE，实为全量），并要求 `publishable_at ≤ NY 日终`（`baseline.py:120`，防未来 run 混入训练）。
2. **时序切分**：按 `run_time` 排序 80/20（`split_time_ordered`, `baseline.py:135-141`）。
3. **分桶估计**：对每个 lead bucket（7 档，`timeutil.py:56-70`）：`bias=mean(r)`, `sigma=pstdev(r)`；向全局收缩 `w=n/(n+5)`，`bias=w·bias_b+(1−w)·bias_g`，`var=(n·σ²+5·σ_g²)/(n+5)`，`sigma=max(√var, 0.75)`（`baseline.py:190-200`，`SHRINK_STRENGTH=5`、`MIN_SIGMA_F=0.75`）。n<4 的桶直接用全局。
4. **点估计**：`mu = F + bias_b`，`se = sigma/√n`（`baseline.py:321-322`）。
5. **bracket 概率**：整度 bracket `[lo,hi] → [lo−0.5, hi+0.5)`（`intervals.py:43-54`），`p_k = Φ((h_k−μ)/σ) − Φ((l_k−μ)/σ)`，截断负值后**归一化到和为 1**（`baseline.py:326-340`，`_cdf = 0.5(1+erf(x/√2))` `baseline.py:358-359`）。
6. **保守概率**：`p_cons[k] = min( p_k(μ−z·se), p_k(μ+z·se) )`，`σ←σ·inflation`（`baseline.py:343-355`；z=`probability_z`=1.0，inflation 默认 1.0，`config.py:96-97`）。

### 6.2 独立推导与数值复核
- **公式形态独立复核**：用 `statistics.NormalDist.cdf`（与实现的 erf 路径独立）按同一 edges 对 `predictions` 全部 3 行重算 raw 与 conservative：**raw 最大绝对差 2.3e-3、cons 最大差 1.4e-3**，差异可完全由存储舍入解释（`bias_f/sigma_f` 存 4 位、`probs_json` 旧版存 6 位，实测 sum=1.000001）。**结构正确，无实现错误。**
- **半度边界**：整度分辨率 → `[lo−0.5, hi+0.5)` 是正确映射（观测先 round 到整度再归桶，`observations.py:187-199` 与 `intervals.py:179-185` 一致）。
- **min(两端点) 的正当性**：单个 bracket 的 `p_k(μ)` 关于 μ 单峰，故在区间 `[μ−z·se, μ+z·se]` 上最小值必在端点 → `min(p(μ−), p(μ+))` 恰是 CI 下确界。**推导成立**（`baseline.py:352-355` 注释的论证正确）。
- **σ 是否单一常数**：**不是**——按 lead bucket 分 7 档、带向全局的伪计数收缩与 0.75°F 下限（`baseline.py:190-200`）。**是否只在样本内拟合**：是——bias/sigma 仅由 train 块估计，80/20 时序切分（`baseline.py:154,181-200`），val 只算 metrics 不参与拟合。
- **se 的严重缺陷（错误）**：`se = sigma/√n`（`baseline.py:322`）把同一 `target_date` 的 ~30 个 run 当独立样本。实测 `model_runs.n_train=1256` 对应约 47 个独立日期 → **有效样本被高估 ~27×，se 低估 ~5.2×**。后果：`p_cons ≈ p_raw`——`predictions` 实测收缩仅 0.0068–0.0090（modal bracket raw 0.2917→cons 0.2833 等），**"保守概率"基本是装饰**。`backtest/benchmark.py:13-14` 的文档明确承认该聚类问题（"treating them as such would make the standard error look ~10x smaller"）并在其 CI 中按日期聚类修正，**但线上信号路径（estimate→conservative_probabilities）从未修正**。
- **验证段泄漏（错误）**：切分按 `run_time` 而非 `target_date`（`baseline.py:137`）→ 同一目标日的早期 run 落入 train、晚期 run 落入 val，**val 的观测值已在 train 中出现**，`val_metrics` 偏乐观。实测 val 段 bias=-2.30 vs train -0.33（`model_runs`），即便泄漏下漂移仍显著。
- **校准检查**：`metrics.calibration_bins`（`metrics.py:62-82`，reliability diagram）与 Brier/log-loss **只存在于离线回测**：`backtest/weather.py:154`（按 run 的校准）、`backtest/benchmark.py:435`（含按日期聚类 CI、区间覆盖率 `interval_coverage`）。**线上 `calibrate` 命令（`cli.py:165-194`）只算回归指标（bias/MAE/RMSE），不做任何校准/可靠性门控**；`fit()` 的 val_metrics 也从不被用于拒绝发布。
- **离线校准实测**（`backend/data/benchmark.json`，29 个独立日期、2026-09-11…10-09）：区间覆盖率 68%→0.680（gap −0.002）、95%→0.948（gap −0.002）**看起来良好**；但配对 Brier：**market 0.615 < raw_normal 0.807 < normal_calibrated 0.829**，top-1 精度 market 0.59 vs model 0.30，`calibrated_vs_market` 差异 CI95=[0.505,0.701]（市场显著更优）；且 `raw_normal` 反而略优于 `calibrated`（CI 重叠）。即**当前 47 天窗口上，bias 校正没有可证的正收益，模型明显不如市场价格**。
- **climatology 基线泄漏**（错误，见 §8）：`climatology_probabilities` 只排除目标日自身、**无 `target_date < target` 上界**（`benchmark.py:108-129`）。

**结论**：概率映射（Normal CDF over bucket edges + 半度边界 + 归一化）在数学上**正确**；分 lead 的 σ、时序切分、样本内拟合、收缩估计**统计上站得住**；但 **se/CI 独立性假设错误**（导致保守化失效）、**验证切分泄漏**、**线上无校准门控** 三处不成立；离线证据显示模型尚不如市场。

---

## 7. Edge 与成本公式审计（含费用是否重复计算、是否用 midpoint 充当成交价）

### 7.1 提取的公式（`backend/quant/signals/engine.py`）
对每个 market 的 YES 侧 ask 逐档吃量（`_walk`, `engine.py:199-247`）：

```
buy      = Σ_i take_i · price_i                      (engine.py:221)
avg      = buy / shares                              (engine.py:226)
fee      = round(shares · fee_rate · avg · (1−avg), 8)      (engine.py:227)
buffer   = round(buy · execution_slippage, 8)               (engine.py:230)   # execution_slippage=0.005
total    = round(buy + fee + buffer, 6)                     (engine.py:232)
cost/sh  = total / shares                            (engine.py:241)
edge     = conservative_probability − cost/sh        (engine.py:149-159, 581)
PASS ⟺ edge ≥ min_net_edge (=0.05)                   (engine.py:598)
```
预算求解：`budget = cap/(1+fee_rate+slippage)`，最多 6 次迭代收缩使 `total ≤ cap`（`plan_execution`, `engine.py:250-276`）。`fee_rate` 取自市场 `fees_enabled ? fee_rate : 0`（`engine.py:566`）。

### 7.2 独立重推
- **费用公式正确且官方一致**：Polymarket 官方费用文档给出 `fee = C × feeRate × p × (1−p)`（taker，maker 免费；来源：[docs.polymarket.com/trading/fees](https://docs.polymarket.com/trading/fees)）——与 `engine.py:227` **逐字一致**；spec（`docs/quant-core.md:153`）亦同。对 DB 全部 264 条 signals 复算 `fee = shares·0.05·p·(1−p)`：**0 条不匹配**。
- **无双重计费**：fee 只进入 `total` 一次；`edge` 只减 `total/shares` 一次；`cost_per_share_cash=(buy+fee)/shares` 是不含 buffer 的平行视图（`engine.py:242`），与 `edge` 不混用。**未发现费用重复计算**（重复计算的形态——如 cost 里含 fee 又单独再减 fee——不存在；paper 侧 `cash -= total` 与 `fees_paid += fee` 中后者只是计数器，非二次扣款，`paper/engine.py:216-218`）。**判定：正确，未重复。**
- **cap 恒成立的证明（迭代实为冗余安全网）**：`fee/buy = fee_rate·(1−p̄)`，故 `total = buy·(1+slip+fee_rate·(1−p̄)) ≤ buy·(1+slip+fee_rate) ≤ budget·(1+slip+fee_rate) = cap`。**首次 walk 即保证 total≤cap**，`engine.py:266-276` 的迭代在现行公式下不会触发（无害）。
- **midpoint 审计：未用 midpoint 充当成交价**。执行价 = ask 逐档 VWAP（`engine.py:208-226`）；mark 用 `best_bid`（可卖出方向，估值合理，`paper/engine.py:271-288`）；mid 仅用于**市场技巧评分**且显式标注不可执行（`metrics.py:5-8`、`backtest/weather.py:192-209`、`backtest/trading.py:176-180`、`benchmark.py:15-16`）。**判定：合规。**
- **edge 单位正确**：YES 合约赔付 1 USDC/股 → `E[payout]−E[cost] = p − cost/sh`（USDC/股），`engine.py:150-156` 注释与量纲一致。
- **实数复核**：14 条 PASS 行按 `p − total/shares` 复算，与存值差 ≤5e-9（历史舍入残差），公式成立。
- **口径漂移（迁移要点）**：§1.22 所述 02:33Z 批次 buffer=0.002/股（`buffer/buy` 达 2.0），与现行 0.5% notional 不同源；同批 `total≈30 > equity×1%`（旧版 cap 只管 buy）。**旧 signals 行的 edge 与"合规性"不可与新行直接比较。**
- 次要：`markets.taker_base_fee=1000`、`fee_exponent` 被存储但**从未参与计算**（`client.py:269-270` 采集、engine 只用 `fee_rate`）；官方文档的最低费 0.00001 USDC 与 5 位小数舍入未实现（`engine.py:227` 用 8 位）——偏差极小，列出备查。

---

## 8. 时间泄漏审查

**已正确防护的路径**：
1. `publishable_at` 强制点：`latest_complete_forecast` 的 SQL 谓词 `r.publishable_at <= ?`（`collect.py:314`），被**信号**（`signals/engine.py:457`）与**预测**（`ops/predict.py:74`）共同使用 → 线上决策不可能用到未公开 run。
2. `publishable_at = run_time + lag(6h)`（`timeutil.py:85-92`，`single_runs.py:73-74,98`）——run 初始化时刻从未被当发布时刻（有测试 `test_timeutil.py:62-71`、`test_weather.py:36-43`）。
3. 训练样本守门：`publishable > NY(target+1) 00:00 → 丢弃`（`baseline.py:120`），日终后才公开的 run 不入训练。
4. benchmark 问题时刻：`issue_time = NY midnight(target) − {6,18,30,42}h`，只取 `publishable_at ≤ issue_time` 的 run（`benchmark.py:320-329`），且 train 严格 `target_date < target`（`benchmark.py:308-312`）。
5. `TARGET_DAY_PARTIALLY_OBSERVED` 守门：目标日过 60% 即拒单（`signals/engine.py:478-483`），避免"模型看不见日内观测、场馆看得见"的信息不对称。
6. `walk_forward` 训练集严格 `target_date < target`（`backtest/weather.py:85`）→ 结果日不会进自己的训练集。

**发现的泄漏/独立性缺陷（均未发现修复代码）**：

- **L1（CI 独立性，线上）**：`se = sigma/√n` 把同日 ~30 个 run 当独立（`baseline.py:322`），z·se 收缩被稀释 ~5×，保守概率形同虚设。benchmark 模块自己文档承认该问题（`benchmark.py:13-14`）并只在**离线 CI**按日期聚类（`benchmark.py:460-478`）——**线上路径未修**。
- **L2（验证集泄漏）**：80/20 按 `run_time` 切（`baseline.py:137`），同 `target_date` 观测横跨 train/val → `val_metrics` 不独立。
- **L3（climatology 基线）**：±30 天 day-of-year 窗口**不过滤未来**（`benchmark.py:108-129` 仅 `target_date <> target`）——对历史评分日，同年"未来"观测可入基线，令基线偏强（影响基线对比，不影响被交易模型）。
- **L4（交易回测的资本前视）**：`backtest/trading.replay` 按信号顺序立即用**当下已存在**的 settlement 给 `equity += proceeds`（`trading.py:144-149`），不比较 `settlements.resolved_at` 与后续信号的 `created_at` → 后面的信号在回测里提前拿到了现实中尚未到账的资金（结算至少隔一天）。**信号级选股无前视（book 快照 ≤300s，`trading.py:30-48,106`），资金曲线有 mild 前视。**
- **L5（walk_forward 的"多次 run=多样本"）**：`walk_forward` 对每个 target 的全部 run 计 Brier/log-loss 并在 `calibration_bins` 按 run 汇总（`weather.py:105-154`），同一日 run 强相关 → 样本量与校准分箱被膨胀（报告同时给了 `n_scoring_dates` 但未按日期聚类；benchmark 才做了）。
- **未发现**任何路径让"未公开预报"进入信号（守门 SQL 是唯一入口且被两处调用）。**未发现** `daily_observations` 参与交易决策（`evaluate_market` 全链不读观测表）。

**多 run 是否被当独立样本算 CI 的明确回答**：线上的 `se`/保守概率——**是，属缺陷（L1）**；离线 benchmark 的差值 CI——**否，已按日期聚类**（`benchmark.py:460-478`）；walk_forward 的均值/校准——**是（L5），但仅报告用**。

---

## 9. 回测机制审查

**`backtest/` 有三个互相独立的模块：**

1. **`weather.py`（模型技巧回测）**——`walk_forward`（`weather.py:57-189`）：按 `target_date` 升序，对每个目标日用**严格更早日期**的样本重训（:85-89），对当日全部 run 出分布、对同一观测算 Brier/log-loss/top-1，并与 `price_history` mid（`run+12h` 参考时刻，:245-271）对比。**时序切分 ✓、观测无前视 ✓**；限制：多样本按 run 聚集（L5）、市场对比仅在有 mid 历史的日期（缺则报 `MARKET_PROBABILITY_UNAVAILABLE`，:192-209）。
2. **`benchmark.py`（配对基准）**——见 §6/§8：问题时刻=纽约日起点前 {6,18,30,42}h，逐时刻取最新可公开 run，6 个变体同键评分（raw/calibrated/rescaled/empirical/climatology/market），差值 CI 按日期聚类+手抄 t 表（:481-494），区间覆盖率按日期聚类（:189-219）。**这是全库最严谨的评估代码**；缺陷仅 climatology 未来泄漏（L3）与 calibration_bins 仍按 run 汇总（:435）。
3. **`trading.py`（交易回放）**——基于**已录制**的 signals + books 快照：`_quote_at` 取信号时刻前最近快照且年龄 ≤300s（:30-48），用真实 `plan_execution`（吃档、部分成交、taker 费）重走，`budget=min(equity·1%, 记录的 total_execution_cost)`（:112），只对 `is_official` 结算计盈亏（:124-149）。**显式承认 Polymarket 无历史盘口 API，无快照窗口报 `HISTORICAL_BOOK_UNAVAILABLE` 而非编造**（:74-89,176-180）。**是否建模盘口深度：是**——逐档 size 走量+partial_fill 标记（`engine.py:208-247`）。缺陷：L4 资金前视；无排队/延迟/逆向选择建模（buffer 只是固定准备金）；`signals` 旧口径批次会使历史 edge 不可比（§5 R8）。
- `cli.py:339-368` 将结果写入 `backtest_runs`（10 行，全 OK）。
- **整体：时序切分正确、无结果日泄漏；最大的回测问题是数据窗口极短（书 1 天、信号 1 天、mid 1 个月、预报 7 周），以及交易回放的结算到账时点前视。**

---

## 10. 账本与资金恒等式审查

**恒等式定义**（`ops/report.py:258-279` `reconcile`，共 9 条）与实现核对：
- `equity = cash + open_position_value`（mark 计价）
- `equity = initial + realized + unrealized`
- `cash = initial − Σ orders.total + Σ settled proceeds`
- `realized = Σ proceeds − Σ settled loaded_cost`
- `Σ order.total = buy + fee + buffer`；`Σ order.total = open.loaded + settled.loaded`
- `account.fees_paid = Σ positions.fees_paid`；一单一仓；每单 ≤1 fill；signals 分类全覆盖。

**真实数据实测（快照 SQL 复算）**：
- `cash − (initial − 29.901755 + 0) = −1.1e-13` ✓
- `Σ buy=28.436018957345972 + Σ fee=1.32355636 + Σ buffer=0.1421796826540293 = 29.901755 = Σ orders.total = Σ open loaded` ✓（order 与 position 三行逐行 `loaded=total` 精确相等）
- `account.fees_paid − Σ pos.fees = 0`；孤儿仓 0；重复 fill 0 ✓
- **结论：账本恒等式在真实数据上全部成立，未发现不变量破坏。**
- 单测亦独立断言（`test_invariants.py:283-352`，含结算后 `cash = initial + realized` 精确等式）；`account_state.reconciles`（`paper/engine.py:104-106`）与 `snapshot()`（:329）双重自检。

**结构性问题（非违约但须知）**：
1. **execution_buffer 被当作已花费**：买入时 `cash -= total_execution_cost`（含 buffer，`paper/engine.py:216`），buffer 从未"退还"，结算时 `realized = proceeds − loaded`（含 buffer，:371-377）→ 纸面账户永久烧掉 buffer。恒等式因此自洽，但 equity 系统性低估（3 笔合计 0.14 USDC，量级可忽略）；这是保守记账选择，新系统需明确语义（预提 vs 实耗）。
2. `positions.execution_buffer_usd` 由迁移回填，与 orders 值差 ~1e-8（`db.py:483-496`），靠 1e-6 容差通过。
3. `integrity_check` 只查孤儿（`db.py:617-626`），**不含资金恒等式**——恒等式只在 `report reconcile`/测试里跑，未纳入常规运维门。
4. 当前 3 仓 mark（0.001/0.008）远低于成本 → `unrealized=-18.72`，账面即刻浮亏（penny bracket 0.0418/0.0314 买入后盘口塌了）——mark 用 best_bid 是对的，但这暴露信号对"1 分钱 ask 巨量"的误判，非账本问题。

---

## 11. 测试质量（含实际运行 pytest 结果）

**实测**：`PYTHONPATH=backend python -m pytest backend/tests -q`（`pytest.ini`：`testpaths=backend/tests`，`--basetemp=.pytest_tmp`；conftest `StubHttp` 令任何网络调用抛错）→
```
144 passed in 189.51s (0:03:09)
```
**规模**：99 个 `def test_`（10 个文件）→ 144 个用例（parametrize 展开）。分布：`test_trading_loop 23`、`test_app 11`、`test_weather 11`、`test_config 11`、`test_invariants 10`、`test_model 10`、`test_intervals 8`、`test_timeutil 8`、`test_market_parsing 7`。

**断言质量分层**：
- **强（独立重算恒等式）**：`test_invariants.py:62-87` 对 8 价格×2费率参数化验证 `total=buy+fee+buffer`、`fee=shares·feeRate·p·(1−p)`、`buffer=buy·0.005`、cap 不超、per-share 恒等；`test_edge_is_probability_minus_total_cost_per_share`（:90-120）在真实决策上从原始字段重推 edge；ledger 恒等式（:283-352）+ 结算后现金精确等式；`test_trading_loop.py:105-143` 参数化 cap 与多档吃量。这些是对"重复计算/口径漂移"的真防线。
- **中（性质测试）**：分布和为 1、非负、尾部集中（`test_model.py:46-55`）；cons ≤ raw（:67-75）；时序切分严格（:93-103）；`normal_cdf(1.96)≈0.975`（`test_intervals.py:105-108`，**唯一硬编码独立期望值**，容差 1e-3）。
- **弱/缺口**：
  1. **`bracket_probabilities` 没有任何"手工独立算出的期望值"对照**（只测结构性质）；`conservative_probabilities` 同样只测性质。
  2. **`backtest/weather.py`、`backtest/trading.py`、`backtest/benchmark.py` 零测试覆盖**（rg 确认测试目录无任何 import；含 climatology、区间聚类 CI 等最复杂逻辑）。
  3. **`latest_complete_forecast` 的 `publishable_at` 守门无直接测试**（rg 仅见 fixture 写入 publishable_at；无"未公开 run 必须被拒"的用例）；**无 `FORECAST_STALE` 用例**。
  4. bias 恢复测试容差过宽（`abs=0.6`，`test_model.py:109`）。
  5. fee 测试是对 spec 公式的恒等重算——能防回归，**不能发现"spec 本身若与场馆不符"**（本次已通过官方文档独立核对，公式一致，见 §7）。
- `test_app.py`（11 个）测的是 `backend/app.py` FastAPI 层，与 quant 核心独立。

---

## 12. 新系统必须修复的设计缺陷清单

1. **CI/保守化的独立性错误**：`se=σ/√n` 把同 target_date 的多次 run 当独立样本（`model/baseline.py:322`），se 低估 ~5×，保守概率几乎不起作用。→ 新系统：以 **target_date（或事件簇）为独立单元**计算 SE/CI（benchmark 的 `_clustered_ci` 已给出正确样板，`benchmark.py:460-478`，应上移到线上路径）。
2. **验证切分泄漏**：train/val 按 `run_time` 切分使同一目标日观测横跨两段（`baseline.py:135-141`）。→ 按 `target_date` 切分。
3. **线上无校准门控**：`calibrate` 只出回归指标（`cli.py:165-194`），reliability/Brier 只在离线回测；val_metrics（bias -2.3）不触发任何拒发。→ 发布模型必须带按日期聚类的 reliability diagram + Brier 分解，超阈值拒发。
4. **信号行可追溯性残缺**：`book_id/snapshot_id` 87.5%/91.7% 缺失，signals 混有 ≥2 个公式的口径（flat 0.002/股 vs 0.5% notional），旧批次还违反 1% 总支出 cap。→ 新系统：**每条决策落库即强制快照外键（非空约束）+ formula_version 列**，schema 迁移禁止无版本回填。
5. **provenance 列系统性缺陷**：`forecast_runs.raw_json` 恒 NULL、`forecast_hourly` 无抓取时间、`books.venue_ts` 100% NULL、`fetched_at` 被 upsert 覆盖、`settlements.resolved_at` 实为导入时间、publishable 依赖隐式配置无 lag 快照。→ 每表强制 `source`+`captured_at`+`payload`（或内容哈希），**事件时间与摄取时间分列且均不可变**。
6. **`is_official` 语义名存实亡**：观测表永不置真，官方性靠 settlements.notes 自由文本承载。→ 布尔语义要么真实落地要么删除；核对结果结构化成列（`crosscheck_status ENUM`），观测↔结算双向外键。
7. **结算核对覆盖率过低且家族错配**：86.7% 结算无观测核对、WU 家族 0 核对、hourly/all 规则冲突仅文本记录（2026-09-20 案例）。→ 结算导入时强制双规则比对+家族适配，冲突入结构化状态机。
8. **回测资金前视**：`trading.replay` 结算款在信号序列即刻入账，不比 `resolved_at`（`trading.py:144-149`）。→ 资金曲线事件驱动（信号时刻 vs 结算时刻合并时间轴）。
9. **climatology 基线未来泄漏**：`benchmark.py:108-129` 无 `target_date < target` 上界。→ 加时间上界。
10. **`backtest/` 三模块零测试**：最复杂的配对基准/聚类 CI/回放无一覆盖。→ 数学模块必须有独立期望值测试（非仅性质测试）；概率函数给手工 Φ 对照表。
11. **数据窗口与采集设计**：books 仅单日×33 market、信号仅 1 天、mid 仅 1 月、raw 每文件覆盖式只留最新。→ 新系统：**周期性不可变归档**（每日快照分层存储）、历史盘口无 API 的现实要在采集端就高频留存。
12. **paper 账本把执行预提金当实耗烧掉**：buffer 进 loaded_cost 后不返还（恒等式自洽但概念混淆）。→ 账本把"预提、实耗、返还"分成三态。
13. **配置/公式的隐式版本**：`model_lag_hours`、buffer 公式、cap 语义都在代码里演进而行数据无版本标记。→ 所有派生行写入 `calc_version`（代码版本+关键参数哈希）。
14. **费用细节未对齐场馆**：最低费 0.00001、5 位小数舍入未实现；`taker_base_fee/fee_exponent` 采集了但不参与计算。→ 费用引擎对齐场馆文档并单测。
15. **`process_locks` 单写者租约仅覆盖 `paper-run`**：`scan/collect/settle` 等命令可并发写库（本次审计期间即见证 live collect 占库致只读打开失败）。→ 所有写路径统一租约或依赖存储层单写者。
16. **`is_official=FALSE` 被用作训练过滤条件**（`baseline.py:105`）：一旦未来有人置真，训练集会**排除**最可信数据。→ 过滤条件应基于"观测可用性"而非该失效布尔。

---

### 附：审计执行说明
- DuckDB 全部查询：`duckdb.connect("E:/Project/WeatherQuant/.audit-out/legacy-snapshot.duckdb", read_only=True)`；快照在占用进程退出并 checkpoint 后复制（104,345,600 B，与原文件字节一致，无 WAL 遗留）。
- 未以写模式运行任何 `quant.cli` 命令；唯一执行的测试命令为上述离线 pytest。
- 官方费用公式经外部文档核对：[Polymarket Fees](https://docs.polymarket.com/trading/fees)。

# KLGA 结算真值审计

2026-10-11。数据来源：旧项目本地库 `backend/data/weatherquant.duckdb`（**只读快照**，未写入）。

本报告回答三个问题：Polymarket 依据什么结算、我们的观测有多少天与之一致、以及旧项目
"1,100 天仅 45 天吻合"这个说法是否成立。

---

## 1. 方法：先排除一个陷阱

### 1.1 循环比较（本次审计最重要的发现之一）

旧库 `settlements.daily_max_temp_f` 与 `daily_observations(rule='all_readings')` 的日最高温
**404 / 404 完全相同**。

这说明该列**不是独立真值，而是把我们自己的观测复制进去的**。拿它和同一份观测比较，
必然 100% 吻合——这个数字没有信息量。

因此本审计的比对对象改为：**观测日最高温落在哪个括号** vs **Polymarket 实际赔付哪个括号**
（由 `yes_price = 1` 推出）。后者是场所自己的裁决，与我们的观测完全独立。

### 1.2 按事件分组，不按日期分组

`target_date` 不唯一标识事件。旧库 2026-05-19 有**两个事件**：

| event_id | slug | 结果 |
|---|---|---|
| `493658` | `arch-highest-temperature-in-nyc-on-may-19-2026` | 赢家 `90-91°F` |
| `503590` | `highest-temperature-in-nyc-on-may-19-2026` | 赢家 `95°F or below` |

按日期分组会把它们合并成"一个事件有两个赢家"，既掩盖了两个事件**判出不同括号**这一事实，
又把结构问题伪装成数据问题。全审计按 `event_id` 分组。

---

## 2. 结算规则原文（逐市场提取）

规则文本按 `rules_text` 的 SHA-256 去重后共 **407 种**变体，覆盖 **408 个事件**。
解析器 `weatherquant.markets.settlement_rules` 对 **408/408 全部 `parsed`**，
无任何必填字段缺失。

### 2.1 NOAA 系（50 个事件）

> This market will resolve to the temperature range that contains the **highest temperature
> recorded by NOAA at the LaGuardia Airport Station** in degrees Fahrenheit on 10 Oct '26.
>
> The resolution source for this market will be information from NOAA, specifically **the highest
> reading under the "Temp" column for all times on this day**, available here:
> `https://www.weather.gov/wrh/timeseries?site=klga`
>
> This market will resolve off of the **Hourly Data** provided using the "Show Hourly Data" button.
>
> If NOAA data for the observation date is unavailable by **11:59 PM ET** on the day following the
> observation date, the **Weather Underground Daily Observations table** will be used as the
> resolution source.
>
> In the event that there is no data for the observation date by 11:59 PM ET on the day following
> the observation date, this market will resolve to **the lowest bracket**.
>
> This market will resolve once the first data point for the following date has been published on
> the resolution source, or by 11:59 PM ET on the day following the observation date, whichever
> comes first.
>
> The resolution source for this market **measures temperatures to whole degrees Fahrenheit**
> (eg, 21°F). Thus, this is the level of precision that will be used when resolving the market.

### 2.2 Weather Underground 系（357 个事件）

> This market will resolve based on the highest temperature recorded in the **'Daily Observations'
> table on Weather Underground**, not the figure displayed in the 'Day High & Low' summary
> section; in the event of any discrepancy between the two, the Daily Observations table shall be
> the primary resolution source.
>
> ... **highest temperature recorded at the LaGuardia Airport Station** in degrees Fahrenheit on
> 19 May '26.
>
> ... available here: `https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA`
>
> This market **can not resolve until** the first data point for the following date has been
> published on the resolution source.
>
> The resolution source for this market **measures temperatures to whole degrees Fahrenheit**.

### 2.3 提取结果汇总（408 个事件）

| 字段 | 结果 |
|---|---|
| 解析站点 | **KLGA 408/408** |
| 统计量 | "highest temperature recorded" 408/408 |
| 单位 / 精度 / 取整 | `degF` / `1.0°` / `nearest_integer_f` —— 408/408 |
| 主来源 | **NOAA 50**、**WEATHER_UNDERGROUND 357**、未命名 1 |
| 备来源 | 仅 NOAA 系声明（WU Daily Observations） |
| 无数据时 | 42 个事件写明 "resolve to the lowest bracket"（NOAA 系） |
| 时区线索 | 364 个含 "11:59 PM ET" 之类具体时刻 → `America/New_York` |
| 解析状态 | **parsed 408 / partial 0 / unparsed 0** |

**两系结构差异**（这是可复现的数据事实，不是缺陷）：WU 系**不写** fallback、**不写** no-data
条款、用 "can not resolve until" 而非 "will resolve once"。解析器必须同时识别两种措辞，
否则 357/408 会被误标为 `partial`。

---

## 3. 覆盖范围

| 项 | 值 |
|---|---|
| 结算记录 | 3,723 条市场级 |
| **已结算事件** | **405** |
| **目标日期** | **404**（2025-06-20 → 2026-10-08，1 天有 2 个事件） |
| `yes_price` 全为 0/1 | 3,723 / 3,723 |
| 有观测覆盖的事件 | **405 / 405**（`daily_observations` 覆盖 2023-10-06 → 2026-10-09，1,100 天） |
| 事件无赢家 | 0 |
| 事件多赢家 | 1（即 §1.2 的 `arch-` 重复事件） |

---

## 4. 交叉核对结果

判据：观测日最高温是否落在 `yes_price = 1` 的那个括号内（含开口端正确处理）。

| 观测规则 | 匹配 | 不匹配 | 匹配率 |
|---|---:|---:|---:|
| **`all_readings`**（全部 METAR 报文，含特殊报文） | **404** | **1** | **99.75%** |
| `hourly_metar`（仅常规整点报文） | 398 | 7 | 98.3% |

### 4.1 唯一的不匹配

| 事件 | 日期 | 观测 | 判出的赢家 |
|---|---|---|---|
| `493658`（`arch-` 前缀） | 2026-05-19 | 95°F | `90-91°F` |

同日期的正本事件 `503590` 判 `95°F or below`，与观测一致。

**两个事件声明的来源相同（WU）、规则文本长度相同（1037 字符），却判出不同括号。**
`arch-` 前缀事件仅 3 个，`start=2026-05-17`（早于目标日两天），`503590` 的
`start=2026-05-19T22:14Z` **晚于其 `end=2026-05-19T12:00Z`**——是重新上架的事件。

**结论**：这是场所侧的重复上架/重新结算问题，不是我们的观测问题。
**不修改观测值去迁就它。**

### 4.2 `hourly_metar` 的 7 天全部是同一模式

| 日期 | 仅常规报文 | 全部报文 | 赢家括号 |
|---|---:|---:|---|
| 2025-10-21 | 66 | **67** | 67-68°F |
| 2025-12-02 | 40 | **41** | 41-42°F |
| 2026-06-09 | 79 | **80** | 80-81°F |
| 2026-06-14 | 89 | **90** | 90-91°F |
| 2026-08-17 | 79 | **80** | 80-81°F |
| 2026-09-20 | 71 | **72** | 72-73°F |
| （另 1 天） | | | |

**规律完全一致**：`all_readings = hourly_metar + 1`，赢家括号总是包含 `all_readings`。

**含义**：结算所据的 NOAA `Temp` 列**包含非常规（special）报文**；只取常规整点 METAR
会系统性低 1°F。这与旧库 `settlements.notes` 中的记录互相印证：

> *"routine hourly METAR only would say 71F (DISAGREES)"*

**结论**：研究用的观测聚合规则必须是 `all_readings`（全部报文），
`hourly_metar` 只能作为诊断口径，不能作为真值代理。

---

## 5. 关于旧项目"1,100 天仅 45 天吻合"

P0 阶段的旧库审计（`research/legacy-quant-review.md` §5，R1）记为：

> daily_observations trusted only if a matching official settlement with equal max_temp exists
> (only 45/1,100 days pass)

**本次审计无法复现这个 45。** 实测：

| 口径 | 天数 |
|---|---:|
| `settlements` 与 `daily_observations` 日期重叠 | **404** |
| 重叠日中 `settlements.daily_max_temp_f == all_readings` | **404**（循环，见 §1.1） |
| 重叠日中 `settlements.daily_max_temp_f == hourly_metar` | 393 |
| 重叠日中观测落入赢家括号（`all_readings`） | **404 / 405 事件** |

`1,100` 是观测覆盖的总天数（2023-10-06 → 2026-10-09），而结算只覆盖 404 天
（2025-06-20 起）。**两个分母不同**——若把"404 个重叠日"与"1,100 个观测日"相除，
会得到一个看似严重、实则只是覆盖期不同的比例。

**判定**：45 这个数字要么来自与上述都不同的第三种口径，要么是当时查询有误。
本审计**不沿用它**，改用 §4 的直接括号比对，并建议后续以本报告口径为准。
原始审计报告保留不动（`research/legacy-quant-review.md`），差异在此记录。

---

## 6. 可回答的问题（本阶段）

| 问题 | 答案 |
|---|---|
| 有多少天可靠的官方结算？ | **405 个已结算事件 / 404 个日期**，`yes_price` 全为 0/1，全部可用 |
| 有多少天有可靠观测？ | 1,100 天观测（2023-10-06 → 2026-10-09），其中与结算重叠 404 天 |
| 观测与结算一致吗？ | `all_readings` 口径 **404/405 一致**；唯一不一致是场所侧重复上架事件 `493658` |
| 哪个观测口径可用？ | **`all_readings`**；`hourly_metar` 会低 1°F（7 天），只能作诊断 |
| 哪些日期可进严格样本外评估？ | 待 P1-1 预报采集完成后由 `point_in_time` 表给出；**结算侧 404 天已全部就绪** |

---

## 7. 已知限制

- 本审计基于**旧库快照**，未向 Polymarket 或 NOAA 发起任何新请求；在线核实见
  `docs/settlement-verification.md`。
- `settlements.daily_max_temp_f` 已被判定为循环字段，后续不再作为真值输入。
- `arch-` 事件仅 3 个，样本不足以判断这是系统性问题还是个案；报告只陈述观测到的事实。
- 观测来自 IEM ASOS（`all_readings` 规则），**不等于** NOAA 官方 `Temp` 列；
  两者的一致性需要 §4 的括号比对来间接证明，而非直接等同。这一点在
  `docs/settlement-verification.md` 中展开。

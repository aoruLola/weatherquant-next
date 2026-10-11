# 气象数据源

2026-10-11。调研原始记录：`E:/Project/.p11-research/open-meteo-single-runs.md`（含逐次请求与响应）。

本文档只记录**实测确认**的结论。每条标注 `[已验证]`（实际发过请求 / 读过官方文档）或
`[推断]`。

---

## 1. 历史预报：Open-Meteo Single Runs API

### 1.1 端点

| 项 | 值 | 标注 |
|---|---|---|
| Base | `https://single-runs-api.open-meteo.com/v1/forecast` | [已验证] |
| 单数 `single-run-api` | **DNS NXDOMAIN**（不是 TLS 失败——修正 P0 审计的说法） | [已验证] |
| `api.open-meteo.com` / `archive-api.open-meteo.com` 带 `run` | HTTP 400 `Parameter 'run' must not be set` | [已验证] |

> **P0 修正**：`docs/open-source-review.md` 当时记为 "`single-run-api` 单数形式 TLS 失败"，
> 实为 DNS 不解析。结论（必须用 `single-runs-api`）不变，原因更正。

**可用请求模板**（实测 200，2026-10-10 UTC）：

```
https://single-runs-api.open-meteo.com/v1/forecast
  ?latitude=40.7794
  &longitude=-73.8803
  &hourly=temperature_2m
  &models=ecmwf_ifs025
  &run=2026-10-09T06:00
  &timezone=America%2FNew_York
  &forecast_hours=240
```

| 参数 | 说明 |
|---|---|
| `run` | **`yyyy-mm-ddThh:mm`，不带 Z**；纯日期隐含 `T00:00` |
| `models` | `ecmwf_ifs025`；GFS 用 **`gfs_seamless`** 或 `gfs_global`；**裸 `gfs` 返回 400**（`Cannot initialize MultiDomains...`） |
| `timezone` | 传 `America/New_York` 后 `hourly.time` 变为当地时间，日界线随之正确 |
| `forecast_hours` | 默认 168（7 天）；`240` = 10 天 |
| `start_date` / `end_date` | **被 400 拒绝** —— 历史范围只能由 `run` 决定 |

**不要手工把 `/` 转成 `%2F`**：`httpx` 会再转义一次变成 `%252FNew_York` → 400
`Invalid timezone`。[已验证，踩过]

### 1.2 响应结构：run 时间在哪里

响应中**没有任何 `run_time` / `initialization_time` / `models` 字段**。

- 序列**从初始化时刻起**逐小时排列，**初始化之前没有数据**（`past_hours` 无效）。
- 默认 UTC 时 `hourly.time[0]` 即初始化时间；带 `timezone` 时需用 `utc_offset_seconds`
  换算回 UTC（例如 `06:00Z` → 本地 `02:00 EDT`，`utc_offset_seconds=-14400`）。
- `generationtime_ms` 是**服务端处理耗时**，每次请求都变，不是 run 标识 ——
  `raw_response_hash` 必须剔除它，否则同一份数据每次哈希都不同。

分辨率：最细 **15 分钟**；`minutely_15` 可用；`minutely`（1 分钟）**被静默忽略并返回 200 空体**。

### 1.3 尾部 null 陷阱 [已验证]

`ecmwf_ifs025` 的 **06Z / 18Z** run 只有 `run+0 … run+146h` 有值，**第 147 点起为 null**
（扩到 240 点仍是 null）。**00Z / 12Z run 完整**（≥10 天）。所测 `gfs_seamless` 无 null。

**影响**：目标日若落在 null 尾部，`n_hours` 会偏低、`covers_full_local_day` 为 False。
这是数据事实，**不得用邻近 run 补齐**——`forecast_daily_max.n_missing_hours` 记录它。

### 1.4 历史覆盖（2026-10-10 实测，KLGA）[已验证]

| 模型 | 最早可用 | 证据 |
|---|---|---|
| `ecmwf_ifs025` | **2026-04-02** | 04-02 的 T00/T06 均 200；04-01 及更早（含 03-31、03-30、03-29、03-24、01-13、2025-12-14、2025-11-14、2025-10-10、2024-10-10）全部 400 |
| `gfs_seamless` / `gfs_global` | **2026-04-02** | 与 ifs025 窗口完全一致；04-01 及更早 400（错误体回显内部名 `ncep_gfs013`） |
| `ecmwf_ifs`（9 km HRES） | **2024-03-14** | 03-14 为 200；2024-03-13、2024-01-15 为 400 |

与官方文档 "Most models are archived from 2nd of April 2026" 吻合 —— **固定归档起始日，不是滚动清除**。

**400 的三种含义无法区分**：未来 run、超出归档、非发报时次（如 03:00），**错误体完全相同**
（`The requested model run is not available. Model: …, run: …`）。因此：

- 记录 `status='unavailable'` + **原始错误文本**；
- **不推断**是"从未存在"还是"已清除"；
- **不插值**补齐。

> **对本项目 60–90 天回填的含义**：2026-10-11 起算 90 天 = 2026-07-13，**远在归档窗口内**，
> 覆盖充足。真正的限制是归档起点 2026-04-02，**更早的历史无法回填**——如实报告，不外推。

### 1.5 可用延迟 [已验证]

观测窗 2026-10-10 17:59–18:40 UTC：

| 模型 | 12Z run 状态 |
|---|---|
| `ecmwf_ifs025` | 18:40 仍 400（**>6.6 小时**，慢于文档 4–6h 口径） |
| `ecmwf_ifs`（9km） | ≤6.5h 已 200 |
| `gfs_seamless` / `gfs_global` | ≤6.5h 已 200 |
| 18Z（三者） | 均未就绪 |

**精确首发时刻未确定**（需轮询）。因此 `publishable_at_utc` 只能估算 —— 见
[`forecast-point-in-time.md`](forecast-point-in-time.md)。

### 1.6 网格点与站点偏差 [已验证]

各模型回显的格心距 KLGA 机场：

| 模型 | 回显坐标 | 距机场 |
|---|---|---:|
| `ecmwf_ifs025` | (40.75, −74.0) | **10.60 km** |
| `gfs_seamless` | (40.774933, −73.89722) | **1.51 km** |
| `gfs_global` | (40.826553, −73.828125) | 6.84 km |
| `ecmwf_ifs` | (40.808433, −73.89206) | 3.38 km |

**要求**：回填时**断言回显坐标**在预期格心附近。换格会让同一 `model_id` 前后不可比，而
10.6 km 的偏差已大于许多待研究效应。

> `gfs025` 返回 200 但**全 null** —— 不要使用。

### 1.7 配额与署名 [已验证]

- 免费额度 **10,000 calls/天**，**非商用**，**CC BY 4.0 必须署名**（Open-Meteo pricing 页）。
- 响应**无缓存头**，相同请求字节不稳定（仅 `generationtime_ms` 变）。
- curl 突发会遭 TLS reset，Python 正常 → 客户端限速 **≤1 req/s**。
  本项目 `RateLimitRegistry` 的 `open_meteo` 设为 **6 次 / 10s**（0.6/s）。

---

## 2. 观测真值：站点日最高温

### 2.1 站号与产品 [已验证]

| 项 | 值 |
|---|---|
| **现役 NCEI 站号** | **`72503014732`**（USAF 725030 + WBAN 14732，"LA GUARDIA AIRPORT"） |
| 旧行 | `999999/14732`，1948-07-01 → 1972-12-31 |
| `72503094728` | **不是 KLGA**（94728 是 Central Park 的 WBAN），`isd-history` 无此行 |
| GHCN 站号 | `USW00014732` |

### 2.2 三个产品，可用性完全不同 [已验证]

| 产品 | 端点 | 覆盖到 | 对结算窗口 |
|---|---|---|---|
| **global-hourly（逐时 ISD）** | `https://noaa-global-hourly-pds.s3.amazonaws.com/<年>/72503014732.csv` | **2025-08-27T04:59**（2025 文件 8,813 行；**2026 文件 HTTP 404**） | ❌ **只覆盖 2025-06-20 → 2025-08-27 约 69 天** |
| LCD / isd-lite / isd-history | 各自端点 | 同样冻结在 2025-08-27 前后 | ❌ 同上 |
| **GHCN-Daily** | `.../v1?dataset=daily-summaries&stations=USW00014732&dataTypes=TMAX` | **2026-10-07（实测）** | ✅ **覆盖整个结算窗口** |

> 旧的 `ncei.noaa.gov/data/global-hourly/access/.../<站号>.csv` 路径全部 **301 到 S3 桶首页**
> —— 这解释了 P0 审计记的"API 返回空"。现行逐站 CSV 只在 S3 上。

**结论**：结算期（2025-06-20 → 2026-10-08）的**官方日最高温只能取 GHCN-Daily `TMAX`**。
global-hourly 只能作为 2025 年夏季那段的**逐时细节补充**，不能当主源。

### 2.3 列与单位 [已验证]

**没有 `Temp` 列，温度列叫 `TMP`**（实测列名：`STATION,DATE,SOURCE,LATITUDE,...,TMP,DEW,SLP,...`）。

`TMP` 格式为 **`+0089,1`** —— 数值 = **°C × 10**，第二子字段为质量码：

| 字段 | 含义 |
|---|---|
| `+0089` | **8.9 °C** |
| `,1` | 质量码 `1` = Passed all quality control checks |
| `+9999,9` | **缺测**（`+9999` = Missing） |

官方 `isd-format-document.pdf` 逐字：`UNITS: Degrees Celsius`、`SCALING FACTOR: 10`、
`+9999 = Missing`。质量码域：`0` gross-limits 通过、`1` 全部质控通过、`2` Suspect、
`3` Erroneous、`4/5/6/7` 来自 NCEI 数据源、`9` 元素存在则通过、`A/C/I/M/P/R/U` 人工/替换类。

实测 FM-15 行的质量码分布：`1` × 5648、`5` × 60、`6` × 1、`7` × 1，**无 `+9999`**。

GHCN-Daily `TMAX` 为 **0.1 °C** 整数（如 `"  256"` = 25.6 °C）；LCD 为 °F 整数。

### 2.4 "一天"的定义 —— 四个来源四种日界线 [已验证]

| 来源 | `DATE` / 日界 | DST |
|---|---|---|
| **global-hourly** | **UTC** | 无 |
| **LCD** | **LST（UTC−5）固定** | **不随 DST 切换** |
| **GHCN-Daily** | **LST 日历日** | 不适用（日聚合） |
| **IEM ASOS** | 由 `tz=` 参数决定 | `America/New_York` 会跳时/重复；`UTC` 连续 |

**实测后果**：
- 按 **UTC 日**分组会把**傍晚峰值算进次日**（2026-07-10 实测 31.1 vs 官方 28.3 °C）。
- EDT 期间本地日与 LST 日的差落在 **00:00–01:00 这一小时**。
- 逐时 `max(TMP)` 比官方日最高**低 0.5–1.1 °C**（10 天里 2 天相等）。

**⇒ 结算级复核应取 GHCN-Daily / SOD 的日聚合值，而不是对逐时 `TMP` 取 max。**
逐时 max 只能作诊断口径——这与 §3 交叉核对里 `hourly_metar` 低 1°F 的现象同源。

### 2.5 Polymarket 规则原文 [已验证]

网络恢复后**已成功取得在线规则**，并与旧库快照逐字节比对：

| 来源 | event_id | rules_len | SHA-256 前缀 |
|---|---|---:|---|
| **在线** `series_slug=nyc-daily-weather` | `1158124` | 2366 | `91e41742fa3a965f` |
| **旧库快照**（2026-10-10） | `1158124` | 2366 | `91e41742fa3a965f` |

**完全一致 —— 规则未发生漂移**，快照可作为权威依据。

在线规则（节选，逐字）：

> This market will resolve to the temperature range that contains the highest temperature
> recorded by NOAA at the LaGuardia Airport Station in degrees Fahrenheit on 10 Oct '26.
>
> The resolution source for this market will be information from NOAA, specifically the highest
> reading under the "Temp" column for all times on this day, available here:
> `https://www.weather.gov/wrh/timeseries?site=klga`
>
> This market will resolve off of the Hourly Data provided using the "Show Hourly Data" button.
>
> If NOAA data for the observation date is unavailable by 11:59 PM ET on the day following the
> observation date, the Weather Underground Daily Observations table will be used...

**发现路径**：`series_slug=nyc-daily-weather` 是精确入口。
**`tag_id=84`（Weather）不可单独使用** —— 实测该标签下混入了年度气温排名、
NASA GISS 指数、甚至"太空数据中心"等非目标市场。P1-0 采集器已用 series 主路径 + tag 兜底，
本节验证了该设计正确。

**结算接口**：`GET /v2/resolutions?condition=<conditionId>` 返回
`status="resolved"`、`was_disputed`、`proposed_price`（实测 200）。
已结算市场 `outcomePrices` 为 `["0","1"]`。

## 3. 尚未确认 / 待补

| 项 | 状态 |
|---|---|
| NOAA 列名与单位 | ✅ **已实测**：列是 `TMP` 不是 `Temp`，单位 °C×10 + 质量码（§2.3） |
| 官方日最高温产品 | ✅ **已实测**：**GHCN-Daily `TMAX`** 覆盖到 2026-10-07，是结算期唯一可用主源；LCD/global-hourly 冻结在 2025-08-27（§2.2） |
| 各源"一天"的定义 | ✅ **已实测**：global-hourly=UTC、LCD=LST 固定、GHCN=LST 日历日、IEM 取决于 `tz=`（§2.4） |
| Polymarket 在线规则原文 | ✅ **已取得并核验**：在线 SHA 与旧库快照**逐字节一致**，规则未漂移（§2.5） |
| IEM ASOS 逐时 max 与 GHCN 日最高的差 | ⚠️ 已知低 0.5–1.1 °C（10 天样本），未做全量比对 |
| Open-Meteo run 的精确首发时刻 | 未确定（需轮询），故 `publishable_at` 只能估算 |
| `ecmwf_ifs025` 06Z/18Z 尾部 null 的成因 | 未确定；只记录现象，不推断 |

---

## 4. 使用约束

1. **不得用 ERA5 / 再分析 / 实际观测替代历史预报。** Single Runs API 是唯一合法来源。
2. **不得把 `run_time_utc` 当作 `publishable_at_utc`。** 见
   [`forecast-point-in-time.md`](forecast-point-in-time.md)。
3. **缺失 run 记为 `unavailable` 并保留原始错误，不插值。**
4. **raw hash 必须剔除 `generationtime_ms`。**
5. **回填前断言回显坐标。**

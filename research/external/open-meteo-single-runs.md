# Open-Meteo Single Runs API 实测报告

- 实测时间（UTC）：2026-10-10 17:55 – 18:40（另有少量 19:00 后复核）
- 实测工具：`curl` + Python `urllib`（全部为真实调用；文档引用均来自 curl 抓取的官方页面 HTML）
- 目标点：LaGuardia (KLGA) `latitude=40.7794&longitude=-73.8803`
- 标注约定：`[已验证]` = 本次实际调用或读取官方文档所得；`[推断]` = 由证据推断、未直接证实；`[未确定]` = 无法确定

---

## 1. 端点与参数（含可直接复用的请求 URL）

### 1.1 Base URL

- 正确主机：`https://single-runs-api.open-meteo.com`，路径 `/v1/forecast`。[已验证]
  - 官方文档页面（https://open-meteo.com/en/docs/single-runs-api ）中出现的 API 地址正是 `https://single-runs-api.open-meteo.com/v1/forecast`。[已验证]
- 单数形式 `single-run-api.open-meteo.com`：**DNS 直接不存在（NXDOMAIN）**，curl 报 `Could not resolve host`，`nslookup` 报 `Non-existent domain`。[已验证]
  - **纠正此前审计**：不是"TLS 失败"，而是域名根本未解析（DNS 层失败，握手都不会发生）。
- `api.open-meteo.com`（实时预报 API）**拒绝** `run` 参数：
  ```json
  {"reason":"Parameter 'run' must not be set","error":true}
  ```
  HTTP 400。[已验证]
- `archive-api.open-meteo.com` 同样拒绝 `run` 参数（HTTP 400，`{"reason":"Parameter 'run' must not be set","error":true}`）；且当 `run` 与 `start_date` 同时出现时返回 400 `{"error":true,"reason":"Parameter 'start_date' must not be set"}`（此时提示指向 start_date，易误导——单独去掉 start_date 后错误才明确指向 run）。[已验证]
- 结论：**只有 single-runs 主机接受 `run=`**，三个主机分工明确，不存在按模型分主机。[已验证]

### 1.2 必选 / 可选参数

| 参数 | 状态 | 说明 |
|---|---|---|
| `latitude`/`longitude` | 必填 | 请求坐标；**响应中回显的是选中的网格中心，不是请求坐标**（见 §6）[已验证] |
| `run` | **必填** | 缺失时 400：`{"error":true,"reason":"Parameter 'run' is required"}` [已验证] |
| `hourly` / `minutely_15` / `daily` | 数据选择 | 见下；`minutely`（1 分钟）会被**静默忽略**：返回 200 但响应里没有任何数据块（只有 latitude/longitude/…/elevation 几个元字段）——这是个坑，取数后必须断言 `hourly` 或 `minutely_15` 键存在。[已验证] |
| `models` | 强烈建议显式给出 | 不传时默认走 best-match：KLGA 处实测等于 `gfs_seamless`（逐值比对与 `models=gfs_seamless` 完全一致）。[已验证] |
| `timezone` | 可选，默认 GMT | 见 §2.3 |
| `forecast_days` | 可选，默认 7 | 文档 UI 写 "7 days (default)"；实测默认 168 个小时点。`forecast_days=10`、`forecast_hours=240` 均被接受（返回 240 点）。[已验证] |
| `forecast_hours` / `past_hours` | 可选 | 文档明确支持。实测 `past_hours=6` **不会**把时间轴前移到 init 之前（仍从 run 时刻开始、168 点不变）——init 之前没有数据。[已验证] |
| `start_date`/`end_date` | **禁止** | 400：`{"error":true,"reason":"Parameter 'start_date' must not be set"}`。**历史范围只能靠 run 选定，窗口 = run 起 7 天（默认）**。[已验证] |
| `timeformat` | 可选 | `timeformat=unixtime` 生效（time 变为 epoch 秒数组）。[已验证] |
| `cell_selection` | 可选 | `nearest` 被接受；文档默认 "Terrain Optimised, Prefers Land"。[已验证] |
| `apikey` | 免费档不需要 | 本次所有 200 均未带 key；文档：商用才需要 apikey（客户端点 `customer-api.open-meteo.com`）。[已验证] |

### 1.3 `run` 的取值格式

- 规范格式 `yyyy-mm-ddThh:mm`（UTC 初始化时间），如 `run=2026-10-09T06:00`。[已验证]
- `run=2026-10-09T06`（省略分钟）→ 200，等价于 06:00。[已验证]
- `run=2026-10-09T06:00:00`（带秒）→ 200。[已验证]
- `run=2026-10-09T06:00Z`（带 Z）→ 400 `{"error":true,"reason":"Bad Request"}`，**不要带 Z**。[已验证]
- 纯日期 `run=2026-10-09` → 200，**隐含 T00:00**（响应从 `2026-10-09T00:00` 开始）。[已验证]
- 非发布周期的小时（如 `2026-10-09T03:00`）→ 400，且错误文本与"被清理/未来时间"**完全相同**（见 §3.4）。[已验证]

### 1.4 可直接复用的请求 URL（均实测 200）

```text
# ECMWF IFS 0.25°，指定 run，UTC 时区，默认 7 天
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&hourly=temperature_2m&models=ecmwf_ifs025&run=2026-10-09T06:00

# GFS（用 gfs_seamless 或 gfs_global；注意 models=gfs 是无效值）
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&hourly=temperature_2m&models=gfs_seamless&run=2026-09-10T06:00

# 本地时区（hourly.time 变为纽约本地时间）
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&hourly=temperature_2m&models=ecmwf_ifs025&run=2026-10-09T06:00&timezone=America%2FNew_York

# 拉满 10 天（240 小时点）
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&hourly=temperature_2m&models=ecmwf_ifs025&run=2026-09-10T00:00&forecast_hours=240

# 15 分钟分辨率
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&minutely_15=temperature_2m&models=gfs_seamless&run=2026-10-09T06:00

# 多模型一次取（键名自动带模型后缀；任一模型缺该 run 则整体 400）
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&hourly=temperature_2m&models=ecmwf_ifs025,gfs_seamless&run=2026-10-09T06:00

# daily 聚合：仅当 run 在"请求时区的 00:00"时可用
https://single-runs-api.open-meteo.com/v1/forecast?latitude=40.7794&longitude=-73.8803&daily=temperature_2m_max,temperature_2m_min&models=ecmwf_ifs025&run=2026-10-09T00:00
```

`daily` 在非午夜 run 下的报错（原文）：`{"error":true,"reason":"Parameter 'daily' is only supported for single-runs-api if 'run' starts at 00:00 in the requested timezone"}`。[已验证]

### 1.5 模型 ID 实测有效性

| `models=` 值 | 结果 |
|---|---|
| `ecmwf_ifs025` | 200，网格回显 (40.75, -74.0) [已验证] |
| `ecmwf_ifs`（9 km HRES） | 200，网格回显 (40.808434, -73.89206)；历史深至 2024-03-14 [已验证] |
| `gfs_seamless` | 200，网格 (40.774933, -73.89722) [已验证] |
| `gfs_global` | 200，网格 (40.826553, -73.828125) [已验证] |
| `gfs_hrrr` | 200，网格与 gfs_seamless 相同 [已验证] |
| `gfs025` | 200 但 **temperature_2m 全为 null**（两个不同 run 均如此），勿用，原因[未确定] [已验证] |
| `gfs` | 400 `{"error":true,"reason":"Invalid value: Cannot initialize MultiDomains from invalid String value gfs"}` —— **不存在裸 `gfs` 这个 ID** [已验证] |
| `gfs_graphcast025` | 内部解析为 `ncep_gfs_graphcast025`，所测 run（06Z）均 400 [已验证] |
| `gdas_seamless` | 400 Invalid value [已验证] |

> 注意：GFS 系列报错时回显的内部模型名是 `ncep_gfs013`（gfs_seamless/gfs_global 的 400 均如此），说明 seamless 内部落到该模型；这是取错误日志时会看到的名字。[已验证]

---

## 2. 响应结构：初始化时间、小时分辨率、时区

### 2.1 顶层结构（verbatim，数组已截断）

```json
{
  "latitude": 40.75,
  "longitude": -74.0,
  "generationtime_ms": 0.2912282943725586,
  "utc_offset_seconds": 0,
  "timezone": "GMT",
  "timezone_abbreviation": "GMT",
  "elevation": 2.0,
  "hourly_units": { "time": "iso8601", "temperature_2m": "°C" },
  "hourly": {
    "time": ["2026-10-09T06:00", "2026-10-09T07:00", "...（共 168 个）...", "2026-10-16T05:00"],
    "temperature_2m": [12.4, 12.0, "..."]
  }
}
```

顶层键恒为：`latitude, longitude, generationtime_ms, utc_offset_seconds, timezone, timezone_abbreviation, elevation, hourly_units, hourly`（daily 请求则为 `daily_units, daily`；minutely_15 请求则为 `minutely_15_units, minutely_15`）。[已验证]

### 2.2 初始化时间在响应里**没有专门字段**

- **没有** `run_time` / `initialization_time` / `run` / `models` 之类的字段。[已验证]
- 默认（UTC）下，**run 初始化时间 == `hourly.time[0]`**；整段序列从 init 时刻起、逐小时排列。[已验证]
- `generationtime_ms` 是**服务端本次生成响应的耗时**（0.001–5 ms 量级，每次请求都变），与初始化时间无关，不能当 run 标识。[已验证]
- 因此落库时必须自行记录 `(models, run, timezone)` 三元组；响应本身不自描述 run 与模型（多模型时只能靠键后缀 `temperature_2m_<model>` 反推）。[已验证]

### 2.3 时区语义

- 默认 `timezone` 未传 → `GMT`（`utc_offset_seconds=0`），`hourly.time` 为 UTC。[已验证]
- `timezone=America%2FNew_York` 实测（verbatim 关键字段）：

```json
{
  "latitude": 40.75,
  "longitude": -74.0,
  "generationtime_ms": 0.27298927307128906,
  "utc_offset_seconds": -14400,
  "timezone": "America/New_York",
  "timezone_abbreviation": "GMT-4",
  "elevation": 2.0,
  "hourly_units": { "time": "iso8601", "temperature_2m": "°C" },
  "hourly": {
    "time": ["2026-10-09T02:00", "2026-10-09T03:00", "...", "2026-10-16T01:00"],
    "temperature_2m": ["..."]
  }
}
```

  - **首个时间戳 = run init 的本地表示**（06:00Z → 02:00 EDT），不是本地午夜；init 之前的本地小时同样缺失。[已验证]
  - "本地日期"因此由 `timezone` 决定：同一 run 在 `America/New_York` 下 `2026-10-09` 这一天只含 02:00–23:00 共 22 个小时。做"本地日聚合"时务必固定 timezone 参数，否则日界线漂移。[已验证]
- `timezone=auto` → 按坐标推断时区，KLGA 实测等价于 America/New_York（首点 `2026-10-09T02:00`）。[已验证]
- 非法时区 → 400 `{"error":true,"reason":"Invalid timezone"}`。[已验证]

### 2.4 分辨率与起止

- 最细可用 = **15 分钟**（`minutely_15`），实测 7 天窗口 = 672 点，首点 `2026-04-02T06:00`（老 run 同样支持）。[已验证]
- `minutely`（1 分钟）：参数被忽略、无数据块（见 §1.2）；官方文档中也只出现 hourly / 15-minutely 控件。**结论：最细 15 分钟，无亚 15 分钟数据** [已验证 + 文档][推断无更细粒度]
- 起点 = run init；终点默认 init+7d（168 点），`forecast_hours=240` 可扩到 init+10d。init 之前无任何数据（`past_hours` 不生效）。[已验证]
- **尾部空值陷阱**（重要）：
  - `ecmwf_ifs025`：**00Z 与 12Z 的 run** → 默认 168 点全有值，`forecast_hours=240` 也全有值（≥10 天完整）；**06Z 与 18Z 的 run** → 第 147 点起为 null（数据只到 run+146h ≈ 6.1 天），扩到 240 点 null 从 147 延续到 240（15 分钟数据同样在 147h 处截断：672 点中 84 个 null）。多个日期（2026-04-02、04-03、04-08、07-12、08-11、09-10、10-09、10-10）复现同一模式。[已验证]
  - `ecmwf_ifs`（9 km）：06Z run 在第 145 点起 null（数据到 run+144h = 整 6 天）；00Z/12Z 完整。[已验证]
  - `gfs_seamless` / `gfs_global`：所测全部日期（30/60/90/180 天前、近两天）168 点**无 null**。[已验证]
  - 截断机制[未确定]；回填管线必须容忍尾部 null，并优先用 00Z/12Z run 覆盖第 7 天。

---

## 3. 历史覆盖实测（ecmwf_ifs025 / gfs 分别列出测试日期与结果）

> 全部为 2026-10-10（UTC）实测，坐标 KLGA。状态含义：200 = 该 run 可取回完整（默认 7 天）窗口；400 = 不可得。

### 3.1 `ecmwf_ifs025`

| run（默认 T06:00，除注明外） | 距今 | HTTP | 错误体（verbatim，截断） |
|---|---|---|---|
| 2026-10-10T00:00 / T06:00 | 当天 | 200 | — |
| 2026-09-10 | 30 天 | 200 | — |
| 2026-08-11 | 60 天 | 200 | — |
| 2026-07-12 | 90 天 | 200 | — |
| 2026-04-13 | 180 天 | 200 | — |
| 2026-04-08 | 185 天 | 200 | — |
| 2026-04-03 | 190 天 | 200 | — |
| **2026-04-02**（T00:00 与 T06:00 都测） | 191 天 | **200** | — |
| **2026-04-01**（T00:00 与 T06:00 都测） | 192 天 | **400** | `The requested model run is not available. Model: ecmwf_ifs025, run: 2026-04-01T06:00Z` |
| 2026-03-31 / 03-30 / 03-29 | 193–195 天 | 400 | 同上格式 |
| 2026-03-24 | 200 天 | 400 | 同上 |
| 2026-01-13 | 270 天 | 400 | 同上 |
| 2025-12-14 | 300 天 | 400 | 同上 |
| 2025-11-14 | 330 天 | 400 | 同上 |
| 2025-10-10 | 365 天 | 400 | `... Model: ecmwf_ifs025, run: 2025-10-10T06:00Z` |
| 2024-10-10 | 730 天 | 400 | 同上 |

**边界 = 2026-04-02（当天可用，2026-04-01 及更早一律 400）**，与官方文档原文一致：

> "Most models are archived from 2nd of April 2026. ECMWF IFS HRES at 9 km is available from March 2024." —— open-meteo.com/en/docs/single-runs-api [已验证]

即这是一个**固定的归档起始日，不是滚动清除窗口**（docs 用 "archived from"；两个不同模型共享同一条 04-02 边界也支持这一点）。[已验证 边界数值；已验证 固定起始日的解释来自官方文档]

### 3.2 `gfs_seamless` / `gfs_global`（裸 `gfs` 无效，见 §1.5）

`gfs_seamless`：

| run（T06:00） | 距今 | HTTP |
|---|---|---|
| 2026-10-10T00/06/12 | 当天 | 200（12Z 见 §4） |
| 2026-09-10 | 30 天 | 200 |
| 2026-08-11 | 60 天 | 200 |
| 2026-07-12 | 90 天 | 200 |
| 2026-04-13 | 180 天 | 200 |
| 2026-04-05 | — | 200 |
| **2026-04-02** | 191 天 | **200** |
| **2026-04-01** | 192 天 | **400** `... Model: ncep_gfs013, run: 2026-04-01T06:00Z` |
| 2026-03-29 / 2026-02-13 / 2026-01-13 / 2025-12-13 | 195–281 天 | 400 |
| 2025-10-10 | 365 天 | 400 |
| 2024-10-10 | 730 天 | 400 |

`gfs_global` 抽查：2026-04-01 → 400；**2026-04-02 → 200**；2025-10-10 → 400。[已验证]

**结论：GFS 与 ecmwf_ifs025 的可取窗口完全一致，起点同为 2026-04-02。**[已验证]

### 3.3 例外：`ecmwf_ifs`（9 km，历史深得多）

| run | HTTP |
|---|---|
| 2026-10-09T06:00（近端） | 200 |
| 2025-06-01T00:00 | 200 |
| 2024-06-15T00:00 | 200 |
| **2024-03-15T00:00** | **200** |
| **2024-03-14T00:00** | **200** |
| 2024-03-13T00:00 | 400 |
| 2024-01-15T00:00 | 400 |

边界恰为 **2024-03-14**，与文档 "available from March 14, 2024 (IFS Cycle 49R1 hindcasts)"、"From May 12, 2026 06 UTC, runs use the updated IFS Cycle 50R1" 一致。[已验证]

> 若研究需要 2026-04-02 之前的数据，`ecmwf_ifs025` 不存在，只能退到 `ecmwf_ifs`（9 km，且 06/18Z 有 §2.4 的 6 天截断）。二者网格也不同（§6）。此前审计"最新 run = 06Z、12Z 400"的观测（2026-10-09 17:00 UTC）与本次 2026-10-10 17:59–18:38 UTC 的观测模式完全一致（详见 §4）。[已验证 行为一致；对 10-09 历史时刻本身无法回放，标注为一致而非复验]

### 3.4 错误语义：无法区分"从未存在"与"已清除"

以下情形返回**完全相同**的 400 与 reason 格式（仅 run 值不同）：
- 未来/尚未发布的 run（如 10-10T12:00）
- 超出归档起点的历史 run（2025-10-10）
- 不存在的发报时次（2026-10-09T03:00）

```json
{"error":true,"reason":"The requested model run is not available. Model: ecmwf_ifs025, run: 2026-10-09T03:00Z"}
```
[已验证] 三类对照实测。**判断"是否存在"只能靠归档起始日 + 发报周期表，不能靠错误码。**另：多模型请求是"全有或全无"——任一模型缺该 run，整个请求 400（实测 `models=ecmwf_ifs025,gfs_graphcast025&run=2026-10-09T06:00` 报 graphcast 缺失）。[已验证]

---

## 4. 运行可用延迟实测

官方文档原文（open-meteo.com/en/docs/single-runs-api）：

> "The &run= parameter specifies the model's initialisation time — the UTC reference time at which the observations are taken — not the time at which the forecast output becomes publicly available. After initialisation, the model requires additional computation time before results are distributed: typically 4–6 hours for global models (e.g. ECMWF IFS, GFS) and 1–3 hours for regional models. This means a run initialised at 00 UTC is generally accessible from approximately 04–06 UTC onwards. The exact availability times for each model are listed on the model updates page." [已验证 文档原文]

另（model-updates 页）：Open-Meteo 多服务器最终一致，"please wait 10 minutes after the availability time"。[已验证 文档原文]

**初始化 ≠ 发布**由此确证。[已验证]

本次采样（观测窗 2026-10-10 17:59–18:38 UTC）：

| 模型 | run | 初始化后经过 | 观测时刻 (UTC) | 状态 |
|---|---|---|---|---|
| ecmwf_ifs025 | 10-10T00:00 | ~18h | 17:59 | 200 |
| ecmwf_ifs025 | 10-10T06:00 | ~12h | 17:59 | 200 |
| ecmwf_ifs025 | 10-10T12:00 | 6h15m / 6h36m / 6h37m / 6h40m | 18:15 / 18:36 / 18:37 / 18:40 | **400（>6.6h 仍不可用）** |
| ecmwf_ifs025 | 10-10T18:00 | ~0.3h | 18:15 | 400 |
| ecmwf_ifs025 | 10-09T12:00 | ≤25.5h | 18:20 | 200（已可见，具体首发时刻未知） |
| ecmwf_ifs025 | 10-09T18:00 | ≤24.3h | 18:20 | 200 |
| ecmwf_ifs（9 km） | 10-10T12:00 | **≤6.6h** | 18:36 | **200** |
| gfs_seamless | 10-10T12:00 | **≤6.5h** | ~18:30 | **200** |
| gfs_global | 10-10T12:00 | **≤6.5h** | ~18:30 | **200** |
| gfs_seamless / gfs_global | 10-10T18:00 | ~0.6h | 18:36 | 400 |
| gfs_seamless | 10-10T00:00 / T06:00 | ≤18h / ≤12.5h | 18:20 | 200 |

结合此前审计（2026-10-09 17:00 UTC：ifs025 最新可用 run 为 06Z，12Z/18Z 400 → 当日 12Z 延迟 >5h），可得：

- **ecmwf_ifs025：12Z run 在 18:37 UTC 仍不可用（>6.6h），明显慢于文档"4–6 小时"的口径**；同日 `ecmwf_ifs`（9 km）与 GFS 的 12Z 在 ≤6.5–6.6h 已可用。[已验证]
- 每个 run 的**精确首发时刻**单点探测测不出来（只能给出上界），需回填时轮询记录；此项 [未确定]（建议：回填程序对每个目标 run 每 30 分钟探测一次，首个 200 即记 `first_seen_at`）。
- GFS 18Z、ECMWF 18Z 均在观测窗内未就绪（其发布约在 init+3–6h，观测窗内 init 才过 0.6h，无信息量）。[已验证]

---

## 5. 速率限制与署名要求

均来自官方 pricing 页（open-meteo.com/en/pricing，curl 抓取）[已验证 文档原文]：

- 免费 API："The free API is for non-commercial use, rate-limited to **10,000 calls/day**, and carries no uptime guarantee."
- 用量统计门户尚在开发："Until it is available, **monthly limits are not enforced**."
- 计费口径：一次 HTTP 请求 ≈ 1 call；**单点 >10 个变量或时间跨度 >2 周计为多 call**（例：2 周 15 变量 = 1.5 call）。7 天单 run 请求 = 1.0 call——回填 190 天 × 4 run/天 × 2 模型 ≈ 1520 call，远低于 10k/天上限。[推断 基于文档计费规则与本次请求形态]
- 商用：需订阅 + `apikey`，端点换 `customer-api.open-meteo.com`（语法相同）。pricing FAQ 另称 "Historical, climate, ensemble… APIs require the Professional API Plan"——但**本 API 全程无 key 实测 200**，single-runs 目前可免费无 key 使用。[已验证 实测；文档口径张力已如实记录]
- **署名**："Open-Meteo relies on open data that is licenced under Attribution 4.0 International (**CC BY 4.0**)… attribution is a requirement under this licence."（非商业也必须署名。）[已验证 文档原文]

### CDN / 缓存 / raw_response_hash

- 响应头仅见 `Date, Content-Type, Connection, Transfer-Encoding`——**无 `Age`/`ETag`/`Cache-Control`/`CF-Cache-Status`/`Via`，也无限速头**。[已验证]
- 相同请求间隔 1.5–30 秒连发两次：**字节级不相同**（sha256 不同），但差异**只在 `generationtime_ms`**；去掉该字段后 JSON 完全一致、`hourly` 数值逐点相同。[已验证]
  - 结论：`raw_response_hash` **必须在剔除 `generationtime_ms` 后计算**，否则永远不稳定。[已验证]
- 未观测到 429；但 2026-10-10 ~18:15 起连续 curl 突发（约 15 分钟 60+ 请求）后，curl 对该主机 TLS 握手持续被 reset（`Recv failure: Connection was reset`）5 分钟以上，而**同一时刻 Python（不同 TLS 指纹/UA）仍 200**，1.2 秒间隔连发约 50 请求无异常。[已验证 观察现象] 触发机制（WAF 指纹、突发限速或 UA）[推断]——工程建议：≤1 req/s 均匀发散、连接 reset 时退避重试、勿用突发。

---

## 6. KLGA 网格点选取与站点偏差

请求坐标 `40.7794, -73.8803`（KLGA 跑道区）；**响应回显的是选中网格中心**，各模型差异显著（球面距离，[已验证] 计算）：

| 模型 | 响应 latitude/longitude | 距 KLGA | 备注 |
|---|---|---|---|
| `ecmwf_ifs025` | `40.75, -74.0` | **10.60 km**（西南） | 0.25° 点阵吸附；`cell_selection=nearest` 结果相同（默认已是该格）[已验证] |
| `ecmwf_ifs`（9 km） | `40.808434, -73.89206` | 3.38 km | 非规则经纬网格的格心 [推断：IFS 原生格] |
| `gfs_seamless` / `gfs_hrrr` / 默认 best-match | `40.774933, -73.89722` | **1.51 km** | 最贴近机场 [已验证] |
| `gfs_global` | `40.826553, -73.828125` | 6.84 km | 坐标不在 0.25° 点阵上 [推断：GFS 格原点偏移或内部名 ncep_gfs013 的格网] |
| `gfs025` | `40.75, -74.0` | 10.60 km | 与 ifs025 同坐标且数据全 null，不可用 [已验证] |

- 所有响应 `elevation` 均为 `2.0`（m）。Open-Meteo 文档：网格选择基于 "90-meter digital elevation model"（`land` 偏好与请求坐标高程相近的陆地格）。[已验证 文档] 因此 elevation 是 DEM 地形高程，不是模式地面高度，也不是机场标高；用它验证格点身份意义有限。[推断]
- 文档对 `cell_selection` 的原文："land finds a suitable grid-cell on land with similar elevation to the requested coordinates using a 90-meter digital elevation model. sea prefers grid-cells on sea. nearest selects the nearest possible grid-cell." 默认 "Terrain Optimised, Prefers Land"。[已验证 文档]
- **机场代表性结论**：
  - GFS seamless（≈HRRR 格）1.5 km，基本可视为机场格；GFS global 偏 6.8 km；**ECMWF ifs025 偏 10.6 km（西南，隔河的泽西/哈德逊一侧），存在站点-网格偏差风险**，尤其是日最高/最低温与海风影响下的机场。[已验证 距离；偏差幅度未测，属风险提示]
  - 实操要求：每次响应**断言回显 `latitude/longitude` 等于该模型的预期格心**（上表），防止上游改吸附逻辑后数据集悄悄换格。[推断 建议]
  - 想验证偏差量级：可把同一 run 的 `ecmwf_ifs025` 与 `gfs_seamless` 并排（多模型键后缀）取回对照（§1.4 URL，实测 200）。[已验证]

---

## 7. 未决问题 / 风险

1. **ecmwf_ifs025 各时次精确发布时刻**：仅有上界（12Z >6.6h @ 2026-10-10），首发时刻 [未确定]；回填时需轮询记录 `first_seen_at`（见 §4 建议）。
2. **06Z/18Z run 尾部截断机制**（ifs025 ≈6.1 天、ecmwf_ifs 整 6 天，00/12Z 却完整 ≥10 天）[未确定]。风险：若研究需要"run 后第 7 天"的逐时值，06/18Z run 拿不到——需用 00/12Z run 补，或接受 null。是否为上游 ECMWF 发布差异所致 [推断]。
3. **2026-04-02 归档起点的性质**：官方措辞 "archived from" 指向固定起始日（非滚动窗口），即数据集下界会随时间**保持 2026-04-02 不漂移**；但"更早的 run 会不会因其他策略被再清除/补录" [未确定]，建议隔周复测 2026-04-01/02 边界与若干深日期。`ecmwf_ifs` 的 2024-03-14 边界同理。
4. **`gfs025` 全 null**：两个 run 均空 [已验证]，原因 [未确定]；勿纳入数据集。
5. **响应不自描述 run/模型**（§2.2）：多模型整体失败（§3.4）、错误码不区分"从未存在/已清除/未来"（§3.4）——管线必须自带 manifest 并容忍 400。
6. **CDN 语义**：无任何缓存头，`generationtime_ms` 每次变化 [已验证]；是否存在透明中间缓存（可能影响 `Age` 语义之外的不可见层）[推断未观测]。哈希必须剔除 `generationtime_ms`（§5）。
7. **限速机制**：文档仅"10,000 calls/day"；突发导致 curl TLS reset、Python 正常的具体规则 [推断未证实]。稳妥做法：串行 ≤1 req/s + 重试退避。
8. **站点-网格偏差量化**（§6）：ifs025 格距机场 10.6 km，偏差方向与幅度 [未确定]，建议先用机场 METAR/NWS 站做一次同期对照再决定是否接受该格。
9. **本地日定义**：`timezone` 决定"local date"的日界线（§2.3）；回填时必须把 timezone 固定写进任务配置并存档，否则同一 run 两种日聚合结果不可比。[已验证 机制]
10. **此前审计复核结论**：正确主机 ✓（但单数域名是 DNS 失败而非 TLS 失败，已纠正）；init ≠ 发布 ✓（文档 + 实测）；"10-09 17:00 最新 run=06Z、12Z 400"与本次同时段行为一致 ✓（10-09 时点本身不可回放，标为行为一致）。

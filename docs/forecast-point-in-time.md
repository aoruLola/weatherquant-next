# 时间点可用性：一条预报什么时候才算"当时可得"

2026-10-11。实现：`weatherquant.weather.daily_max.point_in_time`、
`weatherquant.weather.backfill`。

---

## 1. 三个时刻，绝不能混为一谈

| 字段 | 含义 | 来源 |
|---|---|---|
| `run_time_utc` | 模型**初始化**的时刻 | Open-Meteo 响应的 `hourly.time[0]`（换算 UTC） |
| `publishable_at_utc` | 这次运行**最早可以用于决策**的时刻 | **估算**，见 §2 |
| `decision_deadline_utc` | 对该目标日做严格回溯决策时，预报**必须在此之前可得**的时刻 | 本地日**开始**时刻，见 §3 |

**`run_time_utc` ≠ `publishable_at_utc`。** 初始化时刻不是发布时刻：数据要经过生产、
分发、入库才会可得。把两者当成同一个值，等于假设预报在生成的瞬间就可用 —— 这正是
本项目要防的时间泄漏。

规范要求的字段：`run_time_utc`、`publishable_at_utc`、`fetched_at_utc`、`valid_time_utc`、
`lead_hours`、`station`、`temperature_2m`、`source`、`raw_response_hash` —— 全部落库。

---

## 2. 发布时间只能估算，且必须标记

Open-Meteo Single Runs API **不返回任何发布时刻**（见
[`weather-data-sources.md`](weather-data-sources.md) §1.2）。因此：

```sql
publishable_basis  = 'estimated'   -- 不是 'official'
publishable_lag_hours = 12.0
publishable_note   = "publication time is not provided by the endpoint; estimated as
                      run_time + 12h, chosen to exceed the slowest availability measured
                      while verifying the API"
```

**为什么是 12 小时**：实测（2026-10-10）`ecmwf_ifs025` 的 12Z run 在 **6.6 小时后**仍不可用，
`gfs_seamless` 与 `ecmwf_ifs` 约 6.5 小时可用；官方文档口径为 4–6 小时。
取 12 小时是在**实测最慢值之上再留余量**的保守延迟 —— 它是安全边界，不是测量值。

宁可少用一个可用的 run，也不能把一个当时还不可得的 run 放进严格历史。

**`publishable_basis` 只有三种取值**：

| 值 | 含义 | 能否进严格回测 |
|---|---|---|
| `official` | 提供方给出明确发布时刻 | 能 |
| `estimated` | 按官方运行规律推算，`publishable_note` 记录依据与保守延迟 | 能，但**必须标注为估算** |
| `unknown` | 无法界定 | **不能** |

`unknown` 永远不合格：**无法证明当时可得的预报，不得用于严格时间点回测。**

---

## 3. 判定规则：截止点是目标日的**开始**，不是结束

```
eligible  ⟺  publishable_at_utc ≤ start_of(target_date, America/New_York)
             且 publishable_basis ≠ 'unknown'
             且 publishable_at_utc ≥ run_time_utc
```

**为什么不是"目标日结束"**：目标日**进行中**发布的预报，可能已经包含目标日已发生的
实测数据（模型看到当天的部分实况）。用它去回溯"当天的决策"，无论决策时点怎么选都是
泄漏。截止点取**当地日开始**，是唯一不给这种泄漏留口子的定义。

第三条 `publishable_at_utc ≥ run_time_utc` 是**推导正确性**检查：发布早于初始化
说明估算过程有 bug，不是"预报很早"。

### DST 对截止点的影响

截止点由 `local_day_bounds_utc()` 用 `zoneinfo` 计算，不是 `date + 24h`：

| 2026 日期 | 当地日长度 | 截止点（UTC） |
|---|---:|---|
| 03-07（冬令时） | 24h | 2026-03-08 **05:00Z** |
| **03-03-08（春令时）** | **23h** | 2026-03-09 **04:00Z** |
| 07-01（夏令时） | 24h | 2026-07-02 **04:00Z** |
| **11-01（秋令时）** | **25h** | 2026-11-02 **05:00Z** |

若按固定 24 小时推，转换日的截止点会错 1 小时 —— 恰好落在最需要精确的边界上。
测试：`tests/test_daily_max.py::test_spring_forward_bounds_shift_by_an_hour` 等。

---

## 4. 存储与查询

`point_in_time` 表每行一个 `(provider, model, run_time_utc, target_date, station)`：

```sql
-- 可进入严格样本外评估的 (模型, 目标日) 组合
SELECT model, target_date, COUNT(*)
FROM point_in_time
WHERE eligible AND publishable_basis = 'estimated'
GROUP BY 1, 2;
```

`ineligible_reason` 记录**为什么**不合格，例如：

```
publishable 2026-07-01T12:00:00+00:00 is 12.0h after the target day began
    (2026-07-01T04:00:00+00:00)
publication time unknown
publishable_at precedes run_time
```

**不合格的行照常入库**，只是 `eligible = FALSE`。删掉它们会让覆盖看起来更好而实际更差。

---

## 5. 对"哪些日期可进严格评估"的影响

判定是**逐 run × 逐目标日**的，不是按模型一刀切：

- 同一目标日的**多次运行**分别判定 —— 早上 00Z 的 run 可能合格、当天 12Z 的不合格。
  这正是"同一目标日的不同预报运行不能混淆"的落地。
- 因此"某模型有多少天可用"是一个**按 run 分布**的量，不是一个布尔值。
- 汇总见 [`../research/forecast-coverage.md`](../research/forecast-coverage.md)。

---

## 6. 明确不做

| 不做 | 原因 |
|---|---|
| 用 ERA5 / 再分析替代历史预报 | 后见之明，不是当时的预报 |
| 用实际观测替代预报 | 同上 |
| 把 `run_time` 当作 `publishable_at` | 假设了瞬间可得 |
| 对不可用的 run 插值补一个预报 | 制造了从未存在过的数据 |
| 为了提高合格天数而放宽截止点 | 样本量不值一次泄漏 |

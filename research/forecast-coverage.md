# 预报历史覆盖度

生成时间：2026-10-10T19:47:42+00:00　来源：`data\research\weather-research.duckdb`（由 `scripts/forecast_coverage.py` 生成，可重跑）

> 本报告只陈述从数据库查得的事实。覆盖不足处如实列出，不作外推。

## 1. 抓取到的 run

| 模型 | 状态 | 数量 | 最早 run | 最晚 run |
|---|---|---:|---|---|
| `ecmwf_ifs025` | ok | 242 | 2026-08-11 00:00:00+00:00 | 2026-10-10 06:00:00+00:00 |
| `ecmwf_ifs025` | unavailable | 2 | 2026-10-10 12:00:00+00:00 | 2026-10-10 18:00:00+00:00 |
| `gfs` | ok | 243 | 2026-08-11 00:00:00+00:00 | 2026-10-10 12:00:00+00:00 |
| `gfs` | unavailable | 1 | 2026-10-10 18:00:00+00:00 | 2026-10-10 18:00:00+00:00 |

发布时刻依据：{'estimated': 488}　｜　`publishable_at_utc` 为 NULL：**0**

## 2. 日最高温聚合（每个 run × 每个目标日）

| 模型 | 覆盖目标日数 | 最早 | 最晚 | 聚合行数 | 覆盖不完整行 |
|---|---:|---|---|---:|---:|
| `ecmwf_ifs025` | 63 | 2026-08-11 | 2026-10-12 | 2105 | 462 |
| `gfs` | 63 | 2026-08-11 | 2026-10-12 | 2536 | 455 |

**存在缺测小时的聚合行**（`n_missing_hours > 0`，不做任何插值）：

| 模型 | 行数 | 缺测小时合计 | 单行最大缺测 |
|---|---:|---:|---:|
| `ecmwf_ifs025` | 462 | 5188 | 20 |
| `gfs` | 455 | 5409 | 22 |

## 3. 时间点可用性（strict point-in-time）

| 模型 | 合格 (run×日) | 不合格 (run×日) | 合格率 |
|---|---:|---:|---:|
| `ecmwf_ifs025` | 1743 | 362 | 82.8% |
| `gfs` | 2173 | 363 | 85.7% |

不合格原因分布：

- `2` × publishable 2026-08-11T18:00:00+00:00 is 14.0h after the target day began (2026-08-11T04:00:00+00:00)
- `2` × publishable 2026-08-14T18:00:00+00:00 is 14.0h after the target day began (2026-08-14T04:00:00+00:00)
- `2` × publishable 2026-08-17T06:00:00+00:00 is 2.0h after the target day began (2026-08-17T04:00:00+00:00)
- `2` × publishable 2026-08-17T18:00:00+00:00 is 14.0h after the target day began (2026-08-17T04:00:00+00:00)
- `2` × publishable 2026-08-22T18:00:00+00:00 is 14.0h after the target day began (2026-08-22T04:00:00+00:00)
- `2` × publishable 2026-08-23T12:00:00+00:00 is 32.0h after the target day began (2026-08-22T04:00:00+00:00)
- `2` × publishable 2026-08-28T12:00:00+00:00 is 8.0h after the target day began (2026-08-28T04:00:00+00:00)
- `2` × publishable 2026-09-02T12:00:00+00:00 is 32.0h after the target day began (2026-09-01T04:00:00+00:00)
- `2` × publishable 2026-09-03T18:00:00+00:00 is 14.0h after the target day began (2026-09-03T04:00:00+00:00)
- `2` × publishable 2026-09-06T00:00:00+00:00 is 20.0h after the target day began (2026-09-05T04:00:00+00:00)

## 4. 可进入严格样本外评估的目标日

口径：某 (模型, 目标日) 只要**存在至少一个** `eligible` 的 run 即计入 ——
同一目标日的多次运行分别判定，不能互相顶替。

| 模型 | 有聚合的目标日 | 其中可进严格评估 | 不可进 | 覆盖率 |
|---|---:|---:|---:|---:|
| `ecmwf_ifs025` | 63 | 62 | 1 | 98.4% |
| `gfs` | 63 | 62 | 1 | 98.4% |

## 5. 结论（按数据回答）

- **ecmwf_ifs025**：62 / 63 个目标日可进入严格样本外评估。
- **gfs**：62 / 63 个目标日可进入严格样本外评估。

## 6. 库内行数

- `forecast_daily_max`: 4641
- `forecast_hourly`: 104956
- `forecast_runs`: 488
- `klga_daily_max`: 2200
- `point_in_time`: 4641
- `research_runs`: 2
- `settlement_outcome`: 3723
- `settlement_rules`: 408

## 7. 最近的回填运行

- `6448f4c4` backfill-forecasts **completed** 2026-10-10 18:55:18.079907+00:00 → 2026-10-10 19:23:38.467430+00:00
  - `{"daily_rows": 4641, "hourly_rows": 104956, "pip_eligible": 3916, "pip_ineligible": 725, "problems": [], "runs_failed": 0, "runs_ok": 485, "runs_unavailable": 3}`
- `d1b5cecd` extract-legacy **completed** 2026-10-10 18:54:23.699321+00:00 → 2026-10-10 18:55:17.119060+00:00
  - `{"daily_maxima": 2200, "events": 408, "events_rules_parsed": 408, "problems": [], "settlement_outcomes": 3723}`

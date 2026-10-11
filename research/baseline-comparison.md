# 基线对比：模型 vs 市场

生成时间：2026-10-11T08:32:23+00:00　来源：`data\research\weather-research.duckdb`（由 `scripts/baseline_report.py` 生成，可重跑）

时间顺序切分：训练 70% / 测试 30%，按**目标日期**切分（同一日期的多次运行不跨边）；置信区间按**日期**计算，不按行。
观测口径：`max of all readings`

## 1. 总表

| 基线 | 测试日期数 | Brier ↓ | LogLoss ↓ | ECE ↓ | 日期级 Brier [95% CI] | MAE (°F) |
|---|---:|---:|---:|---:|---|---:|
| `calibrated` | 18 | 1.4907 | 25.7442 | 0.7454 | 1.4907 [1.3005, 1.6810] | 3.31 |
| `ecmwf_ifs025_raw` | 18 | 1.5062 | 26.0107 | 0.7531 | 1.5062 [1.3241, 1.6883] | 3.31 |
| `fusion` | 18 | 1.4907 | 25.7432 | 0.7453 | 1.4916 [1.3319, 1.6514] | 3.29 |
| `gfs_seamless_raw` | 18 | 1.6548 | 28.5778 | 0.8274 | 1.6543 [1.5671, 1.7414] | 4.28 |
| `market` | 29 | 1.0345 | 17.8649 | 0.5172 | 1.0345 [0.6643, 1.4047] | — |

## 2. 与市场对照

市场在决策时点的 Brier = **1.0345**。越低越好。

| 模型 | Brier | 相对市场 | 结论 |
|---|---:|---:|---|
| `calibrated` | 1.4907 | +0.4563 | 劣于市场 |
| `ecmwf_ifs025_raw` | 1.5062 | +0.4717 | 劣于市场 |
| `fusion` | 1.4907 | +0.4562 | 劣于市场 |
| `gfs_seamless_raw` | 1.6548 | +0.6203 | 劣于市场 |

> **「优于市场」不等于可以交易。** 样本只有一个测试窗口，市场本身也有噪声；进入策略前还需要样本外的多窗口复核与成本建模。

## 3. 时间点可用性

| 基线 | 训练窗口 | 测试窗口 | 残差 bias (°F) | σ 按 lead (°F) |
|---|---|---|---:|---|
| `calibrated` | 2026-08-12 .. 2026-09-20 | 2026-09-21 .. 2026-10-08 | 0.030 | 0-24h=1.88, 120h+=4.06, 24-48h=2.14, 48-72h=2.83, 72-120h=2.87 |
| `ecmwf_ifs025_raw` | 2026-08-12 .. 2026-09-20 | 2026-09-21 .. 2026-10-08 | 0.000 | 0-24h=1.88, 120h+=4.06, 24-48h=2.14, 48-72h=2.83, 72-120h=2.87 |
| `fusion` | 2026-08-12 .. 2026-09-20 | 2026-09-21 .. 2026-10-08 | 0.000 | 0-24h=1.88, 120h+=4.07, 24-48h=2.14, 48-72h=2.83, 72-120h=2.88 |
| `gfs_seamless_raw` | 2026-08-12 .. 2026-09-20 | 2026-09-21 .. 2026-10-08 | 0.000 | 0-24h=1.65, 120h+=4.12, 24-48h=2.27, 48-72h=2.65, 72-120h=2.64 |

## 4. 市场基线诊断

- `dates_considered`: 404
- `dates_scored`: 29
- `dates_unpriced_before_deadline`: 375
- `dates_without_a_unique_winner`: 0
- `examples_unpriced`: ['2025-06-20', '2025-06-21', '2025-06-22', '2025-06-23', '2025-06-24']

## 5. 数据问题（已记录，未静默丢弃）

- `calibrated`: 2026-10-09: no settlement ladder
- `calibrated`: 2026-10-10: no observation under 'max of all readings'
- `calibrated`: 2026-10-11: no observation under 'max of all readings'
- `ecmwf_ifs025_raw`: 2026-10-09: no settlement ladder
- `ecmwf_ifs025_raw`: 2026-10-10: no observation under 'max of all readings'
- `ecmwf_ifs025_raw`: 2026-10-11: no observation under 'max of all readings'
- `fusion`: fusion skipped 404 run(s) present in only one of 2 models
- `gfs_seamless_raw`: 2026-10-09: no settlement ladder
- `gfs_seamless_raw`: 2026-10-10: no observation under 'max of all readings'
- `gfs_seamless_raw`: 2026-10-11: no observation under 'max of all readings'

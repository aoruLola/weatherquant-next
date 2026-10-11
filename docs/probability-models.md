# 概率模型：从确定性预报到括号概率

2026-10-11。实现：`weatherquant/models/{residual,probability,baselines,evaluation}.py`。

---

## 1. 为什么不能直接把预报当概率

Open-Meteo Single Runs 给的是**一个确定性数值**（某次运行的 `temperature_2m`），
不是概率，也**没有集合成员**可用来展开分布（见
[`weather-data-sources.md`](weather-data-sources.md) §1）。

规范要求：*"不能直接把确定性气象预报当作概率预测……必须基于历史误差、集合成员或明确的
统计方法生成概率。"* 我们没有集合，所以只能走**历史误差**这条路：

```
residual = observed_daily_max − predicted_daily_max        （训练期）
observed = predicted + bias + ε ,   ε ~ 该 lead 下的历史误差分布
P(括号)  = Φ((hi − (predicted + bias)) / σ) − Φ((lo − (predicted + bias)) / σ)
```

`[lo, hi)` 取自 `Bracket.probability_interval_f()` —— **全库唯一实现**，
含整数报价所需的 ±0.5 换算。审计发现的两个开源项目都在这里出错（漏半度 / °C 容差套 °F），
概率偏差达 1.94× 与 1.79×。

---

## 2. σ 怎么来（三条硬规则）

### 2.1 不是 MAE

| 方法 | 公式 | 结果 |
|---|---|---|
| **错误做法**（某被审计项目） | `σ = MAE` | **低估 20%**，分布过窄 → 概率虚高 → EV 虚高 |
| 正确换算（仅当只有 MAE 时） | `σ = MAE × √(π/2) ≈ 1.2533 × MAE` | `weatherquant.models.residual.sigma_from_mae` |
| **本项目实际做法** | 直接取残差的**样本标准差** | 换算根本不进入主路径 |

测试：`test_sigma_from_mae_applies_the_sqrt_pi_over_2_factor` 断言因子
`√(π/2) = 1.2533141373155003`；`test_mae_round_trips_through_sigma` 断言往返恒等。

### 2.2 先按日期折叠，再算离散度

同一目标日的多次运行共享**同一天的天气**，不是独立样本。直接把它们混进标准差，
会把误差压小约 `√(运行次数)`，把一个幸运日变成"模型有技巧"的证据。

`fit_residual_model` 先在每个 `(target_date, lead_bucket)` 内**取均值**，
再跨日期算标准差：

```
test_spread_is_measured_after_collapsing_by_date:
    一个日期 10 次运行（残差全 +1）+ 另一个日期 1 次（残差 −1）
    折叠后  σ = √2      ≈ 1.414
    直接池化 σ           ≈ 0.603       ← 相差 2.3 倍
```

这与 P0 阶段发现的旧版缺陷同源（`se = σ/√n` 把约 30 个同日运行当独立样本，
SE 低估约 5 倍，"保守概率"只缩 0.007–0.09，纯装饰）。

### 2.3 按 lead time 分桶，薄桶宁可不给数

| 桶 | 含义 |
|---|---|
| `0-24h` / `24-48h` / `48-72h` / `72-120h` / `120h+` | 运行初始化到目标当地日结束的小时数 |

- 禁止硬编码 σ 常数（审计中两个项目都这么干过）。
- 某桶日期数 < `min_samples_per_bucket` → **返回 `None`**，不借用全局 σ。
  *"两个样本算出来的不是离散度"* —— 拿它去交易是更糟的行为。

测试：`test_an_empty_bucket_yields_no_sigma_rather_than_a_fallback`、
`test_a_thin_bucket_does_not_borrow_from_a_thick_one`。

---

## 3. 三条拒绝（宁可不给数）

| 情形 | 行为 | 原因 |
|---|---|---|
| 该 lead 无可用 σ | `bracket_probability` → `None`；`distribution_for_event` → `ValueError` | 编造的离散度会生成看似完整的概率 |
| 桶阶梯两端不封闭 | `ValueError("complete ladder")` | 不封闭则概率和不为 1，归一化等于**凭空造质量** |
| σ = 0（残差全相同） | `None` | 退化拟合不是"确定" |

---

## 4. 评估协议

| 约束 | 实现 | 违反后果 |
|---|---|---|
| **按日期切分**，不按行 | `chronological_split` 断言两侧日期不相交 | 同日多次运行会跨边 → 在"见过的那天"上被打分 |
| **置信区间按日期算** | `date_level_summary(date_values, dates)` | 按行算会把 `√(运行次数/日)` 的虚假精度当成证据 |
| **点预报指标只在有温度时给** | `score(..., predicted_max_f=None)` → `mae=rmse=None` | 市场基线没有点预报，硬造一个会让它"看起来可比" |
| **成对比较只用共有日期** | `evaluate_pairwise` 报告 `shared_dates` / `only_in_*` | 否则"更好"可能只因为它被放在更容易的日子上 |

---

## 5. 基线设计

| 基线 | 点预报 | 用途 |
|---|---|---|
| `ecmwf_raw` | ECMWF 原始值 | 未校准下限 |
| `gfs_raw` | GFS 原始值 | 跨模型对照 |
| `fusion` | 两模型均值 | 单纯融合是否有增益 |
| `calibrated` | 最优 raw + 拟合 bias | **消融**：与 raw 同 σ，差值只来自 bias |
| `market` | 无（读盘口中价） | **被超越的基准** |

`raw` 与 `calibrated` 共用同一套 σ —— 这是刻意的消融设计，保证分数差异**只**归因于
偏置校正，而不是别的什么。

全部四个模型基线共用 `distribution_for_event` 一条概率通路，因此分数差异**不可能**
来自实现差异。

---

## 6. 已知限制

- **单值预报，无集合**：σ 完全来自历史误差。若某类误差不在历史分布里（制度变化、
  极端天气），模型会自信地错。这是数据源的限制，不是模型的选择。
- **测试窗口单一**：目前只有一个按时间切出的测试窗，"优于市场"的结论**不足以**
  支撑交易。进入策略前需要多窗口复核与成本建模。
- **bias 是全局的**：未按季节/温度区间分层。63 个目标日不足以支持更细的划分，
  细分到两个样本就是把噪声当信号。
- **市场基线的中价不可成交**：它衡量"市场相信什么"，不是"能以什么价格成交"。

---

## 7. 复现

```bash
uv sync --group dev
uv run pytest tests/test_probability.py tests/test_baselines.py tests/test_evaluation.py
uv run python scripts/baseline_report.py          # → research/baseline-comparison.md
```

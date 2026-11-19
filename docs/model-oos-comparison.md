# 四模型时序样本外比较（WQN-36）

实现：`weatherquant.models.oos_compare`。该报告比较 `calibrated`、`ecmwf_raw`、`fusion` 和 `gfs_raw`，只读研究数据库，不读取或修改生产数据库、WAL、归档，也不写模型工件。

## 运行

```bash
uv run weatherquant-research --db data/research/weather-research.duckdb \
  models oos-compare --json
```

常用参数：

- `--min-train-dates 5`：首个测试折之前至少需要的对齐日期数；
- `--min-samples-per-bucket 3`：每个 lead-time 桶估计 spread 所需的最少日期数；
- `--bootstrap-samples 1000`、`--seed 36`：bootstrap 次数与固定随机种子；
- `--reference ecmwf_raw`：配对差值的参考模型；
- `--observation-rule "max of all readings"`：观测口径。

不加 `--json` 时输出简要文本表；完整覆盖、逐折、配对差值和分组结果在 JSON 中。命令通过 `duckdb.connect(..., read_only=True)` 打开已有文件，文件不存在或没有可评分的对齐样本时失败，不创建数据库。

## 方法

### 样本与对齐

1. 使用现有 `forecast_daily_max`、同日最高气温观测和 `settlement_outcome` 事件梯子。无观测、无梯子或胜者不唯一的日期沿用基线规则排除并记录。
2. `ecmwf_raw` 与 `gfs_raw` 使用各自原始点预报；`fusion` 只平均同一 `(target_date, run_time_utc, station)` 的 ECMWF/GFS 预报，不用单模型代替缺失的另一条腿；`calibrated` 使用 ECMWF 点预报加逐折拟合的 bias。
3. 四个模型只在完全相同的 forecast run 上评分。任何一个模型缺预报、未通过时间点检查或无法产生完整概率分布时，该 run 不进入最终共同样本。`coverage` 分别报告 eligible、aligned、scored 的日期数和事件数，因此共同样本造成的覆盖损失可见。
4. 同一目标日的多次 run 都保留为事件；置信区间按目标日聚类，不能把同日 run 当成独立天气样本。

### 时序 walk-forward 与校准

采用 expanding-window：首个测试日期之前保留 `min_train_dates` 个共同日期，之后每个目标日依次成为测试折。每个模型、每个折都重新执行：

```text
training target_date < test target_date
```

代码同时检查最大训练日期必须严格小于测试日期。`calibrated` 的 bias 只能由该折之前的残差估计；原始模型 bias 固定为 0，但它们用于概率分布的残差 spread 同样只由过去数据估计。不会先对全样本拟合再回测早期折。`folds` 记录每个模型的 `train_start`、`train_end`、`train_dates`、`train_events`、`bias_f` 和该折指标，便于审计。

所有模型使用现有 `distribution_for_event` 概率路径和相同市场 bracket 定义。完整梯子或可用 spread 不存在时不猜测概率，而是排除并计数。

### 时间点泄漏防护

决策截止点为目标日在 `America/New_York` 的开始时刻，不是结束时刻。每条源预报必须满足：

```text
publishable_at_utc <= local_day_start_utc
publishable_basis != "unknown"
publishable_at_utc >= run_time_utc
```

缺少 `forecast_runs` 可用性记录按 unknown 处理。晚于截止点、发布早于初始化、发布时间缺失或未知的预报均排除，并在 `exclusions.sources` 中记录原因。融合腿必须分别合格，融合的可用时刻取两条腿较晚者。

天气分组也不使用测试期信息：观测温度三分位阈值只由首个测试折之前的共同训练日期估计，之后固定使用。采用最近秩三分位，输出为 `cold`、`moderate`、`hot`；训练观测不足时统一为 `unknown`。阈值和训练边界见 `condition`。

## 指标与不确定度

每个模型报告以下六项指标及 95% CI：

| 指标 | 定义 | 方向 |
|---|---|---|
| Brier | 多类别 `mean_i sum_k (p_ik-y_ik)^2` | 越低越好 |
| Log Loss | 实际类别负对数概率，使用现有概率截断规则 | 越低越好 |
| ECE | Top-1 confidence 与是否命中的分箱加权绝对差 | 越低越好 |
| MAE | 点预报绝对误差均值，单位 °F | 越低越好 |
| RMSE | 点预报均方根误差，单位 °F | 越低越好 |
| Top-1 accuracy | 最大概率类别等于实际类别的比例 | 越高越好 |

`calibrated` 的 MAE/RMSE 使用加 bias 后的点预报；其他模型使用各自原始或融合点预报。

CI 使用**目标日 block bootstrap**：每次有放回抽取与原样本相同数量的目标日，并带走该日全部事件，再重新计算指标。默认 1000 次、seed 36；相同数据和参数得到相同结果。区间为重抽样估计的 2.5%/97.5% 百分位。

`paired_differences` 在每一次相同日期抽取上计算 `模型 - ecmwf_raw`，因此差值与 CI 是配对的。除 Top-1 accuracy 外，负差值表示优于参考模型；Top-1 accuracy 正差值表示优于参考模型。`significant` 仅表示 95% 配对 CI 不含 0，不代表实际交易价值。

只有一个目标日的分组无法估计日间不确定度，CI 返回 `null`，不伪造零宽区间。

## 如何阅读报告

- `models`：每个模型的 `n_dates`、`n_events`、六项指标点估计和 CI；`small_sample` 标记日期数少于 5 的组。
- `paired_differences`：相对参考模型的配对差值、CI 和显著性。
- `ranking`：只按总体 Brier 点估计排序，Log Loss 作为并列时的次排序，同时附 Brier/Log Loss CI 和相对参考模型的配对 Brier CI。CI 重叠或配对 CI 跨 0 时，不应把相邻名次解释为确定优劣。理论 Edge 不参与排序，报告也不提供 Edge 排名。
- `breakdowns.by_month`：按 `YYYY-MM` 分组，每组仍有 n、六项指标和 CI，不在组内生成排名。
- `breakdowns.by_observed_temperature_tercile`：按上述训练期温度三分位分组，同样只报告证据，不生成小组名次。小组必须结合 n、`small_sample` 和 CI 宽度阅读；单日期组 CI 为 `null`。
- `coverage`、`exclusions`、`problems`：覆盖损失、泄漏/概率评分排除和数据质量问题。样本很少或排除很多时，应先检查数据，而不是解读排名。
- `folds`：逐折训练边界、校准参数、事件数和指标，可复核未来数据是否影响过去折。

## 限制

1. Open-Meteo 的 `estimated` 发布时间仍是保守估计而非官方时间戳；严格性依赖现有时间点元数据。
2. 共同 run 对齐避免“不同天气样本”造成假优势，但可能降低覆盖并带来选择偏差；必须同时查看 per-model coverage。
3. 日期 block bootstrap 保留日内相关，不消除跨日天气相关、制度变化或多重比较问题。显著性不是生产验收。
4. 小样本和小组排名没有统计意义；本报告刻意不为月度/天气小组给名次。
5. 概率来自现有条件正态残差近似，不代表分布假设已验证；ECE 也依赖分箱，不能替代完整可靠性图。
6. 当前是单站、已结算事件的离线研究比较，不含市场成本、成交、组合约束或实盘可得性。合并代码或生成本报告均不等于生产验收。

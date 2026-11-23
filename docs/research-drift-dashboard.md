# 校准漂移与市场定价 Edge 研究看板

## 1. 用途与数据边界

本看板用于按时间观察模型校准、市场隐含概率偏差、费用假设和盘口可执行性，并追溯异常桶的来源 Episode。它是**只读研究视图**，不会下单，也不代表真实账户绩效。

- API：`GET /api/v1/performance/research-drift`；不接受写操作。
- 快照：`research-drift.json`，与其余六份 Performance 快照同属一个发布世代。
- 物化构建与流式构建调用同一个 `build_research_drift` 聚合器；Dashboard 只读取已发布结果，不在请求时打开数据库或重新推导研究数值。
- 未发布快照的研究正文为 `null`，同时返回缺失原因和 freshness；不会把“未发布”显示成全零趋势。
- 研究构建只使用导出、Episode、盘口和结算证据，不读写生产 DB/WAL/archive。

## 2. 时间桶

同一 Episode 分别进入以下三类桶，因此三类桶之间不能相加：

| 粒度 | 取值规则 |
| --- | --- |
| ISO 周 | `first_seen_utc` 归一到 UTC 后的 ISO 年周，格式 `YYYY-Www` |
| 月份 | `first_seen_utc` 归一到 UTC 后的日历月，格式 `YYYY-MM` |
| 天气目标日 | Episode 的天气 `target_date`，格式 `YYYY-MM-DD`；不是首次观测日 |

缺少目标日的 Episode 仍进入 ISO 周和月桶，只从目标日桶排除，并计入 `target_date_missing_episode_count` 和警告。每个桶按 `start_date`、`period` 排序。

## 3. 三种样本严格分开

| 字段 | 分母/总体 | 含义 |
| --- | --- | --- |
| `observation_count` | priced ACCEPT observation | 每次通过过滤且带有定价研究字段的评估；同一 Episode 可贡献多次观测 |
| `episode_count` | 独立 Episode | 按现有 Episode 身份键折叠后的机会数；一个 Episode 在所属桶只计一次 |
| `executable_count` | 实际可执行 Episode | `simulate_single` 能依据匹配盘口和已知费用确定成交/规模的 Episode 数 |

`not_executable_count` 是不能形成上述可执行样本的 Episode 数，并附 `not_executable_reason_counts`。观测数不能当作交易数，Episode 数也不能当作可成交数。校准、市场偏差、预期 USD 和已实现 USD 各自另有明确的 `n`，不能互借分母。

## 4. 校准、市场偏差与基线

### 4.1 Episode 首次概率

模型校准和市场定价均使用每个 Episode 的**首次**概率，避免同一机会的重复 ACCEPT 观测放大权重：

- 模型：`first_model_probability`；
- 市场：`first_market_price`，视为 YES 的市场隐含概率；
- 结果：只有 `(market_id, target_date)` 已验证、状态为 `RESOLVED` 且 YES payout 为 0 或 1 的记录。

概率无效、未结算或未通过验证的 Episode 不进入相应指标。这里的 `n` 是具备所需证据的 Episode 数，不是 observation 数。校准使用 YES 结果评估概率本身，因此 SELL YES Episode 也可以在概率证据完整时进入校准；这不代表其交易盈亏已实现。

### 4.2 指标

二元 Brier 分数为：

```text
Brier = mean((model_p - outcome)^2)
```

取值范围为 `[0, 1]`，越低越好。ECE 复用现有 `calibration_bins` 和 `expected_calibration_error`，固定 10 个等宽概率分箱；每个非空分箱发布预测均值、实际频率、校准差和 `count`。ECE 按分箱样本数加权，样本数同时随指标发布。

市场隐含概率偏差定义为：

```text
market_bias = mean(market_p - realised_outcome)
```

正值表示市场平均高估 YES，负值表示低估；它不是交易 Edge，也不是利润。

### 4.3 基线比较

- **市场基线**：在与市场偏差相同的已验证 Episode 上发布市场 Brier、ECE 和偏差，可与模型 Brier/ECE 对照。
- **气候基线**：当前 Performance 导出没有可追溯的气候概率，因此明确标为 `available: false`、`n: 0`、分数为 `null`。不会用常数、市场概率或事后频率冒充气候基线。

没有已验证结果时，相应分数和偏差为 `null`，不是 0。

## 5. 三层 Edge 不得混用

| Edge tier | `n` 的来源 | 单位 | 计算与证据范围 |
| --- | --- | --- | --- |
| `theoretical_observation_edge_per_share` | priced ACCEPT observations | `probability_per_share`，无量纲 | 每桶累加 `episode.mean_edge × observation_count`；是模型理论观测 Edge，不是 USD |
| `executable_expected_net_usd` | 可执行 Episode | USD | 只取 `simulate_single` 的预期净收益，底层成交、费用和 Edge 全部复用 `execution/pricing.py` |
| `verified_realised_net_usd` | 可执行且有受支持已验证结算的 Episode | USD | 只取 `simulate_single` 返回的已验证净盈亏，不把未结算或不可验证结果当作 0 |

每层分别发布 `n`、`sum`、`mean` 和 `unit`。三层的 `sum` 或 `mean` **绝不跨层相加、净额化或拼接成一条收益曲线**。某层无样本时 `n=0` 且 `sum/mean=null`，避免把“无证据”写成“收益为零”。

预期和已实现 USD 使用 `reference_capital_usdc` 对每个可执行 Episode 分别模拟。各 Episode 独立持有这份参考本金，因此这些 USD 合计不是同一共享账户的余额、权益或真实回报，也不提供组合资金占用结论。

## 6. 费用与盘口排除规则

- `fee_unknown_count`：Episode 的 `fee_rate is None`。未知费用绝不按 0 插补。
- `no_book_count`：没有可用于模拟的盘口。不会拿展示报价冒充可成交价格。
- 两个计数独立统计；同一 Episode 同时缺费用和盘口时会分别进入两个计数。
- 未知费用或缺盘口的 Episode 不进入可执行样本，也不进入预期净 USD 或已实现净 USD；原因仍保留在 `not_executable_reason_counts`。
- 已知费率按 `fee_rate` 发布 Episode 数，便于检查费用假设是否随时间变化。
- 理论观测 Edge 是另一总体，仍可统计其 ACCEPT 观测，但不能据此推断存在可成交 USD 收益。

## 7. 异常检测与来源追溯

异常检测在每个粒度、每个指标上独立按时间顺序进行。当前桶必须至少有 **4 个先前的有效桶**才参与判断；指标为 `null` 的桶不进入该指标的历史序列。

对先前有效值计算中位数 `median` 和 MAD：

```text
MAD = median(abs(previous_value - median))
deviation = abs(current_value - median)
```

当 `deviation > 3 × MAD` 时标记异常，比较是严格大于。若 `MAD=0`，任何非零偏离都会标记；完全相同的值不会标记。

监控指标为：

- `calibration.brier`；
- `calibration.ece`；
- `market_bias.mean_market_minus_outcome`；
- 三层 Edge 各自的 `mean`。

每条异常记录包含粒度、周期、指标、当前值、历史中位数、MAD、偏离、阈值、历史桶数及**当前桶全部 `source_episode_ids`**。普通桶也携带来源 ID，前端可展开核对。该规则是稳健的离群提示，不是显著性检验、因果结论或交易排名。

## 8. 小样本与已知限制

- `episode_count < 20` 标记为 `small_sample`；页面展示实际 `n`，不对小样本排名。
- Brier、ECE、市场偏差和基线只覆盖有已验证 YES payout 的 Episode，存在结算覆盖选择偏差。
- Episode 去重降低重复评估权重，但不自动保证不同市场或目标日之间统计独立。
- 未结算、争议或不可验证结果不会进入已实现 USD；无已实现样本不代表盈亏为 0。
- SELL YES 的持仓/结算语义尚未证明，因此其已实现交易 P&L 保持不可用；可执行时仍可由既有定价模块给出预期净 USD，其 YES 概率也可进入校准。
- 目标日桶与首次观测时间桶回答不同问题，不应直接比较其合计。
- 本看板不展示真实收益：系统没有真实下单，所有 USD 数值均为有明确证据范围的研究模拟。

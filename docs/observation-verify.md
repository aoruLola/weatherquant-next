# KLGA 每日最高温观测与市场时间规则校验（WQN-33）

2026-10-26。实现：`weatherquant/truth/observation_verify.py`、研究库迁移
`0003_market_verification.sql`。配套：`markets/settlement_rules.py`（规则
解析）、`weather/daily_max.py`（当地日/DST 算术）、
[`settlement-queue.md`](settlement-queue.md)（本模块产出 verified 清单的
上游工作流）。

---

## 1. 立场：市场的条款是唯一口径

核对**只运行在市场自己的条款上**——规则文本解析出的站点、单位、精度、
当地日界与缺测条款（`ParsedRules`）。校验器绝不悄悄替换另一套气象口径：

- **当地日，不是 UTC 日**：目标日取 `America/New_York` 民事日的半开区间
  `[start, end)`，窗口的 UTC 起止随结果记录（它们同时编码了当日实际生效
  的夏令时偏移）；
- **单位由规则决定**：`degF` 市场读 `temp_f`，`degC` 市场读 `temp_c`；
  规则缺单位/精度 → 判 `unusable_rules`，**不是**默认华氏；
- **聚合只有一种**：市场 Temp 列在目标当地日内的最高读数
  （`aggregation_rule` 字段逐字记录）——不换成日汇总列，不跨源填补；
- **夏令时是算术不是假设**：23/24/25 小时的期望小时数由时区算出
  （`expected_hours`），整日覆盖不足 → 判 `incomplete_observation`，
  **永不**借另一来源补齐后照常判分。

## 2. 双向验证

| 方向 | 含义 |
|---|---|
| `observation → bracket`（`observation_picks_venue_winner`） | 观测最高温按事件自己的档位边界应命中的市场 == 平台赔付的市场 |
| `venue → observation`（`venue_bracket_contains_observation`） | 平台赔付的档位区间包含观测最高温 |

两个方向都真 → `match`；否则 `mismatch`，detail 写明双方各自指向什么、
哪个方向破裂。赔付多于一档 → `ambiguous_winner`（档位重叠或平台异常），
零赔付 → `no_outcome`。

**缺测处理由规则决定**：零读数时，若规则写明 "resolve to the lowest
bracket"，则比较平台赔付与事件最低档（含"X°F or below"型只有上界的
最低档）→ `no_data_match` / `no_data_mismatch`；规则没有 fallback →
`no_observation`，**不得**自行择源。

## 3. 溯源：verdict 之外的一切都在行里

每条验证落入研究库 `market_rule_verification`（迁移 0003），一行携带：

- 规则文本 SHA-256 + 解析器版本（`settlement-rules-1`）+ 校验器版本
  （`observation-verify-1`）——**规则版本保留**，任何 verdict 都能在产生
  它的版本下重跑；
- 观测来源逐个列出：source 名、读数条数、首末观测时刻（**原始来源时间**）；
- `local_day_start_utc` / `local_day_end_utc`（时区/夏令时证据）、
  `expected_hours` / `distinct_hours`、`n_readings`；
- `unit` + `precision_deg`（**精度**，来自规则文本）；
- 聚合规则逐字、`verified_at_utc`。

主键含 `verifier_version`：同一版本重跑替换自己的行，**新版本写新行**
——旧 verdict 的证据永不覆盖。

## 4. 跨日边界与夏令时（测试断言）

| 场景 | 断言 |
|---|---|
| 00:00 ET 的读数（04:00Z） | 属于**开始**的那一天；喂给前一天的核验时按 out-of-day 排除（半开区间） |
| 2026-03-08 春令时 | `expected_hours == 23`，窗口 `05:00Z → 04:00Z(+1d)` |
| 2026-11-01 秋令时 | `expected_hours == 25`，01:00 ET 的两个 UTC 时刻都在日内，`distinct_hours == 25` |
| naive 时间戳 | 记 malformed 拒收——猜一个时区会移动整个 DST 窗口 |
| 部分覆盖 | `incomplete_observation`，不发布最大值 |
| 无单位/精度 | `unusable_rules`，逐字段列出缺失项 |

## 5. 用法与生产路径

```python
from weatherquant.truth.observation_verify import verify_market_rules, persist_verification_results

result = verify_market_rules(
    rules=parsed_rules_row,            # settlement_rules 表的一行
    markets=event_markets,             # 事件的档位（bracket_low/high/inclusive）
    outcomes=venue_outcomes,           # 平台赔付（yes_price==1.0 或 is_winner）
    observations=station_readings,     # klga_observations 的行（可传全量，窗口外自动排除计数）
    target_date=date(2026, 10, 13),
    event_id=event_id,
)
persist_verification_results(con, [result])
```

与 WQN-32 的闭环：队列把 `resolved_unverified` 的对子交给本模块做观测
交叉核验；`match` 的对子进入 verified 清单，回填时经 `--verified` 传入，
此后才允许进入 WIN/LOSS 分母。

## 6. 验收对照与证据

| 工单验收 | 证据 |
|---|---|
| 按市场具体规则核对站点/当地日/时区/华氏/缺测 | `test_observation_verify.py` 12 项：窗口/夏令时/单位缺省拒判/缺测分支逐一 |
| 官方气象记录与市场结算双向验证 | 两个方向各自命名并断言（match 与 mismatch 两个测试都检查双向布尔） |
| 跨日边界测试 | 上表前三行（普通日/春令时/秋令时/午夜归属） |
| 保留原始来源时间、精度与规则版本 | 溯源测试 + `test_verification_rows_persist_with_full_provenance`（迁移 0003 落库逐字段断言） |

**需 NAS/人工的部分（未做）**：对真实 NOAA/WU 原始观测与真实已结算事件
跑批量核验（本机模块测试全部使用合成读数与真实解析器输出的规则条款）；
观测来源的接入（`klga_observations` 的采集路径）不在本工单范围。

# 定价与执行成本研究契约（WQN-38）

本文定义 `weatherquant.execution.pricing` 的唯一职责：把一份订单意图、一个真实订单簿、
一个决策时刻和一份费率表，变成**带状态的**研究结论。任何模块都不得在别处重新推导
手续费、成交均价、滑点或美元利润。

> 状态：这是研究契约，不是生产验收证据。文末「局限」列出的假设都还没有生产数据验证；
> 合并 PR 不等于生产验收（见 Jira WQN-38 备注）。

## 1. 核心对象

```python
from weatherquant.execution.pricing import (
    ExecutionIntent, FeeRole, FeeSchedule, PricingStatus, price_execution,
)

result = price_execution(
    book,                                   # 必须是 intent.token 自己的订单簿
    intent=ExecutionIntent.BUY_NO,          # BUY/SELL × YES/NO
    shares=50.0,
    fee_schedule=FeeSchedule(role=FeeRole.TAKER, rate=0.05),
    decision_time=decided_at,
    max_quote_age_seconds=900.0,
    buffer_fraction=0.0,
    q=p_no,                                 # 该 token 获胜的模型概率
)
assert result.status is PricingStatus.EXECUTABLE
result.total_cost_usd                      # 仅 EXECUTABLE 非 None
```

`ExecutionIntent` 是 `BUY_YES / SELL_YES / BUY_NO / SELL_NO`。它同时决定
**走哪个簿的哪一侧**：

| intent | 订单簿 | 侧 |
| --- | --- | --- |
| `BUY_YES` | YES 簿 | asks |
| `SELL_YES` | YES 簿 | bids |
| `BUY_NO` | NO 簿 | asks |
| `SELL_NO` | NO 簿 | bids |

`q` 始终是**该 intent 所交易 token 的获胜概率**：`BUY_YES`/`SELL_YES` 传 P(YES)，
`BUY_NO`/`SELL_NO` 传 P(NO)。

## 2. 状态与原因码

| status | 含义 | 是否携带成本/美元利润 |
| --- | --- | --- |
| `EXECUTABLE` | 簿可用、报价新鲜、费率已知、请求数量被完整吃掉 | 是 |
| `PARTIAL` | 簿可用，但深度只够一部分；报告已走到的 filled/VWAP/滑点 | 否 |
| `INSUFFICIENT_DEPTH` | 簿存在，但没有可吃数量 | 否 |
| `FEE_UNKNOWN` | 场地方未报告费率；**绝不默认 0** | 否 |
| `STALE_QUOTE` | 报价无法在决策时刻被信任（过旧 / 无时间戳 / 来自未来） | 否 |
| `NOT_EXECUTABLE` | 无订单簿、单侧为空、盘口交叉、非单调 | 否 |

`reasons` 是稳定代码，当前包括：

- `NO_ORDER_BOOK`、`NO_ASK_SIDE`、`NO_BID_SIDE`、`CROSSED_OR_LOCKED`、
  `NON_MONOTONIC_ASK`、`NON_MONOTONIC_BID`
- `QUOTE_TOO_OLD`、`QUOTE_AGE_UNDEFINED`、`LOOK_AHEAD_QUOTE`
- `FEE_RATE_UNKNOWN`
- `UNFILLED_REMAINDER`、`NO_LIQUIDITY`

状态优先级是**因果顺序**，不是集合：

1. 簿不可用 → `NOT_EXECUTABLE`
2. 报价不可信 → `STALE_QUOTE`
3. 费率未知 → `FEE_UNKNOWN`
4. 深度不足 → `PARTIAL` / `INSUFFICIENT_DEPTH`
5. 全部满足 → `EXECUTABLE`

例如：簿为空且费率未知，返回 `NOT_EXECUTABLE`；簿可用、报价过旧且费率未知，
返回 `STALE_QUOTE`。`PARTIAL` **不是**请求数量的可执行交易，因此它的
`fees_usd`、`total_cost_usd`、`net_proceeds_usd`、`expected_net_profit_usd`
全部为 `None`；它只保留 `DepthFill` 中的盘口事实（filled、VWAP、滑点）。

## 3. 公式

记 `C` 为成交股数，`p` 为成交价（USD/share），`rate` 为场地方费率。

### 3.1 手续费（maker / taker）

Polymarket 文档基公式（`docs.polymarket.com/trading/fees`）：

```text
fee = C * rate * p * (1 - p)
```

- taker：`taker_fee_usd`，结果按 5 位小数四舍五入。
- maker：`maker_fee_usd`，同一公式。天气市场公布的 maker 费率是 0，因此
  `rate=0.0` 是**已知的零成本**；`rate=None` 仍抛 `FeeStructureUnknown`，
  不会被当成 0。
- 多档成交时 `p` 取整笔的 VWAP。由于 `p(1-p)` 是凹函数，按 VWAP 计费
  **不低于**逐档计费（Jensen），是保守读法，也保证全代码库只有一个费用函数。
- `FeeSchedule.exponent` 只做审计留存。当前契约不擅自把它塞进公式；如果某个市场
  公布了不同结构，必须先显式建模再信任其费用。
- `max_fee_usd_per_share(rate) = rate / 4`：`rate*p*(1-p)` 在 `p=0.5` 的最大值，
  仅用于下单前预留费用余量，成交后仍以真实费用复核。

### 3.2 VWAP 与滑点

逐档吃单：

```text
filled = Σ min(remaining, level.size)
cost   = Σ take * level.price
VWAP   = cost / filled
```

滑点定义为**相对盘口最优价的逆向价格冲击**：

```text
BUY :  slippage/share = max(VWAP - best_ask, 0)
SELL:  slippage/share = max(best_bid - VWAP, 0)
slippage_usd = slippage/share * filled
```

不能用同一个有符号减法同时服务买卖：卖方 VWAP 本来就更低，`VWAP - best_bid`
会得到负数再被截成 0，从而把「吃穿买盘」的成本隐藏掉。`adverse_slippage_usd_per_share`
是唯一实现。

### 3.3 成本、收入与利润

```text
execution_buffer_usd = fill_cost_usd * buffer_fraction      # 预算，不是场地费
BUY :  total_cost_usd   = fill_cost + fees + buffer
       E[profit]        = q * filled - total_cost_usd
SELL:  net_proceeds_usd = fill_cost - fees - buffer
       E[profit]        = net_proceeds_usd - q * filled
```

`SELL` 的利润公式对应「今天卖出、若该 token 获胜则支付 \$1」；`BUY_NO` 则用买入
NO 的公式 `q_no * shares - total_cost`。两者代数形状相似，但**价格、深度、费用
输入不同**，不能互相替代。

## 4. 报价新鲜度规则

`quote_age_seconds = decision_time - book.venue_ts`。

- 只看 `venue_ts`（场地事件时间），不看 `fetched_at_utc`：抓取时间可能远晚于数据本身。
- `venue_ts is None` → `STALE_QUOTE / QUOTE_AGE_UNDEFINED`。无法证明新鲜，就不算新鲜。
- `age < 0` → `STALE_QUOTE / LOOK_AHEAD_QUOTE`。报价晚于决策时刻是未来信息。
- `age > max_quote_age_seconds` → `STALE_QUOTE / QUOTE_TOO_OLD`。

阈值是**研究假设**，不是场地规则：

- 默认 `DEFAULT_MAX_QUOTE_AGE_SECONDS = 900` 秒，与参考资金模拟和采集静默窗口一致。
- 纸面成交引擎传 90 秒（`paper/fills.py` 的 `max_book_age_seconds`）。
- 调用方可传更严格的值；传 `<= 0` 会直接报错。

非 `EXECUTABLE` 状态下，所有美元成本/利润字段均为 `None`。这样下游无法把
「过期报价」误当成「0 成本成交」。

## 5. SELL YES 与 BUY NO 不是 1−p 镜像

二者必须分别对**自己的订单簿**定价：

- `SELL_YES` 打 YES 簿的 **bid**；
- `BUY_NO` 吃 NO 簿的 **ask**。

它们不同的原因至少有四层：

1. **报价不同**：`NO ask` 不等于 `1 - YES bid`，中间可能隔着价差。
2. **深度不同**：两个 token 的挂单量各自独立。
3. **费用不同**：费用公式对 `p` 对称，但两笔成交的 `C`、`p`、VWAP 不同，
   四舍五入到 5 位后也可能不同。
4. **语义不同**：`SELL_YES` 是平掉已有 YES 多头；`BUY_NO` 是建立 NO 多头。
   纸面引擎当前只允许卖出已有持仓（`paper/risk.py`），没有裸卖空。

数值例子（`tests/test_pricing_contract.py`）：

```text
YES 簿 bids: 0.40 × 100
NO  簿 asks: 0.62 × 30, 0.66 × 100

SELL YES 50 → VWAP 0.40，fee 0.60000
BUY  NO 50 → VWAP 0.636（0.62×30 + 0.66×20），fee 0.57876
1 - 0.40 = 0.60，但真实 NO ask 是 0.62 —— 镜像价格不存在。
```

因此契约里没有、也不应该有 `price_no = 1 - price_yes` 这样的函数。

## 6. Observation Edge 永远不能求和成美元

Observation Edge 是**每次评估的每股期望**（USD/share 量纲的边际量），不是现金：

- `performance/edge.py` 的 `overall.sum`、`daily_entry(...).observation_edge_sum`
  都是同一批每股值的和，单位仍是「每股」；它没有乘以股数，也不是账户利润。
- 把重复评估的 Edge 相加会制造出一个看起来像收益、实际上没有对应交易的数字。
- 美元总额只能来自 `EXECUTABLE` 的 `PricingResult.expected_net_profit_usd`：

```python
from weatherquant.execution.pricing import sum_executable_expected_net_profit_usd

total = sum_executable_expected_net_profit_usd(results)
# - 空列表返回 None（不是 0）
# - 任一非 EXECUTABLE → ValueError
# - 任一 EXECUTABLE 但未提供 q → ValueError（没有利润，不能进总额）
```

这是硬护栏：函数**拒绝**混合列表，而不是静默跳过不可执行项。测试
`test_usd_totals_only_sum_executable_results` 与
`test_observation_edge_is_a_per_share_sum_not_a_usd_total` 锁定该行为。

## 7. 审计表：所有费用 / 价差 / 滑点 / 成交价 / 美元利润的计算点

| 位置 | 计算内容 | 处理 |
| --- | --- | --- |
| `execution/pricing.py` | 手续费、逐档成交、VWAP、滑点、buffer、成本、收入、每股/总利润 | **唯一权威实现**；新增 `price_execution` / `PricingResult` / `DepthFill` |
| `markets/models.py` | `spread_usd_per_share`、`midpoint_usd_per_share`、`depth_notional_usd` | 订单簿**几何记录**，仅供展示、流动性门槛和分析；注释明确禁止当成交价；不涉及费用/利润 |
| `paper/fills.py` | 纸面成交：build plan → execution_cost → 记账 | 费用/成本走 pricing；滑点改为 `adverse_slippage_usd_per_share`（见 §8 行为变更） |
| `paper/risk.py` | 下单前按最高可能费率预留费用余量 | 改为 `max_fee_usd_per_share(rate)`；费率未知 → `UNPRICABLE_EVENT` 拒绝，不再 `or 0.0` |
| `performance/capital.py` | 按资金吃单深度估算股数 | 改为 `max_shares_for_notional`；费用/利润仍走 pricing |
| `shadow/decision.py` | `_price` 与 `estimated_cost_usd` | 费用/成本/利润走 pricing；`_charged_costs` 改为 `execution_charges_usd` |
| `performance/edge.py` | Observation Edge 分布与每日和 | 保持每股值求和，不乘股数、不转美元；不在 USD 路径中 |
| `performance/episodes.py` | `observation_edge_sum`、`unique_opportunity_edge_sum` | 同上，量纲为每股，不是现金 |
| `performance/winrate.py` | 胜负统计、edge band | 只做胜率/分组，不累加美元利润 |
| `dashboard/producer.py` | `bid_depth_usd`、`ask_depth_usd` | 盘口名义深度展示（`Σ p*s`），不是可成交价，也不是成本 |
| `dashboard/builders.py` | 读取 `spread` / depth 展示 | 只读取记录字段，不重算 |
| `data/books.py`、`data/writer.py`、`shadow/store.py` | 存储/持久化盘口与深度 | 不计算执行价格或费用 |
| `paper/ledger.py`、`paper/store.py` | 记录已定价的成交 | 只存 `PricingResult`/`ExecutionCost` 产生的数字，不重算 |
| `mcp/server.py`、`shadow/export.py` | 读取/导出已发布字段 | 不做定价运算 |
| `storage/migrations_*` | 列定义 | 仅持久化，无算术 |

## 8. 行为变更（原有调用方）

只改了两处**原本就错**或会静默忽略未知费率的地方：

1. `paper/fills.py` 的 `slippage_usd_per_share`：
   - 原实现 `max(avg - best, 0)` 对 SELL 恒为 0（因为卖方 VWAP ≤ best bid），
     把吃穿买盘的成本隐藏了。
   - 现改为方向感知的 `adverse_slippage_usd_per_share`：BUY 结果不变，SELL 变为正值。
   - 该字段只用于成交证据/展示，不进入 `ExecutionCost`，因此账本余额与费用不变。
2. `paper/risk.py` 的费用余量：
   - 原实现 `event.fee_rate or 0.0` 在费率为 `None` 时静默按 0 预留。
   - 现改为 `max_fee_usd_per_share`，未知费率拒绝为 `UNPRICABLE_EVENT`。
   - 正常（已知 0.05）路径的数值完全不变：`rate * 0.25`。

其余调用方输出保持不变；重构只是把计算搬进 pricing。

## 9. 局限（均为未验收项）

- **无生产验收证据**：所有测试都是合成盘口；还没有用生产归档盘口跑过端到端定价。
- **费率结构只覆盖文档基公式**：`fee_exponent` 未参与计算；非零 maker 费率按同一
  凹公式处理，这只是建模假设，不是场地确认。
- **天气市场费率 0.05/0 是配置输入**，不是本契约发现的事实；费率未知时会拒绝，
  但不会自动从场地重新抓取。
- **历史盘口不可得**：shadow 库只保留最新 `token_books`；历史深度依赖归档调用方注入。
  缺失时研究结果是 `NOT_EXECUTABLE`，不补造档位。
- **900 秒阈值是研究假设**，纸面引擎的 90 秒更严格；二者都未与真实成交质量校准。
- **BUY NO 尚无纸面/生产下单路径**：本契约能对 NO 自己的簿定价，但 `paper/risk.py`
  仍只支持 `BUY YES` / `SELL YES`；接入前必须解析并校验 NO token 的真实簿。
- **部分成交只报告事实**：`PARTIAL` 不带费用与利润，不能在组合层当作已完成交易。
- **舍入**：只有费用按 5 位小数舍入；VWAP、滑点、利润保留浮点，不做二次舍入。

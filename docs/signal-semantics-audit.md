# WQN-27 信号语义审计：SELL YES、重复 ACCEPT 与可执行性契约

审计日期：2026-11-17。分支：`trae/wqn-27-signal-semantics-audit`（基于 `origin/main` 27b6a63）。
可执行契约：[`tests/test_signal_semantics_contract.py`](../tests/test_signal_semantics_contract.py)。

本文件回答四个问题，并逐条给出代码位置：

1. Shadow 里的 SELL YES 到底是什么意思；
2. Paper 里的 SELL YES 又是什么，和「裸空」「BUY NO」是什么关系；
3. 重复 ACCEPT 如何被去重；
4. `net_edge_usd_per_share` 的单位、公式，以及方向、手续费、执行缓冲和部分成交如何进入这个数。

审计只读取代码与现有文档，不改动任何已落库数据、表结构或阈值。

---

## 1. 结论摘要

| 问题 | 结论 |
|---|---|
| Shadow 的 SELL YES | 对「在 bid 侧卖出 YES」的**估值/观察**，不是订单，也不建立空头。Shadow 没有持仓账本，因此不存在裸空。 |
| Paper 的 SELL YES | 只能**减少/关闭已有 YES 库存**。零库存直接 `NO_INVENTORY`；成交后持仓被夹到 0，永不为负。 |
| 是否映射成 BUY NO | **否**。方向词表只有 `buy_yes | sell_yes`；Paper 没有 NO token 的盘口解析，也没有 BUY NO 分支。 |
| 重复 ACCEPT（performance） | 按五元组 `(market_id, target_date, model_name, direction, mode)` 合并为 1 个 episode，`observation_count` 记录重复次数。 |
| 重复投递（paper） | `paper_signals.decision_id` 是主键，`ON CONFLICT DO NOTHING`，重复行只记 `signal_duplicate`，不产生第二笔交易。 |
| `net_edge_usd_per_share` | 每**已成交股**的 USD 期望净收益；手续费与执行缓冲都已扣除。部分成交时除以 `filled_shares`，不是 `proposed_shares`。 |
| Shadow 与 Paper 的独立性 | Shadow 只写导出文件；Paper 只读导出、只写自己的库。Paper 的拒绝/成交不会回写 Shadow 决策。 |

---

## 2. SELL YES 的语义

### 2.1 Shadow：对 bid 侧的估值，不是订单，也不是裸空

入口在 [`shadow/decision.py`](../src/weatherquant/shadow/decision.py)。

```text
Direction.BUY_YES  -> Side.BUY   -> 走 asks（ask 侧）
Direction.SELL_YES -> Side.SELL  -> 走 bids（bid 侧）
```

四个与方向有关的位置全部一致地读取 YES token 的同一本盘口：

| 位置 | BUY_YES | SELL_YES | 说明 |
|---|---|---|---|
| `Direction.side` | `Side.BUY` | `Side.SELL` | 决定 `build_fill_plan` 走哪一侧 |
| `_market_price()` | `best_ask` | `best_bid` | 记录「这一方向会成交的价格」 |
| `_side_depth()` | ask 侧名义深度 | bid 侧名义深度 | 流动性闸门按被交易的一侧计算 |
| `_blocking_defect()` | `NO_ASK_SIDE` / `NON_MONOTONIC_ASK` | `NO_BID_SIDE` / `NON_MONOTONIC_BID` | 只有被交易的一侧损坏才阻断 |

**关键语义**：Shadow 包内没有持仓、没有库存、没有下单客户端（见
[`docs/shadow-risk-rules.md`](shadow-risk-rules.md) §1）。因此 `SELL_YES`
不是「先借入再卖出」的裸空，而是：

> 在决策时点，如果按真实 bid 侧逐档卖出 100 股 YES，扣掉手续费和执行缓冲后，
> 模型自己的概率认为这笔「现在卖出、结算时承担 YES 赔付」的交易期望是多少。

它是一次**估值观察**。`ACCEPT` 只表示所有闸门通过且这个期望为正、达到阈值，
不表示系统持有 YES，也不表示会真实卖出。中价（midpoint）只用于分析，
从不作为成交价（[`markets/models.py`](../src/weatherquant/markets/models.py) 的
`midpoint_usd_per_share` 注释）。

### 2.2 Paper：只平/减已有 YES 库存，绝不裸空，绝不映射 BUY NO

Paper 的入口分三层，SELL YES 在每一层的含义都是「卖出已有 YES」：

1. **消费闸门** [`paper/consumer.py`](../src/weatherquant/paper/consumer.py)：
   只允许 `LIVE + ACCEPT` 进入账户；其余记录 `NOT_LIVE` / `NOT_ACCEPT` /
   `STALE_SIGNAL`。这一步不检查库存。
2. **库存闸门** [`paper/risk.py`](../src/weatherquant/paper/risk.py)
   `RiskGate.before_fill()`：
   `SELL_YES` 且 `held_shares <= 0` → 在任何定价之前拒绝，原因
   `NO_INVENTORY`，detail 明确写 `no naked shorts`。
3. **缩量** `RiskGate.size()`：卖出的请求量取
   `min(proposed_shares, held_shares)`；若缩量后低于交易所最小下单量，
   同样以 `NO_INVENTORY` 拒绝（库存不足以形成可下单的卖出）。
4. **定价** [`paper/fills.py`](../src/weatherquant/paper/fills.py)：
   `side = Side.BUY if direction == "buy_yes" else Side.SELL`，卖单走 bid 侧。
5. **台账** [`paper/ledger.py`](../src/weatherquant/paper/ledger.py)
   `apply_sell()`：
   - `fill.filled_shares > held + eps` → `InsufficientInventory`；
   - `remaining_shares = max(held - filled_shares, 0.0)`，持仓永不为负；
   - 现金按 `fill_cost_usd - fees_usd` 入账，执行缓冲**不进**台账
     （它不是场所收费，只是定价余量）。

数据库层还有第二把锁：`paper_positions.shares` 带
`CHECK (shares >= -1e-9)`（[`0004_paper_ledger.sql`](../src/weatherquant/storage/migrations_paper/0004_paper_ledger.sql)）。

**BUY NO 不存在**：

- `Direction` 只有 `buy_yes` / `sell_yes`（[`paper/events.py`](../src/weatherquant/paper/events.py)）；
- `FillEngine` 只把 `buy_yes` 映射到 `Side.BUY`，其余映射到 `Side.SELL`；
- `RiskGate` 明确不实现 BUY NO：用 YES 盘口或中价给 NO 定价等于伪造一个
  没人报过的价格；将来若引入 NO 方向，必须先解析并验证 NO token 自己的 L2 盘口。

`expected_net_profit_usd_sell()` 的公式在**收益形状**上与持有一个 NO 头寸等价，
但这只是代数上的估值等价，**不是订单映射**：本系统不会把 SELL YES 信号变成
BUY NO 动作。该函数的文档已按此语义修正。

### 2.3 Shadow 与 Paper 的不对称是有意的

Shadow 的 SELL YES 没有库存概念（不持有任何东西），Paper 的 SELL YES 必须有库存。
因此：

- 一个 Shadow 的 SELL YES `ACCEPT` **永远不会**自动变成 Paper 的卖出成交；
- Paper 可以因为 `NO_INVENTORY` 拒绝一个 Shadow 已经 `ACCEPT` 的信号；
- 这是分层风控的正常结果，不是数据不一致。

---

## 3. 重复 ACCEPT 的去重

### 3.1 Performance：五维 episode

实现：[`performance/episodes.py`](../src/weatherquant/performance/episodes.py)，
契约：[`docs/performance-data-contract.md`](performance-data-contract.md) §2。

`EpisodeKey = (market_id, target_date, model_name, direction, mode)`，五个字段
全部参与身份判定。规则：

| 输入 | 动作 |
|---|---|
| 首次 ACCEPT（该 key 无进行中 episode） | 开新 episode |
| 同 key 连续 ACCEPT，间隔 ≤ `gap_window_seconds`（默认 900s） | 归入当前 episode，`observation_count += 1` |
| 同 key 出现非 ACCEPT | 关闭 episode，`end_reason = state_change` |
| 同 key 间隔 > 900s | 关闭；若期间无任何决策 → `gap_data_missing`，否则 → `gap_signal_break` |
| 不同 `direction` 或不同 `mode` | **不同 key**，各自独立成 episode |

汇总层（`EpisodeSummary`）同时发布两个不可互相替代的数：

- `opportunity_episodes`：机会数（去重后）；
- `accept_observations`：ACCEPT 观测数（重复评估的总次数）。

因此「同一 key 的 3 次 ACCEPT」= 1 个机会、3 次观测；不同方向或不同模式
各算一个独立机会。`unique_opportunity_edge_sum` 使用每个 episode 的
`first_edge`，`observation_edge_sum` 使用 `mean_edge * observation_count`，
两者口径不同且都带 `statistical_scope`。

### 3.2 Paper：`decision_id` 主键

实现：[`paper/store.py`](../src/weatherquant/paper/store.py) `record_signal()`。

```sql
INSERT INTO paper_signals (...) VALUES (...)
ON CONFLICT (decision_id) DO NOTHING
RETURNING decision_id
```

- 同一 `decision_id` 再次投递（游标回退、备份恢复、重复发布）插入 0 行；
- 消费者把这次投递计为 `duplicates`，并记录 `signal_duplicate` 质量事件；
- 信号行、成交行、持仓和现金都不会出现第二份。

成交层还有第二重幂等：`paper_fills` 以 `decision_id` 为冲突键，台账条目
`ledger_entry_id(kind, decision_id)` 也是确定性的，重放结算不会二次扣款。

### 3.3 两类去重的边界

- Performance 的 episode 去重回答「这是几个**机会**」，作用在**读模型**上；
- Paper 的 `decision_id` 去重回答「这条信号是否已经进过账户」，作用在**执行**上。

两者互不替代：同一个 episode 可以对应很多条 decision 行，但只有每一条
`decision_id` 各自最多成交一次。

---

## 4. `net_edge_usd_per_share` 的公式与单位

唯一实现：[`execution/pricing.py`](../src/weatherquant/execution/pricing.py)。

### 4.1 记号

```text
S            已成交股数（filled_shares）[shares]
P            plan.avg_price_usd_per_share = fill_cost_usd / S   [USD/share]
q            模型概率 [0, 1]
fill_cost    sum(price_i * shares_i)，逐真实档位求和 [USD]
fee          C * fee_rate * P * (1 - P)，四舍五入到 5 位小数 [USD]
buffer       fill_cost * buffer_fraction [USD]
net_edge     USD per filled share [USD/share]
```

`fee_rate` 缺失时抛 `FeeStructureUnknown`，绝不当作 0；`buffer_fraction`
是定价余量，不是场所收费。

### 4.2 BUY YES

```text
total_cost = fill_cost + fee + buffer                 [USD]
E[net]     = q * S - total_cost                       [USD]
net_edge   = E[net] / S
           = q - P - fee/S - buffer/S                 [USD/share]
```

### 4.3 SELL YES

```text
net_proceeds = fill_cost - fee - buffer               [USD]
E[net]       = net_proceeds - q * S                   [USD]
net_edge     = E[net] / S
             = P - fee/S - buffer/S - q               [USD/share]
```

这里 `P` 是 bid 侧的 VWAP。SELL 的正 edge 表示「bid 比模型认为的 YES 价值高」，
即现在卖出比持有到结算更划算；它不代表做空收益。

### 4.4 ACCEPT 闸门

[`shadow/decision.py`](../src/weatherquant/shadow/decision.py)：

```text
edge <= 0                               -> NEGATIVE_NET_EDGE (REJECT)
0 < edge < min_net_edge_usd_per_share   -> EDGE_BELOW_THRESHOLD (REJECT)
edge >= min_net_edge_usd_per_share      -> 通过该闸门
```

当前阈值文件 `configs/shadow-thresholds.json`：`min_net_edge_usd_per_share = 0.02`
（USD/share）。因此完全可能出现「毛 edge > 0」但「扣费后 net edge 低于阈值」的
情况，这类决策是 `REJECT/EDGE_BELOW_THRESHOLD`，不是 `ACCEPT`。

### 4.5 部分成交

`build_fill_plan()` 在盘口深度不足时返回**部分成交计划**，`net_edge` 用
`filled_shares` 作分母：

```text
net_edge = (q * filled_shares - total_cost) / filled_shares
```

即「每一股**实际成交**的净 edge」。它**不是**「按 proposed_shares 摊薄后的
机会 edge」。Shadow 的 `require_complete_fill = true` 会把部分成交判为
`INSUFFICIENT_LIQUIDITY`（REJECT），但该行仍会记录以 `filled_shares` 为分母的
`net_edge`，作为「为什么被拒」的证据。Paper 同样默认
`require_complete_fill = True`，部分成交记 `INSUFFICIENT_CAPACITY`，不落成交。

---

## 5. Shadow 与 Paper 的独立性

| 方向 | 事实 | 代码依据 |
|---|---|---|
| Shadow → Paper | Shadow 只发布 append-only decisions export；Paper 只读该导出 | `paper/consumer.py` 顶部注释；`SignalConsumer` 从不打开 `shadow.duckdb` |
| Paper → Shadow | Paper 只写自己的 `paper_*` 表；没有任何反向写入 | `paper/store.py`、`paper/fills.py`、`paper/ledger.py` |
| Shadow ACCEPT ⇒ Paper 成交？ | 否。Paper 还要过消费闸门、库存闸门、盘口证据、sizing、定价后复核、台账 | `paper/consumer._gate`、`paper/risk.py`、`paper/fills.py` |
| Paper 拒绝/成交 ⇒ 改 Shadow 决策？ | 否。导出文件与 Shadow 行保持不变 | 契约测试逐字节断言 |
| 重复投递 ⇒ 第二笔交易？ | 否。`decision_id` 主键 + `signal_duplicate` | `paper/store.record_signal` |

---

## 6. 发现（findings）

### F1（已修复，仅文档）`expected_net_profit_usd_sell` 的「等价于 BUY NO」措辞

原 docstring 写「Equivalent to buying NO at the same book」。这在收益形状上是
代数等价，但容易被读成「SELL YES 应映射为 BUY NO 订单」，与本系统的执行契约
相反。已改为明确说明：这是对**卖出/平掉 YES 头寸**的估值，Paper 只允许平/减
已有库存，绝不生成 BUY NO 动作，也不做裸空。

### F2（已修复，仅文档）`paper/risk.py` 模块文档的「只有空仓才拒绝」

原文写「only an empty position is a refusal」，但 `size()` 还会在
「持有量缩量后低于交易所最小下单量」时以 `NO_INVENTORY` 拒绝。已把文档
改为反映这两个条件，行为不变。

### F3（保留，记录）Performance 胜率对 SELL YES 明确 `UNVERIFIABLE`

[`performance/winrate.py`](../src/weatherquant/performance/winrate.py) 对
`direction == "sell_yes"` 的 episode 返回
`UNVERIFIABLE, "SELL YES position semantics unproven"`。这是保守且正确的：
SELL YES 的盈亏取决于库存成本基础，而 episode 行只有 bid、概率和 edge，
不足以独立判定一笔「平仓」的已实现盈亏。后果是 SELL YES 的 edge 会进入
edge 汇总，但不进入胜率分母。**本次不改**，因为改动会改变已发布指标的语义。

### F4（保留，记录）消费层 `ACCEPTED` 不等于成交

Paper 消费者只按 `LIVE + ACCEPT + 新鲜度` 记录信号，SELL YES 无库存时
仍会先记为 `outcome = ACCEPTED`，随后由 FillEngine 改为
`REFUSED / NO_INVENTORY`。这是分层设计（消费闸门 vs 风险闸门），不是重复
扣费；读数据时必须以最终 `outcome` 为准，不能把 `ACCEPTED` 当成已成交。

### F5（保留，记录）部分成交的 `net_edge` 分母是 `filled_shares`

如 §4.5 所述。Shadow 的 `require_complete_fill = true` 使这类行一定为
`REJECT`，但同一 `net_edge` 若与 `proposed_shares` 的机会规模相乘会高估
机会收益。汇总层已经用 `unique_opportunity_edge_sum`（每个 episode 的
`first_edge`）而不是「edge × proposed」来避免这一点；本审计再次明确该口径。

### F6（保留，记录）Paper 卖出缩量不预检 bid 深度

`RiskGate.size()` 对卖出只按库存缩量，不按 bid 深度缩量；若库存大于盘口
可承接量，FillEngine 会因 `require_complete_fill` 记
`INSUFFICIENT_CAPACITY`，而不是部分成交。这是保守选择（部分成交是另一笔
交易），不是裸空风险；本次不改。

### F7（保留，记录）Shadow 无库存概念，Paper 有

这是两层的职责差异，不是不一致。Shadow 的 SELL YES 是对 bid 的估值；
Paper 的 SELL YES 是平/减库存。任何「Shadow ACCEPT 必须对应 Paper 成交」
的假设都是错误的，契约测试对此有显式断言。

---

## 7. 可执行契约测试覆盖

[`tests/test_signal_semantics_contract.py`](../tests/test_signal_semantics_contract.py)：

| 用例 | 断言 |
|---|---|
| `test_buy_prices_from_ask_and_sell_prices_from_bid_never_midpoint` | BUY 从 ask、SELL 从 bid 定价；两者都不等于 midpoint |
| `test_hand_computed_buy_and_sell_edges_include_fees_and_buffer` | 两个方向的 `net_edge` / `expected_net_profit_usd` 与手算值一致，手续费与缓冲逐项匹配 |
| `test_gross_edge_above_threshold_but_net_below_is_not_accept` | 毛 edge > 阈值，但扣费后净 edge < 阈值 → `REJECT / EDGE_BELOW_THRESHOLD` |
| `test_sell_edge_sign_flips_with_model_probability` | SELL 的 edge 符号由 `bid - q` 决定，正负对应 ACCEPT / NEGATIVE_NET_EDGE |
| `test_net_edge_is_per_filled_share_for_partial_fills` | 部分成交时 `net_edge` 以 `filled_shares` 为分母，不能乘以 `proposed_shares` |
| `test_partial_fill_is_rejected_but_edge_recorded_per_filled_share` | Shadow 部分成交 → `INSUFFICIENT_LIQUIDITY`，但 `net_edge` 仍按已成交股记录 |
| `test_repeated_accepts_are_one_episode_and_direction_mode_separate` | 同 key 3 次 ACCEPT = 1 episode / 3 observations；不同 direction、不同 mode 各自成 episode |
| `test_sell_yes_without_inventory_is_refused_before_pricing` | 零库存 SELL YES → `NO_INVENTORY`，无成交 |
| `test_sell_yes_only_reduces_inventory_and_never_opens_a_no_position` | 有库存时卖出只减仓到 0；成交方向是 `sell_yes`/`sell`；不存在 `buy_no` 或 NO token 持仓 |
| `test_shadow_accept_and_paper_outcomes_are_independent` | Shadow ACCEPT 可被 Paper 拒绝；Paper 成交/拒绝不改写导出；重复投递不产生第二笔成交 |

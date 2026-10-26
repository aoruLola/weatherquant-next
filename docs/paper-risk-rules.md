# Paper 风控规则(mvp-v1,WQN-43)

对应文件:[`configs/paper-risk.json`](../configs/paper-risk.json)。与 shadow 的
`docs/shadow-risk-rules.md` 同一纪律:**先验设定、逐条理由、版本化、不宣称最优**。

## 账户设定

- 初始资金 100 USDC(虚拟,`paper_accounts.initial_capital_usdc`)。
- 不杠杆:任何买入的现金流出不得超过 available。
- 不裸空:SELL YES 必须有足额库存;库存为零直接拒绝(`NO_INVENTORY`)。

## 规则

| 规则 | 值 | 单位 | 理由 |
|---|---|---|---|
| `max_opportunity_fraction` | 0.10 | 初始资金占比/单机会 | 单次成交的现金流出 ≤ 开户资金的 10%(100 USDC 账户即 10 USDC)。按账户而非按市场定标:要点是**没有任何单一信号能主导台账**,与其 edge 主张无关。 |
| `max_market_exposure_fraction` | 0.20 | 初始资金占比/市场(累计) | 同一市场全部持仓的成本基合计 ≤ 开户资金的 20%。**累计而非单笔**:同市场多个 Episode 爬的是同一根上限,小额多次买入与一次大额买入是同一类对象。 |

## 执行方式

- **Sizing,不只是拒绝**:shadow 提议的是研究用规模(`proposed_shares=100`,
  其阈值文件明确写着 "Shadow only - no order is ever placed and no capital
  exists")。paper 账户在自身台账意义上是真钱,所以引擎把机会**缩量**到
  预算能负担的量:`min(提议股数, 预算股数, 市场余量股数, 可用现金股数)`,
  卖出侧 `min(提议, 实际持有)`。绝不超出提议,绝不超出上限。
- **闸门次序**(因果序,最早被破坏的前提最先报告):
  1. 消费闸门(契约/mode/新鲜度,WQN-40);
  2. 库存闸门(卖出,无需盘口);
  3. 盘口证据闸门(缺失/过期/身份,WQN-41);
  4. sizing(预算/敞口/可用现金,按盘口最优价换算,费率上界预留);
  5. 定价后复核(真实跨档流出 ≤ 各上限——深档比最优价贵,这里是超预算交易的最后一道闸);
  6. 台账结算(无资金/无库存的最后一道锁,WQN-42 的 CHECK 兜底)。
- 拒绝原因:`RISK_BUDGET_EXCEEDED`(单机会预算)、`RISK_EXPOSURE_EXCEEDED`
  (单市场敞口)、`INSUFFICIENT_FUNDS`(可用现金)、`NO_INVENTORY`(库存)。
  每条拒绝的 detail 都带上规则版本,便于事后审计是哪一版规则拒的。

## 关于 BUY NO

当前 shadow 的方向词表只有 `buy_yes | sell_yes`,**没有任何 BUY NO 信号能
到达本引擎**。这是有意的保守缺口:用 YES 盘口(或 midpoint)给 NO 定价,
等于伪造一个没人报过的价格。将来若引入 NO 方向,必须先解析并验证 NO
token 自己的 L2 盘口,再在 `configs/paper-risk.json` 中加入对应规则——
而不是把 YES 的价格换算过去。

## 修订纪律

修改任何值必须:提升 `version`、在 PR 中对照本文件说明理由、且**不得**
依据这些规则产出的模拟结果来调参。这些规则还没有产生过任何被评判的
结果,这正是保持它们先验状态的原因。


比例限额以 paper_accounts 落库的不可变开户金额为基数。fill/serve 不使用本次进程环境中的初始金额重算既有账户限额。

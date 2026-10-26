# ADR 0001: Paper Trading 架构、事件契约与独立持久化状态库(WQN-39)

状态: 已接受(2026-10-26)
关联: WQN-39;后续 WQN-40/41/42/43/44/47 在此架构上增量实现
约束来源: README.md「硬性边界」与「量化原则」;Jira WQN-39 验收条款

## 背景

Shadow 引擎(P2-0)已经 24 小时观察决策并发布两样东西:决策历史导出
(`decisions-export/parts/*.jsonl`,append-only、带 cursor)与报告快照。
研究侧(performance)已经按只读方式消费它。现在需要一个 **paper trading
引擎**:把 shadow 的 LIVE ACCEPT 决策当作" proposed trade",在一个纯模拟的
100 USDC 账户里走完 接受→成交→记账→结算 的全流程,为将来的策略评估提供
逐事件可审计的账本。

它必须从第一天起就**不可能**变成真实交易引擎。README 的硬性边界禁止
真实下单、钱包、私钥、交易所凭证代码,本 ADR 说明这些边界如何落在结构上,
而不是落在文档措辞上。

## 决策

### D1. 独立包、第四个数据库文件、单向解耦

```
collector ──写──> raw/{ws,rest,gamma}/YYYY-MM-DD.jsonl   (paper 只读)
shadow    ──写──> shadow.duckdb(独占租约)
          ──发布──> reports/decisions-export/parts/*.jsonl   ← 唯一决策通道
paper     ──写──> paper.duckdb(独占租约,独立 migrations_paper/ 目录)
```

* `src/weatherquant/paper/` 是新顶层包;存储是 `storage/paper.py` +
  `storage/migrations_paper/`,复用同一个校验和式前向迁移器。
* 依赖方向固定:**paper → 已发布的文件与共享原语**;`shadow`、`collector`
  永远不 import `paper`。paper 不 import `storage.shadow`(不打开
  shadow.duckdb,连只读也不),不 import `data.worker` 等采集写路径。
* paper 可以 import `shadow.export` 中**纯读文件**的工具与其 cursor 约定、
  `data.books.parse_book_payload`(保证 book_id 与 collector/shadow 逐字节
  一致,可交叉验证)、`execution.pricing`(唯一的成本计算来源,量化原则 8)。

### D2. 默认关闭,启用即审批

`PaperSettings.paper_enabled` 默认 `False`。`init`/后续 `run` 在未显式设置
`WQ_PAPER_ENABLED=true` 时以命名错误拒绝启动。解析器故意只接受
`true/1/enabled/false/0/disabled`,其它任何值(包括拼写错误)是异常而不是
"视为关闭" —— `WQ_PAPER_ENABLED=ture` 必须被看见。把该开关在部署里显式
打开的动作,就是验收条款中的"单独审批"的落地形式;除此之外没有任何
代码路径、环境变量组合或默认值能把引擎拉起来。

### D3. 100 USDC 虚拟账户,首写为准

账户是单例行(`paper_accounts`,`account_id='paper-main'`)。`init` 创建
它;再次 `init` 只有在金额完全一致时才是 no-op,金额不同则拒绝 ——
开户资金是账本所有历史数字的基准,静默改基准则账本失去意义。

### D4. 事件契约:SignalEvent

决策事件 = decisions-export 的一行,经 `paper.events.parse_signal_event`
解码。契约规则:

1. **mode 是闸门**:只有 `mode=LIVE 且 state=ACCEPT` 的事件
   (`tradeable=True`)才允许被账户考虑。REPLAY 是"现在重演历史",其输入
   在其决策时点不可得,进入 LIVE 账户就是把回测伪装成实盘。
2. **malformed ≠ 拒绝**:解码失败(`EventError`,稳定错误码)是数据质量
   事件,永远不变成 signal 行;解码成功但不 tradeable 是**正常业务结果**,
   必须以 `(outcome, refusal_reason, refusal_detail)` 落库可追踪。
3. **证据随事件走**:`book_id`、`snapshot_id`、venue/collector 时钟、
   fee schedule、thresholds 版本随行携带;完整原始行以 `event_json`
   原样存档,审计不依赖 dataclass 的字段取舍。

### D5. 幂等与崩溃模型(与 shadow 相同,因为崩溃不挑引擎)

* 自然键:`paper_signals.decision_id` 就是 shadow 的确定性决策 id。
  同一信号重启重放由主键吸收 —— "不产生第二笔交易"不是消费者的一次检查,
  而是主键的性质。
* cursor(`paper_state`)与它所描述的行在**同一个事务**里提交:
  崩溃要么整体回放(被主键吸收),要么一起回退;不存在"cursor 前进而
  行没写"或反之。
* `RETURNING` 区分"新插入"与"已存在",重放批次可以放心重跑。

### D6. 没有下单路径,也没有能长出下单路径的接口

paper 包内:无 venue client、无钱包、无私钥、无订单签名;不产生任何
对外下单请求。唯一规划中的网络访问是 WQN-44 的**只读**结算查询
(复用 `performance/settlement.py` 的注入式 getter,POST/签名类操作在
接口层面不存在)。所有"成交"都是本引擎数据库里的一行,由
`execution/pricing.py` 按真实盘口深度计算。

### D7. 记账:六个桶分开,一个守恒律兜底(WQN-42)

现金(available)、占用(occupied,持仓成本占用)、已实现盈亏(realized,
**只来自卖出**,未结算仓位一律没有已实现盈亏)、仓位(份额/均价/成本基)、
台账(append-only,每笔带现金流水余额)、估值(读取时计算,缺标记报
`MARK_UNAVAILABLE`,绝不补零、绝不静默跳过)分别建账。守恒律:

```
available + occupied == initial_capital + realized_pnl
```

三层执行:存储层 CHECK 拒绝负余额;`Ledger.verify()` 用台账重放对账
(重启恢复的证明);`entry_id = hash(kind|ref)` 让重放结算天然幂等
(不重复扣款/释放)。现金金额用**交易所真实口径**:买入流出 =
fill + fees,卖出流入 = fill − fees;execution buffer 是影子定价的
保守边际,不是现金,永不进台账。每次成交 = 台账变动 + 仓位变动 +
fill 行 + 信号状态迁移,同一事务;任何一层拒绝(无资金/无库存)则
整笔不落账。

### D8. 结算:只认官方已验证答案,修订是补偿事件(WQN-44)

结算输入是**发布的** settlement feed(JSONL,按 market+revision 键),
词汇表复用 `performance/settlement.py` 的五态。规则:

1. 只有 `RESOLVED` 且 payout ∈ {0,1} 移动台账;PENDING / DISPUTED /
   UNAVAILABLE / UNVERIFIABLE 一律只记录不应用——**未结算永远没有
   已实现盈亏**;RESOLVED 之外的任何记录带 payout 直接是解析错误。
2. 修订不编辑历史:更高 revision 且 payout 变化 → 台账追加 CORRECTION
   补偿条目(Δ = (新−旧)×结算份额),原 SETTLE_PAYOUT 原样保留。
3. 不可自动补偿的情形**标记人工复核、余额不动**:回拨会让现金变负
   (钱已经花掉,自动回拨等于把审计问题换成资不抵债)、或 RESOLVED
   之后又 DISPUTED(最终答案未知,维持或撤销都是编造)。结算行带
   `review_required` 与理由,`weatherquant-paper status`/settle 输出可见。
4. 幂等:`UNIQUE(market_id, revision)` 自然键 + `(applied revision)`
   判断,重放整个 feed 是 no-op;更旧 revision 视为被取代。

## 后果

* 正面:四个引擎生命周期(collector/shadow/research/paper)互不可写;
  paper 的全部状态可用 `weatherquant-paper status` 离线审计;崩溃/重启
  行为可由主键与事务边界推理,不需要外部协调。
* 代价:决策通道是文件发布,存在最多一个 export 周期
  (`shadow_export_seconds`,默认 300s)的延迟;paper 的成交时点因此
  晚于决策时点,所有时效判断以 `decision_time_utc` 与归档时钟为准,
  不以消费时钟为准。
* 未决(后续 ADR/工单):风控闸门的取值与版本化(WQN-43)、结算源的
  具体形态(WQN-44)、以及任何 NAS 部署验收(本机无法验证的部分在
  各 PR 中标注)。


同一消费批次的待成交信号按决策时间排序，decision_id 只用于同一时刻的稳定排序，避免哈希顺序反转先买后卖。


结算确认和多次更正保留最新 payout，但份额始终追溯原始 SETTLE_PAYOUT 台账。更正不因最新 revision 没有 positions 明细而退化成零金额。


成交保持批次内业务时间顺序。回归用同批先买后卖验证库存释放和两笔成交均落账，避免按哈希 ID 提前拒绝卖单。

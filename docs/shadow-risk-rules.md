# 影子交易引擎：风险闸门与决策状态机

2026-10-11。实现：`src/weatherquant/shadow/decision.py`、
`src/weatherquant/shadow/config.py`、`configs/shadow-thresholds.json`。
可执行版本：`tests/test_shadow_decision.py`（本文件中的每一条断言在测试里都有对应用例）。

---

## 1. 这是什么，不是什么

影子模式是**实时决策观察系统**。它：

| 做 | 不做 |
|---|---|
| 实时生成概率预测 | 真实下单 |
| 读取真实 L2 盘口 | 接入钱包 / 私钥 |
| 按真实成本计算预期净收益 | 管理任何真实或虚拟资金 |
| 判断订单**是否可成交** | 模拟持仓与成交回放 |
| 记录 ACCEPT / REJECT / NO_TRADE | 为了活跃而放宽阈值 |

**边界是结构性的**：`weatherquant.shadow` 包里不存在下单客户端、不存在订单 ID、
不存在持仓账本，也没有任何"开关"可以打开它们。这不是配置项，是代码事实。

> **当前模型没有证明优于市场。** 样本外基线：模型 Brier 1.49，市场 1.03
> （见 [`research/baseline-comparison.md`](../research/baseline-comparison.md)）。
> 系统连续数日输出 NO_TRADE 是**完全正常**的结果。
> 全部决策都是 NO_TRADE、只要理由正确数据可靠，同样是有效运行。

---

## 2. 决策状态机

一次评估 = 一个 `(市场, 方向)` 组合，产出且仅产出一个终态。

```
CANDIDATE ──► NO_TRADE | DATA_UNAVAILABLE | REJECT | ACCEPT
```

`CANDIDATE` 只在评估**开始写入、尚未写入结果**的间隙存在。重启后仍停留在
`CANDIDATE` 的行 = 一次被打断的评估，这是崩溃在数据里唯一可见的形式。

### 2.1 严格定义

| 状态 | 定义 | 判定 |
|---|---|---|
| **NO_TRADE** | 市场本身对我们不开放 | `MARKET_CLOSED`（inactive / 未开启下单 / closed / 目标日已过） |
| **DATA_UNAVAILABLE** | **必需输入缺失** | feed 静默、无盘口、无可发布的预报、无模型工件、元数据无法解析 |
| **REJECT** | 输入**存在**，但闸门拒绝 | 盘口陈旧或单边、手续费未知、深度不足、时钟不一致、edge 为负或过小 |
| **ACCEPT** | 全部闸门通过 | `net_edge >= min_net_edge_usd_per_share` |

**分界线是机械的**：

```
缺失 → DATA_UNAVAILABLE       存在但被拒 → REJECT
```

这一条由规则决定，不由调用点决定。两个失败态之间含糊其辞，是让报告变得
不可读的最快方式。

**优先级**（三条，按序）：

1. `NO_TRADE` —— 市场关闭时，其它一切问题都只是附带信息；
2. `DATA_UNAVAILABLE` —— 只要有一个必需输入缺失，edge 就无从计算，
   "我们无法判断"才是诚实的结论，即使同时还有闸门失败；
3. `REJECT` —— 输入齐全，闸门说不。

### 2.2 状态不是"信号强度"

`ACCEPT` 在影子模式下只是一次**观察**。它不表示这笔交易会盈利，
只表示：在决策时点，所有输入都在、都新鲜、成本已计入、模型自己的期望为正。

---

## 3. 拒绝原因

16 个原因，全部来自规范；不存在 `generic_error` 之类含糊兜底。

```
NO_FORECAST                    FORECAST_NOT_YET_AVAILABLE
NO_VALID_MODEL                 MODEL_STALE
NO_ORDERBOOK                   STALE_ORDERBOOK
INCOMPLETE_ORDERBOOK           INSUFFICIENT_LIQUIDITY
UNKNOWN_FEE                    NEGATIVE_NET_EDGE
EDGE_BELOW_THRESHOLD           TIME_MISMATCH
MARKET_CLOSED                  COLLECTOR_DISCONNECTED
DATA_QUALITY_FAILURE           EVALUATION_ERROR   （规范之外补充，见 §3.2）
```

### 3.1 因果序与主原因

一条记录可以带多个原因（规范允许），但**主原因唯一**，取
`CAUSAL_ORDER` 中最靠前的那个 —— 即"最早断裂的前提"：

```
MARKET_CLOSED → COLLECTOR_DISCONNECTED → DATA_QUALITY_FAILURE → NO_ORDERBOOK
→ NO_FORECAST → FORECAST_NOT_YET_AVAILABLE → NO_VALID_MODEL → MODEL_STALE
→ STALE_ORDERBOOK → TIME_MISMATCH → INCOMPLETE_ORDERBOOK → UNKNOWN_FEE
→ INSUFFICIENT_LIQUIDITY → NEGATIVE_NET_EDGE → EDGE_BELOW_THRESHOLD
→ EVALUATION_ERROR
```

后果不随闸门触发顺序漂移：「市场关闭且盘口陈旧」永远报告
`MARKET_CLOSED`，即使盘口检查先跑。

### 3.2 为什么有 EVALUATION_ERROR

规范要求"拒绝原因不能仅写成 generic_error"。但一个未预料的异常**必须
留下痕迹**，吞掉它比含糊更糟。`EVALUATION_ERROR` 是具体的：`detail` 里
放的是 `类型名: 异常文本`，可 grep、可定位。它归入 `DATA_UNAVAILABLE`，
因为抛异常的那次评估没有算出 edge。

### 3.3 归属

| 原因 | 状态 |
|---|---|
| COLLECTOR_DISCONNECTED, NO_FORECAST, FORECAST_NOT_YET_AVAILABLE, NO_VALID_MODEL, NO_ORDERBOOK, DATA_QUALITY_FAILURE, EVALUATION_ERROR | DATA_UNAVAILABLE |
| MARKET_CLOSED | NO_TRADE |
| STALE_ORDERBOOK, TIME_MISMATCH, INCOMPLETE_ORDERBOOK, UNKNOWN_FEE, INSUFFICIENT_LIQUIDITY, NEGATIVE_NET_EDGE, EDGE_BELOW_THRESHOLD, MODEL_STALE | REJECT |

---

## 4. 阈值

文件：[`configs/shadow-thresholds.json`](../configs/shadow-thresholds.json)。

**为什么是文件不是环境变量**：能在"产生结果的那台机器"上被改宽、且改了
之后不留痕迹的阈值，事后无法审计。改阈值 = 改仓库里的一个文件 + 递增
`version` + 评审 [`docs/shadow-risk-rules.md`](shadow-risk-rules.md)。

**设定依据：先验，不拟合。** 全部取自 Polymarket 公布的费率表与**采集器
实测的**刷新周期，**没有一个**用本系统的决策结果拟合过。用同一批结果调参、
再用同一批结果证明策略有效，是"发明"策略而不是"发现"策略。

| 阈值 | **v2** | 依据 |
|---|---:|---|
| `min_net_edge_usd_per_share` | 0.02 | 天气类 taker 费率峰值 0.05×0.25 = 0.0125/股（p=0.5），执行缓冲 0.5% 名义（p=1 时 0.005）。0.02 清掉最坏情形成本后余 0.0025 —— 尚未对模型技巧作任何假设。低于它，模型得比费率表本身吃掉的还要准。 |
| `max_snapshot_age_ms` | **90000** | **两次采集器扫描。** 单 token 实测刷新间隔 p50 = 37.9s（真实归档，66 token / 387 观测 / 13 分钟），扫描按序进行且每周期有 45s 预算；90s 容得下采集器能产出的最新盘口，又拒掉落后两个周期以上的 —— 那意味着该 token 已经没人再看。 |
| `max_forecast_age_hours` | 30 | 运行初始化后 12 小时才可发布（`PUBLISHABLE_LAG_HOURS`），每 6 小时一次，健康时运行年龄 12–18h。30h 容忍两次上游中断，又不至于让 lead bucket 已经移位。 |
| `proposed_shares` | 100 | 薄盘口会显式表现为部分成交，而不是瞬间成交把自己的空洞藏起来；同时又不至于让深度成为有量市场的约束。纯影子 —— 不下单，没有本金。 |
| `require_complete_fill` | true | 部分成交不是"更小的单"，是**另一笔单**：未成交部分得重新对刚刚被我们扫穿的盘口定价。拒绝它，每一笔 ACCEPT 才可解释。 |
| `min_depth_usd` | 25 | 独立的第二道流动性地板，以美元计 —— 否则一张全部挂在 1 分钱的盘口能通过股数检查，却几乎一文不值。 |
| `max_decision_latency_ms` | 5000 | **仅用于报告，不是闸门。** 超过它，P95 指标标记本周期降级，让"评估器变慢"在变成准确性问题之前就可见。 |

### 4.1 v1 → v2 修正（2026-10-11）

v1 把 `max_snapshot_age_ms` 设为 15000，依据写的是"采集器每 5 秒重询一次盘口"。
**这个前提是错的**：5s 是轮询节拍，不是单 token 的刷新周期，采集器按序扫描
全部 token 且每周期有 45s 预算。对真实归档实测，p50 刷新间隔是 **37.9s** ——
在 15s 门槛下**超过一半的完全健康盘口会被判为陈旧**，这道闸门会无条件拒绝
一切，与市场本身无关。

修正记录在 `configs/shadow-thresholds.json` 的 `corrected_from` 字段里
（改了什么、为什么改、何时实测的）。它纠正的是一个**测错了的量**，不是放松
一个测对了的量：v1 连一条 ACCEPT 都还没产出过，实测发生在看到任何决策结果
之前，且文件的 `version` 已递增、可评审、可回溯。

---

## 5. 逐条闸门

按因果序执行；**能算的都算**，因此一条记录可以同时带多个原因。
但**绝不编造数字**：一旦某道闸门意味着算术没有意义（无费率、无概率、
无盘口），对应字段留 `NULL`，而不是填一个估计值。

### 5.1 时间一致性（规范 §5）

| 检查 | 原因 |
|---|---|
| `venue_ts > decision_time` | TIME_MISMATCH —— 场所时间戳在未来 |
| `collector_received_at > decision_time` | TIME_MISMATCH —— **采集器实际收到的时刻晚于决策时点，当时不可能知道** |
| `shadow_received_at > decision_time` | TIME_MISMATCH —— 本进程读到它的时刻晚于决策时点 |
| `collector_received < venue_ts` | TIME_MISMATCH —— 两个时钟不一致 |
| `forecast.received_at > decision_time` | TIME_MISMATCH —— 预报是在决策之后才取得的 |
| 盘口无 venue 时间戳 | STALE_ORDERBOOK —— 无法证明新鲜 |

**关键**：即使 venue timestamp 早于决策时点，只要 Collector 实际收到数据
的时间晚于决策时点，该盘口**不得使用**。负时间差、异常延迟、无法证明来源
—— 一律拒绝生成可执行信号。

### 5.2 预报可用性

```
publishable_at = run_time + 12h      （PUBLISHABLE_LAG_HOURS，basis='estimated'）
```

- `publishable_at > decision_time` → `FORECAST_NOT_YET_AVAILABLE`
  —— 不是"慢"，是"这份预报还不属于我们"。
- 年龄 > `max_forecast_age_hours` → `MODEL_STALE`（**存在但太旧** → REJECT，
  与"根本还没有"不同）
- `run_time > decision_time` → `FORECAST_NOT_YET_AVAILABLE`

**与严格回测的区分**：回测的截止点是**目标当地日开始**（防止目标日当天
发布的预报污染历史）；实盘影子允许在目标日当天决策 —— 这在实时是合法的。
两者**分别产出**：`usable_at(now)` 决定能否交易，
`research_eligible_at(target_date)` 记录是否可进入严格回测。
把其中一个套用到另一个身上，正是两者开始背离的方式。

### 5.3 模型版本与泄漏

- `prediction.training_cutoff_utc > decision_time` → `NO_VALID_MODEL`
  —— 参数是用决策时刻之后的数据拟合的，这本身就是泄漏。
  **在评估边界再查一次**，不只是在加载工件时查：这是最后一道防线。
- 工件缺失 / 未在 manifest 中固定 → `NO_VALID_MODEL`
- 该 lead bucket 无可用 σ → `NO_VALID_MODEL`
  （两个样本算出来的不是离散度；不借全局 σ）
- 括号阶梯两端不封闭 → `DATA_QUALITY_FAILURE`

### 5.4 盘口

| 情形 | 原因 | 说明 |
|---|---|---|
| 无该 token 的盘口记录 | NO_ORDERBOOK | 输入缺失 → DATA_UNAVAILABLE |
| 缺成交所需的一侧（买单缺 ask / 卖单缺 bid） | INCOMPLETE_ORDERBOOK | 盘口**存在**但不能给这一侧定价 → REJECT |
| 盘口交叉（CROSSED_OR_LOCKED） | INCOMPLETE_ORDERBOOK | 交叉状态下两侧价格都无意义，两个方向都拒 |
| 另一侧非单调 | **不阻断** | 与 `build_fill_plan` 同规则：只阻断被交易的一侧 + 交叉 |
| 年龄 > `max_snapshot_age_ms` | STALE_ORDERBOOK | 仍会算出 edge 作为证据，但状态是 REJECT |

单边盘口是**真实市场状态**（极端括号很常见），不是损坏。它在采集时就被
记录，决策层按方向分别处理。

### 5.5 成本与流动性

所有算术只发生在 `weatherquant/execution/pricing.py` —— 全库唯一一处。

**买入 YES**：从**真实 ask 侧**逐档走 `build_fill_plan(side=BUY)`。

```
cost    = fill_cost + fees + buffer
E[net]  = q × shares − cost
edge    = E[net] / shares
```

**卖出 YES**：从**真实 bid 侧**逐档走 `build_fill_plan(side=SELL)`。

```
net_proceeds = fill − fees − buffer
E[net]       = net_proceeds − q × shares
```

两者共用**同一个三项分解**（`ExecutionCost`），因此手续费不可能在买入时
被计入、卖出时被忘记，也不可能任一方向重复计费。中价（midpoint）
**从不**作为成交价 —— `OrderBook.midpoint` 存在只为分析，注释里写明了这一点。

| 检查 | 原因 |
|---|---|
| `fee_rate is None` | UNKNOWN_FEE —— **未报告的费率表不等于零费率**，拒绝而不是假设免费 |
| 请求量超出盘口 | INSUFFICIENT_LIQUIDITY |
| 成交不完整（`require_complete_fill`） | INSUFFICIENT_LIQUIDITY |
| 一侧深度 < `min_depth_usd` | INSUFFICIENT_LIQUIDITY |
| `proposed_shares < market.min_order_size` | INSUFFICIENT_LIQUIDITY —— 下不了的单不能算 ACCEPT |
| `edge <= 0` | NEGATIVE_NET_EDGE |
| `0 < edge < 阈值` | EDGE_BELOW_THRESHOLD |

---

## 6. 明确不做

| 不做 | 原因 |
|---|---|
| 真实下单 / 钱包 / 私钥 | 本阶段的定义就是观察，不是交易 |
| 用中价当作可成交价 | 中价记录的是"相信什么"，不是"能以什么价格成交" |
| 把增量消息当成完整盘口 | `price_change` 只带一个价位，没有序列号机制能安全重建状态（见 [`shadow-engine.md`](shadow-engine.md) §3） |
| 用未发布的预报运行 | `publishable_at > now` 一律拒绝 |
| 用决策之后才取得的行情 | 见 §5.1 |
| 读取含未来训练样本的模型参数 | 见 §5.3 |
| 修改生产 Collector 数据库 | 影子写自己的文件；见 [`shadow-engine.md`](shadow-engine.md) §2 |
| 与生产 Collector 争抢 DuckDB 写锁 | 结构上不可能：影子从不打开别人的库 |
| 放宽阈值以增加交易数量 | 阈值先验设定，改动需递增版本并评审 |
| 生成 Equity Curve | 没有成交就没有已实现收益；报告里 `equity_curve` 恒为 `null` |

---

## 7. 复现

```bash
uv run pytest tests/test_shadow_decision.py -v
uv run python -c "from weatherquant.shadow.config import load_thresholds; t=load_thresholds('configs/shadow-thresholds.json'); print(t.version, t.as_dict())"
```

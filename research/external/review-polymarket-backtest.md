# luuhongyii/polymarket-backtest 审计

> 审计对象: https://github.com/luuhongyii/polymarket-backtest
> 审计日期: 2026-10-10 | 审计方式: `git clone --depth 1` 后全文静态阅读（安装/运行被权限策略拒绝，测试运行结论均为静态推理）

## 0. 元信息（commit, 文件数, 语言, 最后提交）

- 克隆成功，HEAD = `16f3068689e9c5d1ec7cec73c8d32d207543cfab`，最后提交 `2026-09-23 11:17:06 +0200` "docs: adjust one word in README"。
- GitHub API 元数据：created 2026-06-13，pushed 2026-09-23，共 11 个提交，2 stars，0 forks，0 open issues，47 KB，default branch `main`，language Python，license spdx `MIT`。
- 文件：`git ls-files` = 42 个受跟踪文件。Python 源码 28 个文件 / 2458 行（`src/`），测试 5 个文件 / 196 行 / **9 个测试函数**，`examples/synthetic_strategy.py` 14 行。5 份 README（en/zh/ja/ko/es）。
- 项目定位（从代码而非 README 判断）：**通用 Polymarket 回测框架，主打 crypto up/down 5m/15m 市场的 "complete-set" 双边做市回放**。与天气完全无关——对代码与全部 markdown 执行 `rg -i "weather|temperature|nws|noaa|forecast|airport|ensemble|brier"`，**0 命中**。
- **致命发现**：`src/polymarket_backtest/data/` 整个子包（calendar、pit、schema、ingest、store、orderbook）**不在仓库中**。根因是 `.gitignore:10` 的 `data/` 规则（本意忽略下载的数据集）同时匹配了 `src/polymarket_backtest/data/` 目录。证据链：
  - `.gitignore:10` → `data/`
  - `src/polymarket_backtest/cli.py:9-11`、`backtest/engine.py:12`、`backtest/context.py:7-8`、`backtest/execution.py:8`、`audit/lookahead.py:5`、`backtest/orderbook_replay.py:8`、`examples/synthetic.py:5-6`、`tests/test_calendar.py:1`、`tests/test_pit.py:4` 均 import `polymarket_backtest.data.*`
  - `ls src/polymarket_backtest` 无 `data/` 目录；`git ls-tree HEAD src/polymarket_backtest/` 也无
  - README.md:52 却声称结构含 `data/  # market calendar, PIT helpers, parquet store`
  - 结论：**`import polymarket_backtest` 即失败（`__init__.py:3` → `backtest/__init__.py` → `context.py:7`），9/9 测试在发布状态下全部无法收集**。README 宣称的 "point-in-time safety / lifecycle calendar" 核心实现代码根本未发布。

## 1. 许可证

- `LICENSE:1-21`：完整标准 **MIT License** 文本，`LICENSE:3`：`Copyright (c) 2026 Polymarket Backtest Contributors`。
- `pyproject.toml:11`：`license = { text = "MIT" }`；`pyproject.toml:18`：classifier `License :: OSI Approved :: MIT License`；GitHub API `spdx_id: MIT`。三处一致。
- **OSI-approved：是**（MIT）。
- 复用义务：仅需在副本中保留版权声明与许可文本（`LICENSE:12-14`）。**允许**使用、修改、合并、分发、再授权、商用与闭源衍生（`LICENSE:5-9`），无 copyleft、无专利条款、无地域/领域限制。
- 结论：**可依法复用（含商业化）**，保留 LICENSE 即可。

## 2. 天气模型

**未发现。** 仓库无任何天气模型、气象数据源、确定性/集合预报代码（`rg` 见第 0 节，代码与文档 0 命中）。实际策略对象是 crypto up/down 时间桶市场：slug 规则见 `src/polymarket_backtest/markets/slug.py:27-61`（`btc-updown-5m-{ts}` 等），策略分类见 `strategy/complete_set.py:128-148`。README 亦从不声称天气功能——"WeatherQuant Next" 需求中的天气层需完全自建。

## 3. 概率分布构建

**未发现。** 全仓库无概率分布输出、无 CDF/PDF 拟合、无 `brier` 计算（`rg brier` 0 命中）。最接近的数学对象是完整集合折扣：`strategy/complete_set.py:284-285`

```python
def has_minimum_edge(up_price: float, down_price: float, cfg: CompleteSetConfig) -> bool:
    return (1.0 - (up_price + down_price)) >= cfg.min_edge
```

这是做市库存边际，不是概率模型。从 forecast 到概率的映射层需自行实现。

## 4. 机场观测偏差修正

**未发现**（无任何观测数据、站点、偏差修正概念）。

## 5. 多模型融合

**未发现**（无任何模型，谈不上融合）。

## 6. Edge 计算（独立推导 + 正误判定）

仓库共有 4 个 edge 公式：

**公式 A — 报价门槛** `complete_set.py:284-285`（调用点 `complete_set.py:378`）：
`edge = 1 − (up_px + down_px) ≥ min_edge (0.001)`。
独立推导：完整集合 {Up, Down} 买入成本 = up_px + down_px，结算恒支付 1（与结果无关）→ 每股利润 = 1 − (up_px + down_px)。
**判定：正确**（前提两腿都按报价成交；单腿成交的库存风险由 imbalance/skew 机制另行处理，`complete_set.py:235-248`）。

**公式 B — `complete_set_edge`** `complete_set.py:288-292`：
```python
return 1.0 - up_book.best_bid - down_book.best_bid
```
独立推导：若意图是"买入双边"的毛边际，买方须打 ask，正确形式为 `1 − up.best_ask − down.best_ask`；因 `bid < ask` 恒成立，用 bid 得到的 edge **系统性偏大**。若意图是卖方语义，符号也应为 `(bid_u + bid_d) − 1`。
**判定：形式错误 / 语义不明。** 缓解：`rg` 显示该函数**仅定义、无调用**（死代码）。

**公式 C — taker 补腿对冲边际** `complete_set.py:423-428`（同型 `complete_set.py:462-467`）：
`hedged = 1.0 − (lead_price + book.best_ask)`。
独立推导：先手腿按实际成交价 `lead_price`，滞后腿按 ask 补齐，合计成本 = 两者之和，完整集合付 1 → 边际 = 1 − 成本。**判定：正确。**

**公式 D — 回放指标配对边际** `orderbook_replay.py:336-344`：
```python
vu = (ups["price"] * ups["size"]).sum() / ups["size"].sum()
pair_cost = vu + vd
gross_pair_edge_per_share = 1.0 - pair_cost
paired_shares = min(ups["size"].sum(), dns["size"].sum())
gross_pair_edge_usd = paired_shares * gross_pair_edge_per_share
```
独立推导：对完全配对的双腿，`1 − VWAP_up − VWAP_down` 正确。**判定：配对情形正确，但口径不自洽**——VWAP 用**全部**成交量计算（`orderbook_replay.py:337-338`），USD 边际却乘以 `paired_shares = min(...)`（`:340-342`）：当两腿数量不等时，多余腿的成本进入 VWAP 却不计入配对股数，net edge 失真；且**未配对腿的结算损益完全不进入任何指标**（`:321-364` 无 settlement 项）。

**Kelly / Brier：未发现**（`rg kelly` 0 命中）。

## 7. 手续费与滑点

**主引擎（BacktestEngine）：**
- 滑点：均匀 bps，默认 10（`execution.py:30`），买 `px *= 1 + slip`、卖 `px *= 1 − slip`（`execution.py:64-70`）。成交价 = 决策后第 N 根 bar 的 `last_observation` 价格（`execution.py:54`，数据源 CLOB `/prices-history`，`clients/clob.py:34`），**没有显式 bid-ask spread 项，没有订单簿深度，没有成交量约束，按意图全额成交**（仅现金/持仓上限产生"部分成交"，`execution.py:74-80`）。
- 独立推导滑点充分性：Polymarket 二元合约价差典型 1 tick = 0.01；在 p≈0.50 时 0.01/0.50 = **200 bps**，默认 10 bps 滑点**低估一个数量级**；低价合约（p≈0.10）一个 tick = 1000 bps。结论：主引擎成交成本**严重低估**。
- 手续费：`fee_bps` 默认 0（`execution.py:31`），按名义额线性计（`portfolio.py:36-37`）。这是扁平 bps 形状，无法表达 Polymarket 真实费率对 p 的依赖——README 宣称 "Fill modeling | ... fees"（README.md:40）与默认 0 不符。

**回放引擎（OrderbookReplayEngine）：**
- 费率 `orderbook_replay.py:171-178`：
```python
return float(size * replay.polymarket_taker_fee_rate * price * (1.0 - price))
```
仅对 `TOP_UP/FAST_TOP_UP`（taker 动作）收费，maker `QUOTE` 免费（`:174-175`），默认费率 0.07（`:32`）。
**独立核对官方文档**（docs.polymarket.com/trading/fees）：`fee = C × feeRate × p × (1 − p)`，Crypto 类 taker 0.07，maker 免费。**判定：公式正确、费率取值正确、taker-only 语义正确。** 测试 `tests/test_orderbook_replay.py:26,36-39` 手算锁定了该公式（`10 × 0.07 × 0.5 × 0.5 = 0.175`）。
- spread/深度：maker 在自报价格成交（吃价差），taker 按 `best_ask` 成交且 size 被 `best_ask_size` 限制（`complete_set.py:484-486`）——**仅一级深度，无多级 walk**。README 自己也承认 roadmap 项 "richer order-book queue models"（README.md:171）。

## 8. 回测机制（成交模型、深度、部分成交、时间点一致性）

仓库有**两个**回测引擎：

**(a) BacktestEngine — 点时价格序列，非订单簿回放**
- 数据：CLOB `/prices-history` 价格 tick（`clients/clob.py:15-43`），非快照订单簿。
- 时钟：价格时间戳去重排序（`backtest/clock.py:22-46`）；每 tick 策略决策 → 成交 → 结算 → 盯市（`backtest/engine.py:88-111`）。
- 成交模型：`execution.py:47-54` — `prices[prices["ts"] > decision_ts]` 取第 `latency_bars` 根未来 bar（默认 1），**非深度感知、全额成交、无部分成交（现金/持仓上限除外）、无拒单重试（限价不过即静默丢弃，`execution.py:66-71`）**。
- 缺陷：`lag = min(latency, len(unique_future)) − 1`（`execution.py:52`）——未来 bar 不足时**实际提前于配置延迟成交**。
- 时间点一致性：策略只见 as-of 数据——`ResearchContext.last_prices` 用 `last_observation(..., now_ts)`（`context.py:31-35`），`last_trades` 过滤 `timestamp <= now_ts`（`context.py:40`）。**但 `ctx.prices` / `ctx.trades` 原样暴露含未来行的完整面板**（`context.py:21-22`，`engine.py:89-94` 传入 `self.prices`），PIT 靠约定不靠强制。
- 审计器：`engine.py:78-81` 仅调用 `validate_price_panel`/`validate_trade_panel`，二者只查列名/重复/NaN（`audit/lookahead.py:16-35`）——**结构检查，无法检测内容层面的泄漏**；`assert_context_clean`（`lookahead.py:38-44`）与 `warn_metadata_snapshot`（`lookahead.py:47-61`）**定义后全仓库无调用点**（`rg` 仅命中定义行）。
- 结算：`engine.py:108` → `portfolio.settle_resolved`（`portfolio.py:74-93`）经 `calendar.settlement_price(cond, outcome, as_of_ts)` 做 PIT 结算；**实现模块缺失，无法核实**（测试 `tests/test_calendar.py:25-31` 断言结算前返回 None）。
- 无 stale 数据年龄上限（`context.py:31-35` 无 max-age 判断）。

**(b) OrderbookReplayEngine — BBO 快照回放（更接近真实）**
- 数据：未公开的 CLOB `/orderbook-history`（`clients/orderbook_history.py:13-15` 自述 "undocumented but public"，毫秒时间戳 `:31-41`），up/down 双腿 BBO+size，500ms 容差合并（`orderbook_replay.py:401-407`）。
- 选取：`frame[frame["ts_sec"] <= ts_sec].iloc[-1]` as-of（`orderbook_replay.py:228-231`）——**无过期判断，stale book 照样成交**。
- maker 部分成交：`_try_maker_fill`（`orderbook_replay.py:104-130`）——条件 1：同 tick `last_trade_price <= order.price`（`:114`）；条件 2：对手 best_bid 量衰减 ≥ 8（`:119-129`）。成交量 = `min(remaining, cap, clip) × max(queue_participation, 0.25)`（`:116-117`）。
  **缺陷**：`:117` 的 `max(..., 0.25)` 使配置 `queue_participation=0.12`（`:27`）**永远不生效**，maker 成交量按 ≥25% 参与率计——相对设计意图系统性偏乐观。
- 部分成交 ✓（`remaining` 递减 `:247`）、冷却/单 tick 单笔/窗口上限 ✓（`:89-101`, `:254-255`）、taker 受 `best_ask_size` 限制 ✓（`complete_set.py:485-486`）。
- **taker 成交零延迟**：`evaluate` 与 `_apply_fill` 在同一 `ts_sec` 内完成（`orderbook_replay.py:271-298`）——见第 9 节。
- 无现金/结算账本：`ReplayState` 只有 `exposure_usd = |imbalance| × 0.5`（`:157`），`_metrics` 不结算（`:321-364`）。
- 对照真实成交流水打 `replication_score`（`orderbook_replay.py:410-480`）——校准工具，未见样本外验证。

## 9. 时间信息泄漏（具体 file:line 证据）

**已证实（E1–E7）：**

- **E1（同 tick taker 成交，最严重）** `orderbook_replay.py:271-298`：`actions = self.engine.evaluate(...)` 用当前 tick 的 BBO 决策，`TOP_UP/FAST_TOP_UP` 分支立即 `_apply_fill(..., ts_sec=ts_sec)` 按同 tick 的 `best_ask` 全额成交，**零延迟、零重估**。这直接违反仓库自己写下的原则 README.md:166：*"Model fill latency explicitly; same-bar fills are a common source of lookahead."* 5m/15m 快速市场中，同 bar 成交系统性高估 taker 边际（决策后价格会移动）。
- **E2（非因果权益曲线）** `engine.py:100-111` + `execution.py:47-54`：fill 用**未来 bar 的价格**执行（`execution.py:47`），但在**决策 tick 内**就把持仓记入组合，随后用**当 tick** 的 mark 记权益（`engine.py:109-111`）。可独立复算 README.md:97-107 的示例输出：`initial_cash=100`，t=100 决策买入 10 股、t=200 以 0.42 成交，t=100 权益行 = `100 − 10×0.42 + 10×0.40(当 tick mark) = 99.8`——与 README 输出 `"initial_equity": 99.8` 精确吻合。即**首行权益已含"以未来价格成交的持仓"**，`pnl/return_pct/max_drawdown`（`engine.py:41-49`）均基于该非因果序列。
- **E3（未来数据面板直通策略）** `context.py:21-22`：`prices`、`trades` 作为公开属性传完整面板（含 `> now_ts` 行），`engine.py:89-94` 传入 `self.prices`。策略写 `ctx.prices[ctx.prices.ts > ctx.now_ts]` 即可读未来，**PIT 约定无强制**。
- **E4（maker 成交乐观偏差）** `orderbook_replay.py:114`（同 tick `last_trade_price` 触发）+ `:117`（`max(queue_participation, 0.25)` 覆盖配置 0.12，见 `:27`）→ 成交概率与成交量双高。
- **E5（延迟回退提前成交）** `execution.py:52`：`lag = min(latency, len(unique_future)) − 1`，数据短时实际延迟 < 配置延迟。
- **E6（快照泄漏风险仅"口头警告"且无人调用）** `lookahead.py:47-61` 明确承认 Gamma 快照 `closed`/`outcomePrices` 是下载时点状态，但只是返回警告字符串；`rg` 显示 `warn_metadata_snapshot`、`assert_context_clean` **零调用点**，`engine.py:78-81` 只跑结构校验。
- **E7（样本内校准无 OOS）** `complete_set.py:161-225` 硬编码按剩余秒数的 size 阶梯，注释自述 *"calibrated strategy profiles"*（`:163`）；`orderbook_replay.py:410-480` 对真实成交流水算 replication_score。`rg -i "train|test.split|holdout|oos|out.of.sample|walk.?forward"` 在 `src/` **0 命中**——没有任何样本外/滚动验证机制，校准与评估同一批窗口是典型的过拟合泄漏路径。

**低危：** `engine.py:96-97` 用全历史 `market_meta` 快照生成 tick 时的 universe（仅泄漏 condition_id 列表，可交易性仍受 `ctx.is_tradable` PIT 门控，`engine.py:99`）。

**明确未发现：** 训练/测试非时序切分（无 ML）、全序列 z-score/rolling 统计再切片、收盘价用于盘中决策、同一目标日多次预测当独立样本（无预测数据）、结算数据进入决策上游——结算经 `calendar.settlement_price(cond, outcome, as_of_ts)`（`portfolio.py:83-85`）按 as-of 取用，`tests/test_calendar.py:25-31` 断言结算前不可见；**但实现模块缺失，此保护无法在发布代码中核实**。

## 10. 资金与仓位管理

- 主引擎：`enforce_cash` 现金上限折股 `max_affordable = cash / (px × (1 + fee_rate))`（`execution.py:74-76`）。独立推导：解 `shares × px × (1+f) ≤ cash`，**正确**。`allow_short=False` 默认禁卖空（`execution.py:34,77-80`）。
- **无 Kelly、无固定分数仓位**（`rg kelly` 0 命中）；`HoldToResolutionExample` 固定股数（`strategy/examples.py:15`）；**无单市场/总敞口上限**。
- **资金锁定（Polymarket 抵押）已建模**：买入即扣现金（`portfolio.py:48`），结算才回款（`portfolio.py:92`）→ 持仓期间资本不可用，符合真实抵押语义 ✓。
- 回放引擎：`CompleteSetConfig` 有 bankroll 上限旋钮但**默认关闭**（`complete_set.py:36-38`：`bankroll_usd=None`，两个 fraction=0.0；`_cap_shares` `complete_set.py:304-311` 的 `if cfg.bankroll_usd and ...` 默认恒假）。`ReplayState` 无现金账户，`exposure_usd=|imbalance|×0.5` 是启发式（`orderbook_replay.py:157`），未配对腿的资金占用与结算均不建模。
- 无风险指标（Sharpe 等，`rg sharpe` 0 命中）。Polymarket 现货式全额抵押、无保证金，与代码无保证金概念一致。

## 11. 结算方式

- 主引擎：`portfolio.settle_resolved`（`portfolio.py:74-93`）按 `as_of_ts` 查询 `calendar.settlement_price`，命中后 `cash += shares × settle_px`（`:88-92`），二元 0/1 支付语义正确。
- 结算数据源：`tests/test_calendar.py:6-39` 显示生命周期事件由 Gamma 元数据（`closed`/`closedTime`/`outcomePrices`）构建，赢家由 `outcomePrices` 推断（`:34-39`：`[0.02,0.98] → 1`）。**即结算 = Gamma 快照结果，不是 UMA / 链上 resolution**；`rg -i "uma|station"` 在代码中 0 命中（仅 README.md:174 roadmap 提到 "settlement adapters for on-chain resolution data"，明示尚未实现）。
- 无取整规则、无指定观测站、无争议期/延迟结算建模。
- 回放引擎**完全不结算**（`orderbook_replay.py:321-364` 仅以 `1 − pair_cost` 视作边际）。
- **对天气市场判定：不匹配。** Polymarket 天气合约按指定站点、特定取整规则并经 UMA 仲裁结算；本仓库无该层，且通用结算的实现文件（data/calendar）根本未发布。

## 12. 测试质量

- 共 **9 个测试**：`test_calendar.py` 2、`test_pit.py` 3、`test_engine.py` 2、`test_cli.py` 1、`test_orderbook_replay.py` 1。
- **有独立期望值的（好）**：
  - `tests/test_engine.py:31-40`：从手工合成数据独立断言时序——`decision_ts==100`、`fill_ts==200`、`price==0.42`，直接验证延迟成交；`:43-59` 验证限价拒绝与现金上限（`shares < 10`、`final_cash >= -1e-9`）。
  - `tests/test_orderbook_replay.py:6-40`：手算期望值 `pair_cost=0.95`、`gross=0.05`、`fee=0.175`、`net=0.325`、`net/share=0.0325`——**公式级对拍**（同时也把 `size × rate × p × (1−p)` 费率锁死）。
  - `tests/test_pit.py`：as-of / backward merge 手工样本断言。
  - `tests/test_calendar.py:6-39`：结算时间点不可见性断言（设计良好）。
- **缺口**：`OrderbookReplayEngine.run` 回放主循环（全仓库最复杂的成交启发式，`orderbook_replay.py:205-319`）**零测试**——唯一回放测试只测 `_metrics`。无滑点公式测试、无权益/PnL 数值对拍（engine 测试不断言 equity）、无 Brier（无该功能）、无 stale-book 测试、无 `has_minimum_edge`/`complete_set_edge` 直接单测。
- **关键：9/9 测试当前全部失败**（收集即 ModuleNotFoundError）：每个测试文件都 import 缺失的 `polymarket_backtest.data.*`（`test_calendar.py:1`、`test_pit.py:4`、`test_cli.py:3→cli.py:9`、`test_engine.py:1-2→examples/synthetic.py:5`、`test_orderbook_replay.py:1-3→orderbook_replay.py:8`）。静态结论（安装与运行被权限策略拒绝，未实跑）。
- README 徽章 "tests-pytest-green"（README.md:4）与实际状态不符。

## 13. SDK 与版本兼容性

- **未使用任何官方 Polymarket SDK**（无 `py-clob-client`、无 `py-polymarket`）；纯手写 `httpx` REST 客户端（`clients/http.py:10,18-21`），仅读公开端点、无私钥。
- 端点清单：`gamma-api.polymarket.com`（`config.py:1`）、`clob.polymarket.com /prices-history`（`clients/clob.py:34`）、**未公开文档的 `/orderbook-history`**（`clients/orderbook_history.py:13-15`，毫秒时间戳 `:31-41`）、`data-api.polymarket.com /trades、/activity`（`clients/data_api.py:36,66`）。
- 版本策略：`pyproject.toml:26-31` 仅下限无钉版——`httpx>=0.27`、`numpy>=1.24`、`pandas>=2.0`、`pyarrow>=14.0`；`requires-python >= 3.10`（`:10`）。无 lockfile、无 CI 版本矩阵。
- 兼容性风险：(1) `/orderbook-history` 无文档契约，随时可变（代码自知，docstring 自嘲）；(2) `data_api.py:10,70` `MAX_OFFSET=3000` 分页上限导致交易历史**静默截断**；(3) 固定 0.35s 限速（`config.py:14`）在大规模下载时低效但安全。
- 结论：只读公开端点短期大概率可用；但无版本化保证，且**数据管道的 parquet 层（data/store、data/ingest）未发布，端到端不可用**。

## 14. 设计缺陷清单

1. **D1 · 致命**：`.gitignore:10` 的 `data/` 误删 `src/polymarket_backtest/data/` 整包 → 包不可导入、9/9 测试失败、README.md:52 结构虚假；PIT 核心（calendar/pit）只存在于测试的引用中。
2. **D2 · 严重（泄漏）**：回放 taker 同 tick 成交，`orderbook_replay.py:271-298`；违反 README.md:166 自订原则。
3. **D3 · 严重（度量失真）**：权益记账非因果——未来价成交计入当 tick 权益，`engine.py:100-111` + `execution.py:47-54`（README.md:97-107 输出 99.8 即为实证）。
4. **D4 · 严重**：`orderbook_replay.py:117` `max(queue_participation, 0.25)` 使配置 0.12（`:27`）失效，maker 成交量系统性虚高。
5. **D5 · 严重（防泄漏失效）**：`context.py:21-22` 向策略暴露含未来行的原始面板；`LookaheadAuditor` 仅结构检查（`lookahead.py:16-35`），`warn_metadata_snapshot`/`assert_context_clean` 零调用（`lookahead.py:38-61`）。
6. **D6 · 严重（成本低估）**：主引擎全额成交、10bps 滑点、无 spread/深度/成交量（`execution.py:47-76`）；对 0.50 附近合约真实价差成本 ~200bps。
7. **D7**：`execution.py:52` 延迟回退可早于配置成交。
8. **D8**：无 stale 数据年龄检查（`context.py:31-35`；`orderbook_replay.py:228-231`）。
9. **D9**：`complete_set.py:288-292` 用 bid 计算买入 edge（应为 ask），且为死代码。
10. **D10**：回放指标无结算、未配对腿损益缺失，VWAP 与 `paired_shares` 口径不一致（`orderbook_replay.py:321-364`）。
11. **D11**：bankroll 上限默认关闭（`complete_set.py:36-38`）；回放无现金账本（`orderbook_replay.py:56-63`）。
12. **D12**：样本内校准无 OOS——size 阶梯（`complete_set.py:161-225`）+ replication 对照（`orderbook_replay.py:410-480`），src 无任何 train/split/walk-forward 机制。
13. **D13**：结算源 = Gamma `outcomePrices` 快照，非 UMA/链上，无天气站点/取整规则（`tests/test_calendar.py:25-39`；README.md:174 自认 roadmap 未做）。
14. **D14**：`portfolio.mark_to_market` 对无 mark 的持仓直接跳过（`portfolio.py:69-71`）→ 权益可骤降为纯现金。
15. **D15**：主引擎 `fee_bps` 名义额线性形状无法表达真实 `p(1−p)` 费率（`execution.py:31`、`portfolio.py:36-37`），且默认 0。
16. **D16**：`data_api.py:10,70` `MAX_OFFSET=3000` 静默截断历史。
17. **D17**：回放主循环零测试；测试套件整体不可运行（见第 12 节）。
18. **D18**：README 示例输出依赖 D3 的非因果记账才成立（README.md:97-107），"可复现"的数字本身就是缺陷证据。

## 15. 复用建议：推荐直接使用 / 值得参考的算法 / 可依法复用 / 不应复用

**推荐直接使用：无。**
包在发布状态下不可导入（D1），核心数据层缺失；即使补回 `data/` 模块，其成交模型（D2/D6）过于粗糙，不能支撑任何执行级或收益级结论。README 也自称 "not a live trading bot"（README.md:16）。

**值得参考的算法/设计（抄思路，不抄代码）：**
1. **生命周期事件 + PIT MarketCalendar 模式**：把 Gamma 快照转成 `listed/closed/resolved` 事件流、结算按 as-of 查询（`tests/test_calendar.py:6-39` 的语义 + `portfolio.py:74-93` 的接口）。这是仓库最有价值的思路，对天气市场同样适用。
2. **FillModel 的"决策后第 N 根 bar 成交"延迟框架**（`execution.py:47-54`，去掉 `:52` 的回退 bug 后可用）。
3. **完整集合对冲边际** `1 − (lead_fill + lag_ask)`（`complete_set.py:425,464`）与 `has_minimum_edge` 的 `1 − (up+down)` 形式（`complete_set.py:284-285`）——数学正确。
4. **maker 成交启发式三件套**：对手量衰减检测 + participation 上限 + 冷却/单 tick 限制（`orderbook_replay.py:104-130,89-101`）——作为无 queue model 时的保守近似框架（修掉 `:117` 的 0.25 地板）。
5. **paper-gate 门槛框架**（`audit/paper_gates.py:73-113`）：上线前按窗口配对率、净边际中位数、双腿间隔等指标做 pass/warn/fail，工程上干净。
6. **taker 费率公式** `size × rate × p × (1−p)` + taker-only（`orderbook_replay.py:171-178`）与 Polymarket 官方文档一致，可直接沿用。

**可依法复用：全部代码。** MIT 许可（`LICENSE:1-21`），保留版权声明与许可文本即可，商用、闭源衍生、二次分发均允许，无任何附加义务。

**不应复用：**
- 权益记账时序（`engine.py:100-111`）——先记未来价成交、后当 tick 盯市；
- 回放同 tick taker 成交与 0.25 participation 地板（`orderbook_replay.py:271-298`, `:117`）；
- `complete_set_edge` 的 bid 形式（`complete_set.py:292`）；
- Gamma `outcomePrices` 结算用于天气场景（`portfolio.py:83-85`）——需自建站点/取整/UMA 层；
- 硬编码 size 阶梯 "calibrated profiles"（`complete_set.py:161-225`）——样本内拟合产物；
- 主引擎 10bps 滑点 + 名义额 fee_bps（`execution.py:30-31`）——成本模型不成立；
- README 的示例收益数字（README.md:97-107）——由非因果记账产生，不可作为基线。

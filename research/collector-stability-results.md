# Collector 稳定性：修复前后指标

2026-10-12。工具：`scripts/collector_vitals.py`（前后同一把尺子）。
方法：本机连续两轮真实采集（Polymarket Gamma + CLOB + WS），
`before` 为改动前代码，`after` 为改动后代码，**各取前 10 分钟**比较 ——
两轮运行时长不同（59 分 vs ~12 分），整段比较会把"跑得久"算成"跑得差"。

数据：`data/collector-bench/raw-before`、`raw-after`（运行期产物，不入库）。

---

## 1. 饱和度与样本

| | BEFORE | AFTER |
|---|---|---|
| 窗口内 REST 观测 | 1074 | 1229 |
| 刷新间隔样本 | 517 | 651 |
| token 数 | 42 | 44 |
| WS 帧（整段） | 19 805 | ~1 585（窗口更短） |

---

## 2. 饱和度指标：游标轮转的效果

| 指标 | BEFORE | AFTER | 变化 |
|---|---:|---:|---|
| 刷新间隔 p50 | 20.74 s | 22.31 s | +7.6% |
| 刷新间隔 p90 | 83.62 s | 83.69 s | ±0 |
| **刷新间隔 p95** | **132.31 s** | **101.34 s** | **−23.4%** |
| **刷新间隔 p99** | **189.46 s** | **161.17 s** | **−14.9%** |
| **刷新间隔 max** | **310.94 s** | **173.23 s** | **−44.3%** |
| token 中位数 min | 18.80 s | 18.55 s | −1.3% |
| token 中位数 median | 23.98 s | 23.00 s | −4.1% |
| **token 中位数 max** | **107.59 s** | **63.08 s** | **−41.4%** |
| **最慢 / 中位（饥饿比）** | **5.19×** | **2.83×** | **−45.5%** |
| 超过 90s 的间隔 | 42（8.1%） | 48（7.4%） | −0.7pp |

**怎么读**：p50 与 p90 几乎不动 —— 中位数本来就不是问题所在。
改动作用在**尾部**：p95 / p99 / max 与"最慢 token 的中位数"全部下降，
饥饿比从 5.19 倍降到 2.83 倍。这正是轮转要解决的形状 ——
削减被摊到全集，而不是固定落在同几个 token 上。

**仍然存在的**：最慢 token 的中位数仍是中位数的 2.83 倍，
`超过 90s 的间隔` 仍有 7.4%。轮转**改善**了不公平，没有**消除**它 ——
只要一轮里装不下全部 token，就一定有人等到下一轮。

---

## 3. 请求耗时：真实测量（新增遥测）

改动前**无法测量**：代码里没有"请求发出→收到"的记录，
只有归档的 `venue_ts → received_at`，而那个量被"距上次盘口变动"主导
（见 §4），不是延迟。

改动后心跳新增 `telemetry`，实测（同轮 after，运行中）：

```
rest_latency_last_sweep_ms   n=44   p50=381.0   p95=407.6   p99=637.5   max=683.3
rest_latency_recent_ms       n=200  p50=378.6   p95=418.1   p99=619.5
rest_timeouts                        0
poll_budget                   budget=45.0s  exceeded=0  deferred=0
ws_queue                      depth=19  high_water=90  capacity=20000  frames_dropped=0
per_token_book_age_ms         median=8719  p95=16451  max=17286  (44 tokens)
```

**一次 `/book` 请求的端到端耗时约 0.38s（p50）**，远低于 45s 预算 ——
意味着在健康网络下**预算根本不会被触及**（`exceeded=0` 实测一致），
它防的是网络退化时的最坏情形。

`per_token_book_age_ms` 是饥饿的**直接读数**（不是代理指标）：
中位 8.7s、p95 16.5s、最大 17.3s —— 44 个 token 全部落在一个 sweep 周期量级内。

---

## 4. `venue_ts → received_at`：为什么它不是延迟

| | BEFORE | AFTER |
|---|---:|---:|
| 等窗 p50 | 17 083 ms | 14 921 ms |
| 等窗 min（时钟偏移下界） | **−1737.9 ms** | **−1369.4 ms** |
| 负值占比 | 8.85% | 11.15% |

**先说结论：这个量不是请求延迟。** 它是"这份盘口最后一次变动，距我们收到它
有多久"。一个没人交易的括号，`timestamp` 可以是几分钟前 ——
所以 p50 高达 15–17 秒，而真实请求只要 0.38 秒。两个数相差 40 倍，
本身就说明它们量的不是同一件事。

**负值的来源**：`received_at < venue_ts` 意味着"本机时钟读到的时刻，早于场所
时钟写下的时刻"——即**两台时钟的偏移**。实测下界：

```
BEFORE  −1737.9 ms
AFTER   −1369.4 ms     改善 368.5 ms ≈ 实测请求延迟 p50 381 ms
```

**这个差值正是修复去掉的那一部分**：改动前 `received_at` 取的是**请求发出**时刻，
比响应早约 0.38s；改动后取**响应返回**时刻。于是延迟那一段不再被计入，
下界只剩下纯粹的时钟偏移。

**为什么占比反而升高（8.85% → 11.15%）**：这个百分比不衡量修复。
负值的集合在改动后是改动前的**子集**（判据从 `venue > 发出` 变成 `venue > 收到`，
后者更难满足），但两轮窗口取自**不同时段、不同活跃度**的市场 ——
盘口变动越频繁，落进偏移窗口的比例越高。用它做前后对比，等于拿"市场活跃度"
当"代码正确性"的度量。可比的只有**下界**。

**剩下的 1.37s 偏移是本机与 Polymarket 之间的真实时钟差**，不是代码缺陷。
影子的严格 `TIME_MISMATCH` 会拒绝这些记录 —— 保守，且正确：
无法证明先后顺序时，不生成可执行信号。若将来要放宽，必须是**版本化的显式
容差**，并说明依据，而不是静默改一个阈值。

---

## 5. 尚未解决 / 有意不解决

| 项 | 状态 | 说明 |
|---|---|---|
| WS 与 REST **并发** | ⚠️ 未做 | 需要主循环改成多线程或 asyncio，属重构，本轮明确不做。缓解：阻塞窗口压到预算内 + 两侧排空 WS |
| `received_at < venue_ts` 未归零 | ⚠️ 有意保留 | 残余 ~1.37s 是**跨机时钟偏移**，不是代码缺陷；归零需要 NTP 对齐或引入显式容差 |
| 7.4% 的刷新间隔仍 >90s | ⚠️ 部分改善 | 轮转把 5.19× 压到 2.83×，但一轮装不下全部 token 时，一定有人等下一轮。要根治需按耗时调度或分片 |
| `_run_discovery` / `_run_integrity` / 日任务无预算 | ⚠️ 未改 | 它们也可能阻塞心跳；但改动面更大，且日任务一天一次。心跳已改为 sweep 后用重读时钟再写一次 |
| `books.fetch_book` 是死代码 | ℹ️ 未动 | `_poll_books` 内联了自己的 `get_json`；删除属于清理，不在本轮范围 |
| `ws_subscribe_failures` 从未自增 | ℹ️ 未动 | 字段存在但无写入路径，同上 |

---

## 6. 复现

```bash
# BEFORE：改动前的代码，独立数据目录
WQ_DB_PATH=data/collector-bench/before.duckdb \
WQ_RAW_DIR=data/collector-bench/raw-before uv run weatherquant-collect

# AFTER：改动后
WQ_DB_PATH=data/collector-bench/after.duckdb \
WQ_RAW_DIR=data/collector-bench/raw-after uv run weatherquant-collect

uv run python scripts/collector_vitals.py --run data/collector-bench --raw data/collector-bench/raw-before
uv run python scripts/collector_vitals.py --run data/collector-bench --raw data/collector-bench/raw-after
uv run pytest tests/test_collector_stability.py -v
```

窗口化对比的脚本内嵌于本文件的生成过程：取每个归档**首个 venue 时间戳起
10 分钟**内的 `rest` 记录，再按 token 计算相邻两次的间隔。

# Collector 稳定性：预算、时间戳与公平轮询

2026-10-12。实现：`src/weatherquant/data/{worker,http,stream,heartbeat}.py`。
测试：`tests/test_collector_stability.py`。
指标工具：`scripts/collector_vitals.py`。

**本轮是针对性修复，不是重构。** 主循环仍是单线程；WS 与 REST 之间没有引入
线程或 asyncio 改造。做法是把阻塞窗口**压短并说清楚**，而不是让它消失。

---

## 1. 五个改动

### 1.1 端到端时间预算

**问题**：`POLL_BUDGET_SECONDS = 45` 只在 token **之间**检查，而单次请求本身
不受约束 —— `ResilientHttp` 默认 20s 超时 × 4 次重试 ≈ **110s**。一个 token
就能花掉整轮预算的两倍半，期间心跳、WS 排空、writer flush 全部在同一线程上等待。

**修复**：预算被如实分掉。

```python
timeout = max(POLL_TIMEOUT_FLOOR, min(http_timeout_seconds, remaining))
retries = 1 if remaining >= 2 * timeout + 1.0 else 0
```

- 超时**不超过剩余时间**；
- 只有当"两次尝试 + 余量"**确实装得下**时才允许第二次；
- 装不下时**不重试** —— 采集器自己的轮询节拍（`book_poll_seconds`）就是重试，
  它对循环零成本。

底线 `POLL_TIMEOUT_FLOOR_SECONDS = 2.0`：宁可超支两秒，也不把 100ms 超时
施加给一个慢但活着的上游，那会把普通延迟变成故障。

### 1.2 时间戳不再撒谎

**问题**：`fetched_at` 在 `get_json` **之前**取值，而归档的 `received_at_utc`
用的就是它 —— 于是影子引擎读作"我们何时拿到这份数据"的字段，实际是
"我们何时开口问"。它可以早于响应，也**确实**早于场所自己的时间戳。

**实测（修复前，8343 条真实 REST 记录）**：

```
received_at < venue_ts 的记录：1021 条，12.24%，最小 -1864.9 ms
```

这直接对应影子 `TIME_MISMATCH` 里那句
`collector receipt precedes the venue timestamp`。

**修复**：三者分开，并各归其位。

| 时刻 | 取值位置 | 用途 |
|---|---|---|
| `request_started_at` | 请求发出前 | 只用于延迟统计 |
| `received_at` | **响应返回后** | 归档、`OrderBook.fetched_at_utc`、价格行的 `ts` |
| `venue_ts` | 载荷自带的 `timestamp` | 场所时钟，**永不**被本机时钟替代 |

采集器的 `discovery` 本来就是响应后取时间戳 —— 两个路径此前不一致，现在一致。

### 1.3 公平轮询：游标轮转

**问题**：`for token_id in list(self._books.items())` 按字典序从头遍历，
预算耗尽就 `break` —— **每一轮都砍掉同一个尾巴**。大部分 token 看起来健康，
少数永远刷不到，而聚合指标看不出异常。

**实测（修复前）**：

```
单 token 刷新间隔  p50 21.4s   p90 66.2s   p99 214.3s   max 666.9s
各 token 中位数    min 19.0s   median 23.8s   max 67.8s
最慢 / 中位        3.16 倍
超过 90s 的间隔    302 条（6.7%）
```

**修复**：每轮从上一轮停下的位置再前进一位。

```python
start = self._poll_cursor % len(items)
self._poll_cursor = (start + 1) % len(items)
```

削减于是被摊到全集上，而不是固定落在同几个 token 上。

### 1.4 运行指标（心跳新增 `telemetry`）

放在一个嵌套映射里，而不是每项都进 `Heartbeat` 的顶层字段 ——
它的成员形态各异（分位数 dict、计数、深度），且新增一项不应牵动 schema。
**空映射表示"尚未测得"，与 0 是不同的陈述。**

| 键 | 含义 |
|---|---|
| `rest_latency_last_sweep_ms` | 上一轮 sweep 的 p50/p95/p99/max |
| `rest_latency_recent_ms` | 最近 200 次请求（有界 deque） |
| `rest_timeouts` | 超时次数，**与传输失败分开计** |
| `poll_budget.{budget_seconds,exceeded,deferred_tokens}` | 预算与耗尽 |
| `ws_queue.{depth,high_water,capacity,frames_dropped}` | 背压 |
| `per_token_book_age_ms.{max,p95,median_ms,oldest_token}` | **饥饿的直接读数** |

### 1.5 丢帧：计数而非刷屏

`stream.py` 在队列满时**每帧**打一条 WARNING。20 000 的队列在持续背压下
就是两万条相同日志，把采集器要说的别的事全部埋掉。

**修复**：`_note_drop()` 计数，**第一条 + 每 500 条**打一次。
计数器带锁（生产者线程写、工作线程读），并发布到 `ws_queue.frames_dropped`。

---

## 2. 心跳：写了两次，但第二次本来是死代码

主循环原有的第二次心跳检查：

```python
now = time.monotonic()          # 循环开头
if now - self._last_heartbeat_mono >= self.heartbeat_seconds: self._publish(...)   # 分支 A
...
if now - self._last_heartbeat_mono >= self.heartbeat_seconds: self._publish(...)   # 分支 B
```

`now` 在两处**相同**，而分支 A 刚把 `_last_heartbeat_mono` 设为
`time.monotonic()`（**严格大于** `now`）—— 所以 B 永远不成立。
这条"事后安全网"是死代码。

**修复**：分支 B 改用**重新读取**的时钟，并且放在 sweep 之后。
另外 WS 排空从每周期一次改为**前后各一次**：生产者在独立线程上，主进程
阻塞在 REST 期间队列只增不减，两侧排空把积压上界压到"一次被约束的 sweep"。

> **没有做到的事，说清楚**：WS 与 REST 仍**不并发**。要做到并发需要把主循环
> 改成多线程或 asyncio，那是重构。本轮做的是把阻塞窗口压到预算以内
> （实测见 §4），使心跳与 WS 的延迟上界等于预算，而不是等于最坏请求时间。

---

## 3. 验收对照

| 验收点 | 结论 |
|---|---|
| REST 故障时心跳仍正常更新 | ✅ 心跳在 sweep **之前**写，且在 sweep **之后**用重读时钟再写；预算把窗口压到 ≤45s，远低于探针 120s 的过期阈值 |
| WS 处理不因 REST 阻塞而停止 | ⚠️ **部分**：不并发，但两侧排空 + 窗口受约束，积压上界 = 一次 sweep。见 §2 末 |
| 不出现部分 Token 长期饥饿 | ✅ 游标轮转 + `per_token_book_age_ms` 直接可观测 |
| 时间戳与数据来源真实可审计 | ✅ 三时刻分离，`venue_ts` 永不被本机时钟替代 |
| 不引入新的数据缺口或重复写入 | ✅ `test_a_slow_run_creates_no_gaps_and_no_duplicate_books` |

---

## 4. 指标

工具：`scripts/collector_vitals.py`（前后同一把尺子）。

```
python scripts/collector_vitals.py --run data/collector-bench --raw <archive>
```

数值见 [`../research/collector-stability-results.md`](../research/collector-stability-results.md)。

---

## 5. 复现

```bash
uv run pytest tests/test_collector_stability.py -v
uv run python scripts/collector_vitals.py --run data/collector-bench --raw data/collector-bench/raw-before
```

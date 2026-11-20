# 首次实盘验证报告：把 shadow 与 paper 连成一条漏斗

脚本：`scripts/live_validation_report.py`；测试：`tests/test_live_validation_report.py`。
关联：[`shadow-engine.md`](shadow-engine.md)、[`shadow-risk-rules.md`](shadow-risk-rules.md)、
[`paper-risk-rules.md`](paper-risk-rules.md)、[`late-decision-export.md`](late-decision-export.md)。

首次有人值守的实盘运行时，三个进程写三个文件：shadow 做决策，export 发布
决策，paper 消费发布并模拟成交。单独读任何一个都只能回答一半问题——shadow
说「拒绝了」，paper 说「没成交」，但没人回答这两个说的是不是**同一批信号**。
本脚本按 shadow 的确定性 `decision_id` 把两个库连起来，输出一条漏斗：

```
Shadow LIVE ACCEPT -> 已导出 -> paper 信号 -> paper 成交 -> 已结算
```

外加拒绝原因直方图，以及产生这些拒绝的闸门余量。**它是量尺，不是开关**：
不改任何阈值、不改任何数据、不产生写操作。

## 1. 安全边界（先读这一节）

脚本面对的是生产快照，因此：

- **输入永远是副本。** 每个库都以 `read_only=True` 打开；全脚本没有 DDL、
  没有 `CHECKPOINT`、没有临时表。测试会对两个输入文件做前后的 SHA-256 与
  `mtime` 比对。
- **拒绝打开引擎仍持有的文件。** DuckDB 对写租约是独占的，只读打开会失败。
  脚本捕获这个失败并给出操作指令，而不是抛一条 `Could not set lock`；
  同一进程内「已用读写方式打开」也会被识别为同一种拒绝。
- **拒绝带 `.wal` 的文件。** 干净的快照没有 WAL；存在旁路 WAL 说明库还在写
  或没干净关闭，脚本在 DuckDB 重放它之前就拒绝。
- **如何取得副本**：让 shadow / paper 进程优雅停止（确认 `.wal` 为 0 字节，
  见 [`backup-recovery-drill.md`](backup-recovery-drill.md)），再 `cp` 出
  `.duckdb`。**不要在进程运行时 `cp`**：那会得到一个撕裂的文件，且通常伴随
  一个会被脚本拒绝的 WAL。

## 2. 运行

```bash
uv run python scripts/live_validation_report.py \
    --shadow-db /tmp/live/shadow.duckdb \
    --paper-db  /tmp/live/paper.duckdb \
    --since 2026-11-20T00:00:00Z --until 2026-11-21T00:00:00Z \
    --json /tmp/live/report.json --md /tmp/live/report.md
```

| 参数 | 含义 |
|---|---|
| `--shadow-db` | 必填，shadow 库的**副本** |
| `--paper-db` | 可选；省略时所有 paper 阶段显示 `n/a (no --paper-db)` |
| `--mode` | `LIVE`（默认）或 `REPLAY`；按决策的 `mode` 列过滤 |
| `--since` / `--until` | UTC ISO 时刻，闭区间，作用于 `decision_time_utc` |
| `--json` / `--md` | 落盘路径；两者都不给时把 Markdown 打到 stdout |

退出码：`0` 成功；`2` 输入不可安全读取或参数非法（错误写 stderr，不带堆栈）。

## 3. 报告怎么读

### 3.1 Decisions

`total / ACCEPT / REJECT / NOT_EXECUTABLE` 是任务要求的四个桶。若该版本的
shadow schema 没有 `NOT_EXECUTABLE` 这个状态（当前 schema 的不执行原因是
`NO_TRADE`、`DATA_UNAVAILABLE` 等），该行为 0，并另有一张「本快照出现的全部
state」表，保证总数能对上、没有被藏起来的桶。

按小时、按 `target_date` 的分组使用同样的四个计数列，用来发现「某一小时
全是拒绝」这类时间聚集。

### 3.2 Reject reasons

- `primary`：`rejection_reasons.is_primary` 的原因计数与占比，占比的分母是
  **有 primary 原因的拒绝决策数**（一条决策可能同时触发多道闸门）。
- `all`：所有记录到的原因，包含非 primary 的次级原因。
- `top_per_market_target`：每个 `(market_id, target_date)` 下前 3 个 primary
  原因，用来区分「全市场都薄」和「某一个市场特别差」。

### 3.3 Gate margins

| 闸门 | 观测值 | 阈值来源 |
|---|---|---|
| `book_age` | `snapshot_age_ms` | `max_snapshot_age_ms` |
| `depth` | `available_depth_usd` | `min_depth_usd`（v3 默认 0，即关闭；venue 最小单量才是真正的下限） |
| `net_edge` | `net_edge` | `min_net_edge_usd_per_share` |
| `clock_skew` | `-market_snapshots.latency_ms`（负向偏斜） | `max_clock_skew_ms`（v3 起只惩罚负向偏斜；正向延迟属于陈旧，由 book age 管） |
| `fee` | `fee_rate` 是否存在 | 未知费率即拒绝，故看「present / missing」 |
| `size` | `estimated_fill_shares` | `min_order_size_shares`（v3 取消 complete-fill，部分成交按书缩量，不再因此拒绝） |

阈值只**读取** `configs/shadow-thresholds.json`（引擎运行时用的同一份），
不修改。字段说明：

- `p50 / p90 / p99`：本窗口内**全部决策**（不只拒绝）的观测分布。
- `rejects_within_10pct / 25pct`：**拒绝侧**贴近阈值的数量。对「越小越好」
  的闸门（book age、延迟）取阈值的 `100%~110%` / `100%~125%`；对「越大越好」
  的闸门（深度、净边际）取阈值的 `90%~100%` / `75%~100%`。它回答「这些拒绝
  是刀锋之差，还是差得很远」。
- `would_pass_at`：若阈值取该候选值时，会有多少**当前被拒**的决策通过。
  **纯信息项**，用于回答「阈值是否过紧」，不改变任何配置。

### 3.4 Funnel

每一阶段都以 shadow 的 LIVE `ACCEPT` 集合为基准，用 `decision_id` 求交集，
而不是把三个库的行数直接相加——后者会把 REPLAY、重复投递和别的日期的信号
混进来。

| 阶段 | 定义 |
|---|---|
| `shadow_live_accept` | `shadow_decisions.state = 'ACCEPT'` 且 `mode` 匹配 |
| `exported` | 上述决策中 `export_seq IS NOT NULL` 的部分（列缺失时 `n/a`） |
| `paper_signals` | 上述决策中出现在 `paper_signals` 的部分 |
| `paper_accepted` | 其中 `outcome ∈ ACCEPTED / FILLED / PENDING` |
| `paper_refused` | 其中 `outcome = REFUSED` |
| `paper_filled` | 其中出现在 `paper_fills` 的部分 |
| `settled` | 其中成交市场已有已应用结算（`paper_settlements.applied`，无该列时看 `status = 'RESOLVED'`） |

`conversion_pct` 的分母是上一阶段：`accept→export`、`export→signal`、
`signal→fill`（分母为 paper accepted）、`fill→settled`。若某一阶段大于 100%，
那不是统计错误，而是数据不一致（例如有 signal 却没有 export 记录），值得
当场查。

`paper_refusal_reasons` 是窗口内**全部** paper 信号的拒绝原因；
`no_fill_reasons` 只保留成交侧的拒绝（排除 `NOT_LIVE`、`NOT_ACCEPT`、
`STALE_SIGNAL`、`MARKET_NOT_OPEN`、`DUPLICATE_SIGNAL`、`ENGINE_DISABLED`
这些「根本没成为一笔交易」的消费侧原因）。两者分开，避免把
「重放行被拒」误读成「报价失败」。

### 3.5 Freshness and gaps

最新决策 / 最新 ACCEPT / 最新成交时刻是**全库**口径，**不受** `--since/--until`
影响：它回答「这份快照有多新」。`data_quality_events`（shadow）与
`paper_quality_events`（paper）的类型计数则受窗口限制，回答「窗口内漏了什么」。

## 4. 口径与缺失数据

- 默认只统计 `mode = LIVE`；REPLAY 行不会污染实盘结论。要专门看重放，用
  `--mode REPLAY`。
- 缺表/缺列不崩：该指标显示 `n/a (table missing)` 或 `n/a (column missing)`，
  并在报告末尾的 **Skipped metrics** 逐条列出。这正是它能在「首次实盘刚跑完、
  各分支 schema 尚未完全合流」的快照上先跑起来的原因。
- JSON 是完整数据源；Markdown 是同一个 dict 的渲染，不含额外计算。

## 5. 测试

```bash
uv run pytest tests/test_live_validation_report.py -q
```

覆盖：拒绝原因与 primary 占比、按小时/日期分组、六个闸门的分布与近阈值计数、
按 `decision_id` 求交集的漏斗与各阶段转化率、消费侧与成交侧拒绝的区分、
`--since/--until` 与 `--mode` 的过滤、旧 schema 的缺列/缺表降级、输入文件
前后哈希不变、被占用库与 `.wal` 旁路文件的拒绝、CLI 的 JSON/Markdown 输出。

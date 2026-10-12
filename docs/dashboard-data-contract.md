# Dashboard 数据契约（P2-1）

Collector / Shadow → Dashboard 的只读快照契约。**协调以本文档为准**：生产者与
消费者各自实现本文档，互不读对方的代码，改动先改契约。

版本：`schema_version = 1`（字段只增不改语义；破坏性变更必须升版本）。

---

## 0. 通用规则（所有快照适用）

1. **原子发布**：临时文件 + `flush`/`fsync` + `os.rename`（本仓库
   `weatherquant.shadow.publish.atomic_write_json` 的语义）。读者永远看不到
   半个 JSON。
2. **信封字段**（每个文件的顶层）：

   ```json
   {
     "schema_version": 1,
     "generated_at_utc": "2026-10-12T08:00:00+00:00",
     "source_service": "weatherquant-shadow",
     "source_run_id": "…",
     "source_version": "0.1.1",
     "stale_after_seconds": 300,
     "data": { }
   }
   ```

3. **新鲜度由读者计算**：`age = now − generated_at_utc`；超过
   `stale_after_seconds`（dashboard 加 60s 宽限）→ `stale`。
4. **只放窗口，不放全表**：决策行、分布、市场明细都限量（默认 500 行 /
   开放目标日 × 模型数）。禁止把 GB 级数据搬进快照。
5. **缺失 ≠ 空**：文件不存在时 Dashboard 返回 `freshness.state = "missing"` 并
   给出原因；文件存在但数据为空是合法的空状态。
6. **不含宿主机绝对路径**，不含凭据，不含 `.duckdb` 内容。

---

## 1. 已存在、Dashboard 已消费的快照（无需新集成）

### 1.1 `collector/health/heartbeat.json`（逻辑名 `collector-heartbeat`）

生产者：Collector worker（`weatherquant.data.heartbeat.HeartbeatWriter`，原子）。
Dashboard 消费：全部字段 + `weatherquant.data.heartbeat.evaluate_health`
（与容器探针同一函数，状态永不分歧）。
新鲜度：`updated_at_utc`，窗口 120s。

### 1.2 `shadow/health/heartbeat.json`（逻辑名 `shadow-heartbeat`）

生产者：Shadow runner（`weatherquant.shadow.health.write_heartbeat`）。
Dashboard 关注：`status / mode / configured_mode / primary_model /
thresholds_version / cycles / last_decision_at_utc / collector_last_seen_utc /
last_cycle{duration_ms, decisions, by_state, …} / disk.free_percent /
fatal_error`。
新鲜度：`updated_at_utc`，窗口 120s。**REPLAY 必须原样呈现，不得着色为 LIVE。**

### 1.3 `shadow/reports/report.json`（逻辑名 `shadow-report`）

生产者：Shadow runner 每 60s 从**自己的连接**计算并原子发布
（`publish_report_snapshot`，`schema_version = 1`，自带 `stale_after_seconds`）。
Dashboard 消费：`decisions{by_state, by_mode, accepted, rejected, no_trade,
live_accepted, replay_accepted, primary_reasons, p95…} / theoretical_edge /
coverage{open_markets, open_target_dates, models_tracked, forecast_coverage,
orderbook_coverage} / liveness{…_age_seconds} / prediction_quality.by_model /
data_quality / warnings / equity_curve(必须为 null)`。

> 注意：shadow-v0.1.0（sha-f58d2bc）**早于** report 快照机制（1849187）。生产
> Shadow 升级到含该机制的版本之前，Dashboard 会显示 `shadow_report: missing`——
> 这是设计内降级，不是故障。

### 1.4 `collector/raw/{gamma,rest,ws}/YYYY-MM-DD.jsonl`（逻辑名 `archive-tail`）

不是快照，是**有界读取**：每源只读最新 2 个文件的最后 512 KiB，丢弃半行，
TTL 4s。复用 `weatherquant.shadow.events` 的 `parse_envelope / classify /
parse_gamma / rest_book / parse_ws_frame`——两个读者共享同一份格式实现。
提供：市场目录（问题、区间、目标日、token、状态）+ 每 token 最新完整盘口
（bid/ask/spread/深度/venue 时间/缺陷）。**WebSocket 增量（price_change）与
Shadow 一样只记录不使用。**

---

## 2. Dashboard 已定义、生产者已发布的快照

> **状态：已接入。** `weatherquant.shadow.runner._publish_report()` 在
> `publish_report_snapshot(...)` 之后调用 `publish_detail_snapshots(...)`：
> 同一连接、同一节流（`shadow_report_seconds`），**独立的 try 块**——明细失败
> 只记一条 `detail_publish_failed` 质量事件，报告照常发布，引擎照常运行。
> 生产是否点亮取决于生产 Shadow 镜像是否包含该提交（见 §4）。

三个文件都发布到 **Shadow 数据目录的 `reports/`**（即现有 report.json 旁）：

| 逻辑名 | 文件 | 生产者调用点 |
|---|---|---|
| `market-snapshot` | `shadow/reports/market-snapshot.json` | Shadow 引擎（持有连接的一方） |
| `recent-decisions` | `shadow/reports/recent-decisions.json` | 同上 |
| `forecast-snapshot` | `shadow/reports/forecast-snapshot.json` | 同上 |

### 2.1 `market-snapshot.json`

```json
{ "schema_version": 1, "…信封字段…": "…",
  "data": {
    "primary_model": "calibrated",
    "markets": [
      {
        "market_id": "…", "event_id": "…", "question": "…",
        "target_date": "2026-10-13", "bracket_label": "81°F",
        "bracket_low": 81.0, "bracket_high": 81.0, "station": "KLGA",
        "active": true, "closed": false, "accepting_orders": true,
        "tradeable": true, "token_yes": "…",
        "book": {
          "available": true,
          "best_bid": 0.62, "best_ask": 0.65, "spread": 0.03,
          "bid_depth_usd": 92.5, "ask_depth_usd": 78.4,
          "levels_bid": 2, "levels_ask": 2,
          "defects": [], "quality": "ok",
          "age_seconds": 3.2, "venue_ts_utc": null,
          "as_of_utc": "2026-10-12T08:00:00+00:00"
        },
        "model_probability": 0.41,
        "net_edge_usd": 0.03,
        "last_decision": {"state": "ACCEPT", "decided_at_utc": "…", "mode": "LIVE"},
        "updated_at_utc": "…"
      }
    ]
  }}
```

消费者：Overview 的 Model vs Market、Markets 的模型概率/Edge 列、市场详情。

### 2.2 `recent-decisions.json`

```json
{ "schema_version": 1, "…信封…": "…",
  "data": { "window": 500, "decisions": [
    {
      "decision_id": "…", "decided_at_utc": "…",
      "market_id": "…", "market_question": "…", "bracket_label": "81°F",
      "target_date": "2026-10-13", "direction": "buy_yes",
      "state": "ACCEPT", "mode": "LIVE",
      "model_name": "calibrated", "model_version": "v1",
      "model_probability": 0.41, "market_price": 0.65,
      "best_bid": 0.62, "best_ask": 0.65, "net_edge_usd": 0.03,
      "primary_reason": null, "reasons": [],
      "snapshot_age_ms": 400, "forecast_age_hours": 3.5,
      "available_depth_usd": 48.0,
      "thresholds_version": "v3", "run_id": "…"
    }
  ]}}
```

排序：`decided_at_utc` 倒序，窗口 500。`mode` 必填——LIVE 与 REPLAY 在 UI 上
永远分色分列。消费者：Decisions 行表与筛选、市场详情的最近决策。

### 2.3 `forecast-snapshot.json`

```json
{ "schema_version": 1, "…信封…": "…",
  "data": { "primary_model": "calibrated", "distributions": [
    {
      "model": "calibrated", "version": "v1",
      "target_date": "2026-10-13", "event_id": "…",
      "forecast_run_time_utc": "…", "training_cutoff_utc": "…",
      "forecast_received_at_utc": "…", "predicted_max_f": 81.4,
      "station": "KLGA",
      "buckets": [{"label": "81°F", "probability": 0.41}]
    }
  ]}}
```

窗口：开放目标日 × 各模型最新一次 run（`forecast_run_time_utc <= now`，天然
point-in-time：只携带当时的 run 与训练截止，不允许出现"已知未来"的字段）。

---

## 3. 字段可得性总表（当前状态）

| 数据 | 状态 | 说明 |
|---|---|---|
| Collector 健康/计数/磁盘 | ✅ 可用 | 心跳 |
| Shadow 健康/模式/周期 | ✅ 可用 | 心跳 |
| 决策聚合、LIVE/REPLAY 拆分、拒绝原因 | ✅ 可用* | *需 Shadow ≥ report 快照机制 |
| 市场目录 + 盘口 bid/ask/深度/新鲜度 | ✅ 可用 | 归档有界 tail |
| 模型列表、覆盖率、观测缺失说明 | ✅ 可用 | report |
| 模型概率 / 理论 Edge（逐市场） | ✅ 已接入* | market-snapshot；*需生产 Shadow 镜像含接入提交 |
| 决策行级明细 | ✅ 已接入* | recent-decisions |
| 温度桶概率分布 | ✅ 已接入* | forecast-snapshot |
| 实际观测 / 官方结算 | ❌ 不接入 | 属研究管线回溯评分，决策时点禁止视为已知 |
| WS 队列长度/高水位/丢帧 | ✅ 可用（Collector ≥ v0.1.2） | 心跳 `telemetry.ws_queue{depth, high_water, capacity, frames_dropped}`；旧版无字段时 UI 显示"暂未接入" |
| Shadow 数据库大小 / 版本 | ✅ 可用* | 心跳 `db_bytes` / `app_version` / `image_tag`；旧版无字段时 UI 显示"暂未接入" |
| ACCEPT 独立度（独立市场 / Token / 重复周期） | ✅ 可用* | report `decisions.accept_scope`；*同上 |
| 目标日是否已结束（纽约自然日） | ✅ 可用 | `MarketSummary.target_day_ended`，由 `local_day_bounds_utc` 判定，**不是**宿主机时钟 |
| 收益曲线 | ❌ 结构性禁止 | report 恒为 null，UI 原样传递 |

---

## 4. 集成契约（Shadow 侧）——已执行

**状态：已合入 `shadow/runner.py`。** 调用点在 `_publish_report()`，
在 `publish_report_snapshot(...)` 之后、**独立的 try 块**里：

```python
try:
    publish_detail_snapshots(
        self.db.con,                                    # 引擎已持有的连接，绝不新开
        directory=self.shadow.shadow_report_path.parent,  # <shadow>/reports/
        run_id=self.run_id or "",
        source_version=_app_version(),
        primary_model=self.shadow.shadow_primary_model,
        now=now,
        stale_after_seconds=self.shadow.shadow_report_stale_seconds,
    )
except Exception as exc:
    log.warning("could not publish the detail snapshots: %s", exc)
    self.store.record_quality([QualityEvent(kind="detail_publish_failed", ...)])
```

要点：

- 失败 log-and-swallow 且**不带走报告**（两个独立 try）；
- 节流与 report 共用 `shadow_report_seconds`（默认 60s）；
- 引擎不认识 Dashboard 的 HTTP，只认识这一个目录——单向、只读、可审计。

### 接入过程暴露的缺陷（值得记住的一条）

生产者自带的 `dashboard/tests/test_producer.py` **自造了一张 `model_predictions` 表**，
比真实 schema 多一列 `bracket_label`。`build_forecasts_snapshot` 因此查了一个
真实数据库里不存在的列——**生产者测试全绿，而这条查询在真库上第一次跑就 Binder Error**。
夹具已改为镜像 `migrations_shadow/0001_shadow_schema.sql`，并由新增的
`tests/test_shadow_runner.py::test_the_engine_publishes_the_detail_snapshots_the_dashboard_reads`
（**驱动 Runner** 而非驱动 helper）守住。

这与 checkpoint 缺陷（`research/shadow-validation.md` §11.4）是同一类错误：
**测试替生产代码做了它该做的那一步，于是在生产坏掉时依然全绿。**

### 仍然成立的集成条件（Definition of Done）

UI 从 `unavailable` 变为实时数据，需要以下条件**全部**满足：

1. ✅ `publish_detail_snapshots(...)` 已合入 `shadow/runner.py`（同连接、同节流、失败仅告警）；
2. ✅ 发布目录是 `shadow_report_path` 所在的 `reports/`（Dashboard 的 `shadow/reports`
   只读挂载已就位，无需改 compose）；
3. ✅ 三个文件满足 §0 信封（`schema_version=1`、`generated_at_utc`、
   `stale_after_seconds`、原子 rename）；
4. ✅ `recent-decisions` 按 `decided_at_utc` 倒序、窗口 ≤500 行、`mode` 必填；
5. ✅ `market-snapshot` 的 `model_probability` 必须来自**决策引擎实际读取的**
   那一行 primary-model 预测（同一 `forecast_run_time_utc`），否则 Dashboard
   展示的概率会与决策依据不一致——这是契约里唯一会造成"两个真相"的字段；
6. ✅ Dashboard **适配器**无需改动：`market-snapshot → 归档 tail`、
   `recent-decisions → 不可用` 的优先级读取已就位，
   `test_api_consumes_producer_output_end_to_end`（显式断言概率 / Edge /
   决策明细三层贯通）在文件出现时自动转为 `fresh`。

> **代码合入 ≠ 生产点亮。** 上面六条全部满足的是**源码**；生产 Shadow 容器跑的仍是
> `shadow-v0.1.2`（`b60bbd8`，早于本集成）。在用含本提交的源码重建并替换 Shadow
> 镜像之前，Dashboard 会继续如实显示 `unavailable` + 原因——这是设计内降级，
> 不是故障。影响范围与回滚见 `docs/p2-0.1-audit.md` §7。

同一轮里 Dashboard 还新增了三样**不属于**本节集成的东西（它们各自有字段、
有测试，且不依赖上面任何一条）：Shadow 的 `app_version`/`db_bytes`、
`decisions.accept_scope`、`MarketSummary.target_day_ended`。

## 5. HTTP API 映射（消费者侧）

| 端点 | 主要来源 |
|---|---|
| `GET /healthz` | 三源新鲜度汇总 + 存活（信封包裹） |
| `GET /api/v1/overview` | 心跳×2 + report + 归档 tail |
| `GET /api/v1/collector/health` `/metrics` | collector 心跳 + 本进程趋势 |
| `GET /api/v1/shadow/health` `/report` | shadow 心跳 / report |
| `GET /api/v1/markets` `/markets/{id}` | market-snapshot 或归档 tail |
| `GET /api/v1/forecasts` | report + forecast-snapshot |
| `GET /api/v1/decisions` | report 聚合 + recent-decisions 行窗口 |
| `GET /api/v1/system` | 心跳×2 + report + 趋势环 |

全部为只读 GET，Pydantic 响应模型，分页 `limit/cursor`，筛选参数枚举校验
（非法 → 422），无限流（默认 600/min/IP，可关）。不提供查询、写入、执行类接口，
不提供 DuckDB / 原始 JSONL 下载。

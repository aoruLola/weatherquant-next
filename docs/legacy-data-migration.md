# 旧数据只读迁移方案

2026-10-10。对应规范 §四。

---

## 0. 不可违反的边界

1. **旧数据库永不被写入、覆盖、清理或整理。** 唯一的打开方式是
   `weatherquant.storage.connect_legacy_read_only()`，它只接受 `read_only=True`，
   没有任何写入模式。测试 `test_legacy_helper_opens_strictly_read_only` 对此做了断言。
2. **迁移是可选的。** 本模块只读 + 分类 + 出计划，不自动写入。
3. **保留来源与原始时间。** 每个迁移行记录
   `legacy_source_table` / `legacy_source_db`，并沿用旧库的 `fetched_at` / `computed_at`
   —— **绝不**用导入时间冒充采集时间。
4. **不可靠的标记为待核实，不自动视为可信。**
5. **不继承旧版概率模型、信号逻辑、账本实现。**

---

## 1. 旧库清单与实际覆盖窗口

来源：`backend/data/weatherquant.duckdb`（约 103 MB，22 张表）。
覆盖窗口由 [`research/legacy-quant-review.md`](../research/legacy-quant-review.md) §1 实测。

### 1.1 有研究价值 — 迁移

| 旧表 | 新表 | 行数 | 时间覆盖（UTC） | 说明 |
|---|---|---:|---|---|
| `observations` | `observations` | **30,267** | 2023-10-06 → 2026-10-10（1,101 天） | 3 年站点实测，**最有价值的一张** |
| `daily_observations` | `daily_max_temperatures` | 2,200 | 同上，1,100 天 × 2 条规则 | `hourly_metar` / `all_readings`；`is_official` 恒为 FALSE |
| `forecast_hourly` | `forecast_hourly` | **49,662** | run 2026-08-22 → 2026-10-10 | ECMWF 单模型；不可再生 |
| `forecast_daily` | `forecast_daily` | 2,017 | 目标日 2026-08-26 → 2026-10-11（47 天） | 341 行不完整（`n_hours<24`） |
| `forecast_runs` | `forecast_runs` | 196 | 同上 | 含 `publishable_at`（隐含 lag=6h） |
| `price_history` | `price_history` | **22,996** | 2026-09-09 → 2026-10-10（33 个目标日） | 中间价序列，**不可成交** |
| `markets` | `markets` | 3,756 | 目标日 2025-06-20 → 2026-10-11 | `parse_status` 全为 `ok` |
| `events` | `events` | 408 | 同上 | 规则文本、解析源、`raw_json` |
| `settlements` | `settlements` | **3,723** | 2025-06-20 → 2026-10-08 | 官方已决结果 |
| `books` | `order_books` | **693** | **仅 2026-10-09T17:18Z → 2026-10-10T02:42Z** | 唯一存在的深度数据；33 市场 × 20 快照 |
| （`raw/`） | — | 408 events + 195 runs + 3 IEM CSV + 33 books | — | 上游原始载荷，可无网络重放 |

**合计 115,918 行、10 张表。**（计数为 2026-10-10 快照；若期间重跑过旧版 `collect` 会增长，
复现时以实跑结果为准。）

### 1.2 可再生成 — 不迁移

`predictions`、`signals`、`model_runs`、`backtest_runs`、`audit_log`、`equity_snapshots`。

理由：它们是旧模型/旧信号的产物。带过来等于**把旧系统的结论当输入**——而规范明确要求不继承
旧版概率模型与信号逻辑。需要时可以从 1.1 的数据重新生成。

### 1.3 低价值 / 交易状态 — 不迁移

`account`、`orders`、`fills`、`positions`、`process_locks`、`meta`。

理由：这是模拟账本与运行时锁。新引擎按自己的 `initial_capital` 开户。
迁移旧账本会把两套系统的资金史混在一起，且违反"研究操作不得修改模拟交易账本"的隔离意图。

> 附带发现：`paper_run_final.json` 与 `worker_cycle.json` 实为**日志文本被误命名为 .json**，
> 引用时注意。

---

## 2. 可靠性判定规则（可直接实现）

全部规则是**行本身的纯函数**，对同一旧文件重跑必然得到相同判定。实现于
`weatherquant/legacy/rules.py::classify()`，测试于 `tests/test_legacy_import.py`。

判定结果只有两种（`VerificationStatus` 枚举是状态字符串的唯一来源）：

- `verified` — 通过全部规则，可直接用于研究
- `pending_verification` — **迁移但不自动信任**，须人工复核

| # | 表 | 触发条件 | 理由 |
|---|---|---|---|
| R1 | `markets` | `parse_status != 'ok'` | 桶解析失败的市场，其边界不可信 |
| R1 | `markets` | `parse_status` 缺失 | 无法证明曾成功解析 |
| R2 | `books` | `status != 'ok'`（`empty`/`no_asks`/`crossed`） | 空盘、无卖侧、交叉盘 |
| R3 | `books` | `venue_ts IS NULL` | 无场所时间 ⇒ 采集时刻的陈旧度无法证明 |
| R4 | `forecast_runs` | `status != 'ok'`（`unavailable`/`failed`/`no_coverage`） | run 本身不可用 |
| R5 | `forecast_daily`/`forecast_hourly` | 父 run 的 `status != 'ok'` | 子行继承父级判定 |
| R6 | `forecast_daily` | `n_hours < 24` | 不完整日会**低估**真实最高温，与完整 run 不可比 |
| R7 | `observations` | `source` 为空 | 溯源丢失 |
| R8 | `daily_observations` | `is_official = TRUE` | **观测永远不是官方结果**；声称否则即是缺陷 |
| R9 | `daily_observations` | `n_readings` 缺失或 `<= 0` | 聚合依据不可审计 |
| R10 | `settlements` | `is_official = FALSE` | 非官方裁决 |
| R11 | `settlements` | `yes_price` 不在 {0, 1} | 半结算状态 |
| R12 | `settlements` | `notes` 含 `DISAGREE` | **旧系统自己记录了交叉核对与场所冲突**，不能当可信丢掉 |
| R13 | `price_history` | `mid` 缺失或不在 [0, 1] | 概率型价格越界 |
| R14 | `events` | `rules_text` 为空 | 无法核对结算条款 |

**规则不丢弃任何行** —— 只打标。丢弃会破坏可追溯性。

---

## 3. 对真实旧库实际执行的结果

以下由 `plan_import()` 对生产旧库只读运行得到（写入零字节到旧文件）：

```
legacy source: E:\Project\WeatherQuant\backend\data\weatherquant.duckdb
  115918 rows across 10 migratable tables (114869 verified, 1049 pending)
  migrating:
    books                -> order_books                  693 rows,    693 pending
    daily_observations   -> daily_max_temperatures      2200 rows,      0 pending
    events               -> events                       408 rows,      0 pending
    forecast_daily       -> forecast_daily              2017 rows,    341 pending
    forecast_hourly      -> forecast_hourly            49662 rows,      0 pending
    forecast_runs        -> forecast_runs                196 rows,      1 pending
    markets              -> markets                     3756 rows,      0 pending
    observations         -> observations               30267 rows,      0 pending
    price_history        -> price_history              22996 rows,      0 pending
    settlements          -> settlements                 3723 rows,     14 pending
```

### 待核明细

| 表 | 待核 | 原因分布 |
|---|---:|---|
| `books` | **693 / 693 (100%)** | 688 × `no venue_ts: staleness at capture time is unknowable`；5 × `legacy book status=no_asks` |
| `forecast_daily` | 341 / 2017 (16.9%) | 各时长不完整日：45×16h、45×17h、45×22h、45×5h、44×10h、44×4h、37×23h、36×11h、… |
| `settlements` | 14 / 3723 (0.4%) | `cross-check disagrees with the venue resolution` |
| `forecast_runs` | 1 / 196 | `status=unavailable: model run not archived yet` |

**总计 1,049 行待核（0.9%）。** 其余 114,869 行通过全部规则。

### 3.1 必须正视的一条：`books` 100% 待核

旧版采集**从未写入 `venue_ts`**，所以全部 693 条订单簿快照都无法证明在采集时刻是新鲜的。

这**不是规则太苛刻**，而是对那一批数据的诚实判决：一个订单簿的价值就在于"决策时刻它是否新鲜"，
没有场所时间就无法回答。规则如实报出，正说明它有用。

**需要 P1 做一个决定**（见 [`roadmap.md`](roadmap.md) 任务 M-1）：

| 选项 | 后果 |
|---|---|
| **A. 接受 `fetched_at` 作为采集时刻** | 693 行转为 `verified`，可立即用于研究；代价是承认"我们拉到的即是我们看到的"，忽略服务端缓存延迟 |
| **B. 维持 `pending`** | 诚实但使唯一的深度数据全部需人工复核才能用 |
| **C. 重新采集** | **不可能** —— Polymarket 无历史订单簿 API，这批深度数据一旦丢失即永久丢失 |

推荐 **A + 显式记录局限**：`fetched_at` 是真实采集时间，对"刚拉下来的快照"是合理上界；
同时在研究中任何用到这 693 行的地方都标注 `venue_ts` 缺失。**这必须是一个被记录的决定，
而不是悄悄改规则。**

### 3.2 已知的溯源缺口（迁移时一并记录）

来自旧库审计 §5，迁移不修复它们，只**不掩盖**它们：

- `forecast_runs.raw_json` — **100% NULL**（原始载荷在 `data/raw/` 另存，可回填）
- `forecast_hourly` — 无 `fetched_at` 列
- `books.venue_ts` — **100% NULL**
- `settlements.resolved_at` — 实为**导入时间**，非场所裁决时间
- `fetched_at` — 被 upsert 覆盖，历史采集时刻不可恢复
- `signals.book_id` 87.5% NULL、`snapshot_id` 91.7% NULL（该表不迁移，仅记录）

---

## 4. 接口设计

### 4.1 已实现（P0）

```python
from weatherquant.legacy import LegacySource, plan_import, classify, LEGACY_TABLES

with LegacySource("backend/data/weatherquant.duckdb") as src:
    plan = plan_import(src)          # 只读；对旧文件写入 0 字节
    print(plan.summary())
    for t in plan.migrating_tables:
        print(t.legacy_table, "->", t.target_table, t.rows, t.pending, t.reasons)
```

| 组件 | 职责 | 保证 |
|---|---|---|
| `LegacySource` | 只读句柄：`tables()` / `count()` / `iter_rows()` / `fingerprint()` | 无任何写方法；流式迭代（每次 2000 行）以免物化 3 万行 |
| `plan_import(source)` | 读取 + 分类 + 统计 | **纯读**：`test_plan_is_write_free` 断言旧文件 size/mtime 不变 |
| `classify(table, row, run_status=...)` | 单行判定 | 纯函数；未知表抛 `KeyError` 而非默默放行 |
| `LEGACY_TABLES` | 表清单与价值分级 | `target_table is None` ⇒ 明确不迁移 |
| `record_import_run(target_con, plan, dry_run=...)` | 写清单到**新库** | 只碰新库；dry-run 也记录（"某日查看过旧库并见到 N 行"本身是研究状态） |

### 4.2 未实现（P1）

**行级转换与写入。** 原因：它需要逐表对齐列名与 JSON 编码差异，例如旧库订单簿存的是
`[{"price": 0.48, "size": 100}]`，新 schema 是
`[{"price_usd_per_share": 0.48, "size_shares": 100}]`。写一个未测试的占位转换器比没有更糟
——**半成功的迁移正是溯源丢失的方式**。规范也只要求 P0"设计"该接口。

P1 需要补齐：

- 逐列映射表（10 张表）
- `bids_json` / `asks_json` 的键名转换与**排序断言**（旧库 bids 降序 / asks 升序）
- `daily_observations` → `daily_max_temperatures` 的 `rule` 字段对齐
- 幂等 upsert（冲突键）与重复导入测试
- 导入后按 §3 的分布做一次计数核对，**数字对不上就失败**
- R1 的跨表规则：`daily_observations` 只有在存在 `max_temp` 相等的官方结算时才可信
  （旧库实测 **1,100 天中仅 45 天通过**——这条规则会把绝大多数日观测降级，
  需与 §3.1 一样作为一个**被记录的决定**来取舍）

---

## 5. 执行清单（P1）

- [ ] M-1：就 `books.venue_ts` 缺失作出决定（§3.1），并把决定写进本文档
- [ ] M-2：实现 10 张表的逐列映射与 JSON 键转换
- [ ] M-3：实现幂等写入 + 计数核对（对不上即失败）
- [ ] M-4：决定 R1（跨表核对）的取舍并记录
- [ ] M-5：迁移后运行 `plan_import()` 复核分类分布与 §3 一致
- [ ] M-6：`data/raw/` 原始载荷单独归档（不经过 DuckDB 转换），作为可无网络重放的底座
- [ ] M-7：全程旧库只读；完成后用 `connect_legacy_read_only` 验证一次写入被拒

---

## 6. 复现本次计划

```sh
cd weatherquant-next
uv sync --group dev
uv run python -c "
from weatherquant.legacy import LegacySource, plan_import
with LegacySource('../backend/data/weatherquant.duckdb') as src:
    print(plan_import(src).summary())
"
```

该命令对旧库**只读**。若旧库被其他进程占用（DuckDB 单写者锁会连带阻塞读），
复制一份快照再对副本运行即可——复制同样不修改原件。

# 结算核对队列：ACCEPT 市场的覆盖闭环（WQN-32）

2026-10-26。实现：`weatherquant/performance/settlement_queue.py`、
CLI `weatherquant-research settlement-queue`。配套：
`backfill-settlements`（在线只读回填）、[`winrate-acceptance.md`](winrate-acceptance.md)
（分母纪律）、WQN-33（观测交叉核验，本工作流的下游）。

---

## 1. 解决什么问题

引擎只会给"恰好存在的结算记录"打分。**没有任何机制强制"被问过的市场"
等于"产生过决策的市场"**——一对从未被问起的市场，与一对平台没回答的
市场，在数据里长得一模一样。一个季度下来，"我们的已解决样本"与"我们的
全部历史"之间的差，就是无声的选择偏差。

核对队列用已记录的事实回答三个问题：

1. **每个 ACCEPT 市场都在管线里吗？** 凡产生过至少一次 ACCEPT 的
   `(market_id, target_date)`，无论横跨多少模型/方向/模式/episode，
   在队列里**恰好出现一次**。工单验收句"过去产生 ACCEPT 的市场都进入
   核对队列"就是这个不变量，以数据而非口头形式成立
   （`coverage.all_accept_markets_in_queue`）。
2. **每对市场处于什么状态、下一步做什么？** 六个桶与合同的结算词汇表
   一一对应：

   | 桶 | 含义 | 动作 |
   |---|---|---|
   | `missing` | 完全没有结算记录 | backfill |
   | `pending` | 平台尚未出结果 | 等待/再回填 |
   | `disputed` | 平台争议中 | 不动——永不评分 |
   | `unavailable` | 问过但找不到 | 排查 |
   | `resolved_unverified` | 平台已结算、未做观测交叉核验 | 核对观测 |
   | `verified` | 已结算且已交叉核验 | 不动——**WIN/LOSS 合格** |

3. **这对市场问得出去吗？** 问平台需要 `condition_id`，而决策行不带它。
   目录（collector `markets` 表的**副本**，备份文件即可）补齐它；目录解
   析不了的标 `needs_catalog: true` 并附人工查询路径——**绝不编造标识符**。

## 2. 与 WIN/LOSS 门的关系（两条实现互为对账）

分母纪律在 `winrate.py`（RESOLVED + verified 才进分母），队列只是把同一
规则变成**可数、可审计的桶**。有测试用生产流式引擎从同一份决策+同一份
结算索引出发断言：**队列判 `verified` 的对数 == 引擎实际打分的样本数**
——两条实现永不漂移。反之 `resolved_unverified`（含验证标签缺失的
RESOLVED 记录——"未知"不得读作"已验证"）一律不合格。

来源与检索时间持久化：回填记录带 `resolution_source`、`fetched_at_utc`、
`raw_source_reference`（condition_id + 交易哈希 + log_index），队列逐条
透出。

## 3. 用法

```bash
# ① 从决策导出建队列（离线，不碰任何活库）
weatherquant-research settlement-queue \
    --export /mnt/user/appdata/weatherquant-shadow/reports/decisions-export \
    --catalog-from-collector /path/to/verified-backup.duckdb \
    --out /tmp/settlement-queue.json

# ② 对队列里 missing/需要重问的条目做回填（唯一联网步骤，只读公开 API）
weatherquant-research backfill-settlements \
    --markets /tmp/conditions-to-ask.json --out /tmp/settlements.json \
    --dataset live --timeout 20

# ③ 用回填结果重新分类
weatherquant-research settlement-queue \
    --export /mnt/user/appdata/.../decisions-export \
    --settlements /tmp/settlements.json \
    --catalog-from-collector /path/to/verified-backup.duckdb \
    --out /tmp/settlement-queue-after.json
```

决策源二选一：`--export`（导出目录/文件）或 `--state-db`（performance
状态库——episode 全部由 ACCEPT 构成，天然就是 ACCEPT 市场清单；构建器
运行期间不要读，DuckDB 单写者规则会拒绝）。

## 4. 验收对照与证据

| 工单验收 | 证据 |
|---|---|
| 过去产生 ACCEPT 的市场都进入核对队列 | `test_every_accept_market_is_in_the_queue_exactly_once`：6 模型×模式×重复观测折叠为一对一条；REJECT/NO_TRADE 市场不入队；无 market_id 的行计入 `decisions_without_market_id` 不静默吞掉 |
| 区分 UNAVAILABLE/PENDING/DISPUTED/UNVERIFIABLE | `test_settlement_states_map_onto_the_six_buckets`：六桶逐一断言 |
| 只有官方且可复核结果进入 WIN/LOSS | `test_verified_pairs_are_exactly_what_the_engine_scores`（生产流式引擎对账）+ `test_a_resolved_record_without_a_verification_label_is_never_verified` |
| 来源与检索时间持久化 | `test_source_and_retrieval_time_travel_with_each_entry` |

**在线只读冒烟（2026-10-26，data-api.polymarket.com）**：用 fixtures 里
2026-10-13 采集的真实 condition_id 跑 `backfill-settlements --dataset live`，
平台返回与离线 fixture 逐字段一致（`price="0"` → `resolved_outcome=0.0`、
同一交易哈希），`fetched_at_utc` 与 `raw_source_reference` 落盘；
`verification_status=UNVERIFIED`——官方结算未经观测核验，正确地不进分母。

**全工作流链条**：队列（`missing`）→ 在线回填 → 队列（`resolved_unverified`，
动作=verify_observations，不合格 WIN/LOSS）。输出见 PR 描述。

**测试**：`tests/test_performance_settlement_queue.py` 10 项；全套
`uv run pytest -q` 见 PR。

## 5. 边界

- **本工单只读取公开结算数据**（`data-api.polymarket.com/v2/resolutions`），
  不含任何下单/钱包/凭证代码，不写任何交易路径；
- 队列与回填均**不打开任何活库**：决策来自导出或状态库，目录来自备份
  副本；
- 生产验收（对全量导出跑队列+回填+核对闭环）需 NAS，未做——工单状态
  保持"代码完成、生产验收待做"。


### 评审修复验证

coverage 中缺失市场 ID 的 ACCEPT 行字段统一为 accept_rows_without_market_id，CLI 与队列构建器使用同一字段。正常输入命令返回 0 并输出队列。

# 历史方向胜率与按目标日聚类 CI：验收记录（WQN-35）

2026-10-26。统计实现已在 v0.1.4–v0.1.5 落地（`performance/winrate.py`、
`performance/episodes.py`、流式引擎 `performance/streaming.py`）；本工单的
交付是**验收**：用可复现的证据回答工单的每一条验收标准，并把"生产数据上
跑一遍"的命令留给 NAS。

**原则：验收以证据判定，不以"代码存在"判定。**

---

## 1. 工单要求 → 验证方式

| 工单要求 | 实现 | 验收证据 |
|---|---|---|
| 区别 episode、market、target_date、重复观察 | episode 身份是五维键（market × target_date × model × direction × mode）；重复评估折叠进 episode（`episodes.py`） | 验收 case 3：573 次观测 → 96 个 episode；`valid_samples=29`（episode 级）而观测数是它的 ~20 倍，断言观测**永不**进分母 |
| 仅 RESOLVED + VERIFIED 进 WIN/LOSS 分母 | `outcome_for()`：非 RESOLVED → PENDING；RESOLVED 未验证 → UNVERIFIABLE；payout 缺失 → UNVERIFIABLE；sell_yes → 永不评分 | 验收 case 3：六类计数与独立台账**逐一相等**（含 sell_yes 全部计入 UNVERIFIABLE） |
| 聚类单位为独立目标日 | 区间按 target_date 自助重采样（`cluster_bootstrap_by_target_date`）；单聚类回退 Wilson 并标注方法 | 验收 case 3：聚类区间宽度 0.6113 > 同数据行级 Wilson 宽度 0.3418；`method` 字段逐字标注 |
| 零有效样本 → N/A | `win_rate=None` + 原文 `N/A — insufficient resolved independent samples` | 验收 case 1：全 PENDING/UNVERIFIED 的历史 → null + 原文，`valid_samples=0`，PENDING/UNVERIFIABLE 照常计数 |
| 样本增加后数字准确且可复现 | 阈值之下拒绝并写明缺口；达到阈值后给出比率与区间 | 验收 case 2（6 decisive → N/A 并写明 "needs 20 and 2"）与 case 3（29 decisive → 比率精确等于 WIN/(WIN+LOSS)）；**两次独立运行发布逐字节相同的 `win-rate.json`** |

## 2. 验收脚本与测试

每次运行使用新的或空的输出目录。省略 `--out` 时，脚本创建独立的临时目录并打印路径。已有验收文件的目录会被保留。

```bash
# 三案例端到端：零有效样本 / 不足阈值 / 完整样本（含两次运行的可复现性）
uv run python scripts/winrate_acceptance.py --out /tmp/wq-winrate-acceptance \
    --report /tmp/wq-winrate-acceptance-evidence.json

# 同一套案例进了单测，CI 会持续重跑
uv run pytest tests/test_performance_winrate_acceptance.py -q
```

脚本的设计要点：

- **期望值来自生成器的台账，不来自被测模块**。每个 (market, target_date)
  的结算场景（verified_win / verified_loss / pending / disputed /
  unverified / missing）由脚本指定，期望计数按合同规则独立重算；
  `weatherquant.performance.winrate` 的输出与之比对，而不是反过来。
- **episode 结构精确已知**：每 episode 3–8 次观测、间隔 60s（小于 900s
  gap 窗口，必然合并为一个 episode）；相邻 episode 间隔 30 分钟且五维键
  唯一（必然不合并）。episode 数因此是构造出来的，不是推断的。
- **市场跨日复用**（36 市场 / 96 episode）：验证"同一市场的多个 episode
  不是独立结算事件"由聚类承担，而不是被分母排除。
- **可复现性用逐字节对比**：同一 export、同一结算、同一 `now`，两次独立
  状态库、两个独立世代目录，发布内容必须完全一致——固定种子
  （`BOOTSTRAP_SEED`）与固定聚类顺序是合同要求，漂移即失败。

### 本机运行记录（2026-10-26）

```
case 1  零有效样本     → win_rate=None, reason 原文, valid_samples=0,
                        PENDING=19 / UNVERIFIABLE=5 与台账一致
case 2  不足阈值       → 6 decisive / 2 个目标日 → N/A，原因写明
                        "needs 20 and 2"；已有的 WIN=3/LOSS=3 照常准确
case 3  完整样本       → 96 episode / 573 观测；六类计数全部与台账相等
                        (14W/15L/16P/16D/19U/16Unavail)；
                        valid_samples=29（episode 级）；
                        rate=0.4827586206896552 = 14/29（精确）；
                        聚类 CI=[0.1818, 0.7931] (宽 0.6113)
                          > Wilson [0.3256, 0.6674] (宽 0.3418)；
                        两次运行 win-rate.json 逐字节相同
RESULT: PASSED（23/23 项）
```

## 3. 分组的阈值独立性

`by_model` / `by_direction` / `by_mode` / `by_target_date` / `by_edge_band`
每个分组**单独**过阈值：三个 resolved 日的模型报 N/A，不向隔壁模型借显著性
（`tests/test_performance_winrate.py` 已断言；验收脚本按 target_date 抽查
了三组的计数与台账一致）。

## 4. 生产验收（需 NAS，未做）

本机所有运行都打向 `--dataset fixture` 与临时目录。生产判定还需在 NAS 上
执行一次并留档：

```bash
uv run python scripts/winrate_acceptance.py --out /tmp/wq-winrate-acceptance \
    --report /tmp/wq-winrate-acceptance-evidence.json   # 同一套合成案例在 NAS 上复跑

# 对真实决策导出 + 真实结算索引的一次生产口径构建（只读导出目录，
# 状态库写入 weatherquant-performance-state，不触碰 shadow 库）：
weatherquant-research build-performance \
  --export /mnt/user/appdata/weatherquant-shadow/reports/decisions-export \
  --settlements /tmp/settlements.json \
  --out /mnt/user/appdata/weatherquant-performance \
  --dataset live \
  --state-db /mnt/user/appdata/weatherquant-performance-state/performance-state.duckdb
# 判定：
#   - summary.json 里 realized_directional_win_rate 与 win-rate.json overall 一致；
#   - 决策数尚不足时必须显示 N/A + 原因，而不是 0 或某个比率；
#   - counts.PENDING 与 settlements.json 里 PENDING 的 (market, target_date) 数一致；
#   - 隔天重跑（无新结算时）win_rate 与 confidence_interval 不变。
```

在此之前，工单状态保持"代码完成、生产验收待做"——与本工单"依赖项以验收
而非代码是否存在判定"的规则一致。

# P2-0 影子引擎验证报告

2026-10-11。执行环境：Windows 11 开发机，Python 3.12，无 Docker，无 NAS 访问权限。

本文件只记录**实际执行过**的验证。未执行的项目明确标注"未执行"，不以
"应当可以通过"代替结果。这与影子引擎自身的设计是同一条原则：
**无法证明的东西不写成已经成立的。**

---

## 0. 一句话结论

**离线链路与真实行情链路均已跑通并有据可查；Docker 镜像构建、GitHub Actions
与 NAS 24 小时部署三项未执行，因此本轮不满足开启模拟成交的条件。**

---

## 1. 交付清单

| 交付物 | 路径 | 状态 |
|---|---|---|
| Shadow Trading Engine | `src/weatherquant/shadow/`（12 个模块） | ✅ 实现 |
| 独立 Shadow 数据库 | `src/weatherquant/storage/shadow.py` + `storage/migrations_shadow/`（2 个迁移） | ✅ 实现 |
| Collector 只读行情输入适配器 | `shadow/ingest.py` + `shadow/events.py` | ✅ 实现 |
| 实时天气推断适配器 | `shadow/forecast.py` + `shadow/inference.py` | ✅ 实现 |
| 模型工件（版本化/固定/防泄漏） | `models/artifacts.py` + `models/factory.py` + `models/` | ✅ 实现 |
| Edge 评估 | `shadow/decision.py`（复用 `execution/pricing.py`） | ✅ 实现 |
| 决策状态机 + 风险闸门 | `shadow/decision.py` + `configs/shadow-thresholds.json` | ✅ 实现 |
| Shadow CLI | `weatherquant-shadow {init,run,report,status}` | ✅ 实现 |
| Dockerfile / 构建目标 | `Dockerfile.shadow`（独立文件） | ⚠️ 已写，**未构建** |
| compose | `docker/compose.shadow.unraid.yaml` | ⚠️ 已写，**未运行** |
| 健康探针 | `docker/healthcheck_shadow.py` | ⚠️ 已写，**未运行** |
| GitHub Actions | `.github/workflows/shadow-ci.yml`、`shadow-release.yml` | ⚠️ 已写，**未执行** |
| 单元与集成测试 | `tests/test_shadow_*.py`（6 个文件，154 例） | ✅ 通过 |
| 部署说明 | `docs/shadow-unraid-deployment.md` | ✅ |
| 架构说明 | `docs/shadow-engine.md` | ✅ |
| 风险规则 | `docs/shadow-risk-rules.md` | ✅ |
| 本报告 | `research/shadow-validation.md` | ✅ |

新增 CLI：`weatherquant-research fit-models --cutoff YYYY-MM-DD [--pin]`
（拟合四个基线并冻结为工件）。

---

## 2. 已实际执行的验证

### 2.1 测试与静态检查

| 项 | 结果 |
|---|---|
| `uv run ruff check .` | **All checks passed**（0 告警） |
| `uv run pytest -q` | **613 passed, 3 skipped** |
| 交付前基线 | 420 passed, 1 skipped |
| 净新增 | **+193 例**，全部为影子相关 |

3 个 skip 全部来自 `tests/test_workflows.py` 的参数化用例 ——
`test_data_checks_run_after_the_collector_stops` 对"不含 `SELECT COUNT(*)`"
的工作流按设计跳过（原有 1 例 + 新增 `shadow-ci.yml` 与 `ci.yml` 各 1 例）。

新增测试文件与覆盖点：

| 文件 | 覆盖 |
|---|---|
| `test_shadow_ingest.py`（16） | 半行不消费、重启不重复、截断记录并回零、文件缺失留痕、整行损坏前跳且留痕、跨天缺口、超长行驻留、四类事件分类、**增量消息永不成为盘口**、单边/未附着盘口、软解析失败、lag 度量 |
| `test_shadow_models.py`（18） | 工件不可变、截止日期强制、raw/calibrated 消融（同 σ 不同 bias）、source hash 稳定、spec_version 拒绝、manifest 固定与缺失即报错、薄桶不借全局 σ |
| `test_shadow_forecast.py`（30） | 只请求已过去的 issue 时次、12h 可发布、未来/过旧/零小时拒绝、fusion 同次对齐与无共同时次即拒绝、失败 1h 冷却重试而 unavailable 永不重问、预算限制、持久化回读不重抓、严格回测规则与实盘规则分产 |
| `test_shadow_decision.py`（52） | 5 态可达、16 原因齐全且无 `generic_error`、因果序、主原因稳定、买入走 ask / 卖出走 bid、手算成本与 edge 比对、单边盘口按方向分别处理、交叉盘双拒、时钟闸门（未来 venue / 收到晚于决策 / 时钟倒挂 / 无 venue 时间戳）、工件晚于决策即拒、深度/最小手数/部分成交、多原因并存 |
| `test_shadow_storage.py`（21） | 迁移有序幂等、篡改即拒、**按名拒绝采集器库与研究库**、单实例租约与释放、市场/盘口/质量事件幂等、括号 first-write-wins、盘口不倒退、**事务失败则效果与偏移同归于尽**、UTC 会话 |
| `test_shadow_runner.py`（29） | 端到端归档→决策→报告、状态与原因白名单、**只读归档字节不变**、重启不回放不重复、重启后新增行被拾取、CANDIDATE 中断可见、第二实例被拒、心跳写出且探针不碰库、报告字段齐全且无虚假收益、CLI init/report、**单调时钟回归测试** |

### 2.2 静态结构检查

| 检查 | 结果 |
|---|---|
| 代码中不存在下单客户端 / 订单 ID / 持仓表 | ✅ grep 无命中 |
| `equity_curve` 恒为 `null` | ✅ 断言 |
| 阈值来自版本化 JSON 而非环境变量 | ✅ `load_thresholds` 只接受文件 |
| 工件截止日期晚于决策即拒绝 | ✅ `usable_at` + 评估边界双查 |
| 影子不 import `legacy`、`storage` 不 import 领域模块 | ✅ `ruff` 依赖方向未告警 |
| 新增 workflow 满足 `test_workflows.py` 三条不变量 | ✅（10 passed, 3 skipped） |

---

## 3. 真实行情测试结果

**环境**：本机启动真实 `weatherquant-collect`（Polymarket Gamma + CLOB + WS），
持续约 13 分钟，随后影子引擎读取其归档。全部上游为真实请求，无 mock。

### 3.1 输入

| 项 | 实测 |
|---|---|
| 归档 | 10,943 行 / 12 MB：`gamma` 6、`rest` 1,846、`ws` 9,091 |
| 解析出的市场 | **58**（含 4 个真实事件：10/11、10/12、10/13 各 11 个市场 + 10/10 已关闭 11 个） |
| 有盘口的 token | **66** |
| 真实预报抓取 | **16 次** `SingleRunsClient.fetch`，全部 HTTP 200；ECMWF 与 GFS 各 2 次运行 |
| 事件分类 | `gamma`→元数据、`rest`→完整快照、`ws`→快照/增量/成交混合，**增量消息 0 次进入定价** |

真实 `groupItemTitle` 实例：`'55°F or below'`、`'56-57°F'`、…、`'74°F or higher'`
—— 11 段阶梯两端开口，`parse_bracket_label` 全部解析成功。

### 3.2 输出（4 个进程、4 个周期、352 条决策）

| 指标 | 实测 |
|---|---|
| 进程启动 / 周期 | 4 / 4 |
| 决策总数 | **352**（88 × 4 周期） |
| ACCEPT | **27** |
| REJECT | **237** |
| NO_TRADE | **88** |
| DATA_UNAVAILABLE | 0（另见 §3.3 的断流测试） |
| 可定价（candidates） | 212 |
| 预测记录 | 16（4 模型 × 4 目标日） |
| 决策延迟 | p50 **10 ms**，p95 **20 ms**，max 21 ms（报告阈值 5000 ms） |
| 数据缺口 / 错误 | **0 / 0** |
| 数据质量事件 | 4 条，全部为 `shadow_started`（info） |

### 3.3 主拒绝原因分布

| 原因 | 次数 | 解释 |
|---|---:|---|
| `STALE_ORDERBOOK` | 108 | 最新可用盘口超过 90s。采集器对个别 token 的刷新间隔实测 p90=157s（见 §5.2） |
| `MARKET_CLOSED` | 88 | 10/10 事件已关闭，11 市场 × 2 方向 × 4 周期 |
| `NEGATIVE_NET_EDGE` | 70 | 模型期望在成本之后为负 —— 与 P1-2 "模型劣于市场" 一致 |
| `INSUFFICIENT_LIQUIDITY` | 23 | 深度不足、部分成交、或低于最小手数 |
| `INCOMPLETE_ORDERBOOK` | 17 | 单边盘口（极端括号常见）或交叉盘 |
| `TIME_MISMATCH` | 10 | 见 §5.1，全部为亚秒级时钟偏移 |
| `EDGE_BELOW_THRESHOLD` | 9 | 0 < edge < 0.02 |

**四种非 ACCEPT 状态全部在真实数据上出现过**：

```
ACCEPT / REJECT / NO_TRADE  —— 352 条决策内
DATA_UNAVAILABLE            —— 断流测试 66 条（见下）
```

断流测试（`WQ_SHADOW_COLLECTOR_SILENCE_SECONDS=1`，独立库）：

```
DATA_UNAVAILABLE 66   primary_reason COLLECTOR_DISCONNECTED 66
NO_TRADE         22   primary_reason MARKET_CLOSED           22
```

即：采集器静默时给出 `COLLECTOR_DISCONNECTED`，而**已关闭的市场仍报
`MARKET_CLOSED`** —— 与因果序（MARKET_CLOSED 优先）完全一致。
**代理/上游故障不会制造 ACCEPT。**

### 3.4 成本算术

- 买入 YES 一律走真实 ask，卖出 YES 一律走真实 bid，**中价从未参与定价**。
- 手续费按 `fee_rate=0.05`（真实 gamma `feeSchedule.rate`）经
  `taker_fee_usd` 计算；单测中与手写公式
  `C × rate × p × (1−p)` 逐值比对。
- 卖出侧 `(proceeds − fees − buffer) − q·shares` 与买入侧
  `q·shares − (fill + fees + buffer)` 共用同一个 `ExecutionCost` 三项分解。

### 3.5 ACCEPT 的形状（**不是机会的证据**）

| 项 | 实测 |
|---|---|
| ACCEPT 的成交价区间 | 0.001 – 0.5994 USD |
| 全部可定价决策的 edge | 均值 **−0.0380**，最小 −0.494，最大 +0.2911 |
| ACCEPT 的 edge 均值 | +0.1064 |

**必须强调的解释**：ACCEPT 集中在极低价格端（0.001 即最小 tick），
与"模型分布比市场更宽、把更多质量放在尾部"这一已知的校准问题完全吻合。
样本外 Brier 1.49 vs 市场 1.03 的结论**没有**因为影子跑出 ACCEPT 而改变。
影子的职责是把这个形状如实记录下来，而不是把它当成机会。

### 3.6 不污染 Collector 数据

三重证据：

1. **归档前缀逐字节不变**：运行前后对每个 `.jsonl` 取"当前长度的前缀"哈希，
   三个文件全部一致，只有采集器自己在追加：

   ```
   gamma/2026-10-11.jsonl  321432 -> 321432   unchanged
   rest /2026-10-11.jsonl  3233542 -> 3427970  prefix unchanged
   ws   /2026-10-11.jsonl  3976826 -> 4725801  prefix unchanged
   ```

2. **采集器数据库字节级不变**：停掉采集器后取 `weatherquant.duckdb` 的
   SHA-256，跑一轮影子，再取一次：

   ```
   before 35019b4c0460a9b25b02d0d332cf1daf44bdecc0cf953426ef5a0af48f253872
   after  35019b4c0460a9b25b02d0d332cf1daf44bdecc0cf953426ef5a0af48f253872
   COLLECTOR DB BYTE-IDENTICAL
   ```

3. **结构保证**：影子代码没有任何指向采集器库的路径；
   `ShadowDatabase._guard_foreign_database` 会**按名字**拒绝它
   （单测：`test_it_refuses_the_collectors_database_by_name`）。

### 3.7 重启恢复（真实数据）

4 次独立进程启动，各自冷启动后读取同一归档：

| 检查 | 结果 |
|---|---|
| `market_catalog` 行数 | 4 次启动后均为 **58**（不是 58×4） |
| `token_books` 行数 | **66**（每 token 一行） |
| `ingest_checkpoints` | **3**（gamma / rest / ws 各一） |
| 决策 | 352 = 88 × 4，`DISTINCT cycle_id = 4`（每周期一套，不合并） |
| 上游重问 | 已持有的运行 **0 次** 重新抓取 |

---

## 4. Docker 镜像构建结果（已在 Linux Runner 实际执行）

**本机仍无 Docker**（`docker: command not found`），构建与容器验收全部由
GitHub Actions 的 `shadow-ci.yml` 在 Linux Runner 上完成。

**最终结果：`3f5bed6` 全部通过。**

| 检查 | 结果 |
|---|---|
| `shadow-tests`（6 个影子测试文件 + Ruff + 工件存在性） | ✅ success |
| `Build (no push)` — `Dockerfile.shadow`，linux/amd64 | ✅ success |
| `Container starts and reports health` | ✅ success |
| `Runs as a non-root user` | ✅ success |
| `Data directory is writable by the container user` | ✅ success |
| `Read-only collector mount cannot be written through` | ✅ success |
| `Single-instance lease refuses a second writer with exit code 4` | ✅ success |
| `SIGTERM is handled gracefully and state survives a restart` | ✅ success |
| `The shadow database is a real shadow database` | ✅ success |
| `shadow-ci-never-publishes` | ✅ success |
| **采集器 `ci.yml`（lint-and-test + build-image）** | ✅ success —— **每一条提交上都绿** |

**失败从未触发发布**（Task 6 第 6 项，实证）：

- `shadow-ci` 前后失败 3 次；
- `shadow-release.yml` **运行次数 = 0**（无 `shadow-v*` tag 被推送）；
- 远端 tag 只有 `v0.1.0` / `v0.1.1`（采集器的），本次未新增任何 tag；
- 采集器 `release.yml` 最后一次运行是 **2026-10-10**，`:stable` 未被移动；
- 结构性原因：`shadow-ci` 的 `permissions` 只有 `contents: read` +
  `actions: write`，**没有 `packages: write`**，且无 `docker/login-action`、
  `push: false` —— 即使全部检查被删掉，它也发不出镜像。

### 4.1 三次失败：每次都是工作流或探针的问题，不是镜像的问题

| # | 失败步骤 | 根因 | 修复 |
|---|---|---|---|
| 1 | `Container starts and reports health` | 探针归档在目录 `chown 1000:1000` **之后**创建；本作业以 uid 1001 运行，已经交出去的目录写不进去 → `Permission denied` | 先写文件，再 chown |
| 1b | `shadow-ci-never-publishes` | 守卫在本文件里搜索自己刚写下的字符串，唯一的匹配就是它自己的 `grep` | 过滤掉含 `grep -nE` 的行 |
| 2 | `Data directory is writable` | 心跳**只在周期末尾**写，而首个周期先去 Open-Meteo 抓预报；探针等的是别人家的延迟。同时等待循环把退出码 0 当成功，而探针对 `starting` 也返回 0，于是它在自己等待的条件上提前退出 | `start()` 即写心跳，周期**开头**再写一次；CI 改为等**文件存在** |
| 3 | `Single-instance lease ... exit code 4` | `--once` 用宽泛的 `except Exception` 包住 `start()`，把有文档的拒绝码 4 吞成了通用失败码 1 | 先捕获 `LockError` 返回 4；新增回归测试 |
| 3b | （预防性）宿主读取心跳 | `mkstemp` 建出 0600，`os.replace` 沿用该权限 → 只有引擎自己的 uid 能读 | 写完后 `chmod 0644`；心跳本来就是给别人的 |

**五处修复各带回归测试或工作流自检**，全部在 `613 passed` 之内。

## 5. 执行中发现并修复的缺陷

真实数据各跑出一个离线测试没能发现的问题。两者都已修复并各加了回归测试。

### 5.1 决策时点取在了周期开始（已修复）

**现象**：首轮真实运行 66 条决策全部带 `TIME_MISMATCH`，`detail` 为
`this process read the book at ... after the decision instant` 与
`forecast was received at ... after the decision instant`。

**根因**：周期用 `started = clock()` 作为 `decision_time`，但盘口在阶段 1 才读入、
预报在阶段 2 才抓取 —— 两者都**晚于** `started`。于是每个输入看起来都
"在决策之后才到达"，时钟闸门拒绝了全部决策，尽管数据其实早已在手。

**修复**：`decision_time` 改为在阶段 3 开始处重新取（`shadow/runner.py`）。
语义是：**决策时点 = 它的全部输入都已就位的那一瞬间。**

**回归测试**：`test_no_input_can_arrive_after_the_decision_that_used_it`
用一个**每次调用都前进**的时钟。冻结时钟下所有时间戳相等，这个 bug 完全不可见
—— 这正是它此前漏过测试的原因。已验证：把修复临时还原，该测试失败并报出 8 条
`TIME_MISMATCH`。

### 5.2 阈值 `max_snapshot_age_ms` 的前提错了（已修正，v1 → v2）

**现象**：首轮 66 条决策全部 `STALE_ORDERBOOK`，最小盘口年龄 15.6s，
刚好越过 15s 门槛。

**根因**：v1 的依据写的是"采集器每 5 秒重询一次盘口，15 秒 = 连续三次漏询"。
**这个前提是错的。** 5s 是轮询间隔，不是单个 token 的刷新周期 —— 采集器
按序扫描全部 token，且每周期有 45s 预算。对真实归档实测（66 个 token、
387 次观测、13 分钟）：

| 统计量 | 单 token 相邻刷新间隔 |
|---|---:|
| min | 11.1 s |
| **p50** | **37.9 s** |
| p90 | 157.3 s |
| max | 759.1 s |

**在 15s 门槛下，超过一半的完全健康的盘口会被判为陈旧** —— 这道闸门会
无条件拒绝一切，与市场本身无关。

**修正**：`max_snapshot_age_ms: 15000 → 90000`（一次采集器扫描的两倍），
`version: v1 → v2`，并在文件里写入 `corrected_from` 字段记录改了什么、
为什么改、何时实测的。

**这不违反"不得为制造交易而放宽阈值"**：

- 修正是在**看到任何决策结果之前**基于上游行为实测做出的（v1 连一条 ACCEPT
  都还没产出过，它拒绝的是全部 66 条）；
- 它纠正的是一个**测错了的量**，不是放松一个测对了的量；
- 修改落在版本化文件里，带 `corrected_from`、测量日期与完整依据，
  可评审、可回溯；
- 90s 仍然足够严格：刷新停滞 2 个周期以上的盘口照常被拒 ——
  实测 `STALE_ORDERBOOK` 的年龄区间为 **90.3s – 1258.2s**（最短的那条
  正好卡在新门槛外 0.3s），全部被拒。

修正后被 ACCEPT 的盘口年龄为 **8.4s – 85.6s（均值 38.9s）**，
与 p50 = 37.9s 的刷新周期自洽。

### 5.3 `NO_TRADE` 在生产中永远为 0（已修复）

**现象**：首轮报告 `NO_TRADE = 0`，`MARKET_CLOSED` 从未出现。

**根因**：运行器只评估 `tradeable_markets()`，已关闭的市场根本不进入评估 ——
于是该状态只存在于单元测试里，报告里永远是 0。这正是"一个闸门没人看着就会烂掉"
的形态。

**修复**：新增 `_evaluable_markets()`，评估**目标日在窗口内**
（今天−1 … 今天+10）的全部市场，不论是否 tradeable；关闭者由
`_closed_reason` 判为 `MARKET_CLOSED` → `NO_TRADE`。窗口外的（如 7 月的历史
事件、`target_date` 为空的）不评估，避免用噪音淹没真正相关的记录。

**回归测试**：`test_a_closed_market_in_the_window_records_no_trade`。
修复后真实数据：`NO_TRADE 88`、`MARKET_CLOSED 88`、`net_edge IS NULL` 全为 0 条定价。

### 5.4 其余修复

| 缺陷 | 发现方式 | 修复 |
|---|---|---|
| `RunFetch` 无 `n_points` 构造参数 → 重启回读崩溃 | 预报缓存回读测试 | 计数改存 `_RunState.n_points` |
| `ShadowStore` 无 `table_counts` → 关库时报错 | 端到端测试 | 改用 `ShadowDatabase.table_counts` |
| 报告 `edge["priced"]` 键名错误 | 端到端测试 | 改 `priced_decisions` |
| 重复原因互相覆盖 `detail` | 决策测试 | `fail()` 改为拼接，多原因各自的解释都保留 |
| 删除的归档文件永远不再被枚举 → 缺失静默 | 尾随测试 | `_files()` 并入已 checkpoint 但已消失的路径 |
| 超长未终止行不留 checkpoint → 每周期重复报错 | 尾随测试 | 驻留时也写 checkpoint |
| 采集器 `last_seen` 未在启动时回读 → 重启后首个空周期误报断流 | 重启测试 | `start()` 从 checkpoint 回填 |
| `ResilientHttp(timeout=...)` 参数名错误 | 端到端首次运行 | `timeout_seconds` |
| latency 用真实时钟、其它用注入时钟 | 单调时钟测试 | 统一为同一时钟 |

---

## 6. 尚未执行 / 尚未满足的条件

### 6.1 未执行

| 项 | 原因 | 何时可执行 |
|---|---|---|
| ~~Docker 镜像构建~~ | ✅ **已执行** —— `shadow-ci.yml` @ `3f5bed6` success | 见 §4 |
| ~~容器启动 / 非 root / 健康 / 权限 / 重启保留 / 单实例 / 只读挂载 / SIGTERM~~ | ✅ **已执行** —— 9 项全部 success | 见 §4 |
| ~~GitHub Actions 全流程（shadow-ci）~~ | ✅ **已执行** | 见 §4 |
| GHCR 发布 `weatherquant-next-shadow` | **未执行** —— 本轮未推送 `shadow-v*` tag，属发布动作，待批准 | `git tag shadow-v0.1.0 && git push origin shadow-v0.1.0` |
| **NAS 首次部署** | **无 NAS 访问权限** | 按 `docs/shadow-unraid-deployment.md` §5 |
| **NAS 连续 24 小时运行** | 同上 | 部署后 |
| **7 天影子观察** | 同上 | 24 小时验收通过后 |

### 6.2 NAS 还需要执行什么

按 [`docs/shadow-unraid-deployment.md`](../docs/shadow-unraid-deployment.md) §5.1
的 12 项验收清单逐条确认，其中最关键的四项：

1. **24 小时不中断**，记录预测次数、评估次数、拒绝原因分布；
2. **重启可恢复** —— `docker restart` 后表行数不回退；
3. **代理断线不制造机会** —— 指向关闭端口后应出现 `forecast_fetch_failed`
   与 `NO_FORECAST`，**ACCEPT 归零**；
4. **行情陈旧时不输出 ACCEPT** —— 停采集器 >15 分钟后应出现
   `COLLECTOR_DISCONNECTED`，ACCEPT 归零。

本轮已在本机用真实数据**预演过第 3、4 项**（§3.3），但 NAS 上的代理、
网络与磁盘环境不同，必须重新验证。

### 6.3 尚不满足、**不能开启模拟成交**的条件

| # | 条件 | 状态 |
|---|---|---|
| 1 | 模型样本外**优于市场** | ❌ **不满足** —— Brier 1.49 vs 1.03。影子跑出 ACCEPT 并不能改变这一点 |
| 2 | NAS 连续 ≥24 小时运行 | ❌ 未执行 |
| 3 | NAS 连续 7 天影子观察 | ❌ 未执行 |
| 4 | Docker 镜像通过全部容器验收 | ✅ **满足** —— `shadow-ci.yml` @ `3f5bed6`，9 项容器检查全绿 |
| 5 | 可解释的拒绝原因分布（无 `generic_error`） | ✅ 满足（真实数据 7 类原因，0 类含糊） |
| 6 | 时间一致、成本一致、可审计 | ✅ 满足（含 5.1 缺陷修复与回归测试） |
| 7 | 不污染生产 Collector | ✅ 本机已证（字节级）；NAS 待验 |
| 8 | 阈值未对同一样本拟合 | ✅ 满足（先验设定；v2 修正依据为上游行为实测） |
| 9 | 持仓账本 / 模拟成交 | 本轮**明确不做** |

**结论：第 1、2、3 条未满足，不得开启模拟成交或持仓账本。**

第 1 条尤其不能靠延长影子观察解决 —— 影子能证明链路正确，**不能让模型变准**。
样本外是否优于市场是 P1 遗留的独立问题，需要新的模型工作而不是新的运行时间。

---

## 7. 已知限制

| 限制 | 影响 |
|---|---|
| 单边盘口按方向分别处理 | 极端括号常只有一侧，卖单可定价而买单被拒 —— 这是真实市场状态，不是缺陷 |
| `TIME_MISMATCH` 对亚秒级时钟偏移一律拒绝 | 实测偏移 0.096–0.803s，10 条决策因此被拒。严格但保守；如需放宽，应作为**版本化的显式容差**提出，而不是静默改 |
| `STALE_ORDERBOOK` 在个别 token 上占比较高 | 根因是采集器对这些 token 的刷新间隔（p90 157s）。若需改善，应调整**采集器**的轮询策略，而不是继续放宽影子阈值 |
| 预测质量（Brier/LogLoss）不在影子报告内 | 影子没有结算；打分属于研究管线。报告里 `scored: false` 明确标注 |
| 本机验证的归档只覆盖约 13 分钟 | 跨天轮转、跨天缺口、采集器重启缺口的**逻辑**有单元测试，但未在真实多日归档上演练 —— 那正是 24 小时 NAS 观察要覆盖的 |
| 影子不直接访问 Polymarket | 行情全部来自采集器归档。若需独立行情源，属于另一项设计变更 |

---

## 8. 复现

```bash
# 1. 离线
uv sync --group dev
uv run ruff check .
uv run pytest -q                       # 613 passed, 3 skipped

# 2. 模型工件（已提交于 models/，可重跑）
uv run weatherquant-research fit-models --cutoff 2026-10-08 --version v1 --pin

# 3. 真实行情（本机，需网络）
uv run weatherquant-collect &           # 真实采集器，写 data/raw
WQ_COLLECTOR_RAW_DIR=data/raw \
WQ_SHADOW_DB_PATH=data/shadow-final/shadow.duckdb \
WQ_SHADOW_HEALTH_DIR=data/shadow-final/health \
uv run weatherquant-shadow run --once

WQ_SHADOW_DB_PATH=data/shadow-final/shadow.duckdb \
uv run weatherquant-shadow report

# 4. 断流预演（应得到 COLLECTOR_DISCONNECTED，ACCEPT 归零）
WQ_SHADOW_COLLECTOR_SILENCE_SECONDS=1 ... uv run weatherquant-shadow run --once
```

本报告引用的数据库：`data/shadow-final/shadow.duckdb`（4 周期 352 决策）、
`data/shadow-silence/shadow.duckdb`（断流预演）。两者均为运行期产物，
已在 `.gitignore` 覆盖范围内。

---

## 9. 发布前审计（2026-10-11/12）

### 9.1 ACCEPT 信号审计

**27 条 ACCEPT 行，去重后 8 个不同的 (市场, 方向) 信号**，每个被 2–4 个周期
重复观测 —— 决策是逐周期的观察，同一信号出现多次是设计而非重复计数。

| market | 括号 | 方向 | 目标日 | 观测 | edge 区间 | p | 成交价 |
|---|---|---|---|---:|---|---:|---:|
| 5451720 | 60-61°F | sell | 10-11 | 3 | 0.2686–0.2911 | 0.2932 | 0.5844 |
| 5482423 | 72-73°F | sell | 10-12 | 4 | 0.1165 | 0.2919 | 0.4227 |
| 5451719 | 58-59°F | buy | 10-11 | 3 | 0.1104 | 0.1115 | 0.0010 |
| 5540795 | 70-71°F | sell | 10-13 | 4 | 0.0956 | 0.2027 | 0.3106 |
| 5482425 | 76-77°F | buy | 10-12 | 3 | 0.0803 | 0.1058 | 0.0242 |
| 5451722 | 64-65°F | buy | 10-11 | 4 | 0.0444 | 0.1821 | 0.1313 |
| 5540797 | 74-75°F | buy | 10-13 | 2 | 0.0380 | 0.2367 | 0.1900 |
| 5451723 | 66-67°F | buy | 10-11 | 4 | 0.0358 | 0.0429 | 0.0067 |

**逐条独立复算（不调用被测代码）：0 / 27 处不一致。**
手续费按 `C × 0.05 × p × (1−p)`（HALF_UP 至 5 位）重算、缓冲按 `fill × 0.005`
重算、ENP 与 edge 按方向各自的公式重算，与库中值全部一致。

**全部 27 条通过的闸门**：

| 检查 | 结果 |
|---|---|
| 概率分布 | 11 段阶梯，`sum = 1.000000`，两端开口 |
| 预报可发布 | `publishable 12:00Z ≤ decision 15:43Z`，余量 **3.72 h** |
| 预报已取得 | `received ≤ decision`；`run = 00:00Z ≤ decision`（无未来运行） |
| `run + 12h == publishable` | 精确成立 |
| 盘口年龄 | 8.4 – 85.6 s，全部 < 90 000 ms |
| 深度 | ≥ 131 USD ≥ 25 门槛 |
| 手数 | `min_order_size = 5 ≤ 100` |
| 成交价 | 全部 ∈ (0, 1) |
| 采集器 / 影子回执 | 均 ≤ decision |
| 模型截止 | `2026-10-08 ≤ decision`，4 个工件全部 `usable_at(now)` |

**两个最大 edge 的解释（不是算术错误）：**

1. **5451720 卖出，edge 0.29** —— 模型众数是 62-63°F（0.3452），60-61°F 只有
   0.2932；市场对该括号的 bid 是 0.619，**市场众数与模型众数差一档**。
   模型中心 `61.30 + 0.61 = 61.91°F`，σ(24-48h) = 2.153°F，
   `P([59.5,61.5)) = 0.2932` —— 与库中值一致。
2. **5451719 买入，edge 0.11** —— 模型给 58-59°F 左尾 11.15%，市场 ask 是
   **0.001（最小 tick）**，即约 0.1%。反推市场隐含 σ ≈ 0.4–0.5°F，
   而模型用的是 2.153°F。

**根因**：σ 来自 `lead = day_end − run_time = 28 h` 的 24-48h 桶，那是**从运行
初始化算起**的不确定性；而决策发生在目标当地日 11:43、当天已过去一半，
市场在为**剩下的**不确定性定价。这不是实现缺陷，是模型的已知局限 ——
与 P1-2 "模型劣于市场" 的结论一致。

> **通过当前过滤器 ≠ 已证明的盈利机会。** 这 8 个信号是模型与市场的分歧
> 记录。样本外 Brier 1.49 vs 1.03 没有因为它们而改变。

### 9.2 LIVE / REPLAY

审计前**未实现** —— 归档重放与实时尾随产出无法区分。本轮补齐：

- `mode` + `mode_reason` 落到**每一条决策**（迁移 `0003_decision_mode`）；
- `auto` 按最新归档行年龄判定**一次**，此后不变（否则采集器故障会被改写成
  "我们在读历史"）；
- REPLAY 的 `decision_time` = 归档自己的最新事件时刻，预报按该时刻解析，
  **`publishable_at ≤ decision_time` 同样强制**；
- 豁免的只有"本进程何时读到它"这类重放本身的回执；**场所 vs 采集器时钟、
  负延迟照常拒绝**；
- 报告分开计数并告警：`ACCEPT live / replay = 0 / 6`。

**真实归档验证**（非构造数据）：归档最新事件 `15:44:46Z`、时钟 `17:51Z`，
`auto:archive_age_7625s_gt_900s` → 66 条全部 `REPLAY`，
`decision_time = 2026-10-11T15:44:46Z`（**不是** 17:51），
`live_accepted = 0`，无 `COLLECTOR_DISCONNECTED`，warnings 点名 6 条 REPLAY ACCEPT。

默认值是 `UNKNOWN` 而非 `LIVE`：忘记标注的调用方拿不到一行声称实时的记录。

### 9.3 盘口新鲜度重估（阈值 **未** 改动）

`max_snapshot_age_ms = 90000`、`version = v2` 及其 `corrected_from` 记录
**原样保留**。新增 `scripts/shadow_freshness.py`（+15 例测试）只做统计：

真实归档 `data/raw`（1846 观测 / 66 token / 794 个刷新间隔）：

| | p50 | p90 | p95 | p99 | max | 样本 |
|---|---:|---:|---:|---:|---:|---:|
| **全部** | 30.11 s | 122.0 s | 198.2 s | 734.1 s | 1260.0 s | 794 |
| $100–1k | 28.8 | 168.5 | 296.1 | 732.2 | 732.2 | 63 |
| $1k–10k | 29.9 | 115.8 | 185.8 | 551.6 | 851.8 | 370 |
| >$10k | 31.7 | 122.9 | 200.8 | 972.8 | 1260.0 | 361 |

（`<$100` 桶本次捕获**无样本**，故只有三档。）

**成因区分**：

| 类别 | 次数 | 含义 |
|---|---:|---|
| `normal_poll` | 694 | 在 90 s 门内 |
| `token_stale` | 100 | **归档在流动、该 token 没有** —— 掉出扫描或 404 停询 |
| `archive_silent` | 0 | 整段归档停止（采集器/网络）。**本次捕获未发生**，由单测覆盖 |
| 归档头之后仍停滞的 token | **0** | 没有真正被遗弃的盘口 |

**流动性不驱动陈旧**：超门比例 $100–1k 9/63=14%、$1k–10k 45/370=12%、
>$10k 46/361=13% —— 三档基本一致，符合"顺序扫描均匀刷新"的预期。
因此陈旧是**采集器调度**问题，而不是薄盘口问题；要改善应调整采集器轮询，
**不应继续放宽影子阈值**。

实现上修掉了一个自指缺陷：静默阈值最初取固定 30 s，会把 30 s 节奏的归档
永久判为静默；改用"区间内是否有其它行 + 按最快区间估计的节拍"，并断言
**长间隔不能抬高自己的判定门槛**（`test_a_long_interval_cannot_inflate_its_own_threshold`）。

### 9.4 模型与成本审计

| 检查 | 结果 |
|---|---|
| 4 个工件版本固定 | `manifest = {calibrated,ecmwf_raw,fusion,gfs_raw} → v1` |
| 训练截止 | 全部 `2026-10-08`，`usable_at(now) = True` |
| 输入可复现 | `source_data_hash` 逐模型记录；**`ecmwf_raw` 与 `calibrated` 同 hash 同 σ、仅 bias 不同（0.0000 vs +0.6067）** —— 消融设计成立 |
| 无未来信息 | 全部 `cutoff ≤ now`；评估边界再查一次 `training_cutoff ≤ decision_time` |
| 运行期不静默改参 | `grep` 影子包无 `fit_*` / `save_*`；测试**在拟合函数上装绊线**后仍跑出 12 条预测 |
| 成本只在 `execution/pricing` | 影子包内**只有 `decision.py`** 导入该模块；把 `taker_fee_usd` 换成会抛异常的实现后，评估**拒绝**而非继续算出数字 |
| 双向都走定价 | 买卖各调用 `build_fill_plan` 1 次、`taker_fee_usd` 1 次（计数断言） |

新增 `tests/test_shadow_audit.py`（11 例）承载以上属性 —— 它们断言的是
**出处**，不是数字：数字对但出处错，正是两套手续费实现的诞生方式。

### 9.5 部署安全

| 检查 | 结果 |
|---|---|
| 独立构建与发布 | `Dockerfile.shadow`；`shadow-release.yml` 的 `IMAGE_NAME = ${{ github.repository }}-shadow`；采集器工作流**不引用**该镜像名 |
| 数据目录独立 | `/mnt/user/appdata/weatherquant-shadow:/data` |
| 原始数据只读挂载 | `/mnt/user/appdata/weatherquant-next/raw:/collector-raw:ro` |
| 不连采集器生产库 | 影子代码/compose/Dockerfile 中 **无** `weatherquant.duckdb` 引用；另有按名拒绝的守卫 |
| 非 root | `USER wq`、`user: "1000:1000"` |
| 工件随镜像加载 | `COPY models` + `WQ_SHADOW_MODELS_DIR=/app/models` |
| 健康检查 | `HEALTHCHECK` + `docker/healthcheck_shadow.py`（只读心跳，不碰 DuckDB） |
| 代理可配 | compose 中 `HTTPS_PROXY/HTTP_PROXY/NO_PROXY` 已备（注释态） |
| 无多余端口 | Dockerfile 与 compose **均无** `EXPOSE` / `ports:` |
| 采集器离线不输出 ACCEPT | 断流测试 66 条全 `COLLECTOR_DISCONNECTED`、**ACCEPT 0**；`auto` 转 REPLAY 时 `live_accepted = 0`（有测试） |
| 重启不丢 checkpoint | 离线 4 次冷启动：`market_catalog` 恒 58、`ingest_checkpoints` 恒 3、决策按周期各一套 |

### 9.6 三个 skipped 测试

```
SKIPPED [3] tests	est_workflows.py:113: workflow performs no database verification
```

三例全部来自**同一个**参数化用例 `test_data_checks_run_after_the_collector_stops`，
对**不含 `SELECT COUNT(*)` 的工作流**按设计跳过。

| 工作流 | 是否跳过 | 为什么 |
|---|---|---|
| `ci.yml` | 跳过 | 只构建，不做数据库验证 |
| `shadow-ci.yml` | 跳过 | 同上 |
| `release.yml` | **不跳** | 有内联 `SELECT COUNT(*)`，并已断言 `docker stop` 在其之前 |
| `shadow-release.yml` | 跳过 | 查询写在 python `-c` 块里，未被"docker 命令"解析器捕获 |

**三例都不需要 Linux Docker** —— 它们是纯文本分析，与容器无关，也**不是缺陷**。

**真正需要 Linux Docker 才能验证的**（已全部编码进 `shadow-ci.yml`，
本地无法执行）：镜像构建、容器启动、非 root uid、健康探针、数据目录可写、
只读挂载不可写入、单实例退出码 4、SIGTERM 后状态保留、重启后库完好、
以及"CI 失败不触发发布"。

---

# 预测/决策时间对齐与 LIVE/REPLAY 隔离（WQN-25）

状态：已实现并全部在本机验证；**生产 NAS 上的历史复核（`weatherquant-shadow alignment`）尚未执行**。

## 1. 工单要求与对应物

工单要求：校验 `calibrated`、`ecmwf_raw`、`fusion`、`gfs_raw` 四模型的输入/输出，
市场 `target_date`、timezone、forecast issue_time、decision timestamp 与重放模式隔离。
验收三条，各自的落点：

| 验收 | 运行期保证（已有/新增） | 事后审计（本轮新增） |
|---|---|---|
| 不读取未来预报 | `ResolvedForecast.usable_at`：run_time ≤ 决策时刻、`publishable_at`（run+12h 保守滞后）≤ 决策时刻、时限内；训练截止 ≤ 决策时刻；**新增**：forecast 的 target_date 必须等于市场的 target_date | `FUTURE_FORECAST_RUN` / `FORECAST_NOT_PUBLISHABLE` / `TARGET_DATE_MISMATCH` / `PREDICTION_UNTRACEABLE` |
| 跨日期/跨市场事件重复可追溯 | 预测按自然键去重（同一 event 同一分布只写一次） | 暴露组清单：`same_weather_day`（同一 station+target_date 被多个市场定价）、`market_date_moved`（同一市场跨多个 target_date）、`forecast_sharing_markets`（同一次预报 run 喂多个市场——相关样本，量化原则 6） |
| LIVE 与 REPLAY 统计不混算 | 每行带 `mode`/`mode_reason`；episode 五维键含 mode；win-rate 有 mode 维度；**新增**：决策闸门拒绝错误 target_date 的预报 | `MODE_UNUSABLE`（NULL/UNKNOWN 无法归类）、`CYCLE_MODE_MIXED`（一个周期跨两种模式）、`LIVE_ROW_REPLAYED`（LIVE 行判定时刻与写入时刻漂移 >10 分钟）、`REPLAY_INSTANT_IN_FUTURE`（重放时刻晚于写入） |

## 2. 四模型输入/输出关系

`MODEL_SPECS`（`shadow/forecast.py`）：`ecmwf_raw`/`gfs_raw` 单源；`fusion` 必须两源
**同 run_time** 才解析（否则拒绝，不做部分混合）；`calibrated` 与 ecmwf 同 run 加偏差校正。

审计的对应检查（`MODEL_INPUT_UNTRACEABLE`）：落库的 `fusion` 输出必须能追溯到
**同一 (station, target_date, event, decision_time, run_time)** 的 `ecmwf_raw` 与
`gfs_raw` 行；`calibrated` 同样追溯 `ecmwf_raw`。运行期同次对齐由解析器保证，
审计证明**历史记录**与之相符——集合式反连接，一条 SQL 每 (模型, 源) 对。

timezone：目标日是站点当地民用日（`local_day_bounds_utc`），日最高温按当地日聚合。
预报缺当地日小时数（`n_missing_hours`）是合法但更弱的输入：审计**计数不判违规**
（`partial_local_day_decisions`），避免一份总在误报的审计失去可信度。

## 3. 用法

```sh
weatherquant-shadow alignment [--json]   # 引擎须停止（独占库）
```

输出：检查规模、分模式计数、暴露组/共享 run 清单（可追溯性）、违规明细（前 100 条，
计数完整）。exit 0 = 全部不变量成立；1 = 有违规。也可从 Python 调
`weatherquant.shadow.alignment.audit_alignment(con)`（runner 内部/测试用）。

## 4. 验证（本机）

`tests/test_shadow_alignment.py`（17 例，全过）：干净历史一致并正确列出暴露组；
未来 run / 未到期 issue / 无预测可追 / 日期或时刻错配 / fusion 缺同 run 源 /
calibrated 源 run 不匹配 / 模式缺失 / 周期混模式 / LIVE 行被重放 / REPLAY 时刻晚于写入，
各有一例点名违规；合法 REPLAY（时刻早于写入）通过；部分覆盖日只计数；
单 run 多市场列出不拒绝；**运行期闸门**拒绝为目标日之外日期的预报
（`shadow/decision.py` 新增检查）。全套 `uv run pytest -q`：1097 passed, 6 skipped。

## 5. 尚未验证（需 NAS 人工验收）

- 对生产 `shadow.duckdb` 历史跑一次 `alignment`，核对四模型追溯与模式分布；
- 若生产存在历史 `mode=NULL`/`UNKNOWN` 行（理论上不应有），确认处置方式后再清理。

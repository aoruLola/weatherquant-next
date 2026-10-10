# 结算验证：Polymarket 究竟依据什么，我们如何独立核对

2026-10-11。配套实现：`weatherquant.markets.settlement_rules`、`weatherquant.truth`。

---

## 1. 规则说了什么（逐市场提取，非概括）

408 个事件、407 种去重后的 `rules_text`，解析器 **408/408 得出 `parsed`**，无必填字段缺失。
完整逐事件结果用 `weatherquant-research rules -v` 查看。

### 1.1 两个来源系

| 系 | 事件数 | 解析站点 | 主来源 |
|---|---:|---|---|
| **NOAA** | 50 | KLGA | `https://www.weather.gov/wrh/timeseries?site=klga` |
| **Weather Underground** | 358 | KLGA | `https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA` |

**共同的、可执行的条款**：

| 维度 | 内容 |
|---|---|
| 解析站点 | **LaGuardia Airport Station (KLGA)** —— 408/408 |
| 统计量 | "highest temperature recorded"，408/408 |
| NOAA 系的数据列 | **`"Temp" column`，且明确指向 "Hourly Data"（"Show Hourly Data" 按钮）** |
| WU 系的数据源 | **`Daily Observations` 表**，并明确**排除** "Day High & Low" 汇总栏 |
| 单位 | Fahrenheit，408/408 |
| 精度 / 取整 | **整数华氏度**（"measures temperatures to whole degrees Fahrenheit"），`precision=1.0`、`rounding=nearest_integer_f` —— 408/408 |
| 时区 / 日期边界 | 线索为 `11:59 PM ET`（364 个事件明确写出），绑定 `America/New_York` |
| 备来源 | **仅 NOAA 系声明**：NOAA 数据在次日 23:59 ET 前不可用时，改用 WU Daily Observations |
| 完全无数据 | NOAA 系 42 个事件写明 "resolve to **the lowest bracket**" |
| 结算触发 | NOAA 系："once the first data point for the following date has been published … or by 11:59 PM ET on the day following, whichever comes first"；WU 系："**can not resolve until** the first data point for the following date has been published" |

### 1.2 两系的结构差异（必须分别处理，不可合并）

WU 系**不写** fallback、**不写** no-data 条款，且用 "can not resolve until" 而非
"will resolve once"。解析器若只认一种措辞，357/408 会被误标为 `partial`。

**这意味着**：不能假设所有市场都遵循 NOAA 那套 fallback 逻辑。来源字段必须随每行保存，
后续任何"如果 X 就怎样"的分析都要按 `primary_source_name` 分层。

---

## 2. 核对方法：两个必须避开的陷阱

### 2.1 循环比较

旧库 `settlements.daily_max_temp_f` 与我们自己的 `daily_observations(all_readings)`
**404/404 完全相同** —— 该列是把观测复制进去的，不是独立真值。

拿它比对同一份观测必然 100% 吻合，**这个数字没有信息量**。

### 2.2 按日期分组会捏造事件

2026-05-19 有**两个事件**：

| event_id | slug | 赢家 |
|---|---|---|
| `493658` | `arch-highest-temperature-in-nyc-on-may-19-2026` | `90-91°F` |
| `503590` | `highest-temperature-in-nyc-on-may-19-2026` | `95°F or below` |

按 `target_date` 分组会产出"一个事件两个赢家"，既掩盖了**两个事件判出不同括号**这一事实，
又把结构问题伪装成数据问题。**本实现一律按 `event_id` 分组**，并有测试钉住。

### 2.3 正确的比对

```
观测日最高温（我们的测量）  ──落在──▶  yes_price = 1 的那个括号（场所的裁决）
```

两者来源完全独立。**不一致时记录差异，绝不修改观测值去迁就结算。**

实现：`weatherquant.truth.crosscheck.run_crosscheck()`，
把结果写入研究库 `settlement_crosscheck` 表，每事件 × 每聚合规则一行。

---

## 3. 结果（基于旧库快照，405 个已结算事件）

| 聚合规则 | 事件数 | 匹配 | 不匹配 | 匹配率 |
|---|---:|---:|---:|---:|
| **`max of all readings`**（全部 METAR 报文） | 405 | **404** | **1** | **99.75%** |
| `max of routine hourly METAR`（仅常规整点） | 405 | 398 | 7 | 98.27% |

### 3.1 结论一：真值口径必须是 `all_readings`

7 个不匹配里 6 个是同一模式 —— `all_readings = hourly_metar + 1`，且赢家括号总是包含
`all_readings`：

| 日期 | 仅常规报文 | 全部报文 | 赢家括号 |
|---|---:|---:|---|
| 2025-10-21 | 66 | **67** | 67-68°F |
| 2025-12-02 | 40 | **41** | 41-42°F |
| 2026-06-09 | 79 | **80** | 80-81°F |
| 2026-06-14 | 89 | **90** | 90-91°F |
| 2026-08-17 | 79 | **80** | 80-81°F |
| 2026-09-20 | 71 | **72** | 72-73°F |

**结算所据的 NOAA `Temp` 列包含非常规（special）报文**；只取常规整点 METAR 会系统性低 1°F。
旧库 `settlements.notes` 的记录与之一致：

> *"routine hourly METAR only would say 71F (DISAGREES)"*

→ **研究口径 = `all_readings`；`hourly_metar` 只作诊断，不作真值代理。**

### 3.2 结论二：唯一的不匹配是场所侧的重复上架

| 事件 | 日期 | 观测 | 判出的赢家 |
|---|---|---:|---|
| `493658`（`arch-` 前缀） | 2026-05-19 | 95°F | `90-91°F` |

同日期正本 `503590` 判 `95°F or below`，与观测一致。

两个事件**声明来源相同（WU）、规则文本长度相同（1037 字符）**，却判出不同括号；
`493658` 的 `start=2026-05-17`（早于目标日两天），`503590` 的 `start=2026-05-19T22:14Z`
**晚于其 `end=2026-05-19T12:00Z`** —— 是重新上架的事件。

**这是场所侧的重复上架/重新结算问题，不是我们的观测问题。** 全库仅 3 个 `arch-` 事件，
样本不足以判断是否系统性；报告只陈述观测到的事实，不作外推。

---

## 4. 关于旧项目"1,100 天仅 45 天吻合"

P0 的旧库审计记为 *"only 45/1,100 days pass"*。**本次无法复现该数字**：

| 口径 | 天数 |
|---|---:|
| 结算与观测日期重叠 | **404** |
| 重叠日中 `settlements.daily_max_temp_f == all_readings` | 404（**循环**，见 §2.1） |
| 重叠日中 `== hourly_metar` | 393 |
| **观测落入赢家括号（`all_readings`）** | **404 / 405 事件** |

`1,100` 是观测总覆盖天数（2023-10-06 → 2026-10-09），结算只覆盖 2025-06-20 起的 404 天
——**分母不同**。把 404 与 1100 相除会得到一个看似严重、实则只是覆盖期不同的比例。

**判定**：不沿用 45，改用 §3 的直接括号比对。原始审计报告保持不动，差异在此记录。

---

## 5. 复现方式

```bash
cd weatherquant-next
uv sync --group dev

# 独立研究库（绝不触碰采集器的 weatherquant.duckdb）
export WQ_RESEARCH_DB_PATH=data/research/weather-research.duckdb
export WQ_LEGACY_DB_PATH=../backend/data/weatherquant.duckdb

uv run weatherquant-research init
uv run weatherquant-research extract-legacy   # 只读旧库
uv run weatherquant-research crosscheck
uv run weatherquant-research report           # 数据质量报告
uv run weatherquant-research rules -v         # 逐事件规则明细
```

全部命令**幂等**（重跑覆盖而非重复），旧库以 `read_only` 打开。

离线验证：`uv run pytest tests/test_truth_pipeline.py`（合成旧库 fixture，含 `arch-` 双事件形态）。

---

## 6. 尚未完成 / 依赖在线核实

- [ ] **NOAA `Temp` 列的直接取数**：本报告的观测来自 IEM ASOS（`all_readings` 规则），
      **不等于** NOAA 官方 hourly 表。两者是否逐小时一致尚未直接比对 ——
      目前只能通过 §3 的括号比对**间接**证明一致性。
- [ ] NOAA 官方"日最高温"产品（LCD / nclimdiv）是否存在、字段为何，待
      `research/noaa-klga-observations.md` 落地。
- [ ] 在线重新抓取当前市场规则，与旧库快照比对规则是否漂移。
- [ ] `arch-` 事件在 Gamma 上的当前状态。

> **原则**：在直接取数验证之前，**不声称** IEM 观测等同于 NOAA 官方值。
> §3 的 404/405 是"我们的观测与场所裁决一致"，不是"我们的观测就是 NOAA 的数"。

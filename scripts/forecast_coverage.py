#!/usr/bin/env python3
"""Generate research/forecast-coverage.md from the research database.

Answers, from data rather than assertion:
  * how many days of forecast history do we actually have, per model?
  * how many of those were point-in-time eligible?
  * where does coverage end, and why?

Re-runnable: the markdown is regenerated, never hand-edited.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weatherquant.config import Settings
from weatherquant.storage import ResearchDatabase


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="research database path")
    parser.add_argument("--out", default="research/forecast-coverage.md")
    args = parser.parse_args()

    settings = Settings()
    db_path = Path(args.db) if args.db else settings.research_db_path
    out = Path(args.out)

    with ResearchDatabase(db_path) as db:
        db.open()
        con = db.con

        runs = con.execute(
            "SELECT model, status, COUNT(*), MIN(run_time_utc), MAX(run_time_utc) "
            "FROM forecast_runs GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
        bases = con.execute(
            "SELECT publishable_basis, COUNT(*) FROM forecast_runs GROUP BY 1"
        ).fetchall()
        daily = con.execute(
            "SELECT model, COUNT(DISTINCT target_date), MIN(target_date), MAX(target_date), "
            "COUNT(*), SUM(CASE WHEN NOT covers_full_local_day THEN 1 ELSE 0 END) "
            "FROM forecast_daily_max GROUP BY 1 ORDER BY 1"
        ).fetchall()
        pip = con.execute(
            "SELECT model, target_date, "
            "SUM(CASE WHEN eligible THEN 1 ELSE 0 END) AS elig, "
            "SUM(CASE WHEN NOT eligible THEN 1 ELSE 0 END) AS inelig "
            "FROM point_in_time GROUP BY 1, 2"
        ).fetchall()
        pip_by_model = con.execute(
            "SELECT model, SUM(CASE WHEN eligible THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN NOT eligible THEN 1 ELSE 0 END) FROM point_in_time GROUP BY 1"
        ).fetchall()
        reasons = con.execute(
            "SELECT ineligible_reason, COUNT(*) FROM point_in_time "
            "WHERE NOT eligible GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ).fetchall()
        null_hours = con.execute(
            "SELECT model, COUNT(*), SUM(n_missing_hours), MAX(n_missing_hours) "
            "FROM forecast_daily_max WHERE n_missing_hours > 0 GROUP BY 1"
        ).fetchall()
        runs_table = con.execute(
            "SELECT run_id, purpose, status, started_at_utc, finished_at_utc, stats "
            "FROM research_runs ORDER BY started_at_utc DESC LIMIT 5"
        ).fetchall()
        counts = db.table_counts()
        null_basis = con.execute(
            "SELECT COUNT(*) FROM forecast_runs WHERE publishable_at_utc IS NULL"
        ).fetchone()[0]

    # Strict-OOS candidate days: a (model, target_date) with at least one
    # eligible run. Multiple runs for one date are collapsed - the date enters
    # only if *some* run was provably available before the day began.
    eligible_days: dict[str, set] = defaultdict(set)
    all_days: dict[str, set] = defaultdict(set)
    for model, target_date, _e, _i in pip:
        all_days[model].add(target_date)
        if _e:
            eligible_days[model].add(target_date)

    lines: list[str] = []
    lines.append("# 预报历史覆盖度")
    lines.append("")
    lines.append(f"生成时间：{datetime.now(tz=UTC).isoformat(timespec='seconds')}　"
                 f"来源：`{db_path}`（由 `scripts/forecast_coverage.py` 生成，可重跑）")
    lines.append("")
    lines.append("> 本报告只陈述从数据库查得的事实。覆盖不足处如实列出，不作外推。")
    lines.append("")

    lines.append("## 1. 抓取到的 run")
    lines.append("")
    lines.append("| 模型 | 状态 | 数量 | 最早 run | 最晚 run |")
    lines.append("|---|---|---:|---|---|")
    for model, status, n, first, last in runs:
        lines.append(f"| `{model}` | {status} | {n} | {first} | {last} |")
    lines.append("")
    lines.append(f"发布时刻依据：{dict(bases)}　｜　`publishable_at_utc` 为 NULL：**{null_basis}**")
    lines.append("")

    lines.append("## 2. 日最高温聚合（每个 run × 每个目标日）")
    lines.append("")
    lines.append("| 模型 | 覆盖目标日数 | 最早 | 最晚 | 聚合行数 | 覆盖不完整行 |")
    lines.append("|---|---:|---|---|---:|---:|")
    for model, n_days, first, last, n_rows, n_incomplete in daily:
        lines.append(f"| `{model}` | {n_days} | {first} | {last} | {n_rows} | {n_incomplete or 0} |")
    lines.append("")

    if null_hours:
        lines.append("**存在缺测小时的聚合行**（`n_missing_hours > 0`，不做任何插值）：")
        lines.append("")
        lines.append("| 模型 | 行数 | 缺测小时合计 | 单行最大缺测 |")
        lines.append("|---|---:|---:|---:|")
        for model, n, total, worst in null_hours:
            lines.append(f"| `{model}` | {n} | {total} | {worst} |")
        lines.append("")

    lines.append("## 3. 时间点可用性（strict point-in-time）")
    lines.append("")
    lines.append("| 模型 | 合格 (run×日) | 不合格 (run×日) | 合格率 |")
    lines.append("|---|---:|---:|---:|")
    for model, elig, inelig in pip_by_model:
        total = (elig or 0) + (inelig or 0)
        rate = 100.0 * (elig or 0) / total if total else 0.0
        lines.append(f"| `{model}` | {elig} | {inelig} | {rate:.1f}% |")
    lines.append("")

    if reasons:
        lines.append("不合格原因分布：")
        lines.append("")
        for reason, n in reasons:
            lines.append(f"- `{n}` × {reason}")
        lines.append("")

    lines.append("## 4. 可进入严格样本外评估的目标日")
    lines.append("")
    lines.append("口径：某 (模型, 目标日) 只要**存在至少一个** `eligible` 的 run 即计入 ——")
    lines.append("同一目标日的多次运行分别判定，不能互相顶替。")
    lines.append("")
    lines.append("| 模型 | 有聚合的目标日 | 其中可进严格评估 | 不可进 | 覆盖率 |")
    lines.append("|---|---:|---:|---:|---:|")
    for model in sorted(all_days):
        total = len(all_days[model])
        ok = len(eligible_days.get(model, set()))
        lines.append(f"| `{model}` | {total} | {ok} | {total - ok} | {100.0 * ok / total if total else 0:.1f}% |")
    lines.append("")

    lines.append("## 5. 结论（按数据回答）")
    lines.append("")
    for model in sorted(all_days):
        total = len(all_days[model])
        ok = len(eligible_days.get(model, set()))
        lines.append(f"- **{model}**：{ok} / {total} 个目标日可进入严格样本外评估。")
    if not all_days:
        lines.append("- 尚无 `point_in_time` 记录 —— 先运行 `scripts/backfill_forecasts.py`。")
    lines.append("")

    lines.append("## 6. 库内行数")
    lines.append("")
    for name, count in sorted(counts.items()):
        if count:
            lines.append(f"- `{name}`: {count}")
    lines.append("")

    if runs_table:
        lines.append("## 7. 最近的回填运行")
        lines.append("")
        for run_id, purpose, status, started, finished, stats in runs_table:
            lines.append(f"- `{run_id[:8]}` {purpose} **{status}** {started} → {finished}")
            if stats:
                lines.append(f"  - `{stats}`")
        lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

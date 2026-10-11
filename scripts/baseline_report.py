#!/usr/bin/env python3
"""Generate research/baseline-comparison.md from the research database.

Answers the project's central question with numbers rather than assertion:
does a model beat the market on data it could not have seen?

Re-runnable: the markdown is regenerated, never hand-edited.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weatherquant.config import Settings
from weatherquant.models.baselines import (
    load_event_ladders,
    run_model_baselines,
)
from weatherquant.models.market_baseline import run_market_baseline
from weatherquant.storage import ResearchDatabase


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--observation-rule", default="max of all readings")
    parser.add_argument("--out", default="research/baseline-comparison.md")
    args = parser.parse_args()

    settings = Settings()
    db_path = Path(args.db) if args.db else settings.research_db_path
    out = Path(args.out)

    with ResearchDatabase(db_path) as db:
        db.open()
        con = db.con
        ladders = load_event_ladders(con)

        model_results = run_model_baselines(
            con,
            models=[settings.ecmwf_model_id, settings.gfs_model_id],
            observation_rule=args.observation_rule,
            train_fraction=args.train_fraction,
        )
        market_score, market_diag = run_market_baseline(con, ladders=ladders)

        lines: list[str] = []
        lines.append("# 基线对比：模型 vs 市场")
        lines.append("")
        lines.append(
            f"生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}　"
            f"来源：`{db_path}`（由 `scripts/baseline_report.py` 生成，可重跑）"
        )
        lines.append("")
        lines.append(
            f"时间顺序切分：训练 {args.train_fraction:.0%} / 测试 {1 - args.train_fraction:.0%}，"
            "按**目标日期**切分（同一日期的多次运行不跨边）；置信区间按**日期**计算，不按行。"
        )
        lines.append(f"观测口径：`{args.observation_rule}`")
        lines.append("")

        lines.append("## 1. 总表")
        lines.append("")
        lines.append("| 基线 | 测试日期数 | Brier ↓ | LogLoss ↓ | ECE ↓ | 日期级 Brier [95% CI] | MAE (°F) |")
        lines.append("|---|---:|---:|---:|---:|---|---:|")

        rows: list[tuple[str, object]] = []
        for name, run in sorted(model_results.items()):
            s = run.scores.get("brier_and_log_loss")
            rows.append((name, s))
        if market_score is not None:
            rows.append(("market", market_score))

        for name, s in rows:
            if s is None:
                lines.append(f"| `{name}` | — | — | — | — | **未评分** | — |")
                continue
            dl = s.date_level
            ci = (
                f"{_fmt(dl['mean'])} [{_fmt(dl['ci95_low'])}, {_fmt(dl['ci95_high'])}]"
                if dl.get("ci95_low") is not None
                else _fmt(dl.get("mean"))
            )
            lines.append(
                f"| `{name}` | {s.n_dates} | {_fmt(s.brier)} | {_fmt(s.log_loss)} | "
                f"{_fmt(s.ece)} | {ci} | {_fmt(s.mae, 2)} |"
            )
        lines.append("")

        market_row = next((r for r in rows if r[0] == "market"), None)
        market_brier = market_row[1].brier if market_row and market_row[1] else None

        lines.append("## 2. 与市场对照")
        lines.append("")
        if market_brier is None:
            lines.append(
                "> **市场基线未评分** —— `market_price_history` 尚无数据，或在决策时点前无价格。"
                "此时模型之间只能互比，**不能**声称优于市场。"
            )
        else:
            lines.append(f"市场在决策时点的 Brier = **{_fmt(market_brier)}**。越低越好。")
            lines.append("")
            lines.append("| 模型 | Brier | 相对市场 | 结论 |")
            lines.append("|---|---:|---:|---|")
            for name, s in rows:
                if name == "market" or s is None:
                    continue
                delta = s.brier - market_brier
                verdict = "优于市场" if delta < -0.01 else ("劣于市场" if delta > 0.01 else "基本持平")
                lines.append(f"| `{name}` | {_fmt(s.brier)} | {delta:+.4f} | {verdict} |")
            lines.append("")
            lines.append(
                "> **「优于市场」不等于可以交易。** 样本只有一个测试窗口，"
                "市场本身也有噪声；进入策略前还需要样本外的多窗口复核与成本建模。"
            )
        lines.append("")

        lines.append("## 3. 时间点可用性")
        lines.append("")
        lines.append("| 基线 | 训练窗口 | 测试窗口 | 残差 bias (°F) | σ 按 lead (°F) |")
        lines.append("|---|---|---|---:|---|")
        for name, run in sorted(model_results.items()):
            rs = run.residual_summary or {}
            sigma = rs.get("sigma_by_lead") or {}
            sigma_text = ", ".join(f"{k}={v:.2f}" for k, v in sorted(sigma.items())) or "—"
            lines.append(
                f"| `{name}` | {rs.get('train_start')} .. {rs.get('train_end')} | "
                f"{run.test_start} .. {run.test_end} | {_fmt(rs.get('bias_f'), 3)} | {sigma_text} |"
            )
        lines.append("")

        lines.append("## 4. 市场基线诊断")
        lines.append("")
        for key, value in market_diag.items():
            lines.append(f"- `{key}`: {value}")
        lines.append("")

        problems = []
        for name, run in sorted(model_results.items()):
            for problem in run.problems[:3]:
                problems.append(f"- `{name}`: {problem}")
        if problems:
            lines.append("## 5. 数据问题（已记录，未静默丢弃）")
            lines.append("")
            lines.extend(problems[:15])
            lines.append("")

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {out} ({len(lines)} lines)")

        print("\n".join(lines[:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

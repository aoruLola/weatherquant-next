#!/usr/bin/env python3
"""Win-rate acceptance: the denominator, the clusters and the refusals, end to end.

WQN-35's acceptance criteria, exercised against the **production streaming
engine** (the same `build_performance_streaming` the research CLI runs), with
expectations derived from the generator's ground truth rather than from the
module under test:

1.  **Zero valid samples is N/A.** A history whose settlements are all
    pending or unverifiable publishes `win_rate: null` with the contract's
    exact reason - never 0, never a number shaped like a rate.
2.  **Too few samples is still N/A**, with the reason that says what is
    missing ("N decisive, N distinct target date(s); needs 20 and 2").
3.  **After samples accrue, the numbers are accurate**: WIN/LOSS/PENDING and
    the other outcome counts match an independent tally of the ground truth;
    the rate is WIN/(WIN+LOSS) computed on episodes, not observations
    (the generator emits 4-8 repeated evaluations per episode, and the
    acceptance asserts observations never enter the denominator); the
    interval is clustered by target date and wider than the row-level Wilson
    interval on the same data.
4.  **And reproducible**: two independent runs over the same export and
    settlements, same `now`, produce byte-identical `win-rate.json`.

Nothing here touches a production export or database. Usage::

    uv run python scripts/winrate_acceptance.py --out /tmp/wq-winrate-acceptance
    uv run python scripts/winrate_acceptance.py --out /tmp/x --report evidence.json
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weatherquant.performance.streaming import build_performance_streaming  # noqa: E402

T0 = datetime(2026, 10, 13, tzinfo=UTC)
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

INSUFFICIENT_REASON = "N/A — insufficient resolved independent samples"

#: Settlement scenarios cycled per (market, target_date). This table IS the
#: ground truth: every expected count below is derived from it, never from
#: weatherquant.performance.winrate.
SCENARIOS = ("verified_win", "verified_loss", "pending", "disputed", "unverified", "missing")


def scenario_settlement(scenario: str, market_id: str, target_date: str) -> dict | None:
    """The settlement record a scenario publishes, or None for 'missing'."""
    fetched = NOW.isoformat()
    if scenario == "verified_win":
        return {"market_id": market_id, "target_date": target_date, "settlement_status": "RESOLVED",
                "resolved_outcome": 1.0, "verification_status": "VERIFIED", "fetched_at_utc": fetched}
    if scenario == "verified_loss":
        return {"market_id": market_id, "target_date": target_date, "settlement_status": "RESOLVED",
                "resolved_outcome": 0.0, "verification_status": "VERIFIED", "fetched_at_utc": fetched}
    if scenario == "pending":
        return {"market_id": market_id, "target_date": target_date, "settlement_status": "PENDING",
                "resolved_outcome": None, "verification_status": "UNVERIFIED", "fetched_at_utc": fetched}
    if scenario == "disputed":
        return {"market_id": market_id, "target_date": target_date, "settlement_status": "DISPUTED",
                "resolved_outcome": None, "verification_status": "UNVERIFIED", "fetched_at_utc": fetched}
    if scenario == "unverified":
        # Resolved on the platform, but the cross-check against observation
        # never happened - which is exactly what must NOT become a win.
        return {"market_id": market_id, "target_date": target_date, "settlement_status": "RESOLVED",
                "resolved_outcome": 1.0, "verification_status": "UNVERIFIED", "fetched_at_utc": fetched}
    return None


# ---------------------------------------------------------------------------
# synthetic history with exact episode boundaries
# ---------------------------------------------------------------------------


class History:
    """A generated decision history whose episode structure is known exactly.

    One call to :meth:`add_episode` emits ``observations`` ACCEPT rows for one
    five-dimensional key, 60s apart (inside the 900s gap window, so they form
    exactly one episode), and never reuses that key - so the expected episode
    count is the number of calls, by construction.
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.episodes: list[dict] = []  # {"key": (...), "n_observations": int}
        self.clock = T0
        self.seq = 0

    def add_episode(
        self,
        *,
        market_id: str,
        target_date: str,
        direction: str = "buy_yes",
        mode: str = "LIVE",
        model: str | None = "calibrated",
        observations: int | None = None,
    ) -> None:
        observations = observations if observations is not None else 3 + (self.seq % 6)
        for k in range(observations):
            self.seq += 1
            self.rows.append(
                {
                    "decision_id": f"d{self.seq:07d}",
                    "decision_time_utc": (self.clock + timedelta(seconds=60 * k)).isoformat(),
                    "market_id": market_id,
                    "target_date": target_date,
                    "model_name": model,
                    "direction": direction,
                    "mode": mode,
                    "state": "ACCEPT",
                    "net_edge": 0.03 + (self.seq % 17) / 100.0,
                    "market_price": 0.6,
                    "model_probability": 0.65,
                    "available_depth_usd": 500.0,
                    "fee_rate": 0.0,
                    "station": "KLGA",
                    "bracket_label": "81°F",
                    "market_question": "Will it be 81F?",
                }
            )
        self.episodes.append(
            {
                "key": (market_id, target_date, model, direction, mode),
                "market_id": market_id,
                "target_date": target_date,
                "direction": direction,
                "n_observations": observations,
            }
        )
        # Next episode starts 30 minutes later: further apart than the gap
        # window, so no two episodes can ever merge.
        self.clock += timedelta(minutes=30)

    def write_export(self, directory: Path, parts: int = 3) -> Path:
        parts_dir = directory / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        self.rows.sort(key=lambda r: (r["decision_time_utc"], r["decision_id"]))
        size = max(1, (len(self.rows) + parts - 1) // parts)
        for index in range(parts):
            chunk = self.rows[index * size : (index + 1) * size]
            if not chunk:
                continue
            path = parts_dir / f"part-{index + 1:09d}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in chunk), encoding="utf-8")
        return directory

    def settlements(self, *, scenario_for) -> list[dict]:
        out = []
        for episode in self.episodes:
            scenario = scenario_for(episode)
            record = scenario_settlement(scenario, episode["market_id"], episode["target_date"])
            if record is not None:
                out.append(record)
        # One settlement per (market, date): the same pair never carries two.
        seen = set()
        unique = []
        for record in out:
            key = (record["market_id"], record["target_date"])
            if key not in seen:
                seen.add(key)
                unique.append(record)
        return unique


# ---------------------------------------------------------------------------
# the independent tally: outcome per episode, straight from the contract
# ---------------------------------------------------------------------------


def expected_counts(history: History, settlements: list[dict]) -> dict[str, int]:
    """Tally outcomes per EPISODE from ground truth - no winrate.py involved.

    Mirrors the contract's assignment rules (and nothing else): only a
    RESOLVED settlement that also passed verification, with a payout, on a
    buy_yes direction, can be WIN or LOSS. Everything else is counted under
    its own label and stays out of the denominator.
    """
    answers = {
        (s["market_id"], s["target_date"]): s  # type: ignore[index]
        for s in settlements
    }
    tally = {
        "WIN": 0, "LOSS": 0, "PENDING": 0,
        "DISPUTED": 0, "UNVERIFIABLE": 0, "UNAVAILABLE": 0,
    }
    for episode in history.episodes:
        key = (episode["market_id"], episode["target_date"])
        answer = answers.get(key)
        if answer is None:
            tally["UNAVAILABLE"] += 1
            continue
        status = answer["settlement_status"]
        if status == "DISPUTED":
            tally["DISPUTED"] += 1
            continue
        if status != "RESOLVED":
            tally["PENDING"] += 1
            continue
        verified = answer.get("verification_status") == "VERIFIED"
        payout = answer.get("resolved_outcome")
        if not verified or payout not in (0.0, 1.0):
            tally["UNVERIFIABLE"] += 1
            continue
        if episode["direction"] == "sell_yes":
            tally["UNVERIFIABLE"] += 1
            continue
        tally["WIN" if payout == 1.0 else "LOSS"] += 1
    return tally


def _wilson(wins: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """An independent Wilson interval - the row-level counterfactual."""
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(centre - margin, 0.0), min(centre + margin, 1.0))


# ---------------------------------------------------------------------------
# run the production engine
# ---------------------------------------------------------------------------


def run_engine(export: Path, out: Path, state_db: Path, settlements: list[dict]) -> dict:
    build_performance_streaming(
        export=export,
        out_dir=out,
        dataset="fixture",
        state_db=state_db,
        settlements=settlements,
        capital_usdc=100.0,
        now=NOW,
    )
    # The published layout is a generation directory switched in by the
    # current.json pointer - readers see one complete batch or the previous one.
    generation = json.loads((out / "current.json").read_text(encoding="utf-8"))["generation_id"]
    payload = json.loads(
        (out / "generations" / generation / "win-rate.json").read_text(encoding="utf-8")
    )
    return payload


def check(name: str, ok: bool, detail: str, failures: list[str]) -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}: {detail}")
    if not ok:
        failures.append(f"{name}: {detail}")


# ---------------------------------------------------------------------------
# the three acceptance cases
# ---------------------------------------------------------------------------


def case_zero_valid_samples(root: Path, failures: list[str]) -> None:
    """A history whose settlements are all pending or unverifiable: N/A."""
    print("\ncase 1 - zero valid samples must be N/A, not zero")
    history = History()
    dates = [f"2026-10-{13 + i:02d}" for i in range(6)]
    for i in range(24):
        history.add_episode(
            market_id=f"mz-{i}", target_date=dates[i % len(dates)],
            direction="sell_yes" if i % 5 == 0 else "buy_yes",
        )
    settlements = history.settlements(
        scenario_for=lambda ep: "pending" if ep["direction"] == "buy_yes" else "unverified"
    )
    payload = run_engine(
        history.write_export(root / "case1-export"),
        root / "case1-out",
        root / "case1-state.duckdb",
        settlements,
    )
    overall = payload["data"]["overall"]
    expected = expected_counts(history, settlements)
    check(
        "win_rate is null",
        overall["win_rate"] is None,
        f"win_rate={overall['win_rate']!r}",
        failures,
    )
    check(
        "reason is the contract's, verbatim",
        overall["reason"] == INSUFFICIENT_REASON,
        f"reason={overall['reason']!r}",
        failures,
    )
    check(
        "nothing entered the denominator",
        overall["valid_samples"] == 0,
        f"valid_samples={overall['valid_samples']}",
        failures,
    )
    check(
        "pending/unverifiable episodes are still counted",
        overall["counts"].get("PENDING") == expected["PENDING"]
        and overall["counts"].get("UNVERIFIABLE") == expected["UNVERIFIABLE"],
        f"counts={overall['counts']} expected={expected}",
        failures,
    )


def case_insufficient_samples(root: Path, failures: list[str]) -> None:
    """Decisive samples exist but below the threshold: N/A with what's missing."""
    print("\ncase 2 - below the threshold is N/A with the shortfall stated")
    history = History()
    for i in range(6):
        history.add_episode(market_id=f"mi-{i}", target_date=f"2026-10-{13 + (i % 2):02d}")
    settlements = history.settlements(
        scenario_for=lambda ep: "verified_win" if ep["market_id"].endswith(("0", "2", "4")) else "verified_loss"
    )
    payload = run_engine(
        history.write_export(root / "case2-export"),
        root / "case2-out",
        root / "case2-state.duckdb",
        settlements,
    )
    overall = payload["data"]["overall"]
    expected = expected_counts(history, settlements)
    decisive = expected["WIN"] + expected["LOSS"]
    check(
        "win_rate is null below 20 decisive samples",
        overall["win_rate"] is None and decisive < 20,
        f"win_rate={overall['win_rate']!r} decisive={decisive}",
        failures,
    )
    reason = overall["reason"] or ""
    check(
        "reason starts with the contract's text and states the shortfall",
        reason.startswith(INSUFFICIENT_REASON) and str(decisive) in reason and "needs" in reason,
        f"reason={reason!r}",
        failures,
    )
    check(
        "the counts that exist are still accurate",
        overall["counts"].get("WIN") == expected["WIN"]
        and overall["counts"].get("LOSS") == expected["LOSS"],
        f"counts={overall['counts']} expected={expected}",
        failures,
    )


def _full_history() -> History:
    """~96 episodes over 10 dates, mixed models/directions/modes/scenarios.

    Sized so ~2/6 of episodes land on verified settlements and enough of
    those are buy_yes to clear the 20-decisive-sample threshold. Markets are
    reused across different dates (one market, several episodes), and every
    episode carries 3-8 repeated observations, so the observation count is
    several times the episode count by construction.
    """
    history = History()
    dates = [f"2026-10-{13 + i:02d}" for i in range(10)]
    for i in range(96):
        market = f"mf-{i % 36}"  # 36 markets, 96 episodes: some repeat across days
        history.add_episode(
            market_id=market,
            target_date=dates[i % len(dates)],
            direction="sell_yes" if i % 11 == 0 else "buy_yes",
            mode="REPLAY" if i % 9 == 0 else "LIVE",
            model="calibrated" if i % 3 == 0 else ("fusion" if i % 3 == 1 else "gfs_raw"),
        )
    return history


def case_full_samples(root: Path, failures: list[str]) -> dict:
    """The populated case: accuracy, clustering, and byte-level reproducibility."""
    print("\ncase 3 - samples accrued: accurate, clustered, and reproducible")
    history = _full_history()

    # Assign scenarios deterministically per (market, date) pair, in order of
    # first appearance, so both engine runs and the tally see the same truth.
    scenario_map: dict[tuple[str, str], str] = {}
    for episode in history.episodes:
        key = (episode["market_id"], episode["target_date"])
        scenario_map.setdefault(key, SCENARIOS[len(scenario_map) % len(SCENARIOS)])
    settlements = history.settlements(scenario_for=lambda ep: scenario_map[(ep["market_id"], ep["target_date"])])

    export = history.write_export(root / "case3-export")
    payload_a = run_engine(export, root / "case3-out-a", root / "case3-state-a.duckdb", settlements)
    run_engine(export, root / "case3-out-b", root / "case3-state-b.duckdb", settlements)

    overall = payload_a["data"]["overall"]
    expected = expected_counts(history, settlements)

    for outcome in ("WIN", "LOSS", "PENDING", "DISPUTED", "UNVERIFIABLE", "UNAVAILABLE"):
        check(
            f"count[{outcome}] matches the ground truth",
            overall["counts"].get(outcome) == expected[outcome],
            f"published={overall['counts'].get(outcome)} expected={expected[outcome]}",
            failures,
        )

    wins, losses = expected["WIN"], expected["LOSS"]
    decisive = wins + losses
    check(
        "valid_samples counts episodes, not observations",
        overall["valid_samples"] == decisive
        and decisive < sum(e["n_observations"] for e in history.episodes),
        f"valid_samples={overall['valid_samples']} decisive={decisive} "
        f"observations={sum(e['n_observations'] for e in history.episodes)}",
        failures,
    )
    check(
        "rate is WIN/(WIN+LOSS)",
        overall["win_rate"] == wins / decisive,
        f"published={overall['win_rate']} expected={wins / decisive}",
        failures,
    )
    decisive_dates = {e["target_date"] for e in history.episodes
                      if scenario_map[(e["market_id"], e["target_date"])] in ("verified_win", "verified_loss")
                      and e["direction"] != "sell_yes"}
    check(
        "distinct markets and target dates are episode-level truths",
        overall["distinct_target_dates"] == len(decisive_dates),
        f"published={overall['distinct_target_dates']} expected={len(decisive_dates)}",
        failures,
    )
    check(
        "interval is clustered by target date and says so",
        overall["method"] == "cluster_bootstrap_by_target_date"
        and overall["confidence_interval"] is not None,
        f"method={overall['method']} ci={overall['confidence_interval']}",
        failures,
    )
    low, high = overall["confidence_interval"]
    wilson_low, wilson_high = _wilson(wins, decisive)
    check(
        "clustered interval is no narrower than the row-level Wilson one",
        (high - low) >= (wilson_high - wilson_low) - 1e-9
        and 0.0 <= low <= high <= 1.0,
        f"clustered width={high - low:.4f} wilson width={wilson_high - wilson_low:.4f}",
        failures,
    )

    # Per-target-date grouping: the cluster unit is visible and accurate.
    by_date = payload_a["data"]["by_target_date"]
    sample_dates = sorted(decisive_dates)[:3]
    for date in sample_dates:
        expected_for_date = expected_counts(
            _filter_history(history, date), _filter_settlements(settlements, date)
        )
        group = by_date.get(date)
        check(
            f"by_target_date[{date}] matches the ground truth",
            group is not None
            and group["counts"].get("WIN") == expected_for_date["WIN"]
            and group["counts"].get("LOSS") == expected_for_date["LOSS"],
            f"published={group and group['counts']} expected={expected_for_date}",
            failures,
        )

    check(
        "sell_yes episodes are never scored as WIN",
        overall["counts"].get("WIN", 0) <= wins,
        f"published WIN={overall['counts'].get('WIN')} expected={wins}",
        failures,
    )

    check(
        "envelope sample_size is the episode-level denominator",
        payload_a["sample_size"] == decisive,
        f"sample_size={payload_a['sample_size']} decisive={decisive}",
        failures,
    )

    # Reproducibility: same export, same settlements, same now.
    generation_a = json.loads((root / "case3-out-a" / "current.json").read_text(encoding="utf-8"))["generation_id"]
    generation_b = json.loads((root / "case3-out-b" / "current.json").read_text(encoding="utf-8"))["generation_id"]
    bytes_a = (root / "case3-out-a" / "generations" / generation_a / "win-rate.json").read_bytes()
    bytes_b = (root / "case3-out-b" / "generations" / generation_b / "win-rate.json").read_bytes()
    check(
        "two independent runs publish byte-identical win-rate.json",
        bytes_a == bytes_b,
        "payloads identical" if bytes_a == bytes_b else "payloads differ",
        failures,
    )
    return {
        "overall": overall,
        "expected_counts": expected,
        "decisive": decisive,
        "clustered_ci": [low, high],
        "wilson_ci": [wilson_low, wilson_high],
    }


def _filter_history(history: History, date: str) -> History:
    subset = History()
    subset.episodes = [e for e in history.episodes if e["target_date"] == date]
    return subset


def _filter_settlements(settlements: list[dict], date: str) -> list[dict]:
    return [s for s in settlements if s["target_date"] == date]  # type: ignore[union-attr]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/wq-winrate-acceptance"))
    parser.add_argument("--report", type=Path, default=None,
                        help="write machine-readable evidence to this JSON file")
    args = parser.parse_args()

    root = args.out.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    failures: list[str] = []
    print(f"win-rate acceptance over {root}")

    case_zero_valid_samples(root, failures)
    case_insufficient_samples(root, failures)
    full = case_full_samples(root, failures)

    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("RESULT: PASSED")
    print(
        f"  populated case: {full['decisive']} decisive episodes "
        f"({full['expected_counts']['WIN']}W/{full['expected_counts']['LOSS']}L), "
        f"rate={full['overall']['win_rate']:.4f}, "
        f"clustered CI=[{full['clustered_ci'][0]:.4f}, {full['clustered_ci'][1]:.4f}] "
        f"({full['overall']['method']}), reproducible across two runs."
    )
    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "generated_at_utc": datetime.now(tz=UTC).isoformat(),
                    "now_used": NOW.isoformat(),
                    "cases": {
                        "zero_valid": "pass",
                        "insufficient": "pass",
                        "full": {
                            "status": "pass",
                            "overall": full["overall"],
                            "expected_counts": full["expected_counts"],
                            "clustered_ci": full["clustered_ci"],
                            "wilson_ci": full["wilson_ci"],
                        },
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  evidence written to {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

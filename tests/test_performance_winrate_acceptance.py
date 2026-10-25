"""Win-rate acceptance: WQN-35's criteria, driven through the production engine.

The statistical rules already have unit tests (``test_performance_winrate.py``).
This file runs the *acceptance*: the same three cases the acceptance script
walks, against the real streaming engine, with expectations derived from the
generator's ground truth rather than from the module under test. If the
streaming path ever drifts from the contract - scores an unverified market,
counts an observation as a sample, publishes a rate where the contract says
N/A - one of these cases goes red.

The generator lives in ``scripts/winrate_acceptance.py`` (not a package;
loaded by path, like ``test_shadow_freshness.py`` loads its tool).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "winrate_acceptance.py"
_spec = importlib.util.spec_from_file_location("wq_winrate_acceptance", _SCRIPT)
assert _spec and _spec.loader
acceptance = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = acceptance
_spec.loader.exec_module(acceptance)


def test_zero_valid_samples_is_na_and_nothing_else(tmp_path: Path) -> None:
    failures: list[str] = []
    acceptance.case_zero_valid_samples(tmp_path, failures)
    assert failures == [], failures


def test_below_the_threshold_is_na_with_the_shortfall_stated(tmp_path: Path) -> None:
    failures: list[str] = []
    acceptance.case_insufficient_samples(tmp_path, failures)
    assert failures == [], failures


def test_accrued_samples_are_accurate_clustered_and_reproducible(tmp_path: Path) -> None:
    failures: list[str] = []
    full = acceptance.case_full_samples(tmp_path, failures)
    assert failures == [], failures
    # The populated case cleared the threshold: a rate, not a refusal.
    assert full["decisive"] >= 20
    assert full["overall"]["win_rate"] is not None
    assert full["overall"]["method"] == "cluster_bootstrap_by_target_date"


def test_the_tally_counts_episodes_and_respects_sell_yes() -> None:
    """The independent tally: sell_yes on a verified win is never a WIN."""
    history = acceptance.History()
    history.add_episode(market_id="m-a", target_date="2026-10-13", direction="buy_yes")
    history.add_episode(market_id="m-b", target_date="2026-10-13", direction="sell_yes")
    history.add_episode(market_id="m-c", target_date="2026-10-14", direction="buy_yes")
    settlements = history.settlements(
        scenario_for=lambda ep: "verified_win" if ep["market_id"] != "m-c" else "pending"
    )
    counts = acceptance.expected_counts(history, settlements)

    assert counts["WIN"] == 1, "only the verified buy_yes win is scored"
    assert counts["UNVERIFIABLE"] == 1, "sell_yes is counted, never scored"
    assert counts["PENDING"] == 1, "pending is counted, never scored"
    assert counts["LOSS"] == 0

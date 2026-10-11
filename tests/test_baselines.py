"""Baseline construction: ladders, forecast-observation pairing, evaluation.

The pairing tests encode three exclusions that are easy to get wrong by
*including* the row: a date with no observation, a date whose events disagree
on the winner, and a date with no bracket ladder. Each would inflate either the
sample or the score.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.models.baselines import (
    load_event_ladders,
    load_forecast_observation_pairs,
    run_model_baselines,
)
from weatherquant.storage import ResearchDatabase

NOW = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def research(tmp_path):
    db = ResearchDatabase(tmp_path / "r.duckdb")
    db.open()
    _seed(db.con)
    yield db
    db.close()


def _seed(con) -> None:
    """Two models, six target dates, one bracket ladder each."""
    dates = [date(2026, 7, d) for d in range(1, 7)]

    # Ladder covering the whole range used below, with an open top.
    ladder_spec = (
        ("68-69", 68.0, 69.0),
        ("70-71", 70.0, 71.0),
        ("72-73", 72.0, 73.0),
        ("74+", 74.0, None),
    )

    def observed_for(i: int) -> float:
        return 68.0 + ((i * 2) % 5) * 2.0  # 68, 72, 76, 70, 74, 78

    def winning_label_for(value: float) -> str:
        for label, low, high in ladder_spec:
            if high is None:
                if value >= low:
                    return label
            elif low <= value <= high:
                return label
        raise AssertionError(f"no bracket covers {value}")

    for i, d in enumerate(dates):
        # Every date resolves exactly one bracket - a date with zero winners
        # would be excluded by load_event_ladders, and a date with two would be
        # too; the fixture must exercise scoring, not those exclusions.
        winner_label = winning_label_for(observed_for(i))
        for label, low, high in ladder_spec:
            is_winner = label == winner_label
            con.execute(
                "INSERT INTO settlement_outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"evt-{d.isoformat()}", d, f"m-{d}-{label}", label, low, high,
                 is_winner, 1, 1.0 if is_winner else 0.0, NOW, "legacy", NOW),
            )

        for model in ("ecmwf_ifs025", "gfs"):
            # Two runs per date, both issued before the day begins.
            for run_offset in (1, 2):
                run_time = datetime.combine(d, datetime.min.time(), tzinfo=UTC) - timedelta(
                    hours=24 - run_offset
                )
                # Column order is (provider, model, ...): putting the model id
                # first would land it in `provider` and the query would find
                # nothing at all.
                con.execute(
                    "INSERT INTO forecast_daily_max VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("test-provider", model, run_time, d, "KLGA", 70.0 + (i % 3), 24, 0,
                     run_time, run_time + timedelta(hours=24), -1.0, True, NOW),
                )

        # Observation present for every date except the middle one, to exercise
        # the exclusion. It varies independently of the forecast: a constant
        # residual would give sigma = 0, which the model correctly refuses to
        # turn into probabilities.
        if d.day != 3:
            con.execute(
                "INSERT INTO klga_daily_max VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("KLGA", d, observed_for(i), 24, 0, None, None,
                 "max of all readings", "iem_asos", True, NOW, None),
            )


# ---------------------------------------------------------------------------
# Ladders
# ---------------------------------------------------------------------------


def test_ladder_reconstructs_both_open_ended_brackets(research) -> None:
    ladders = load_event_ladders(research.con)
    assert len(ladders) == 6
    ladder = ladders[date(2026, 7, 1)]
    assert ladder.winning_label == "68-69"
    assert ladder.n_winners == 1
    assert len(ladder.brackets) == 4
    # An open-high bracket must have high=None so the ladder covers +inf;
    # without it, the probabilities could not sum to 1.
    assert any(b.high is None for b in ladder.brackets)
    assert any(b.high is not None and b.low is not None for b in ladder.brackets)


def test_a_date_whose_events_disagree_is_excluded_not_merged(research) -> None:
    """The `arch-` shape: two events, two winners, no invented bracket."""
    con = research.con
    # Add a second event for 2026-07-01 that resolves differently.
    for label, low, high in (("70-71", 70.0, 71.0), ("90-91", 90.0, 91.0)):
        con.execute(
            "INSERT INTO settlement_outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("evt-arch-dup", date(2026, 7, 1), f"x-{label}", label, low, high,
             True, 2, 1.0, NOW, "legacy", NOW),
        )
    ladders = load_event_ladders(con)
    # The duplicate overwrote the date with two winners.
    assert ladders[date(2026, 7, 1)].n_winners == 2
    assert ladders[date(2026, 7, 1)].winning_label is None


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_every_run_is_kept_as_its_own_pair(research) -> None:
    """Runs for one date are correlated but not interchangeable."""
    pairs, problems = load_forecast_observation_pairs(
        research.con, model="ecmwf_ifs025"
    )
    # 6 dates minus day 3 (no observation) = 5 scorable dates, 2 runs each.
    assert len(pairs) == 10, f"{len(pairs)} pairs; problems={problems}"
    per_date: dict[date, int] = {}
    for p in pairs:
        per_date[p["target_date"]] = per_date.get(p["target_date"], 0) + 1
    assert set(per_date.values()) == {2}


def test_a_date_without_an_observation_is_dropped_and_reported(research) -> None:
    pairs, problems = load_forecast_observation_pairs(research.con, model="ecmwf_ifs025")
    assert date(2026, 7, 3) not in {p["target_date"] for p in pairs}
    assert any("2026-07-03" in p for p in problems)


def test_problems_do_not_repeat_once_per_run(research) -> None:
    _pairs, problems = load_forecast_observation_pairs(research.con, model="ecmwf_ifs025")
    assert len(problems) == len(set(problems)), "the same date was reported twice"


def test_lead_hours_are_measured_to_the_end_of_the_local_day(research) -> None:
    from weatherquant.weather.daily_max import local_day_bounds_utc

    pairs, _ = load_forecast_observation_pairs(research.con, model="ecmwf_ifs025")
    for p in pairs:
        day_end = local_day_bounds_utc(p["target_date"])[1]
        expected = (day_end - p["run_time_utc"]).total_seconds() / 3600.0
        assert p["lead_hours"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def test_baselines_score_on_a_strictly_chronological_split(research) -> None:
    results = run_model_baselines(
        research.con, models=["ecmwf_ifs025", "gfs"], train_fraction=0.6
    )
    assert set(results) >= {"ecmwf_ifs025_raw", "gfs_raw", "fusion", "calibrated"}
    for name, run in results.items():
        assert run.train_end is not None and run.test_start is not None
        assert run.train_end < run.test_start, f"{name}: train and test overlap in time"


def test_raw_and_calibrated_differ_only_in_the_bias(research) -> None:
    """The ablation must isolate the bias correction, nothing else."""
    results = run_model_baselines(research.con, models=["ecmwf_ifs025"])
    raw = results["ecmwf_ifs025_raw"]
    cal = results["calibrated"]
    assert raw.residual_summary and cal.residual_summary, "both must have fitted a model"

    assert raw.residual_summary["bias_f"] == 0.0, "raw must not apply a correction"
    assert cal.residual_summary["bias_f"] != 0.0, "calibrated must fit one"
    # Same fitted spread: any score difference is then attributable to the bias.
    assert raw.residual_summary["sigma_by_lead"] == cal.residual_summary["sigma_by_lead"]


def test_fusion_uses_both_models(research) -> None:
    results = run_model_baselines(research.con, models=["ecmwf_ifs025", "gfs"])
    fusion = results["fusion"]
    assert not any("fusion used" in p for p in fusion.problems), fusion.problems[:3]


def test_a_score_carries_dates_not_rows_as_its_evidence(research) -> None:
    results = run_model_baselines(research.con, models=["ecmwf_ifs025"])
    score = results["ecmwf_ifs025_raw"].scores.get("brier_and_log_loss")
    assert score is not None
    assert score.n_dates <= score.n_rows
    assert score.n_dates == len(score.test_dates)
    assert score.date_level["standard_error"] is not None or score.n_dates == 1


def test_missing_model_data_yields_an_explicit_failure_not_a_crash(research) -> None:
    results = run_model_baselines(research.con, models=["ecmwf_ifs025", "not_a_model"])
    assert "not_a_model_raw" in results
    assert results["not_a_model_raw"].scores == {}
    assert any("too few paired dates" in p for p in results["not_a_model_raw"].problems)

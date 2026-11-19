"""Synthetic checks for the strict four-model walk-forward comparison."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from weatherquant.models.oos_compare import (
    MODEL_NAMES,
    ScoredForecast,
    filter_point_in_time,
    run_oos_comparison,
    score_aligned_rows,
)
from weatherquant.storage import ResearchDatabase
from weatherquant.storage.db import connect

NOW = datetime(2026, 10, 10, tzinfo=UTC)
DATES = [date(2026, 6, 29) + timedelta(days=offset) for offset in range(10)]
OBSERVED = [66.0, 68.0, 70.0, 72.0, 74.0, 76.0, 69.0, 71.0, 73.0, 75.0]
ECMWF_ERRORS = [-4.0, -2.0, 2.0, 4.0, -3.0, 3.0, -1.0, 1.0, 5.0, -5.0]
GFS_ERRORS = [2.0, 4.0, -2.0, -4.0, 3.0, -3.0, 1.0, -1.0, -5.0, 5.0]
LADDER = (
    ("<68", None, 67.0),
    ("68-69", 68.0, 69.0),
    ("70-71", 70.0, 71.0),
    ("72-73", 72.0, 73.0),
    ("74+", 74.0, None),
)


def _winner(value: float) -> str:
    for label, low, high in LADDER:
        if low is None and value <= high:
            return label
        if high is None and value >= low:
            return label
        if low is not None and high is not None and low <= value <= high:
            return label
    raise AssertionError(value)


def _insert_forecast(con, model: str, target: date, run: datetime, publishable: datetime, point: float) -> None:
    con.execute(
        "INSERT INTO forecast_runs (provider, model, model_version, run_time_utc, publishable_at_utc, "
        "publishable_basis, publishable_lag_hours, publishable_note, fetched_at_utc, http_status, status, "
        "raw_path, raw_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["test", model, "v1", run, publishable, "official", 1.0, "fixture", NOW, 200, "ok", None, "hash"],
    )
    con.execute(
        "INSERT INTO forecast_daily_max VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["test", model, run, target, "KLGA", point, 24, 0, None, None, -1.0, True, NOW],
    )


def _seed(con) -> None:
    for index, (target, observed) in enumerate(zip(DATES, OBSERVED, strict=True)):
        winner = _winner(observed)
        for label, low, high in LADDER:
            con.execute(
                "INSERT INTO settlement_outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    f"event-{target}",
                    target,
                    f"market-{target}-{label}",
                    label,
                    low,
                    high,
                    label == winner,
                    1,
                    1.0 if label == winner else 0.0,
                    NOW,
                    "fixture",
                    NOW,
                ],
            )
        con.execute(
            "INSERT INTO klga_daily_max VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ["KLGA", target, observed, 24, 0, None, None, "max of all readings", "fixture", True, NOW, None],
        )
        run = datetime.combine(target - timedelta(days=2), time.min, tzinfo=UTC)
        for model, errors in (("ecmwf_ifs025", ECMWF_ERRORS), ("gfs_seamless", GFS_ERRORS)):
            _insert_forecast(con, model, target, run, run + timedelta(hours=1), observed + errors[index])

    # Both sources have a run that exists but was not available at the decision cutoff.
    leaky_target = DATES[7]
    leaky_run = datetime.combine(leaky_target, time(hour=5), tzinfo=UTC)
    for model in ("ecmwf_ifs025", "gfs_seamless"):
        _insert_forecast(con, model, leaky_target, leaky_run, leaky_run + timedelta(hours=1), 99.0)


@pytest.fixture
def research_path(tmp_path) -> Path:
    path = tmp_path / "oos.duckdb"
    db = ResearchDatabase(path)
    db.open()
    _seed(db.con)
    db.close()
    return path


def _run(path: Path, **overrides) -> dict:
    options = {
        "min_train_dates": 3,
        "min_samples_per_bucket": 2,
        "bootstrap_samples": 20,
        "seed": 46,
    }
    options.update(overrides)
    con = connect(path, read_only=True)
    try:
        return run_oos_comparison(con, **options)
    finally:
        con.close()


def test_metrics_match_hand_computed_values() -> None:
    rows = [
        ScoredForecast(
            target_date=date(2026, 7, 1),
            run_time_utc=datetime(2026, 6, 30, tzinfo=UTC),
            station="KLGA",
            event_id="one",
            actual_bracket="A",
            probabilities={"A": 0.8, "B": 0.2},
            predicted_max_f=70.0,
            observed_max_f=72.0,
            condition="cold",
        ),
        ScoredForecast(
            target_date=date(2026, 7, 2),
            run_time_utc=datetime(2026, 7, 1, tzinfo=UTC),
            station="KLGA",
            event_id="two",
            actual_bracket="B",
            probabilities={"A": 0.6, "B": 0.4},
            predicted_max_f=74.0,
            observed_max_f=71.0,
            condition="hot",
        ),
    ]
    report = score_aligned_rows({name: rows for name in MODEL_NAMES}, bootstrap_samples=20)
    metrics = report["models"]["ecmwf_raw"]["metrics"]

    assert metrics["brier"]["value"] == pytest.approx(0.4)
    assert metrics["log_loss"]["value"] == pytest.approx((-math.log(0.8) - math.log(0.4)) / 2)
    assert metrics["ece"]["value"] == pytest.approx(0.2)
    assert metrics["mae"]["value"] == pytest.approx(2.5)
    assert metrics["rmse"]["value"] == pytest.approx(math.sqrt(6.5))
    assert metrics["top1_accuracy"]["value"] == pytest.approx(0.5)


def test_late_forecast_is_excluded_and_flagged(research_path) -> None:
    target = date(2026, 7, 2)
    _eligible, excluded = filter_point_in_time(
        [
            {
                "target_date": target,
                "run_time_utc": datetime(2026, 7, 1, 12, tzinfo=UTC),
                "publishable_at_utc": datetime(2026, 7, 2, 12, tzinfo=UTC),
                "publishable_basis": "official",
            }
        ]
    )
    assert len(excluded) == 1
    assert "after the target day began" in excluded[0]["reason"]

    report = _run(research_path)
    for source in ("ecmwf_ifs025", "gfs_seamless"):
        assert any("after the target day began" in item["reason"] for item in report["exclusions"]["sources"][source])
    assert report["models"]["calibrated"]["n_events"] == 7


def test_future_forecast_mutation_does_not_change_an_earlier_fold(research_path) -> None:
    before = _run(research_path)
    first_fold = before["folds"][0]
    test_date = date.fromisoformat(first_fold["test_date"])
    assert date.fromisoformat(first_fold["models"]["calibrated"]["train_end"]) < test_date

    con = connect(research_path)
    try:
        con.execute(
            "UPDATE forecast_daily_max SET predicted_max_f = predicted_max_f + 20 WHERE target_date >= ?",
            [DATES[7]],
        )
    finally:
        con.close()
    after = _run(research_path)

    assert after["folds"][0]["models"]["calibrated"]["bias_f"] == pytest.approx(
        first_fold["models"]["calibrated"]["bias_f"]
    )
    assert after["folds"][0]["models"]["calibrated"]["metrics"] == pytest.approx(
        first_fold["models"]["calibrated"]["metrics"]
    )


def test_bootstrap_is_deterministic_and_breakdowns_report_n(research_path) -> None:
    first = _run(research_path)
    second = _run(research_path)
    assert first == second

    calibrated = first["models"]["calibrated"]
    assert calibrated["n_dates"] == 7
    assert calibrated["n_events"] == 7
    for metric in ("brier", "log_loss", "ece", "mae", "rmse", "top1_accuracy"):
        ci = calibrated["metrics"][metric]["ci95"]
        assert ci is not None and ci[0] <= ci[1]

    month = first["breakdowns"]["by_month"]["2026-07"]["calibrated"]
    assert month["n_dates"] == 7
    assert month["n_events"] == 7
    condition_buckets = first["breakdowns"]["by_observed_temperature_tercile"]
    assert condition_buckets
    for bucket in condition_buckets.values():
        assert bucket["calibrated"]["n_events"] >= 1
        assert "rank" not in bucket["calibrated"]


def test_cli_json_shape_and_database_is_read_only(research_path, capsys) -> None:
    from weatherquant.cli import main

    before = hashlib.sha256(research_path.read_bytes()).hexdigest()
    code = main(
        [
            "--db",
            str(research_path),
            "models",
            "oos-compare",
            "--json",
            "--min-train-dates",
            "3",
            "--min-samples-per-bucket",
            "2",
            "--bootstrap-samples",
            "20",
        ]
    )
    assert code == 0, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["models"]) == set(MODEL_NAMES)
    assert payload["coverage"]["calibrated"]["scored_events"] == 7
    assert payload["paired_differences"]["calibrated"]["brier"]["ci95"] is not None
    assert {item["model"] for item in payload["ranking"]} == set(MODEL_NAMES)
    assert "edge" not in json.dumps(payload).lower()
    assert hashlib.sha256(research_path.read_bytes()).hexdigest() == before
    assert not Path(f"{research_path}.wal").exists()

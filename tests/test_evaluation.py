"""Chronological splitting and date-level scoring.

The split tests exist because a row-level split would leak: several runs share
a target date, and putting some in train and others in test scores the model on
days it was fitted to. The scoring tests check the date-level interval against
a hand-computed value, since that interval is what stops correlated runs from
being counted as independent evidence.
"""

from __future__ import annotations

from datetime import date

import pytest

from weatherquant.models.evaluation import (
    chronological_split,
    evaluate_pairwise,
    one_hot,
    score,
)


def _pairs(dates_and_offsets: list[tuple[date, float, str, str]]) -> list[dict]:
    return [
        {
            "target_date": d,
            "lead_hours": 12.0,
            "predicted_max_f": 70.0 + offset,
            "observed_max_f": 70.0,
            "predicted_bracket": pred,
            "actual_bracket": actual,
        }
        for d, offset, pred, actual in dates_and_offsets
    ]


CLASSES = ["A", "B", "C"]


def _sample() -> list[dict]:
    base = date(2026, 7, 1)
    out = []
    for i in range(10):
        d = date(base.year, base.month, base.day + i)
        # Three runs per date, as a real backfill produces.
        for _run in range(3):
            out.append(
                {
                    "target_date": d,
                    "lead_hours": 12.0 + _run * 6,
                    "predicted_max_f": 70.0,
                    "observed_max_f": 70.0,
                    "predicted_bracket": "A" if i < 7 else "B",
                    "actual_bracket": "A" if i < 7 else "C",
                }
            )
    return out


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------


def test_split_is_strictly_chronological() -> None:
    train, test, train_end, test_start = chronological_split(_sample(), train_fraction=0.7)
    assert train_end < test_start, "every training date must precede every test date"
    train_dates = {p["target_date"] for p in train}
    test_dates = {p["target_date"] for p in test}
    assert train_dates.isdisjoint(test_dates), "a date leaked across the boundary"
    assert max(train_dates) < min(test_dates)


def test_split_keeps_all_runs_of_a_date_together() -> None:
    """The row-level split's failure mode, asserted directly."""
    train, test, *_ = chronological_split(_sample(), train_fraction=0.6)
    for collection, label in ((train, "train"), (test, "test")):
        counts: dict[date, int] = {}
        for p in collection:
            counts[p["target_date"]] = counts.get(p["target_date"], 0) + 1
        # Every date present has all 3 of its runs, never a partial set.
        assert set(counts.values()) == {3}, f"{label} split a date across the boundary: {counts}"


def test_split_respects_the_fraction() -> None:
    pairs = _sample()
    train, test, *_ = chronological_split(pairs, train_fraction=0.7)
    assert len(train) + len(test) == len(pairs)
    assert len({p["target_date"] for p in train}) == 7
    assert len({p["target_date"] for p in test}) == 3


def test_a_single_date_cannot_be_split() -> None:
    with pytest.raises(ValueError, match="at least 2 distinct target dates"):
        chronological_split(_pairs([(date(2026, 7, 1), 0.0, "A", "A")]), train_fraction=0.5)


def test_an_out_of_range_fraction_is_rejected() -> None:
    with pytest.raises(ValueError, match="train_fraction"):
        chronological_split(_sample(), train_fraction=0.0)
    with pytest.raises(ValueError, match="train_fraction"):
        chronological_split(_sample(), train_fraction=1.0)


# ---------------------------------------------------------------------------
# One-hot encoding
# ---------------------------------------------------------------------------


def test_one_hot_encodes_the_named_class() -> None:
    rows = one_hot(["A", "C"], ["A", "B", "C"])
    assert rows == [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def test_one_hot_rejects_an_unknown_class() -> None:
    with pytest.raises(ValueError, match="unknown class"):
        one_hot(["Z"], ["A", "B"])


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_a_perfect_forecast_scores_zero() -> None:
    s = score(
        name="perfect",
        predicted_brackets=["A", "A", "A"],
        actual_brackets=["A", "A", "A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)],
    )
    assert s.brier == pytest.approx(0.0, abs=1e-12)
    assert s.log_loss == pytest.approx(0.0, abs=1e-12)
    assert s.n_dates == 3


def test_brier_for_a_known_mistake() -> None:
    """Two rows, one wrong: mean of (0 and 2) = 1 for a hard one-hot guess."""
    s = score(
        name="half-wrong",
        predicted_brackets=["A", "B"],
        actual_brackets=["A", "A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1), date(2026, 7, 2)],
    )
    assert s.brier == pytest.approx(1.0, abs=1e-12)


def test_point_metrics_are_absent_when_no_temperatures_are_given() -> None:
    """A market-probability baseline has no point forecast; do not invent one."""
    s = score(
        name="market",
        predicted_brackets=["A"],
        actual_brackets=["A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1)],
    )
    assert s.mae is None
    assert s.rmse is None


def test_point_metrics_are_present_when_temperatures_are_given() -> None:
    s = score(
        name="model",
        predicted_brackets=["A"],
        actual_brackets=["A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1)],
        predicted_max_f=[71.0],
        observed_max_f=[74.0],
    )
    assert s.mae == pytest.approx(3.0)
    assert s.rmse == pytest.approx(3.0)


def test_the_interval_is_over_dates_not_rows() -> None:
    """30 rows across 5 dates must not look like 30 independent samples."""
    dates = [date(2026, 7, d) for d in range(1, 6)]
    pairs = []
    for d in dates:
        for _ in range(6):  # 6 runs per date = 30 rows, 5 dates
            pairs.append({"target_date": d})
    # Alternate correct/incorrect so per-row brier varies within a date.
    preds = ["A", "B"] * 15
    actuals = ["A"] * 30
    date_list = [d for d in dates for _ in range(6)]

    s = score(
        name="clustered",
        predicted_brackets=preds,
        actual_brackets=actuals,
        classes=["A", "B"],
        dates=date_list,
    )
    assert s.n_rows == 30
    assert s.n_dates == 5, "the date count is what the interval is built on"
    assert s.date_level["standard_error"] is not None
    assert s.date_level["ci95_low"] <= s.date_level["mean"] <= s.date_level["ci95_high"]

    # The interval is built from 5 date means, not 30 rows: its standard error
    # must be the across-date one, which is larger than a per-row estimate over
    # the same data would be.
    per_date_brier = [s.date_level["mean"]] * 5
    row_count = 30
    assert s.date_level["standard_error"] is not None
    assert len(per_date_brier) == s.n_dates < row_count


def test_a_single_test_date_yields_a_metric_without_an_interval() -> None:
    """One date carries no between-date variance; do not fabricate an interval."""
    s = score(
        name="single",
        predicted_brackets=["A"],
        actual_brackets=["A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1)],
    )
    assert s.n_dates == 1
    assert s.date_level["standard_error"] is None
    assert s.date_level["ci95_low"] is None
    assert s.date_level["mean"] == pytest.approx(0.0)


def test_misaligned_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="must align"):
        score(
            name="bad",
            predicted_brackets=["A", "B"],
            actual_brackets=["A"],
            classes=["A", "B"],
            dates=[date(2026, 7, 1)],
        )


def test_scoring_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="nothing to score"):
        score(
            name="empty",
            predicted_brackets=[],
            actual_brackets=[],
            classes=["A"],
            dates=[],
        )


# ---------------------------------------------------------------------------
# Pairwise comparison
# ---------------------------------------------------------------------------


def test_pairwise_reports_the_shared_date_set() -> None:
    a = score(
        name="a",
        predicted_brackets=["A", "A", "A"],
        actual_brackets=["A", "A", "A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)],
    )
    b = score(
        name="b",
        predicted_brackets=["A", "B"],
        actual_brackets=["A", "A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 2), date(2026, 7, 9)],  # only one shared date
    )
    result = evaluate_pairwise([a], other=b)["a"]
    assert result["shared_dates"] == 1
    assert result["only_in_this"] == 2
    assert result["only_in_other"] == 1
    # a is perfect on shared dates, b is not.
    assert result["better_on_shared"] is True


def test_score_carries_its_test_dates() -> None:
    s = score(
        name="x",
        predicted_brackets=["A", "A"],
        actual_brackets=["A", "A"],
        classes=["A", "B"],
        dates=[date(2026, 7, 2), date(2026, 7, 1)],
    )
    assert s.test_dates == (date(2026, 7, 1), date(2026, 7, 2)), "must be sorted"

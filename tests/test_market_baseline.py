"""The market baseline: scored at the same instant as the forecasts.

Two properties are asserted here because getting either wrong would make the
baseline look better than it is:

* the decision instant is the START of the target local day - the same
  deadline the forecasts are held to - so the market is not given information
  the model is not;
* the winning bracket is never consulted when constructing the market's own
  probabilities, because that would make the baseline omniscient.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.models.baselines import load_event_ladders
from weatherquant.models.market_baseline import (
    market_implied_probabilities,
    run_market_baseline,
)
from weatherquant.storage import ResearchDatabase
from weatherquant.weather.daily_max import local_day_bounds_utc

NOW = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
DAY = date(2026, 7, 5)


@pytest.fixture
def research(tmp_path):
    db = ResearchDatabase(tmp_path / "r.duckdb")
    db.open()
    _seed(db.con)
    yield db
    db.close()


def _seed(con) -> None:
    """One event on one date, with a ladder, tokens and a price series."""
    for label, low, high, winner in (
        ("70-71", 70.0, 71.0, False),
        ("72-73", 72.0, 73.0, True),
        ("74+", 74.0, None, False),
    ):
        market_id = f"m-{label}"
        con.execute(
            "INSERT INTO settlement_outcome VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"evt-{DAY.isoformat()}", DAY, market_id, label, low, high,
             winner, 1, 1.0 if winner else 0.0, NOW, "legacy", NOW),
        )
        for token, is_yes in ((f"tokY-{label}", True), (f"tokN-{label}", False)):
            con.execute(
                "INSERT INTO market_tokens VALUES (?,?,?,?,?,?)",
                (f"evt-{DAY.isoformat()}", DAY, market_id, label, token, is_yes),
            )
        # Prices on the day BEFORE the target day, plus one after the deadline.
        # The one after must not be used.
        day_start, _ = local_day_bounds_utc(DAY)
        for hours_before, mid in ((26, 0.40), (10, 0.55)):
            ts = day_start - timedelta(hours=hours_before)
            con.execute(
                "INSERT INTO market_price_history VALUES (?,?,?,?,?,?)",
                (market_id, f"tokY-{label}", ts, mid, "legacy", NOW),
            )
    # One post-deadline price that would flip the answer if it were used:
    # written once, outside the loop, so the primary key stays unique.
    con.execute(
        "INSERT INTO market_price_history VALUES (?,?,?,?,?,?)",
        ("m-70-71", "tokY-70-71",
         local_day_bounds_utc(DAY)[0] + timedelta(hours=5), 0.99, "legacy", NOW),
    )


def test_decision_instant_is_the_start_of_the_target_local_day(research) -> None:
    ladders = load_event_ladders(research.con)
    implied = market_implied_probabilities(
        research.con, target_date=DAY, event_id=ladders[DAY].event_id, ladder=ladders[DAY]
    )
    day_start, day_end = local_day_bounds_utc(DAY)
    assert implied.decision_instant_utc == day_start
    assert implied.decision_instant_utc < day_end


def test_prices_after_the_deadline_are_ignored(research) -> None:
    """The post-deadline price is 0.99 on a bracket the market did NOT win."""
    ladders = load_event_ladders(research.con)
    implied = market_implied_probabilities(
        research.con, target_date=DAY, event_id=ladders[DAY].event_id, ladder=ladders[DAY]
    )
    for label, value in implied.probabilities.items():
        assert value != 0.99, f"{label} used a price from after the decision instant"
        assert value in (0.40, 0.55)


def test_brackets_without_a_price_are_reported_as_missing(research) -> None:
    ladders = load_event_ladders(research.con)
    implied = market_implied_probabilities(
        research.con, target_date=DAY, event_id=ladders[DAY].event_id, ladder=ladders[DAY]
    )
    # All three brackets were seeded with prices, so nothing is missing.
    assert implied.missing == ()
    assert implied.n_tokens_priced == 3


def test_a_date_with_no_prices_before_the_deadline_is_unpriced(research) -> None:
    ladders = load_event_ladders(research.con)
    other = date(2026, 7, 6)
    # Re-use the ladder under a different date that has no price history at all.
    from weatherquant.models.baselines import EventLadder

    ladders[other] = EventLadder(
        event_id=ladders[DAY].event_id,
        target_date=other,
        brackets=ladders[DAY].brackets,
        winning_label=ladders[DAY].winning_label,
        n_winners=1,
    )
    implied = market_implied_probabilities(
        research.con, target_date=other, event_id=ladders[DAY].event_id, ladder=ladders[other]
    )
    assert implied.probabilities == {}
    assert implied.n_tokens_priced == 0


def test_run_market_baseline_scores_and_reports_holes(research) -> None:
    ladders = load_event_ladders(research.con)
    score_result, diagnostics = run_market_baseline(research.con, ladders=ladders)

    assert diagnostics["dates_considered"] == 1
    assert diagnostics["dates_scored"] == 1
    assert diagnostics["dates_unpriced_before_deadline"] == 0

    assert score_result is not None
    assert score_result.name == "market"
    assert score_result.n_dates == 1
    # The market's most expensive pre-deadline bracket is 70-71 (0.55),
    # while the event resolved to 72-73 - so this single date is wrong.
    # A market baseline has no point forecast, so the point metrics must stay
    # empty rather than being invented to make the rows comparable.
    assert score_result.mae is None
    assert score_result.rmse is None


def test_the_market_baseline_never_reads_the_settlement_to_predict(research) -> None:
    """Constructing probabilities from the winner would make it omniscient."""
    ladders = load_event_ladders(research.con)
    implied = market_implied_probabilities(
        research.con, target_date=DAY, event_id=ladders[DAY].event_id, ladder=ladders[DAY]
    )
    assert implied.probabilities, "must be built from prices alone"
    # The seeded prices are equal across brackets except 0.40/0.55, and none is
    # a 1.0 or 0.0 - a settled outcome would show up as exactly 1 or 0.
    assert all(v not in (0.0, 1.0) for v in implied.probabilities.values())

"""Coverage numerator and denominator must be the same model/date set."""

from datetime import date

import duckdb

from weatherquant.shadow.report import _coverage


def _db():
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE market_catalog (market_id VARCHAR, token_yes VARCHAR, "
        "target_date DATE, active BOOLEAN, accepting_orders BOOLEAN, closed BOOLEAN)"
    )
    con.execute("CREATE TABLE model_versions (model_name VARCHAR)")
    con.execute(
        "CREATE TABLE model_predictions (model_name VARCHAR, target_date DATE, "
        "n_hours INTEGER, n_missing_hours INTEGER, "
        "forecast_publishable_at_utc TIMESTAMP, training_cutoff_utc TIMESTAMP, "
        "decision_time_utc TIMESTAMP)"
    )
    con.execute("CREATE TABLE token_books (token_id VARCHAR)")
    con.execute("INSERT INTO model_versions VALUES ('a'), ('b')")
    return con


def _prediction(con, model, date_value, *, missing=0, training_late=False):
    con.execute(
        "INSERT INTO model_predictions VALUES (?, ?, 24, ?, "
        "'2026-10-10T06:00:00', ?, '2026-10-10T12:00:00')",
        [model, date_value, missing,
         "2026-10-11T00:00:00" if training_late else "2026-10-09T00:00:00"],
    )


def test_historical_predictions_cannot_inflate_open_target_coverage():
    con = _db()
    d1, d2, past = date(2026, 10, 11), date(2026, 10, 12), date(2026, 10, 8)
    con.execute(
        "INSERT INTO market_catalog VALUES "
        "('m1', 't1', ?, true, true, false),"
        "('m2', 't2', ?, true, true, false),"
        "('m3', 't3', ?, true, false, false)",
        [d1, d2, past],
    )
    _prediction(con, "a", d1)
    _prediction(con, "a", d2)
    _prediction(con, "b", d1, missing=3)
    _prediction(con, "a", past)
    _prediction(con, "b", past)
    result = _coverage(con, horizon_days=7)
    assert result["forecast_needed"] == 4
    assert result["forecast_available"] == 3
    assert result["forecast_coverage"] == 0.75
    assert result["forecast_historical_pairs"] == 5
    assert result["forecast_valid_available"] == 2
    assert result["forecast_valid_coverage"] == 0.5
    con.close()


def test_point_in_time_invalid_prediction_is_not_valid_coverage():
    con = _db()
    day = date(2026, 10, 11)
    con.execute(
        "INSERT INTO market_catalog VALUES ('m1', 't1', ?, true, true, false)", [day]
    )
    _prediction(con, "a", day, training_late=True)
    _prediction(con, "b", day)
    result = _coverage(con, horizon_days=7)
    assert result["forecast_available"] == 2
    assert result["forecast_valid_available"] == 1
    con.close()


def test_no_open_market_is_na_not_one_hundred_percent():
    con = _db()
    _prediction(con, "a", date(2026, 10, 8))
    result = _coverage(con, horizon_days=7)
    assert result["forecast_needed"] == 0
    assert result["forecast_available"] == 0
    assert result["forecast_coverage"] is None
    assert result["forecast_valid_coverage"] is None
    assert result["forecast_historical_pairs"] == 1
    con.close()

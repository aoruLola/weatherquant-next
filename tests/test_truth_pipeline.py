"""The settlement-truth pipeline: extract -> cross-check -> report.

Runs entirely offline against a synthetic legacy database whose shape matches
the real one, so the pipeline is testable without touching production data.

Two cases exist because of findings made against the real legacy database:

* ``settlements.daily_max_temp_f`` there is a copy of our own observation, so
  the pipeline must cross-check against the *winning bracket* instead;
* one date can carry two events (an ``arch-`` duplicate), so winners are counted
  per event and never per date.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from weatherquant.cli import main as cli_main
from weatherquant.storage import ResearchDatabase
from weatherquant.truth.crosscheck import run_crosscheck
from weatherquant.truth.legacy_extract import extract_from_legacy

NOW = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)

WUNDERGROUND_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature "
    "recorded at the LaGuardia Airport Station in degrees Fahrenheit on 19 May '26.\n\n"
    "The resolution source for this market will be information from Wunderground, specifically "
    "the highest temperature recorded for all times on this day for the LaGuardia Airport "
    "Station, available here: "
    "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA.\n\n"
    "This market can not resolve until the first data point for the following date has been "
    "published on the resolution source.\n\n"
    "The resolution source for this market measures temperatures to whole degrees Fahrenheit "
    "(eg, 21°F). Thus, this is the level of precision that will be used when resolving "
    "the market."
)

NOAA_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature "
    "recorded by NOAA at the LaGuardia Airport Station in degrees Fahrenheit on 10 Oct '26.\n\n"
    'The resolution source for this market will be information from NOAA, specifically the '
    'highest reading under the "Temp" column for all times on this day, available here: '
    "https://www.weather.gov/wrh/timeseries?site=klga\n\n"
    "In the event that there is no data for the observation date by 11:59 PM ET on the day "
    "following the observation date, this market will resolve to the lowest bracket.\n\n"
    "This market will resolve once the first data point for the following date has been "
    "published on the resolution source, or by 11:59 PM ET on the day following the observation "
    "date, whichever comes first.\n\n"
    "The resolution source for this market measures temperatures to whole degrees Fahrenheit "
    "(eg, 21°F). Thus, this is the level of precision that will be used when resolving the market."
)

_LEGACY_DDL = """
CREATE TABLE events (
    event_id VARCHAR, slug VARCHAR, title VARCHAR, event_date DATE,
    resolution_source VARCHAR, rules_text VARCHAR, fetched_at TIMESTAMPTZ, raw_json VARCHAR
);
CREATE TABLE markets (
    market_id VARCHAR, event_id VARCHAR, bracket_label VARCHAR,
    bracket_low DOUBLE, bracket_high DOUBLE, target_date DATE,
    parse_status VARCHAR, active BOOLEAN, closed BOOLEAN,
    token_yes VARCHAR, token_no VARCHAR
);
CREATE TABLE settlements (
    market_id VARCHAR, event_id VARCHAR, yes_price DOUBLE,
    resolved_at TIMESTAMPTZ, is_official BOOLEAN, daily_max_temp_f DOUBLE
);
CREATE TABLE daily_observations (
    station VARCHAR, target_date DATE, max_temp_f DOUBLE,
    n_readings INTEGER, rule VARCHAR, source VARCHAR,
    computed_at TIMESTAMPTZ, is_official BOOLEAN
);
CREATE TABLE price_history (
    market_id VARCHAR, token_id VARCHAR, ts TIMESTAMPTZ,
    mid DOUBLE, fetched_at TIMESTAMPTZ
);
-- The old system's conclusions. They must be *present* for the test to prove
-- they are skipped, not merely absent from the source.
CREATE TABLE signals (signal_id VARCHAR, decision VARCHAR);
CREATE TABLE orders (order_id VARCHAR, status VARCHAR);
CREATE TABLE account (account_id VARCHAR, cash DOUBLE);
CREATE TABLE predictions (prediction_id VARCHAR, probs VARCHAR);
CREATE TABLE model_runs (model_version VARCHAR, status VARCHAR);
"""

_DAY1 = date(2026, 6, 14)
_DAY2 = date(2026, 5, 19)


def _build_legacy(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(_LEGACY_DDL)

    con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", [
        "evt-noaa", "highest-temperature-in-nyc", "Highest temperature in NYC",
        _DAY1, "https://www.weather.gov/wrh/timeseries?site=klga", NOAA_RULES, NOW, "{}",
    ])
    # Two events sharing one target date - the `arch-` shape seen in production.
    for event_id, slug, winner in (
        ("evt-arch", "arch-highest-temperature-in-nyc-on-may-19-2026", "90-91°F"),
        ("evt-main", "highest-temperature-in-nyc-on-may-19-2026", "95°F or below"),
    ):
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", [
            event_id, slug, "Highest temperature in NYC on May 19?", _DAY2,
            "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
            WUNDERGROUND_RULES, NOW, "{}",
        ])
        # Both events share a bracket ladder; only which one *wins* differs.
        # evt-arch resolves 90-91 while the observation is 95 - the production
        # `arch-` duplicate, which is exactly the disagreement under test.
        brackets = [
            ("81°F or below", None, 81.0),
            ("90-91°F", 90.0, 91.0),
            ("95°F or below", None, 95.0),
            ("96-97°F", 96.0, 97.0),
        ]
        winning = winner
        for label, low, high in brackets:
            market_id = f"{event_id}-{label}"
            con.execute("INSERT INTO markets (market_id, event_id, bracket_label, bracket_low, bracket_high, target_date, parse_status, active, closed, token_yes, token_no) VALUES (?,?,?,?,?,?,?,True,False,?,?)", [
                market_id, event_id, label, low, high, _DAY2, "ok",
                f"tokY-{event_id}-{label}", f"tokN-{event_id}-{label}",
            ])
            con.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?)", [
                market_id, event_id, 1.0 if label == winning else 0.0, NOW, True, 95.0,
            ])

    # Markets for evt-noaa: winner 90-91, observation will be 91 (match).
    for label, low, high in [("90-91°F", 90.0, 91.0), ("92-93°F", 92.0, 93.0)]:
        market_id = f"evt-noaa-{label}"
        con.execute("INSERT INTO markets (market_id, event_id, bracket_label, bracket_low, bracket_high, target_date, parse_status, active, closed, token_yes, token_no) VALUES (?,?,?,?,?,?,?,True,False,?,?)", [
            market_id, "evt-noaa", label, low, high, _DAY1, "ok",
            f"tokY-{label}", f"tokN-{label}",
        ])
        con.execute("INSERT INTO settlements VALUES (?,?,?,?,?,?)", [
            market_id, "evt-noaa", 1.0 if label == "90-91°F" else 0.0, NOW, True, 91.0,
        ])

    for market_id in ("evt-noaa-90-91°F", "evt-arch-90-91°F", "evt-main-95°F or below"):
        for hours in (1, 2):
            con.execute("INSERT INTO price_history VALUES (?,?,?,?,?)", [
                market_id, f"tok-{hours}", datetime(2026, 9, 9, hours, 0, tzinfo=UTC),
                0.5, NOW,
            ])

    for day, all_readings, hourly in ((_DAY1, 91.0, 91.0), (_DAY2, 95.0, 95.0)):
        con.execute("INSERT INTO daily_observations VALUES (?,?,?,?,?,?,?,False)", [
            "KLGA", day, all_readings, 30, "all_readings", "iem_asos", NOW,
        ])
        con.execute("INSERT INTO daily_observations VALUES (?,?,?,?,?,?,?,False)", [
            "KLGA", day, hourly, 24, "hourly_metar", "iem_asos", NOW,
        ])
    con.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.duckdb"
    _build_legacy(path)
    return path


@pytest.fixture
def research(legacy_db: Path, tmp_path: Path):
    db = ResearchDatabase(tmp_path / "research.duckdb")
    db.open()
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extracts_rules_outcomes_and_observations(research, legacy_db: Path) -> None:
    stats = extract_from_legacy(research, legacy_db)
    assert stats.events == 3
    assert stats.events_rules_parsed == 3, "the NOAA rules must parse"
    assert stats.settlement_outcomes > 0
    assert stats.daily_maxima == 4, "two dates x two aggregation rules"

    counts = research.table_counts()
    assert counts["settlement_rules"] == 3
    assert counts["settlement_outcome"] == stats.settlement_outcomes
    assert counts["klga_daily_max"] == 4


def test_rules_are_parsed_structurally(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    row = research.con.execute(
        "SELECT station, primary_source_name, unit, precision_deg, rounding, timezone_hint, "
        "no_data_resolution, parse_status FROM settlement_rules WHERE event_id='evt-noaa'"
    ).fetchone()
    assert row[0] == "KLGA"
    assert row[1] == "NOAA"
    assert row[2] == "degF"
    assert row[3] == 1.0
    assert row[4] == "nearest_integer_f"
    assert row[5] == "11:59 PM ET"
    assert row[6] is not None and "lowest bracket" in row[6].lower()
    assert row[7] == "parsed"


def test_two_events_on_one_date_produce_two_rows_with_one_winner_each(
    research, legacy_db: Path
) -> None:
    """The `arch-` shape: winners are counted per event, never per date."""
    extract_from_legacy(research, legacy_db)
    rows = research.con.execute(
        "SELECT event_id, SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) AS winners, "
        "MAX(n_winners_in_event) AS declared "
        "FROM settlement_outcome WHERE target_date = ? GROUP BY 1 ORDER BY 1",
        [_DAY2],
    ).fetchall()
    assert len(rows) == 2, "two events sharing a date must stay two events"
    for event_id, winners, declared in rows:
        assert winners == 1, f"{event_id} must have exactly one winner"
        assert declared == 1

    # ...and they resolved to different brackets.
    labels = dict(
        research.con.execute(
            "SELECT event_id, bracket_label FROM settlement_outcome WHERE is_winner"
        ).fetchall()
    )
    assert labels["evt-arch"] == "90-91°F"
    assert labels["evt-main"] == "95°F or below"


def test_both_aggregation_rules_are_kept_separately(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    rules = {
        r[0]
        for r in research.con.execute("SELECT DISTINCT aggregation_rule FROM klga_daily_max").fetchall()
    }
    assert rules == {"max of all readings", "max of routine hourly METAR"}


def test_a_day_with_more_than_24_readings_is_not_flagged_incomplete(
    research, legacy_db: Path
) -> None:
    """`all_readings` legitimately exceeds 24/hour-day; a count is not a coverage check."""
    extract_from_legacy(research, legacy_db)
    row = research.con.execute(
        "SELECT n_readings, is_complete, n_missing_hours FROM klga_daily_max "
        "WHERE aggregation_rule='max of all readings' AND target_date=?", [_DAY1]
    ).fetchone()
    assert row[0] == 30, "several METARs per hour is normal"
    assert row[1] is True
    assert row[2] == -1, "hour coverage is unknown from this source, not zero"


def test_extraction_is_idempotent(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    first = research.table_counts()
    extract_from_legacy(research, legacy_db)
    assert research.table_counts() == first, "a re-run must not duplicate rows"


def test_extraction_never_writes_to_the_legacy_file(research, legacy_db: Path) -> None:
    before = legacy_db.stat().st_size, legacy_db.stat().st_mtime_ns
    extract_from_legacy(research, legacy_db)
    after = legacy_db.stat().st_size, legacy_db.stat().st_mtime_ns
    assert before == after


def test_conclusion_tables_are_not_extracted(research, legacy_db: Path) -> None:
    """The old system's answers must not arrive as if they were evidence."""
    stats = extract_from_legacy(research, legacy_db)
    for table in ("signals", "predictions", "model_runs", "orders", "account"):
        assert table in stats.skipped_tables, table
    tables = set(research.table_counts())
    assert "signals" not in tables and "orders" not in tables


# ---------------------------------------------------------------------------
# Cross-check
# ---------------------------------------------------------------------------


def test_crosscheck_separates_the_two_aggregation_rules(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    summary = run_crosscheck(research)

    assert set(summary) == {"max of all readings", "max of routine hourly METAR"}
    all_readings = summary["max of all readings"]
    # evt-noaa (obs 91, winner 90-91) and evt-main (obs 95, winner <=95) agree;
    # evt-arch resolves 90-91 while the observation is 95 - the production
    # `arch-` duplicate, and the reason the two rules are reported separately.
    assert all_readings["total"] == 3
    assert all_readings["match"] == 2
    assert all_readings["mismatch"] == 1

    hourly = summary["max of routine hourly METAR"]
    assert hourly["total"] == 3


def test_a_mismatch_is_recorded_with_its_detail(research, legacy_db: Path) -> None:
    """Move the observation outside the winning bracket and the row must say so."""
    extract_from_legacy(research, legacy_db)
    con = research.con
    con.execute(
        "UPDATE klga_daily_max SET max_temp_f = 80.0 WHERE target_date = ? "
        "AND aggregation_rule = 'max of all readings'",
        [_DAY1],
    )
    summary = run_crosscheck(research)
    # evt-arch already mismatched; moving evt-noaa's observation out of its
    # winning bracket makes a second one.
    assert summary["max of all readings"]["mismatch"] == 2

    row = con.execute(
        "SELECT observed_max_f, winning_bracket_label, detail FROM settlement_crosscheck "
        "WHERE verdict='mismatch' AND aggregation_rule='max of all readings' "
        "AND target_date = ?", [_DAY1]
    ).fetchone()
    assert row[0] == 80.0
    assert row[1] == "90-91°F"
    assert "outside the winning bracket" in row[2]
    # The observation is left as it is - a mismatch is evidence, not an error.
    assert con.execute(
        "SELECT max_temp_f FROM klga_daily_max WHERE target_date = ?", [_DAY1]
    ).fetchone()[0] == 80.0


def test_crosscheck_is_idempotent(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    run_crosscheck(research)
    first = research.table_counts()["settlement_crosscheck"]
    run_crosscheck(research)
    assert research.table_counts()["settlement_crosscheck"] == first


def test_crosscheck_results_keep_provenance(research, legacy_db: Path) -> None:
    extract_from_legacy(research, legacy_db)
    run_crosscheck(research)
    row = research.con.execute(
        "SELECT station, observation_source, aggregation_rule, checked_at_utc "
        "FROM settlement_crosscheck LIMIT 1"
    ).fetchone()
    assert row[0] == "KLGA"
    assert row[1].startswith("legacy_")
    assert row[2]
    assert row[3] is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_pipeline_runs_end_to_end(
    legacy_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "cli-research.duckdb"
    env_db, env_legacy = str(db_path), str(legacy_db)
    import os

    saved = {k: os.environ.get(k) for k in ("WQ_RESEARCH_DB_PATH", "WQ_LEGACY_DB_PATH")}
    try:
        os.environ["WQ_RESEARCH_DB_PATH"] = env_db
        os.environ["WQ_LEGACY_DB_PATH"] = env_legacy
        assert cli_main(["init"]) == 0
        assert cli_main(["extract-legacy"]) == 0
        assert cli_main(["crosscheck"]) == 0
        assert cli_main(["report"]) == 0
        assert cli_main(["rules", "--json"]) == 0
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    out = capsys.readouterr().out
    assert "settlement cross-check" in out
    assert "405" in out or "3" in out
    assert "research database" in out


def test_cli_report_json_is_parseable(
    legacy_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    db_path = tmp_path / "cli-json.duckdb"
    saved = {k: os.environ.get(k) for k in ("WQ_RESEARCH_DB_PATH", "WQ_LEGACY_DB_PATH")}
    try:
        os.environ["WQ_RESEARCH_DB_PATH"] = str(db_path)
        os.environ["WQ_LEGACY_DB_PATH"] = str(legacy_db)
        assert cli_main(["init"]) == 0
        assert cli_main(["extract-legacy"]) == 0
        assert cli_main(["crosscheck"]) == 0
        capsys.readouterr()
        assert cli_main(["report", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert payload["tables"]["settlement_rules"] == 3
    assert "crosscheck" in payload

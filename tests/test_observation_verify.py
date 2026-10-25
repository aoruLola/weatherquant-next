"""Observation verification: the market's own terms, both directions, all edges.

The tests that matter most live at the day's edges, because that is where a
verifier drifts into a different meteorological convention without noticing:
a UTC-day window is wrong by an hour twice a year, a naive timestamp would
silently move the DST boundary, and a partial observation day must be a
verdict of its own - never a reason to quietly borrow another source.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from weatherquant.markets.settlement_rules import parse_rules_text
from weatherquant.truth.observation_verify import (
    AMBIGUOUS_WINNER,
    INCOMPLETE_OBSERVATION,
    MATCH,
    MISMATCH,
    NO_DATA_MATCH,
    NO_DATA_MISMATCH,
    NO_OBSERVATION,
    NO_OUTCOME,
    UNUSABLE_RULES,
    verify_market_rules,
)

EVENT = "e-1"

#: One NOAA-family event's rules text (the shape 50/408 events use).
RULES_TEXT = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded at the LaGuardia Airport Station (KLGA), "
    "NY, according to https://www.weather.gov/wrh/timeseries?site=klga, "
    "based on the highest reading under the \"Temp\" column for all times on "
    "this day. This market measures temperatures to whole degrees Fahrenheit. "
    "If no data for the observation date is available from NOAA at 11:59 PM "
    "ET on the day following, this market will resolve to the lowest bracket. "
    "This market will resolve once the first data point for the following "
    "date has been published."
)


def _rules(**overrides) -> dict:
    parsed = parse_rules_text(RULES_TEXT, event_id=EVENT)
    base = {
        "station": parsed.station,
        "unit": parsed.unit,
        "precision_deg": parsed.precision_deg,
        "no_data_resolution": parsed.no_data_resolution,
        "rules_sha256": parsed.rules_sha256,
        "parser_version": parsed.parser_version if hasattr(parsed, "parser_version") else None,
        "rules_text": parsed.rules_text,
    }
    base.update(overrides)
    return base


def _markets(low: float, high: float, label: str | None = None, market_id: str = "m-1") -> list[dict]:
    return [{
        "market_id": market_id,
        "bracket_label": label or f"{low}-{high}",
        "bracket_low": low,
        "bracket_high": high,
        "bracket_low_inclusive": True,
        "bracket_high_inclusive": True,
    }]


def _outcome(market_id: str, *, winner: bool = True) -> dict:
    return {"market_id": market_id, "is_winner": winner, "yes_price": 1.0 if winner else 0.0}


def _obs(day: date, hour_utc: int, temp: float, *, source: str = "noaa-timeseries",
         minute: int = 0) -> dict:
    # hour_utc may exceed 23: it rolls into the following UTC day, which is
    # how the tests express "the window that spans the local midnight".
    when = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
        hours=hour_utc, minutes=minute
    )
    return {"obs_time_utc": when, "temp_f": temp, "source": source}


# --- the two directions, on an ordinary 24-hour day ---------------------------


def test_a_matching_settlement_is_a_match_in_both_directions() -> None:
    # 2026-10-13: a normal 24-hour New York day (EDT, UTC-4).
    observations = [_obs(date(2026, 10, 13), h, 60 + h % 7) for h in range(4, 28)]
    observations[-1]["temp_f"] = 81.0  # the day's peak

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(79, 81, "79-81°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=date(2026, 10, 13),
        event_id=EVENT,
    )

    assert result.verdict == MATCH
    assert result.observation_picks_venue_winner is True
    assert result.venue_bracket_contains_observation is True
    assert result.observed_max_whole_f == 81
    assert result.expected_hours == 24
    assert result.distinct_hours == 24


def test_a_venue_paying_the_wrong_bracket_is_a_mismatch_with_both_directions_named() -> None:
    day = date(2026, 10, 13)
    observations = [_obs(day, h, 60) for h in range(4, 28)]
    observations[10]["temp_f"] = 83.0

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(79, 81, "79-81°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.verdict == MISMATCH
    assert result.observation_picks_venue_winner is False
    assert result.venue_bracket_contains_observation is False
    assert "83" in result.detail and "79-81" in result.detail


# --- the local day, not the UTC day -------------------------------------------


def test_the_day_window_is_new_york_civil_time_not_utc() -> None:
    """A reading at 00:00 ET belongs to the day it starts, not the UTC one.

    2026-10-14 00:00 ET = 04:00Z on Oct 14. Fed to a verification FOR Oct 13,
    it must be excluded as out-of-day - and the day's UTC bounds recorded in
    the result are the evidence of which window was used.
    """
    day = date(2026, 10, 13)
    observations = [_obs(day, h, 60) for h in range(4, 28)]
    observations.append(_obs(date(2026, 10, 14), 4, 95.0))  # next day's midnight ET

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(59, 61, "59-61°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.verdict == MATCH
    assert result.n_out_of_day == 1
    assert result.observed_max_whole_f == 60, "the 95°F reading belongs to Oct 14"
    assert result.local_day_start_utc == "2026-10-13T04:00:00+00:00"
    assert result.local_day_end_utc == "2026-10-14T04:00:00+00:00"


def test_spring_forward_day_is_23_hours() -> None:
    """2026-03-08: 02:00 EST does not exist; the day is 23 hours long."""
    day = date(2026, 3, 8)
    # 05:00Z is the first instant (00:00 EST); 04:00Z belongs to Mar 7.
    observations = [_obs(day, h, 50) for h in range(5, 28)]
    observations[-1]["temp_f"] = 77.0

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(75, 77, "75-77°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.expected_hours == 23
    assert result.distinct_hours == 23
    assert result.local_day_start_utc == "2026-03-08T05:00:00+00:00"
    assert result.verdict == MATCH


def test_fall_back_day_is_25_hours() -> None:
    """2026-11-01: 01:00 ET happens twice; both UTC instants are in-day."""
    day = date(2026, 11, 1)
    # 04:00Z (00:00 EDT) through 05:00Z next day (00:00 EST) = 25 hours.
    observations = [_obs(day, h, 55) for h in range(4, 29)]

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(53, 55, "53-55°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.expected_hours == 25
    assert result.distinct_hours == 25
    assert result.verdict == MATCH


# --- missing data: the rules decide, never the verifier ------------------------


def test_partial_coverage_is_its_own_verdict_not_a_guess() -> None:
    """Twelve of 24 hours is not a day; the verifier must not grade it."""
    day = date(2026, 10, 13)
    observations = [_obs(day, h, 70) for h in range(4, 16)]  # 12 hours only

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(69, 71, "69-71°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.verdict == INCOMPLETE_OBSERVATION
    assert "12 of 24" in result.detail
    assert result.observed_max_f is None, "a partial day publishes no maximum"


def test_zero_readings_with_a_lowest_bracket_rule_follows_it() -> None:
    day = date(2026, 10, 13)
    markets = [
        {"market_id": "m-low", "bracket_label": "<=60°F", "bracket_low": None,
         "bracket_high": 60.0},
        {"market_id": "m-mid", "bracket_label": "61-80°F", "bracket_low": 61.0,
         "bracket_high": 80.0},
    ]
    result = verify_market_rules(
        rules=_rules(),
        markets=markets,
        outcomes=[_outcome("m-low")],
        observations=[],
        target_date=day,
        event_id=EVENT,
    )
    assert result.verdict == NO_DATA_MATCH

    result = verify_market_rules(
        rules=_rules(),
        markets=markets,
        outcomes=[_outcome("m-mid")],
        observations=[],
        target_date=day,
        event_id=EVENT,
    )
    assert result.verdict == NO_DATA_MISMATCH


def test_zero_readings_without_a_fallback_is_no_observation() -> None:
    result = verify_market_rules(
        rules=_rules(no_data_resolution=None),
        markets=_markets(69, 71, "69-71°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=[],
        target_date=date(2026, 10, 13),
        event_id=EVENT,
    )
    assert result.verdict == NO_OBSERVATION


# --- provenance and refusals ---------------------------------------------------


def test_provenance_travels_with_the_verdict() -> None:
    day = date(2026, 10, 13)
    observations = [_obs(day, h, 60) for h in range(4, 28)]
    observations[5]["source"] = "wu-daily"

    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(59, 61, "59-61°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    sources = {s["source"]: s for s in result.observation_sources}
    assert set(sources) == {"noaa-timeseries", "wu-daily"}
    assert sources["wu-daily"]["n_readings"] == 1
    assert result.rules_sha256
    assert result.verifier_version == "observation-verify-1"
    assert result.aggregation_rule.startswith("max of hourly readings")


def test_rules_missing_required_fields_are_unusable_never_guessed() -> None:
    """No unit, no precision: the verifier refuses rather than assumes °F."""
    result = verify_market_rules(
        rules={"station": "KLGA", "rules_sha256": "x"},
        markets=_markets(59, 61, "59-61°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=[_obs(date(2026, 10, 13), 12, 60.0)],
        target_date=date(2026, 10, 13),
        event_id=EVENT,
    )
    assert result.verdict == UNUSABLE_RULES
    assert "unit" in result.detail and "precision_deg" in result.detail


def test_venue_ambiguity_and_silence_are_verdicts_of_their_own() -> None:
    day = date(2026, 10, 13)
    observations = [_obs(day, h, 60) for h in range(4, 28)]
    base = dict(rules=_rules(), markets=_markets(59, 61, "59-61°F", market_id="m-win"),
                observations=observations, target_date=day, event_id=EVENT)

    ambiguous = verify_market_rules(
        markets=[
            {"market_id": "m-a", "bracket_label": "59-61°F", "bracket_low": 59.0,
             "bracket_high": 61.0},
            {"market_id": "m-b", "bracket_label": "61-63°F", "bracket_low": 61.0,
             "bracket_high": 63.0},
        ],
        outcomes=[_outcome("m-a"), _outcome("m-b")],
        **{k: v for k, v in base.items() if k != "markets"},
    )
    assert ambiguous.verdict == AMBIGUOUS_WINNER

    silent = verify_market_rules(outcomes=[], **base)
    assert silent.verdict == NO_OUTCOME


# --- persistence: the verdict and its provenance land in the research DB ------


def test_verification_rows_persist_with_full_provenance(tmp_path) -> None:
    from datetime import datetime as dt

    from weatherquant.storage.research import initialize_research
    from weatherquant.truth.observation_verify import (
        MARKET_VERIFY_VERSION,
        persist_verification_results,
    )

    db = tmp_path / "research.duckdb"
    con, _applied = initialize_research(db)
    assert any("market_rule_verification" in name for name in _applied) or con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='market_rule_verification'"
    ).fetchone()[0] == 1

    day = date(2026, 10, 13)
    observations = [_obs(day, h, 70) for h in range(4, 16)]  # deliberately partial
    result = verify_market_rules(
        rules=_rules(),
        markets=_markets(69, 71, "69-71°F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )
    count = persist_verification_results(
        con, [result], verified_at_utc=dt(2026, 10, 26, 12, 0, tzinfo=UTC)
    )
    assert count == 1

    row = con.execute(
        "SELECT verdict, detail, expected_hours, distinct_hours, observed_max_f, "
        "rules_sha256, verifier_version, observation_sources_json "
        "FROM market_rule_verification WHERE event_id = ?",
        [EVENT],
    ).fetchone()
    con.close()

    assert row[0] == INCOMPLETE_OBSERVATION
    assert row[2] == 24 and row[3] == 12
    assert row[4] is None, "a partial day publishes no maximum"
    assert row[5], "the rules hash travels with the verdict"
    assert row[6] == MARKET_VERIFY_VERSION
    sources = json.loads(row[7])
    assert sources and sources[0]["source"] == "noaa-timeseries"

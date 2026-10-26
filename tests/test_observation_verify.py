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

NOW_ISO = datetime(2026, 10, 26, 12, 0, tzinfo=UTC).isoformat()


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
    'based on the highest reading under the "Temp" column for all times on '
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
    return [
        {
            "market_id": market_id,
            "bracket_label": label or f"{low}-{high}",
            "bracket_low": low,
            "bracket_high": high,
            "bracket_low_inclusive": True,
            "bracket_high_inclusive": True,
        }
    ]


def _outcome(market_id: str, *, winner: bool = True) -> dict:
    return {"market_id": market_id, "is_winner": winner, "yes_price": 1.0 if winner else 0.0}


def _obs(day: date, hour_utc: int, temp: float, *, source: str = "noaa-timeseries", minute: int = 0) -> dict:
    # hour_utc may exceed 23: it rolls into the following UTC day, which is
    # how the tests express "the window that spans the local midnight".
    when = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour_utc, minutes=minute)
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
        {"market_id": "m-low", "bracket_label": "<=60°F", "bracket_low": None, "bracket_high": 60.0},
        {"market_id": "m-mid", "bracket_label": "61-80°F", "bracket_low": 61.0, "bracket_high": 80.0},
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
    assert result.verifier_version == "observation-verify-2"
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
    base = dict(
        rules=_rules(),
        markets=_markets(59, 61, "59-61°F", market_id="m-win"),
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    ambiguous = verify_market_rules(
        markets=[
            {"market_id": "m-a", "bracket_label": "59-61°F", "bracket_low": 59.0, "bracket_high": 61.0},
            {"market_id": "m-b", "bracket_label": "61-63°F", "bracket_low": 61.0, "bracket_high": 63.0},
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
    assert (
        any("market_rule_verification" in name for name in _applied)
        or con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='market_rule_verification'"
        ).fetchone()[0]
        == 1
    )

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
    written, failed = persist_verification_results(con, [result], verified_at_utc=dt(2026, 10, 26, 12, 0, tzinfo=UTC))
    assert (written, failed) == (1, [])

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


# --- the current METAR / T-group rules family (real rules text) ---------------


def _current_rules(**overrides) -> dict:
    """Rules parsed from the REAL 2026-10 rules text (KLGA, METAR family)."""
    from pathlib import Path

    from weatherquant.markets.settlement_rules import parse_rules_text

    fixture = Path(__file__).parent / "fixtures" / "rules" / "klga_metar_2026.txt"
    parsed = parse_rules_text(fixture.read_text(encoding="utf-8"), event_id=EVENT)
    base = {
        "station": parsed.station,
        "unit": parsed.unit,
        "precision_deg": parsed.precision_deg,
        "rounding": parsed.rounding,
        "observation_unit": parsed.observation_unit,
        "observation_filter": parsed.observation_filter,
        "no_data_resolution": parsed.no_data_resolution,
        "rules_sha256": parsed.rules_sha256,
        "parser_version": parsed.parser_version,
    }
    base.update(overrides)
    return base


def _metar(day: date, hour_utc: int, temp_c: float, *, special: bool = False) -> dict:
    when = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour_utc)
    return {
        "obs_time_utc": when,
        "temp_c": temp_c,
        "source": "aviationweather-metar",
        "report_type": "SPECI" if special else "METAR",
    }


def test_the_current_rules_family_parses_every_term_the_verifier_needs() -> None:
    from weatherquant.markets.settlement_rules import RULES_PARSER_VERSION

    rules = _current_rules()
    assert rules["station"] == "KLGA"
    assert rules["unit"] == "degF", "brackets are quoted in whole degrees Fahrenheit"
    assert rules["observation_unit"] == "degC", "the station reports tenths of a degree Celsius"
    assert rules["precision_deg"] == 0.1
    assert rules["rounding"] == "half_up_f"
    assert rules["observation_filter"] == "routine_excluding_speci"
    assert rules["no_data_resolution"] == "the lowest bracket"
    assert rules["parser_version"] == RULES_PARSER_VERSION


def test_the_2026_10_24_case_resolves_to_62_63F() -> None:
    """The acceptance's hand calc, end to end: max 16.7C -> 62.06F -> 62 ->
    62-63F, matching what the venue paid. Under the old verifier this event
    was unusable_rules (no precision parsed) and, with precision forced,
    a false mismatch (16.7C compared against a Fahrenheit bracket)."""
    day = date(2026, 10, 24)
    # A full local day (24h) of routine METARs in tenths of a degree Celsius.
    observations = [_metar(day, h, 12.0 + (h % 5) * 0.4) for h in range(4, 28)]
    observations.append(_metar(day, 21, 16.7))  # the day's peak (62.06F -> 62)

    result = verify_market_rules(
        rules=_current_rules(),
        markets=_markets(61, 62, "61-62F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.observed_max_whole_f == 62, result.detail
    assert result.verdict == MATCH
    assert result.observation_picks_venue_winner is True
    assert result.venue_bracket_contains_observation is True


def test_speci_reports_never_win_even_when_warmer() -> None:
    """The rules exclude special reports by line type - a warmer SPECI is
    counted (not silently dropped) and still not the qualifying value."""
    day = date(2026, 10, 24)
    observations = [_metar(day, h, 12.0) for h in range(4, 28)]
    observations.append(_metar(day, 21, 16.7))  # routine peak -> 62F
    observations.append(_metar(day, 22, 19.9, special=True))  # SPECI, warmer

    result = verify_market_rules(
        rules=_current_rules(),
        markets=_markets(61, 62, "61-62F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )

    assert result.n_excluded_special == 1
    assert result.observed_max_whole_f == 62
    assert result.verdict == MATCH


def test_the_rules_own_rounding_examples_hold() -> None:
    """18.3C -> 65F, 17.8C -> 64F, 19C -> 66F, -22.5C -> -8F: the exact-half
    rounds UP, which Python's banker's round() gets wrong on both ends."""
    from weatherquant.truth.observation_verify import _round_half_up

    assert round(18.3 * 9 / 5 + 32, 2) == 64.94
    assert _round_half_up(18.3 * 9 / 5 + 32) == 65
    assert _round_half_up(17.8 * 9 / 5 + 32) == 64
    assert _round_half_up(19.0 * 9 / 5 + 32) == 66
    assert _round_half_up(-22.5 * 9 / 5 + 32) == -8  # exactly -8.5 rounds up to -8


def test_a_degC_reading_is_never_compared_against_a_degF_bracket() -> None:
    """The acceptance's forced-precision scenario: with the units wired, the
    same observations that produced a false mismatch now match."""
    day = date(2026, 10, 24)
    observations = [_metar(day, h, 12.0) for h in range(4, 28)]
    observations.append(_metar(day, 21, 16.7))

    result = verify_market_rules(
        rules=_current_rules(),
        markets=_markets(61, 62, "61-62F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=observations,
        target_date=day,
        event_id=EVENT,
    )
    assert result.verdict == MATCH
    # observed_max_f carries the converted-but-unrounded value; the bracket
    # comparison uses the whole-degree rounding the rules declare.
    assert result.observed_max_f is not None and abs(result.observed_max_f - 62.06) < 0.01
    assert result.observed_max_whole_f == 62


def test_a_rules_row_without_station_still_persists_and_spares_the_batch(tmp_path) -> None:
    """One UNUSABLE row with no station must not roll back its batch - and
    the row itself lands under the documented sentinel."""

    from weatherquant.storage.research import initialize_research
    from weatherquant.truth.observation_verify import persist_verification_results

    db = tmp_path / "research.duckdb"
    con, _applied = initialize_research(db)

    broken = verify_market_rules(
        rules={"station": None, "unit": "degF", "precision_deg": 1.0, "rules_sha256": "x"},
        markets=_markets(59, 61, "59-61F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=[_obs(date(2026, 10, 13), 12, 60.0)],
        target_date=date(2026, 10, 13),
        event_id="e-broken",
    )
    good = verify_market_rules(
        rules=_rules(),
        markets=_markets(59, 61, "59-61F", market_id="m-win"),
        outcomes=[_outcome("m-win")],
        observations=[_obs(date(2026, 10, 14), h, 60.0) for h in range(4, 28)],
        target_date=date(2026, 10, 14),
        event_id=EVENT,
    )
    written, failed = persist_verification_results(con, [broken, good])
    con.close()

    assert written == 2, f"the good row must survive the broken one: {failed}"
    assert failed == []


# --- CLI wiring: verify-market over a collector copy, end to end ---------------


def test_verify_market_cli_matches_and_emits_verified_pairs(tmp_path) -> None:
    """The wired flow: collector copy (real rules text + brackets) +
    settlement index + readings -> MATCH -> verified pairs for
    backfill-settlements --verified, and a persisted row that carries
    parser_version."""

    import duckdb

    from weatherquant.cli import main

    collector = tmp_path / "collector.duckdb"
    con, _ = _init_collector_like(collector)
    con.execute(
        "INSERT INTO events (event_id, slug, title, target_date, fetched_at_utc, raw) "
        "VALUES ('e-1024', 'slug-1024', 't', '2026-10-24', now(), '{}')"
    )
    con.execute(
        "INSERT INTO markets (market_id, event_id, condition_id, question, "
        "bracket_label, bracket_kind, bracket_low, bracket_high, token_yes, token_no, "
        "target_date, station, tick_size, min_order_size_shares, active, closed, "
        "accepting_orders, rules_text, fetched_at_utc, raw) VALUES "
        "('700001', 'e-1024', '0xabc', 'q', '61-62°F', 'range', 61.0, 62.0, 'yes', 'no', "
        "'2026-10-24', 'KLGA', 0.01, 5, true, false, true, ?, now(), '{}')",
        [_rules_text()],
    )
    con.close()

    settlements = tmp_path / "settlements.json"
    settlements.write_text(
        json.dumps(
            [
                {
                    "market_id": "700001",
                    "target_date": "2026-10-24",
                    "settlement_status": "RESOLVED",
                    "resolved_outcome": 1.0,
                    "verification_status": "UNVERIFIED",
                    "fetched_at_utc": NOW_ISO,
                },
            ]
        ),
        encoding="utf-8",
    )

    day = date(2026, 10, 24)
    readings = []
    for hour in range(4, 28):
        when = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=hour)
        readings.append(
            {
                "obs_time_utc": when.isoformat(),
                "temp_c": 12.0,
                "report_type": "METAR",
                "source": "aviationweather-metar",
            }
        )
    peak = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(hours=21)
    readings.append(
        {"obs_time_utc": peak.isoformat(), "temp_c": 16.7, "report_type": "METAR", "source": "aviationweather-metar"}
    )
    obs_file = tmp_path / "observations.json"
    obs_file.write_text(json.dumps(readings), encoding="utf-8")

    out = tmp_path / "verified.json"
    research = tmp_path / "research.duckdb"
    code = main(
        [
            "verify-market",
            "--collector-copy",
            str(collector),
            "--date",
            "2026-10-24",
            "--settlements",
            str(settlements),
            "--observations-file",
            str(obs_file),
            "--persist",
            str(research),
            "--out",
            str(out),
        ]
    )
    assert code == 0

    pairs = json.loads(out.read_text(encoding="utf-8"))
    assert pairs == [{"market_id": "700001", "target_date": "2026-10-24"}]

    row = (
        duckdb.connect(str(research), read_only=True)
        .execute(
            "SELECT verdict, parser_version, n_excluded_special, observed_max_whole_f FROM market_rule_verification"
        )
        .fetchall()
    )
    verdict, parser_version, _special, whole = row[0]
    assert verdict == "match"
    assert parser_version == "settlement-rules-1", "the rules version must land"
    assert whole == 62


def _init_collector_like(path):
    from weatherquant.storage import initialize

    return initialize(path)


def _rules_text() -> str:
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "rules" / "klga_metar_2026.txt"
    return fixture.read_text(encoding="utf-8")


def test_cli_verifies_resolved_losers_as_well_as_the_winner(tmp_path):
    import duckdb

    from weatherquant.cli import main

    db = tmp_path / "collector.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE markets (market_id VARCHAR, event_id VARCHAR, bracket_label VARCHAR, "
        "bracket_low DOUBLE, bracket_high DOUBLE, bracket_low_inclusive BOOLEAN, "
        "bracket_high_inclusive BOOLEAN, rules_text VARCHAR, target_date DATE)"
    )
    for mid, low, high in [("winner", 80, 81), ("loser", 82, 83)]:
        con.execute(
            "INSERT INTO markets VALUES (?, 'event', ?, ?, ?, true, true, ?, ?)",
            [mid, mid, low, high, RULES_TEXT, date(2026, 10, 13)],
        )
    con.close()
    observations = [_obs(date(2026, 10, 13), hour, 81) for hour in range(4, 28)]
    obs = tmp_path / "observations.json"
    obs.write_text(json.dumps(observations, default=str), encoding="utf-8")
    settlements = tmp_path / "settlements.json"
    settlements.write_text(
        json.dumps(
            [
                {"market_id": "winner", "resolved_outcome": 1.0},
                {"market_id": "loser", "resolved_outcome": 0.0},
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "verified.json"
    assert (
        main(
            [
                "verify-market",
                "--date",
                "2026-10-13",
                "--collector-copy",
                str(db),
                "--observations-file",
                str(obs),
                "--settlements",
                str(settlements),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert {row["market_id"] for row in json.loads(out.read_text())} == {"winner", "loser"}

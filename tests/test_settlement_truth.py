"""Settlement-truth parsing and cross-checking.

Two of these tests exist because of bugs that were actually made while building
this module, and both would have produced a plausible-looking dataset:

* an inverted open-bracket test mislabelled every ``X or higher`` event;
* grouping outcomes by target date invented "two winners" out of two events
  that share a date (the legacy data contains an ``arch-`` duplicate exactly
  like this).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from weatherquant.markets.settlement_rules import parse_rules_text
from weatherquant.truth.crosscheck import (
    AMBIGUOUS_WINNER,
    MATCH,
    MISMATCH,
    NO_OBSERVATION,
    NO_WINNER,
    bracket_contains,
    crosscheck_events,
    verdict_counts,
)

# Verbatim shape of a NOAA-sourced market's rules, trimmed to the clauses this
# parser reads. The full text is 1000+ characters and mostly boilerplate.
NOAA_RULES = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded by NOAA at the LaGuardia Airport Station in degrees "
    "Fahrenheit on 10 Oct '26.\n\n"
    "The resolution source for this market will be information from NOAA, "
    'specifically the highest reading under the "Temp" column for all times on '
    "this day, available here: https://www.weather.gov/wrh/timeseries?site=klga\n\n"
    "This market will resolve off of the Hourly Data provided using the "
    '"Show Hourly Data" button.\n\n'
    "If NOAA data for the observation date is unavailable by 11:59 PM ET on the "
    "day following the observation date, the Weather Underground Daily Observations "
    "table will be used as the resolution source.\n\n"
    "In the event that there is no data for the observation date by 11:59 PM ET "
    "on the day following the observation date, this market will resolve to the "
    "lowest bracket.\n\n"
    "This market will resolve once the first data point for the following date "
    "has been published on the resolution source, or by 11:59 PM ET on the day "
    "following the observation date, whichever comes first.\n\n"
    "The resolution source for this market measures temperatures to whole "
    "degrees Fahrenheit (eg, 21°F). Thus, this is the level of precision that "
    "will be used when resolving the market."
)

WUNDERGROUND_RULES = (
    "This market will resolve to the temperature range that contains the highest "
    "temperature recorded at the LaGuardia Airport Station in degrees Fahrenheit "
    "on 19 May '26.\n\n"
    "The resolution source for this market will be information from Wunderground, "
    "specifically the highest temperature recorded for all times on this day, "
    "available here: https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA\n\n"
    "This market can not resolve until the first data point for the following "
    "date has been published on the resolution source.\n\n"
    "The resolution source for this market measures temperatures to whole "
    "degrees Fahrenheit (eg, 21°F). Thus, this is the level of precision "
    "that will be used when resolving the market."
)


# ---------------------------------------------------------------------------
# Bracket containment
# ---------------------------------------------------------------------------


def test_closed_bracket_is_inclusive_on_both_ends() -> None:
    assert bracket_contains(90.0, 91.0, 90.0)
    assert bracket_contains(90.0, 91.0, 91.0)
    assert not bracket_contains(90.0, 91.0, 89.0)
    assert not bracket_contains(90.0, 91.0, 92.0)


def test_open_high_bracket_accepts_values_above_the_bound() -> None:
    """`90°F or higher` means >= 90, not <= 90.

    Getting this backwards marked every open-bracket day a mismatch during
    development - 25 false mismatches out of 404 days.
    """
    assert bracket_contains(90.0, None, 95.0)
    assert bracket_contains(90.0, None, 90.0)
    assert not bracket_contains(90.0, None, 89.0)


def test_open_low_bracket_accepts_values_below_the_bound() -> None:
    """`59°F or below` means <= 59."""
    assert bracket_contains(None, 59.0, 40.0)
    assert bracket_contains(None, 59.0, 59.0)
    assert not bracket_contains(None, 59.0, 60.0)


def test_a_bracket_with_no_bounds_contains_nothing() -> None:
    assert not bracket_contains(None, None, 70.0)


# ---------------------------------------------------------------------------
# Cross-check verdicts
# ---------------------------------------------------------------------------


def _event(event_id: str, target_date: date, markets: list[tuple[str, float | None, float | None, float]]) -> list[dict]:
    """markets: (bracket_label, low, high, yes_price)"""
    return [
        {
            "event_id": event_id,
            "target_date": target_date,
            "bracket_label": label,
            "bracket_low": low,
            "bracket_high": high,
            "yes_price": yes,
        }
        for label, low, high, yes in markets
    ]


def _rules(parse_status: str = "parsed"):
    class _R:
        pass

    r = _R()
    r.parse_status = parse_status
    return r


DAY = date(2026, 6, 14)


def test_a_matching_observation_verdicts_match() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0), ("92-93°F", 92.0, 93.0, 0.0)])
    results = crosscheck_events(
        events=events,
        observations={DAY: 90.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    assert len(results) == 1
    assert results[0].verdict == MATCH
    assert results[0].observed_max_f == 90.0
    assert results[0].winning_bracket_label == "90-91°F"


def test_an_observation_outside_the_winning_bracket_verdicts_mismatch() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0)])
    results = crosscheck_events(
        events=events,
        observations={DAY: 95.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    assert results[0].verdict == MISMATCH
    assert "95" in results[0].detail


def test_a_missing_observation_is_reported_not_guessed() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0)])
    results = crosscheck_events(
        events=events,
        observations={},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    assert results[0].verdict == NO_OBSERVATION
    assert results[0].observed_max_f is None


def test_an_event_with_no_winner_is_reported() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 0.0), ("92-93°F", 92.0, 93.0, 0.0)])
    results = crosscheck_events(
        events=events,
        observations={DAY: 90.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    assert results[0].verdict == NO_WINNER


def test_two_winners_in_one_event_is_ambiguous_not_a_match() -> None:
    events = _event(
        "e1", DAY, [("90-91°F", 90.0, 91.0, 1.0), ("95°F or below", None, 95.0, 1.0)]
    )
    results = crosscheck_events(
        events=events,
        observations={DAY: 95.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    assert results[0].verdict == AMBIGUOUS_WINNER
    assert results[0].n_winners == 2
    assert results[0].winning_bracket_label is None


def test_events_are_keyed_individually_so_a_shared_date_is_not_merged() -> None:
    """The `arch-` lesson: two events on one date are two events.

    Merging them would produce a single row with two winners and hide the fact
    that the two resolved to *different* brackets - which is the actual finding.
    """
    legacy = _event(
        "493658", DAY, [("90-91°F", 90.0, 91.0, 1.0), ("94-95°F", 94.0, 95.0, 0.0)]
    )
    current = _event("503590", DAY, [("95°F or below", None, 95.0, 1.0), ("96-97°F", 96.0, 97.0, 0.0)])
    results = crosscheck_events(
        events=legacy + current,
        observations={DAY: 95.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"493658": _rules(), "503590": _rules()},
    )

    by_id = {r.event_id: r for r in results}
    assert set(by_id) == {"493658", "503590"}
    assert by_id["493658"].verdict == MISMATCH  # resolved 90-91, observed 95
    assert by_id["503590"].verdict == MATCH     # resolved <=95, observed 95
    assert by_id["493658"].n_winners == 1, "each event must be judged on its own"


def test_unparsed_rules_are_separated_from_a_disagreement() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0)])
    results = crosscheck_events(
        events=events,
        observations={DAY: 95.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules("unparsed")},
    )
    assert results[0].verdict == "unparsed_rules"


def test_verdict_counts_cover_every_category() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0)])
    results = crosscheck_events(
        events=events,
        observations={DAY: 90.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
    )
    counts = verdict_counts(results)
    assert counts["match"] == 1
    assert sum(counts.values()) == len(results)


def test_results_carry_timestamp_and_provenance() -> None:
    events = _event("e1", DAY, [("90-91°F", 90.0, 91.0, 1.0)])
    fixed = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    results = crosscheck_events(
        events=events,
        observations={DAY: 90.0},
        station="KLGA",
        observation_source="iem_asos",
        aggregation_rule="all_readings",
        rules_by_event={"e1": _rules()},
        now=fixed,
    )
    row = results[0].to_row()
    assert row[0] == "e1"
    assert row[2] == "KLGA"
    assert row[3] == "iem_asos"
    assert row[4] == "all_readings"
    assert row[-1] == fixed
    assert results[0].checked_at_utc.tzinfo is not None


# ---------------------------------------------------------------------------
# Rules parsing
# ---------------------------------------------------------------------------


def test_noaa_rules_parse_completely() -> None:
    parsed = parse_rules_text(NOAA_RULES, event_id="e1")
    assert parsed.parse_status == "parsed"
    assert parsed.station == "KLGA"
    assert parsed.primary_source_name == "NOAA"
    assert "timeseries?site=klga" in (parsed.primary_source_url or "")
    assert parsed.fallback_source_name == "WEATHER_UNDERGROUND"
    assert parsed.no_data_resolution is not None
    assert "lowest bracket" in parsed.no_data_resolution.lower()
    assert parsed.unit == "degF"
    assert parsed.precision_deg == 1.0
    assert parsed.rounding == "nearest_integer_f"
    assert parsed.timezone_name == "America/New_York"
    assert parsed.timezone_hint == "11:59 PM ET"
    assert parsed.observation_field is not None
    assert parsed.resolve_by_rule is not None
    assert parsed.missing_fields == []


def test_wunderground_rules_parse_without_inventing_missing_clauses() -> None:
    """Absence must stay absence: the WU text has no fallback and no deadline."""
    parsed = parse_rules_text(WUNDERGROUND_RULES, event_id="e1")
    assert parsed.parse_status == "parsed"
    assert parsed.station == "KLGA"
    assert parsed.primary_source_name == "WEATHER_UNDERGROUND"
    assert parsed.fallback_source_name is None, "no fallback is stated - do not infer one"
    assert parsed.timezone_hint is None, "no deadline is stated - do not infer one"
    assert parsed.no_data_resolution is None
    assert parsed.unit == "degF"


def test_empty_rules_are_unparsed_and_never_dropped() -> None:
    parsed = parse_rules_text("", event_id="e1")
    assert parsed.parse_status == "unparsed"
    assert "empty rules text" in parsed.parse_notes
    assert parsed.rules_sha256


def test_an_unfamiliar_station_is_reported_not_assumed() -> None:
    """The audited bots shipped KNYC while markets resolved on KLGA."""
    text = NOAA_RULES.replace("LaGuardia Airport Station", "Some Unknown Field")
    parsed = parse_rules_text(text, event_id="e1")
    # The URL still names site=klga, so the station is established from there.
    assert parsed.station == "KLGA"
    assert parsed.station_source == "rules_url"

    no_url = text.replace("https://www.weather.gov/wrh/timeseries?site=klga", "https://example.com/x")
    parsed2 = parse_rules_text(no_url, event_id="e1")
    assert parsed2.station is None, "an unidentified station must stay unidentified"
    assert any("no station identified" in n for n in parsed2.parse_notes)


def test_rules_sha256_is_stable_and_input_dependent() -> None:
    a = parse_rules_text(NOAA_RULES, event_id="e1")
    b = parse_rules_text(NOAA_RULES, event_id="e2")
    c = parse_rules_text(WUNDERGROUND_RULES, event_id="e1")
    assert a.rules_sha256 == b.rules_sha256, "same text must hash the same"
    assert a.rules_sha256 != c.rules_sha256, "different text must hash differently"
    assert len(a.rules_sha256) == 64

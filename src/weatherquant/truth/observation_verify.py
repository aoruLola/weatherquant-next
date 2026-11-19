"""Verify station observations against the venue's resolution, by the market's own rules.

What this module refuses to do is the whole point: it never substitutes a
different meteorological convention. The market's rules text names the
station, the unit, the precision, the local date and the no-data fallback;
those parsed terms (:mod:`weatherquant.markets.settlement_rules`) are the
*only* inputs the comparison runs on. A verifier that quietly swapped in a
daily-summary column, a UTC day, or another station would produce a
verdict that looks authoritative and answers a different question.

Two directions, both named:

* **observation → bracket**: the highest reading inside the target New York
  civil day, mapped through the event's own bracket bounds, must equal what
  the venue paid.
* **venue → observation**: the bracket the venue paid on must contain the
  observed maximum. Symmetric to the first by construction, but stated
  separately so a report can show *which* direction failed.

Provenance travels with every verdict: the rules text's SHA-256 and parser
version, the verifier version, the observation sources with their fetch
times, the day's UTC bounds (which encode the DST offset actually in force),
and the expected hour count (23, 24 or 25). A mismatch is recorded with all
of that intact - it is evidence, never something to reconcile away.

Verdicts:

========================  ====================================================
``match``                 both directions agree
``mismatch``              they disagree
``ambiguous_winner``      venue paid on more than one market in the event
``no_outcome``            venue paid on nothing (unresolved?)
``incomplete_observation`` the observation source does not cover the whole
                          local day - never judged by guessing another source
``no_data_match`` / ``no_data_mismatch``  zero readings and the rules say
                          the market resolves to the lowest bracket
``no_observation``        zero readings and the rules name no fallback
``unusable_rules``        the parsed terms lack a field the comparison needs
========================  ====================================================
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from weatherquant.truth.crosscheck import bracket_contains
from weatherquant.weather.daily_max import (
    expected_local_hours,
    local_day_bounds_utc,
)

__all__ = [
    "MARKET_VERIFY_VERSION",
    "VERDICTS",
    "MarketVerifyResult",
    "verify_market_rules",
]

#: Bumped when the comparison rules change; every persisted row carries it,
#: so a verdict can always be re-run under the version that produced it.
MARKET_VERIFY_VERSION = "observation-verify-3"

#: The parser version to record when a rules dict carries a hash but no
#: parser_version field - i.e. it was built before the parser stamped itself.
RULES_PARSER_FALLBACK = "settlement-rules-1"

MATCH = "match"
MISMATCH = "mismatch"
AMBIGUOUS_WINNER = "ambiguous_winner"
NO_OUTCOME = "no_outcome"
INCOMPLETE_OBSERVATION = "incomplete_observation"
NO_DATA_MATCH = "no_data_match"
NO_DATA_MISMATCH = "no_data_mismatch"
NO_OBSERVATION = "no_observation"
UNUSABLE_RULES = "unusable_rules"

VERDICTS = (
    MATCH, MISMATCH, AMBIGUOUS_WINNER, NO_OUTCOME, INCOMPLETE_OBSERVATION,
    NO_DATA_MATCH, NO_DATA_MISMATCH, NO_OBSERVATION, UNUSABLE_RULES,
)

#: The only aggregation this verifier performs, and the only one the parsed
#: rules describe: the highest reading under the market's Temp column inside
#: the target local day. No daily-summary substitution, no cross-source fill.
AGGREGATION_RULE = "max of hourly readings in the market's local day"


@dataclass(frozen=True, slots=True)
class MarketVerifyResult:
    """One event's two-way verification, with the provenance to re-run it."""

    event_id: str
    target_date: date
    station: str

    verdict: str
    detail: str

    local_day_start_utc: str
    local_day_end_utc: str
    expected_hours: int
    distinct_hours: int
    n_readings: int
    n_out_of_day: int
    n_malformed: int
    n_excluded_special: int = 0

    # The two directions, separately. Both true is `match`; any other
    # combination is a mismatch, and the pair says which way it broke.
    # None on verdicts that never reached the comparison (incomplete data,
    # no outcome, unusable rules).
    observation_picks_venue_winner: bool | None = None
    venue_bracket_contains_observation: bool | None = None
    observed_max_f: float | None = None
    observed_max_whole_f: int | None = None

    aggregation_rule: str = AGGREGATION_RULE
    verifier_version: str = MARKET_VERIFY_VERSION
    rules_sha256: str | None = None
    parser_version: str | None = None
    unit: str | None = None
    precision_deg: float | None = None
    observation_sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "target_date": self.target_date.isoformat(),
            "station": self.station,
            "verdict": self.verdict,
            "detail": self.detail,
            "observation_picks_venue_winner": self.observation_picks_venue_winner,
            "venue_bracket_contains_observation": self.venue_bracket_contains_observation,
            "observed_max_f": self.observed_max_f,
            "observed_max_whole_f": self.observed_max_whole_f,
            "local_day_start_utc": self.local_day_start_utc,
            "local_day_end_utc": self.local_day_end_utc,
            "expected_hours": self.expected_hours,
            "distinct_hours": self.distinct_hours,
            "n_readings": self.n_readings,
            "aggregation_rule": self.aggregation_rule,
            "verifier_version": self.verifier_version,
            "rules_sha256": self.rules_sha256,
            "parser_version": self.parser_version,
            "unit": self.unit,
            "precision_deg": self.precision_deg,
            "observation_sources": list(self.observation_sources),
        }


def _require(
    rules: Mapping[str, Any],
    key: str,
    failures: list[str],
) -> Any:
    value = rules.get(key)
    if value is None:
        failures.append(f"rules lack {key}")
    return value


def _round_half_up(value: float) -> int:
    """Round to the nearest whole degree, exactly-half UP - the rounding the
    METAR family's rules spell out ("18.3°C is 64.94°F and counts as 65°F;
    -22.5°C is exactly -8.5°F and counts as -8°F"). Python's ``round`` is
    banker's rounding and gets both of those wrong; floor(x + 0.5) matches
    the rules for positives and negatives alike."""
    return math.floor(value + 0.5)


def verify_market_rules(
    *,
    rules: Mapping[str, Any],
    markets: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    target_date: date,
    event_id: str = "",
) -> MarketVerifyResult:
    """Run the two-way verification for one event on one target date.

    ``rules`` is a parsed rules row (station, unit, precision_deg, rounding,
    no_data_resolution, rules_sha256, parser_version, and - for the METAR
    family - observation_unit and observation_filter) - the market's own
    terms, never a convention this module prefers. ``markets`` are the
    event's brackets; ``outcomes`` say what the venue paid; ``observations``
    are station readings for any range of time (out-of-day points are
    counted and excluded, so a caller may pass a whole archive).

    Units are the rules' business: ``observation_unit`` says which column a
    reading arrives in (degF for the NOAA/WU families, degC for the METAR
    family); ``unit`` says what the brackets are quoted in. When the two
    differ the maximum is converted (°F = °C x 9/5 + 32) and rounded per the
    rules' rounding clause - comparing a °C reading against a °F bracket is
    exactly the silent convention swap this module exists to refuse.
    """
    start, end = local_day_bounds_utc(target_date)
    expected = expected_local_hours(target_date)
    # parser_version: recorded from the rules when the caller supplies it;
    # rules that carry a hash came from the parser, which since
    # settlement-rules-1 stamps its own version - an absent field on a
    # hashed row means an older dict shape, and the version that parsed it
    # is the one to record. A row with neither is not from the parser at all.
    parser_version = rules.get("parser_version") or (
        RULES_PARSER_FALLBACK if rules.get("rules_sha256") else None
    )
    common = {
        "event_id": event_id,
        "target_date": target_date,
        "station": rules.get("station"),
        "local_day_start_utc": start.isoformat(),
        "local_day_end_utc": end.isoformat(),
        "expected_hours": expected,
        "rules_sha256": rules.get("rules_sha256"),
        "parser_version": parser_version,
        "unit": rules.get("unit"),
        "precision_deg": rules.get("precision_deg"),
    }

    failures: list[str] = []
    station = _require(rules, "station", failures)
    unit = _require(rules, "unit", failures)
    _require(rules, "precision_deg", failures)
    if not markets:
        failures.append("the event has no market brackets to compare against")
    if failures:
        return MarketVerifyResult(
            verdict=UNUSABLE_RULES,
            detail="; ".join(failures),
            observation_picks_venue_winner=None,
            venue_bracket_contains_observation=None,
            observed_max_f=None,
            observed_max_whole_f=None,
            distinct_hours=0,
            n_readings=0,
            n_out_of_day=0,
            n_malformed=0,
            **common,
        )

    # --- observations inside the market's local day -------------------------
    # Which column a reading arrives in is the OBSERVATION unit from the
    # rules (degC for the METAR family, degF for the legacy ones) - not a
    # guess from the bracket unit.
    observation_unit = str(rules.get("observation_unit") or unit)
    temp_key = "temp_f" if observation_unit.lower() == "degf" else "temp_c"
    # The METAR family's SPECI clause: a special observation never counts.
    # Rows carry the report type verbatim; only an explicit rules filter
    # turns it into an exclusion, so legacy families are untouched.
    exclude_special = str(rules.get("observation_filter") or "") == "routine_excluding_speci"
    other_station = 0
    in_day: list[tuple[datetime, float]] = []
    out_of_day = malformed = 0
    excluded_special = 0
    sources: dict[str, dict[str, Any]] = {}
    for item in observations:
        if str(item.get("station") or "").strip().upper() != station:
            other_station += 1
            continue
        when_raw = item.get("obs_time_utc")
        value_raw = item.get(temp_key)
        try:
            when = (
                when_raw
                if isinstance(when_raw, datetime)
                else datetime.fromisoformat(str(when_raw))
            )
        except (TypeError, ValueError):
            malformed += 1
            continue
        if when.tzinfo is None:
            # A naive stamp has no instant; guessing a zone would quietly move
            # the DST window the whole comparison sits inside.
            malformed += 1
            continue
        when = when.astimezone(start.tzinfo)
        try:
            value = float(value_raw)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if value != value:
            malformed += 1
            continue
        source = str(item.get("source") or "unknown")
        if exclude_special and str(item.get("report_type") or "").upper().startswith("SPECI"):
            # The rules exclude special reports by line type, whether or not
            # they carry a higher reading; they are counted, not dropped.
            excluded_special += 1
            continue
        if not (start <= when < end):
            out_of_day += 1
            continue
        stats = sources.setdefault(source, {"n": 0, "first": when, "last": when})
        stats["n"] += 1
        stats["first"] = min(stats["first"], when)
        stats["last"] = max(stats["last"], when)
        in_day.append((when, value))

    distinct = {w.replace(minute=0, second=0, microsecond=0) for w, _ in in_day}
    provenance = tuple(
        {
            "source": name,
            "n_readings": stats["n"],
            "first_obs_utc": stats["first"].isoformat(),
            "last_obs_utc": stats["last"].isoformat(),
        }
        for name, stats in sorted(sources.items())
    )

    def result(verdict: str, detail: str, **extra: Any) -> MarketVerifyResult:
        return MarketVerifyResult(
            distinct_hours=len(distinct),
            n_readings=len(in_day),
            n_out_of_day=out_of_day,
            n_malformed=malformed,
            n_excluded_special=excluded_special,
            observation_sources=provenance,
            verdict=verdict,
            detail=detail,
            **common,
            **extra,
        )

    # --- zero readings: the rules' no-data clause decides -------------------
    if not in_day:
        if other_station:
            return result(NO_OBSERVATION, f"no readings attributable to {station} in the target day")
        no_data_rule = str(rules.get("no_data_resolution") or "").lower()
        venue = _venue_winners(markets, outcomes)
        if venue is None or len(venue) != 1:
            verdict = NO_OUTCOME if not venue else AMBIGUOUS_WINNER
            return result(verdict, f"no readings and venue winners={len(venue or [])}")
        lowest = _lowest_bracket(markets)
        if "lowest" in no_data_rule and lowest is not None:
            paid = {m["bracket_label"] for m in venue}
            ok = lowest["bracket_label"] in paid
            return result(
                NO_DATA_MATCH if ok else NO_DATA_MISMATCH,
                f"no readings; rules resolve to the lowest bracket "
                f"({lowest['bracket_label']}); venue paid {sorted(paid)}",
            )
        return result(
            NO_OBSERVATION,
            "no readings in the target day and the rules name no fallback",
        )

    # --- partial coverage is its own verdict, never a guess -----------------
    if len(distinct) < expected:
        return result(
            INCOMPLETE_OBSERVATION,
            f"observation source covers {len(distinct)} of {expected} local hours; "
            "the market's terms name no substitute source for the gap",
        )

    observed = max(value for _, value in in_day)
    # The comparison value, per the rules: the maximum in the OBSERVATION
    # unit, converted to the bracket unit when they differ (°F = °C x 9/5 +
    # 32), then rounded exactly-half UP. Max-then-convert is what the METAR
    # family's text describes; the conversion is monotonic, so the two
    # orders agree, but the text is followed literally. A °C reading is
    # never compared against a °F bracket unconverted.
    if observation_unit.lower() != str(unit).lower():
        if observation_unit.lower() == "degc" and str(unit).lower() == "degf":
            observed = observed * 9.0 / 5.0 + 32.0
        else:
            return result(
                UNUSABLE_RULES,
                f"no conversion defined from {observation_unit} to {unit}; "
                "refusing to compare across units",
            )
    observed_whole = _round_half_up(observed)

    # --- observation → bracket ---------------------------------------------
    computed = [
        market
        for market in markets
        if _contains_observation(market, observed_whole)
    ]
    # --- venue → observation ------------------------------------------------
    venue = _venue_winners(markets, outcomes)
    if not venue:
        return result(
            NO_OUTCOME,
            f"the venue paid on nothing for {target_date}; nothing to verify against",
            observed_max_f=observed,
            observed_max_whole_f=observed_whole,
        )
    if len(venue) > 1:
        paid = sorted({str(m.get("bracket_label")) for m in venue})
        return result(
            AMBIGUOUS_WINNER,
            f"the venue paid on {len(venue)} markets in one event: {paid}",
            observed_max_f=observed,
            observed_max_whole_f=observed_whole,
        )

    computed_labels = {str(m.get("bracket_label")) for m in computed}
    paid = venue[0]
    paid_label = str(paid.get("bracket_label"))

    forward = len(computed) == 1 and str(computed[0].get("market_id")) == str(paid.get("market_id"))
    # The reverse direction, separately: the bracket the venue paid on must
    # contain the observed maximum. Same comparison by construction; stating
    # both keeps a failure legible as to *which* direction broke.
    reverse = _contains_observation(paid, observed_whole)

    if forward and reverse:
        verdict, detail = MATCH, (
            f"observed {observed_whole}°F → bracket {paid_label}; venue paid {paid_label}"
        )
    elif len(computed) > 1:
        verdict, detail = AMBIGUOUS_WINNER, (
            f"observed {observed_whole}°F falls in {sorted(computed_labels)}: "
            "the event's brackets overlap"
        )
    else:
        verdict, detail = MISMATCH, (
            f"observed {observed_whole}°F → {sorted(computed_labels) or 'no bracket'}; "
            f"venue paid {paid_label} "
            f"(forward={forward}, reverse={reverse})"
        )

    return result(
        verdict,
        detail,
        observation_picks_venue_winner=forward,
        venue_bracket_contains_observation=reverse,
        observed_max_f=observed,
        observed_max_whole_f=observed_whole,
    )


def _contains_observation(market: Mapping[str, Any], value: float) -> bool:
    low = market.get("bracket_low")
    high = market.get("bracket_high")
    if market.get("bracket_kind") == "exact":
        return value == low
    if value == low and market.get("bracket_low_inclusive") is False:
        return False
    if value == high and market.get("bracket_high_inclusive") is False:
        return False
    return bracket_contains(low, high, value)


def _venue_winners(
    markets: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]] | None:
    """The markets the venue paid on, as market rows.

    Keyed by ``market_id`` when the outcome carries one, otherwise paired by
    position - the legacy loader guarantees both shapes never mix in one run.
    Returns None when the outcomes say nothing at all.
    """
    if not outcomes:
        return None
    by_id = {str(m.get("market_id")): m for m in markets}
    winners: list[Mapping[str, Any]] = []
    for outcome in outcomes:
        is_winner = outcome.get("is_winner")
        if is_winner is None:
            is_winner = outcome.get("yes_price") == 1.0
        if not is_winner:
            continue
        market_id = str(outcome.get("market_id") or "")
        market = by_id.get(market_id)
        if market is not None:
            winners.append({**market, "yes_price": outcome.get("yes_price")})
        elif markets:
            winners.append(dict(outcome))
    return winners


def _lowest_bracket(markets: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The event's lowest bracket - what 'resolve to the lowest' means.

    The lowest bracket is usually the open-ended "X°F or below" one, which
    has no lower bound at all; filtering on ``bracket_low`` would skip exactly
    the bracket the rule points at.
    """
    for market in markets:
        if market.get("bracket_low") is None and market.get("bracket_high") is not None:
            return market
    with_bounds = [m for m in markets if m.get("bracket_low") is not None]
    if not with_bounds:
        return None
    return min(with_bounds, key=lambda m: float(m["bracket_low"]))


# ---------------------------------------------------------------------------
# persistence: the research DB's market_rule_verification table
# ---------------------------------------------------------------------------

_VERIFICATION_COLUMNS = (
    "event_id, target_date, station, verdict, detail, observed_max_f, "
    "observed_max_whole_f, observation_picks_venue_winner, "
    "venue_bracket_contains_observation, local_day_start_utc, local_day_end_utc, "
    "expected_hours, distinct_hours, n_readings, n_excluded_special, "
    "aggregation_rule, unit, precision_deg, rules_sha256, parser_version, "
    "verifier_version, observation_sources_json, verified_at_utc"
)


def to_row(result: MarketVerifyResult, *, verified_at_utc: datetime) -> tuple:
    """Map a result onto the market_rule_verification column order."""
    import json

    return (
        result.event_id,
        result.target_date,
        # A verdict whose rules could not name a station is still evidence;
        # the sentinel keeps the column's NOT NULL honest without losing the
        # row (local acceptance: one such row used to roll back the batch).
        result.station or "unknown",
        result.verdict,
        result.detail,
        result.observed_max_f,
        result.observed_max_whole_f,
        result.observation_picks_venue_winner,
        result.venue_bracket_contains_observation,
        result.local_day_start_utc,
        result.local_day_end_utc,
        result.expected_hours,
        result.distinct_hours,
        result.n_readings,
        result.n_excluded_special,
        result.aggregation_rule,
        result.unit,
        result.precision_deg,
        result.rules_sha256,
        result.parser_version,
        result.verifier_version,
        json.dumps([dict(s) for s in result.observation_sources]),
        verified_at_utc,
    )


def persist_verification_results(
    con,
    results: Iterable[MarketVerifyResult],
    *,
    verified_at_utc: datetime | None = None,
) -> tuple[int, list[str]]:
    """Insert verification rows one by one, and keep going past a failure.

    ``executemany`` is atomic: one bad row (a constraint the row shape did
    not expect) rolled back the whole batch, losing verdicts that were fine.
    Each row stands on its own here; the failed ones come back as
    descriptions so the caller can report exactly what did not land.

    INSERT OR REPLACE on the versioned key: a re-run under the SAME verifier
    version replaces its own row (it re-derived it from the same inputs); a
    NEW verifier version writes a new row, so no verdict's provenance is
    ever overwritten. Returns ``(written, failed_descriptions)``.
    """
    verified_at = verified_at_utc or datetime.now(tz=UTC)
    placeholders = ", ".join("?" for _ in range(len(_VERIFICATION_COLUMNS.split(","))))
    statement = (
        f"INSERT OR REPLACE INTO market_rule_verification ({_VERIFICATION_COLUMNS}) "
        f"VALUES ({placeholders})"
    )
    written = 0
    failed: list[str] = []
    for result in results:
        row = to_row(result, verified_at_utc=verified_at)
        try:
            con.execute(statement, row)
            written += 1
        except Exception as exc:
            failed.append(f"{result.event_id}/{result.target_date}: {exc}")
    return written, failed

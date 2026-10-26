"""Parse a Polymarket weather event's rules text into structured settlement terms.

The rules text is the only authoritative statement of what a market settles
on, and it is written in prose that varies between markets and between source
families (NOAA vs Weather Underground). Three principles govern this module:

**Parse conservatively.** Anything not literally present in the text becomes
``None`` with a note, never a plausible-sounding default. A parser that guesses
"KLGA" when the text does not name a station produces a dataset that looks
complete and is quietly wrong - and the station question is exactly where
open-source bots in our audit got it wrong (see docs/open-source-review.md §5.3).

**Keep the prose.** The verbatim text and its SHA-256 travel with every parsed
row so any parse can be re-audited against what the venue actually said.

**Report what is missing as loudly as what is found.** ``parse_status`` is
``parsed`` / ``partial`` / ``unparsed``, and ``parse_notes`` lists every field
that could not be established - a partial parse is a fact to record, not a
problem to hide.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = ["RULES_PARSER_VERSION", "ParsedRules", "parse_rules_text"]

RULES_PARSER_VERSION = "settlement-rules-1"

# Families, in the order they are looked for. A market may name a primary and a
# fallback; the pair matters because the fallback only applies under the
# condition stated next to it.
_NOAA_URL_RE = re.compile(r"weather\.gov/[^\s)\]]*site=(\w+)", re.IGNORECASE)
_WUNDERGROUND_URL_RE = re.compile(
    r"wunderground\.com/history/daily/[^\s)\]]*?/([a-z]{4})/?", re.IGNORECASE
)

# "the highest reading under the "Temp" column for all times on this day"
_TEMP_COLUMN_RE = re.compile(
    r'highest\s+reading\s+under\s+the\s+"?Temp"?\s+column', re.IGNORECASE
)
_TEMP_COLUMN_ALT_RE = re.compile(r'under\s+the\s+"?Temp"?\s+column', re.IGNORECASE)

_HIGHEST_TEMP_RE = re.compile(
    r"(highest\s+temperature\s+recorded|highest\s+(?:temperature|reading)\s+recorded)", re.IGNORECASE
)
_STATION_RE = re.compile(
    r"(LaGuardia(?:\s+Airport)?(?:\s+Station)?|KLGA|KNYC|Central\s+Park)", re.IGNORECASE
)
_WHOLE_DEG_RE = re.compile(
    r"measures\s+temperatures\s+to\s+whole\s+degrees\s+(Fahrenheit|Celsius)", re.IGNORECASE
)
_UNIT_RE = re.compile(r"\b(degrees\s+(Fahrenheit|Celsius)|°\s?F|°\s?C)\b", re.IGNORECASE)
_ET_DEADLINE_RE = re.compile(r"(\d{1,2}:\d{2}\s*(?:AM|PM)\s*ET)", re.IGNORECASE)
_SOURCE_FROM_HERE_RE = re.compile(
    r"resolution source[^.]*?from\s+(NOAA|Weather\s+Underground|Wunderground)", re.IGNORECASE
)
_FALLBACK_RE = re.compile(
    r"(?:if|should)\s+([^,]+(?:unavailable|not\s+available)[^,]*),\s*the\s+"
    r"(Weather\s+Underground|NOAA)\s+([^,.]+?)\s+will\s+be\s+used",
    re.IGNORECASE,
)
# Anchored on the no-data clause, not on any "resolve to": the rules text opens
# with "This market will resolve to the temperature range that contains the
# highest temperature recorded...", and an unanchored pattern captures that
# sentence instead of the fallback instruction that actually matters.
_NO_DATA_RE = re.compile(
    r"no data for the observation date[^.]{0,220}?resolve\s+to\s+([^.;]+)",
    re.IGNORECASE,
)
# "...this market will resolve once <clause>."
_RESOLVE_WHEN_RE = re.compile(
    r"(?:market will resolve once|market can not resolve until) ([^.]+)\.",
    re.IGNORECASE,
)

# --- the current METAR family (KLGA since ~2026-10) -------------------------
# "The qualifying value is the highest temperature reported in the hourly
# temperature remark of any routine (METAR) observation for LaGuardia
# Airport (KLGA) on that date, in tenths of a degree Celsius as reported."
# This family quotes its brackets in whole degrees Fahrenheit, converts
# with °F = °C x 9/5 + 32 and rounds exactly-half UP, excludes SPECI
# reports, and names the Aviation Weather Center's METAR feed as its
# resolution source - no NOAA Temp column, no WU daily table.
_METAR_STATISTIC_RE = re.compile(
    r"highest\s+temperature\s+reported\s+in\s+the\s+hourly\s+temperature\s+remark",
    re.IGNORECASE,
)
_TENTHS_UNIT_RE = re.compile(
    r"in\s+tenths\s+of\s+a\s+degree\s+(Celsius|Fahrenheit)\s+as\s+reported",
    re.IGNORECASE,
)
_BRACKET_UNIT_RE = re.compile(
    r"states?\s+its\s+range\s+in\s+whole\s+degrees\s+(Celsius|Fahrenheit)",
    re.IGNORECASE,
)
_HALF_UP_RE = re.compile(
    r"exactly\s+half\s+rounded\s+up|half\s+rounded\s+up\s+to\s+the\s+next\s+higher",
    re.IGNORECASE,
)
_SPECI_EXCLUSION_RE = re.compile(r"line\s+begins\s+with\s+SPECI", re.IGNORECASE)
_SOURCE_PUBLISHED_HERE_RE = re.compile(
    r"resolution\s+source\s+for\s+this\s+market\s+will\s+be\s+the\s+routine\s+"
    r"observations\s+for\s+[\w\s]+?\(\w+\)\s+published\s+here:\s*(https?://\S+)",
    re.IGNORECASE,
)
# The new family words its no-data clause differently ("...neither source has
# published data sufficient to determine a temperature... resolve to the
# lowest bracket"); the old anchor ("no data for the observation date")
# does not match it, and the clause is the one fact a zero-reading day turns
# on, so it gets its own, direct pattern.
_NO_DATA_LOWEST_RE = re.compile(
    r"resolve\s+to\s+the\s+lowest\s+bracket", re.IGNORECASE
)

# Station abbreviations we are willing to conclude from a URL alone. Anything
# outside this set is reported as unidentified rather than assumed.
_KNOWN_STATION_FROM_URL = {"klga": "KLGA", "knyc": "KNYC"}


@dataclass(frozen=True, slots=True)
class ParsedRules:
    """Structured settlement terms extracted from one event's rules text."""

    rules_text: str
    rules_sha256: str

    station: str | None = None
    station_source: str | None = None        # 'rules_text' | 'rules_url' | None
    statistic_definition: str | None = None  # verbatim phrase
    observation_field: str | None = None     # e.g. 'Temp column, hourly data'
    timezone_name: str | None = None         # IANA if established
    timezone_hint: str | None = None         # raw, e.g. '11:59 PM ET'
    unit: str | None = None                  # the unit the BRACKETS are quoted in
    precision_deg: float | None = None       # the observation's granularity, in unit
    rounding: str | None = None              # 'nearest_integer_f' | 'half_up_f' | None

    primary_source_name: str | None = None
    primary_source_url: str | None = None
    fallback_source_name: str | None = None
    fallback_source_url: str | None = None
    no_data_resolution: str | None = None
    resolve_by_rule: str | None = None
    resolution_condition: str | None = None

    parse_status: str = "unparsed"           # 'parsed' | 'partial' | 'unparsed'
    parse_notes: tuple[str, ...] = field(default_factory=tuple)

    # --- fields that only some families set (kept out of parsed_to_row) -----
    # The unit the STATION reports in: degF for the NOAA/WU families, degC
    # for the METAR family, whose brackets are still quoted in degF. Defaults
    # to `unit` so older callers see one unit everywhere.
    observation_unit: str | None = None
    # 'routine_excluding_speci' for the METAR family: a report whose line
    # begins with SPECI does not count. None = every reading counts.
    observation_filter: str | None = None
    # Which wording family produced this parse; drives the verifier's
    # reading-extraction path. 'metar_t_group' | None (legacy families).
    statistic_family: str | None = None
    # The version of THIS parser that produced the parse - recorded on the
    # result so a persisted verdict always says which rules version it ran
    # under (local acceptance: this used to be read off an attribute that
    # did not exist and landed in the DB as NULL).
    parser_version: str = RULES_PARSER_VERSION

    @property
    def missing_fields(self) -> list[str]:
        required = (
            "station",
            "statistic_definition",
            "unit",
            "precision_deg",
            "primary_source_name",
        )
        return [name for name in required if getattr(self, name) is None]


def parse_rules_text(text: str | None, *, event_id: str = "") -> ParsedRules:
    """Parse one event's rules text.

    Never raises on malformed input: an empty or unparseable text produces
    ``parse_status='unparsed'`` with notes, because dropping such an event
    would silently reduce coverage while making the remainder look cleaner.
    """
    if not text or not text.strip():
        return ParsedRules(
            rules_text=text or "",
            rules_sha256=hashlib.sha256((text or "").encode("utf-8")).hexdigest(),
            parse_status="unparsed",
            parse_notes=("empty rules text",),
        )

    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    notes: list[str] = []
    flat = " ".join(text.split())

    # --- station -----------------------------------------------------------
    station: str | None = None
    station_source: str | None = None
    m = _NOAA_URL_RE.search(flat)
    if m:
        station = _KNOWN_STATION_FROM_URL.get(m.group(1).lower())
        station_source = "rules_url" if station else None
        if station is None:
            notes.append(f"NOAA URL names site={m.group(1)!r}, which is not a known station")
    if station is None:
        m = _WUNDERGROUND_URL_RE.search(flat)
        if m:
            station = _KNOWN_STATION_FROM_URL.get(m.group(1).lower())
            station_source = "rules_url" if station else None
    if station is None:
        m = _STATION_RE.search(flat)
        if m:
            token = m.group(1).upper().replace(" ", "")
            station = {"LAGUARDIA": "KLGA", "LAGUARDIAAIRPORT": "KLGA",
                       "LAGUARDIAAIRPORTSTATION": "KLGA", "KLGA": "KLGA",
                       "KNYC": "KNYC", "CENTRALPARK": "KNYC"}.get(token)
            station_source = "rules_text" if station else None
            if station is None:
                notes.append(f"rules name {m.group(1)!r}, which is not a known station")
    if station is None:
        notes.append("no station identified in rules text or resolution URL")

    # --- family first: the current METAR / T-group wording ------------------
    # Detected by its statistic clause, which neither legacy family uses.
    # Everything this branch sets is what the observation verifier needs:
    # the station reports °C in tenths from the T-group, the brackets are
    # quoted in whole °F, conversion rounds exactly-half UP, SPECI reports
    # do not count, and the resolution source is the METAR feed itself.
    # Running this BEFORE the legacy blocks keeps their "could not find..."
    # notes off a successful METAR parse.
    family: str | None = None
    observation_unit: str | None = None
    observation_filter: str | None = None
    unit: str | None = None
    precision: float | None = None
    rounding: str | None = None
    primary_name: str | None = None
    primary_url: str | None = None
    fallback_name: str | None = None
    fallback_url: str | None = None

    if _METAR_STATISTIC_RE.search(flat):
        family = "metar_t_group"

    # --- statistic ---------------------------------------------------------
    statistic: str | None = None
    m = _HIGHEST_TEMP_RE.search(flat)
    if m:
        # Keep the whole sentence fragment so the claim stays auditable.
        start = max(0, m.start() - 40)
        statistic = flat[start:m.end() + 60].strip()
    elif family == "metar_t_group":
        statistic = " ".join(_METAR_STATISTIC_RE.search(flat).group(0).split())
    else:
        notes.append("could not find a 'highest temperature recorded' clause")

    observation_field: str | None = None
    if _TEMP_COLUMN_RE.search(flat) or _TEMP_COLUMN_ALT_RE.search(flat):
        observation_field = "NOAA hourly Temp column"
    elif family == "metar_t_group":
        observation_field = "METAR hourly temperature remark (T-group), body group as fallback"
    elif "hourly" in flat.lower():
        observation_field = "hourly data (column unspecified)"
        notes.append("hourly data mentioned but the Temp column is not named explicitly")

    if family == "metar_t_group":
        m_tenths = _TENTHS_UNIT_RE.search(flat)
        if m_tenths:
            observation_unit = "degC" if m_tenths.group(1).lower().startswith("c") else "degF"
            precision = 0.1
        else:
            notes.append("METAR family without the 'tenths of a degree' clause")
        m_bracket = _BRACKET_UNIT_RE.search(flat)
        if m_bracket:
            unit = "degF" if m_bracket.group(1).lower().startswith("f") else "degC"
        else:
            notes.append("METAR family without a 'range in whole degrees' clause")
        if _HALF_UP_RE.search(flat):
            rounding = "half_up_f"
        else:
            notes.append("METAR family without an explicit rounding clause")
        if _SPECI_EXCLUSION_RE.search(flat):
            observation_filter = "routine_excluding_speci"
        m_source = _SOURCE_PUBLISHED_HERE_RE.search(flat)
        if m_source:
            primary_name, primary_url = "METAR", m_source.group(1)
        else:
            # The feed is the source even where the sentence shape differs;
            # naming it 'METAR' says what the family resolves from.
            primary_name = "METAR"
            notes.append("METAR family: resolution-source URL not captured verbatim")

    # --- unit and precision (legacy families) -------------------------------
    if family is None:
        m = _WHOLE_DEG_RE.search(flat)
        if m:
            unit = "degF" if m.group(1).lower().startswith("f") else "degC"
            precision = 1.0
            rounding = "nearest_integer_f"
        else:
            m = _UNIT_RE.search(flat)
            if m:
                token = m.group(0).lower()
                unit = "degF" if ("f" in token and "c" not in token.replace("f", "")) else None
                if unit is None:
                    unit = "degF" if "fahrenheit" in token else "degC"
                notes.append("unit present but no explicit precision/rounding clause found")
            else:
                notes.append("no temperature unit stated")

    # --- sources (legacy families) ------------------------------------------
    if family is None:
        if m := _NOAA_URL_RE.search(flat):
            primary_url = m.group(0)
            primary_name = "NOAA"
        if m := _WUNDERGROUND_URL_RE.search(flat):
            wu_url = m.group(0)
            if primary_name is None:
                primary_name, primary_url = "WEATHER_UNDERGROUND", wu_url
            else:
                fallback_name, fallback_url = "WEATHER_UNDERGROUND", wu_url

        if m := _SOURCE_FROM_HERE_RE.search(flat):
            named = "NOAA" if m.group(1).upper().startswith("NOAA") else "WEATHER_UNDERGROUND"
            if primary_name is None:
                primary_name = named
            elif named != primary_name and fallback_name is None:
                fallback_name = named

        if m := _FALLBACK_RE.search(flat):
            fb = "NOAA" if m.group(2).upper().startswith("NOAA") else "WEATHER_UNDERGROUND"
            if fallback_name is None:
                fallback_name = fb

        if primary_name is None:
            notes.append("no resolution source named")
    if fallback_name is not None and fallback_url is None and fallback_name == "WEATHER_UNDERGROUND":
        fallback_url = None  # present in prose only; recorded but not fabricated

    # --- no-data resolution ------------------------------------------------
    no_data: str | None = None
    if m := _NO_DATA_RE.search(flat):
        no_data = " ".join(m.group(1).split())
    elif _NO_DATA_LOWEST_RE.search(flat):
        no_data = "the lowest bracket"

    # --- timing ------------------------------------------------------------
    resolve_by: str | None = None
    if m := _RESOLVE_WHEN_RE.search(flat):
        resolve_by = m.group(1).strip()

    tz_name: str | None = None
    tz_hint: str | None = None
    if m := _ET_DEADLINE_RE.search(flat):
        tz_hint = m.group(1)
        # The text says "ET", which is a region rather than a fixed offset.
        # It is only safe to bind to the IANA zone when the market is a US
        # local-civil-date market, which the station/URL already implies.
        tz_name = "America/New_York" if station in (None, "KLGA", "KNYC") else None
        if tz_name is None:
            notes.append("'ET' deadline present but the station is unidentified")
    else:
        notes.append("no explicit deadline/timezone clause found")

    # --- verdict -----------------------------------------------------------
    required_present = [
        station is not None,
        statistic is not None,
        unit is not None,
        precision is not None,
        primary_name is not None,
    ]
    if all(required_present):
        status = "parsed"
    elif any(required_present):
        status = "partial"
    else:
        status = "unparsed"

    return ParsedRules(
        rules_text=text,
        rules_sha256=sha,
        station=station,
        station_source=station_source,
        statistic_definition=statistic,
        observation_field=observation_field,
        timezone_name=tz_name,
        timezone_hint=tz_hint,
        unit=unit,
        precision_deg=precision,
        rounding=rounding,
        primary_source_name=primary_name,
        primary_source_url=primary_url,
        fallback_source_name=fallback_name,
        fallback_source_url=fallback_url,
        no_data_resolution=no_data,
        resolve_by_rule=resolve_by,
        resolution_condition=None,
        parse_status=status,
        parse_notes=tuple(notes),
        observation_unit=observation_unit or unit,
        observation_filter=observation_filter,
        statistic_family=family,
        parser_version=RULES_PARSER_VERSION,
    )


def parsed_to_row(parsed: ParsedRules, *, event_id: str, target_date, slug: str | None,
                  parser_version: str = RULES_PARSER_VERSION) -> tuple:
    """Map a ParsedRules onto the settlement_rules column order."""
    return (
        event_id,
        target_date,
        slug,
        parsed.station,
        parsed.station_source,
        parsed.statistic_definition,
        parsed.observation_field,
        parsed.timezone_name,
        parsed.timezone_hint,
        parsed.unit,
        parsed.precision_deg,
        parsed.rounding,
        parsed.primary_source_name,
        parsed.primary_source_url,
        parsed.fallback_source_name,
        parsed.fallback_source_url,
        parsed.no_data_resolution,
        parsed.resolve_by_rule,
        parsed.resolution_condition,
        parsed.rules_text,
        parsed.rules_sha256,
        parsed.parse_status,
        "; ".join(parsed.parse_notes) or None,
        parser_version,
        datetime.now(tz=UTC),
    )


SETTLEMENT_RULES_COLUMNS = (
    "event_id, target_date, slug, station, station_source, statistic_definition, "
    "observation_field, timezone_name, timezone_hint, unit, precision_deg, rounding, "
    "primary_source_name, primary_source_url, fallback_source_name, fallback_source_url, "
    "no_data_resolution, resolve_by_rule, resolution_condition, rules_text, rules_sha256, "
    "parse_status, parse_notes, parser_version, parsed_at_utc"
)

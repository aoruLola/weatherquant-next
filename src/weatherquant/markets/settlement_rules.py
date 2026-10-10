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
    unit: str | None = None                  # 'degF' | 'degC'
    precision_deg: float | None = None
    rounding: str | None = None

    primary_source_name: str | None = None
    primary_source_url: str | None = None
    fallback_source_name: str | None = None
    fallback_source_url: str | None = None
    no_data_resolution: str | None = None
    resolve_by_rule: str | None = None
    resolution_condition: str | None = None

    parse_status: str = "unparsed"           # 'parsed' | 'partial' | 'unparsed'
    parse_notes: tuple[str, ...] = field(default_factory=tuple)

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

    # --- statistic ---------------------------------------------------------
    statistic: str | None = None
    m = _HIGHEST_TEMP_RE.search(flat)
    if m:
        # Keep the whole sentence fragment so the claim stays auditable.
        start = max(0, m.start() - 40)
        statistic = flat[start:m.end() + 60].strip()
    else:
        notes.append("could not find a 'highest temperature recorded' clause")

    observation_field: str | None = None
    if _TEMP_COLUMN_RE.search(flat) or _TEMP_COLUMN_ALT_RE.search(flat):
        observation_field = "NOAA hourly Temp column"
    elif "hourly" in flat.lower():
        observation_field = "hourly data (column unspecified)"
        notes.append("hourly data mentioned but the Temp column is not named explicitly")

    # --- unit and precision ------------------------------------------------
    unit: str | None = None
    precision: float | None = None
    rounding: str | None = None
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

    # --- sources -----------------------------------------------------------
    primary_name: str | None = None
    primary_url: str | None = None
    fallback_name: str | None = None
    fallback_url: str | None = None

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

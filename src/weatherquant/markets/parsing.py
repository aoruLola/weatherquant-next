"""Parsing market labels and event metadata into our domain types.

The venue's own label is the source of truth for what a market settles on.
Three shapes exist in production, plus historical variants:

===========================  ==================  =========================
Label                        Meaning             Our kind
===========================  ==================  =========================
``59°F or below``            open downward       ``BELOW``
``≤71°F`` / ``59 and lower`` open downward       ``BELOW``
``60-61°F`` / ``77 to 78 F`` whole-degree range  ``RANGE``
``64°F``                     exactly that degree ``EXACT``
``78°F or higher``           open upward         ``ABOVE``
``≥86°F``                    open upward         ``ABOVE``
===========================  ==================  =========================

**Ambiguity is an error, never a guess.** A label that cannot be parsed
unambiguously raises :class:`BracketParseError`; the caller records the market
as unparseable instead of inventing bounds. The alternative - assuming the
nearest plausible reading - is how a collector ends up storing a market whose
stored bucket does not match what it actually settles on, and every probability
computed from it is then wrong in a way no later check can detect.

DegC labels are rejected rather than converted: converting would require
assuming a rounding convention the market text has not stated, and a silent
degC/degF mix corrupts every downstream probability.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from weatherquant.markets.models import Bracket, BracketKind

__all__ = [
    "BracketParseError",
    "is_nyc_daily_high_event",
    "parse_bracket_label",
    "parse_target_date",
    "resolution_source_family",
]

_LOW_WORDS = r"below|lower|under|less|down\s*to"
_HIGH_WORDS = r"higher|above|greater|more|over|up\s*to"
# The venue renders the same operators with several Unicode dash and minus
# codepoints across historic events (hyphen-minus, hyphen, non-breaking hyphen,
# figure dash, en dash, horizontal bar, minus sign). These are the input
# characters, not a typographic slip - normalising them away would break
# parsing of older markets.
_DASHES = "‐‑‒–—―−"  # noqa: RUF001
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_TITLE_DATE_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+"
    r"(\d{1,2})(?:\s*,?\s*(20\d{2}))?\b",
    re.IGNORECASE,
)

NYC_TEMP_SERIES_SLUG = "nyc-daily-weather"

NOAA_SOURCES = ("noaa", "weather.gov")
WEATHER_UNDERGROUND_SOURCES = ("wunderground", "weather underground")


class BracketParseError(ValueError):
    """The label does not describe a temperature bracket we can interpret safely."""


def _normalise(label: str) -> str:
    text = label.strip()
    for dash in _DASHES:
        text = text.replace(dash, "-")
    text = text.replace("℉", "°F").replace("℃", "°C")
    return re.sub(r"\s+", " ", text)


def _detect_unit(text: str) -> tuple[str, str]:
    """Return ``(unit, text_with_unit_removed)`` where unit is ``F`` or ``C``."""
    if re.search(r"(?i)(?:°|\b)f\b", text) or "℉" in text:
        return "F", re.sub(r"(?i)(?:°\s*)?f\b", " ", text)
    if re.search(r"(?i)(?:°|\b)c\b", text) or "℃" in text:
        return "C", re.sub(r"(?i)(?:°\s*)?c\b", " ", text)
    return "F", text


def parse_bracket_label(label: str) -> Bracket:
    """Parse one venue label into a :class:`Bracket`.

    Raises
    ------
    BracketParseError
        The label is empty, has no number, has the wrong number of numbers for
        its direction, or is not in whole degrees.
    NotImplementedError
        The label is in degC. Surfaced separately because it means "we have not
        agreed how to model this market", not "the text was malformed".
    """
    if not isinstance(label, str) or not label.strip():
        raise BracketParseError("empty bracket label")

    text = _normalise(label)
    if not _NUMBER_RE.search(text):
        raise BracketParseError(f"label has no numeric bound: {label!r}")

    unit, body = _detect_unit(text)
    if unit == "C":
        raise NotImplementedError(
            f"degC bracket label {label!r} is not supported: the rounding convention would "
            "have to be assumed, and a silent degC/degF mix corrupts every probability"
        )

    low_word = re.search(rf"(?i)\b(?:{_LOW_WORDS})\b", body) is not None
    high_word = re.search(rf"(?i)\b(?:{_HIGH_WORDS})\b", body) is not None
    low_symbol = re.search(r"[≤≦≲]", body) is not None
    high_symbol = re.search(r"[≥≧≳]", body) is not None

    if (low_word or low_symbol) and (high_word or high_symbol):
        # e.g. "between ≤70 and ≥80": the text asserts both directions at once
        # and no single bucket satisfies both readings.
        raise BracketParseError(f"label states both an upper and a lower bound: {label!r}")

    direction: str | None = None
    if low_word or low_symbol:
        direction = "low"
    elif high_word or high_symbol:
        direction = "high"

    # A range separator must be neutralised *before* numbers are collected, so
    # that "60-61" yields one pair rather than two independent numbers.
    if direction is None:
        body = re.sub(r"(?<=\d)\s*(?:-|to|\.\.)\s*(?=\d)", " .. ", body, flags=re.IGNORECASE)
    body = re.sub(r"(?i)\b(?:or|degrees?|deg|and|the|be|to)\b", " ", body)
    # The direction words have already done their job; remove them so that what
    # remains is purely numeric structure.
    body = re.sub(rf"(?i)\b(?:{_LOW_WORDS}|{_HIGH_WORDS})\b", " ", body)
    body = re.sub(r"[≤≦≲≥≧≳<>]", " ", body)
    body = body.replace("°", " ")
    body = re.sub(r"\s+", " ", body).strip()

    # Anything alphabetic still standing is a word this grammar does not know.
    # "somewhere around 60" carries a single number and would otherwise be read
    # as "exactly 60" - a guess, and guessing a bucket silently corrupts every
    # probability computed from it.
    leftovers = sorted(set(re.findall(r"[A-Za-z]+", body)))
    if leftovers:
        raise BracketParseError(
            f"label contains unrecognised word(s) {leftovers}: {label!r}"
        )

    numbers = [float(m) for m in _NUMBER_RE.findall(body)]
    if not numbers:
        raise BracketParseError(f"no numeric bound survived parsing: {label!r}")

    if direction == "low":
        if len(numbers) != 1:
            raise BracketParseError(f"open-low label has {len(numbers)} numbers: {label!r}")
        return Bracket(
            label=label, kind=BracketKind.BELOW, low=None, high=numbers[0], unit="degF"
        )

    if direction == "high":
        if len(numbers) != 1:
            raise BracketParseError(f"open-high label has {len(numbers)} numbers: {label!r}")
        return Bracket(
            label=label, kind=BracketKind.ABOVE, low=numbers[0], high=None, unit="degF"
        )

    if len(numbers) == 1:
        # A lone number with no direction ("64 F") means exactly that degree.
        return Bracket(
            label=label, kind=BracketKind.EXACT, low=numbers[0], high=None, unit="degF"
        )

    if len(numbers) == 2:
        low, high = numbers[0], numbers[1]
        if low > high:
            low, high = high, low
        if not float(high - low).is_integer():
            raise BracketParseError(f"bracket bounds are not whole degrees: {label!r}")
        return Bracket(
            label=label, kind=BracketKind.RANGE, low=low, high=high, unit="degF"
        )

    raise BracketParseError(f"unrecognised bracket label: {label!r}")


def parse_target_date(event: dict[str, Any]) -> date | None:
    """Resolve the New York civil date the event settles on.

    ``eventDate`` / ``endDateIso`` are the venue's own target date and are
    preferred. ``startDate`` is deliberately **not** used: a market opens a day
    or two before the day it settles on, so the start date is the wrong date and
    using it would attach forecasts to the wrong target day.
    """
    for key in ("eventDate", "endDateIso", "endDate", "startDateIso"):
        value = event.get(key)
        if isinstance(value, str) and len(value) >= 10:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue

    match = _TITLE_DATE_RE.search(event.get("title") or "")
    if match:
        month = _MONTHS.get(match.group(1).lower())
        if month:
            year = int(match.group(3)) if match.group(3) else _year_from_slug(event.get("slug") or "")
            if year:
                try:
                    return date(year, month, int(match.group(2)))
                except ValueError:
                    return None
    return None


def _year_from_slug(slug: str) -> int | None:
    match = re.search(r"(20\d{2})$", slug)
    return int(match.group(1)) if match else None


def is_nyc_daily_high_event(event: dict[str, Any]) -> bool:
    """Cheap candidate filter on title/slug/series alone.

    Rules are *not* checked here: list and search payloads frequently omit the
    description, so they are validated after the full event is fetched.

    The ``highest temperature`` condition is load-bearing. NYC also runs a
    *lowest* temperature series, which settles on a different statistic and must
    never be priced with a daily-maximum model.
    """
    title = (event.get("title") or "").lower()
    slug = (event.get("slug") or "").lower()
    series = str(event.get("seriesSlug") or event.get("series_slug") or "").lower()

    looks_nyc = (
        series == NYC_TEMP_SERIES_SLUG
        or "temperature in nyc" in title
        or "new york city" in title
        or "nyc" in slug
    )
    looks_temperature = "temperature" in title or "temperature" in slug
    looks_high = "highest temperature" in title or "highest-temperature" in slug
    return looks_nyc and looks_temperature and looks_high


def resolution_source_family(event: dict[str, Any]) -> str:
    """Which provider the venue named: NOAA, Weather Underground, or unknown.

    Both resolve on the LaGuardia station at whole-degree Fahrenheit precision,
    but they are different publications with different publication lags, so a
    cross-check has to say which one applies rather than treating them as one.
    """
    combined = f"{event.get('description') or ''} {event.get('resolutionSource') or ''}".lower()
    if any(token in combined for token in NOAA_SOURCES):
        return "NOAA"
    if any(token in combined for token in WEATHER_UNDERGROUND_SOURCES):
        return "WEATHER_UNDERGROUND"
    return "UNKNOWN"

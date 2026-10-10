"""Discovery of NYC/KLGA daily-high-temperature events from the Gamma API.

Discovery is deliberately two-pass:

1. a **cheap listing** (tag / search) to find candidate events - these payloads
   frequently omit ``description``, so nothing that needs the rules is decided
   here;
2. a **full fetch** per candidate, which is where the rules text is read and
   where bracket parsing happens.

Everything the collector stores is derived from the venue's own payload, which
is archived verbatim alongside the parsed row. If a parsed row later looks
wrong, the raw event is what lets us tell "the venue said something odd" from
"we parsed it wrongly".
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from weatherquant.data.http import ResilientHttp
from weatherquant.markets.parsing import (
    NYC_TEMP_SERIES_SLUG,
    BracketParseError,
    is_nyc_daily_high_event,
    parse_bracket_label,
    parse_target_date,
    resolution_source_family,
)

log = logging.getLogger(__name__)

__all__ = [
    "DiscoveredEvent",
    "GammaDiscovery",
    "build_event_row",
    "build_market_rows",
    "detect_station",
]

GAMMA_BASE = "https://gamma-api.polymarket.com"
WEATHER_TAG_ID = 84

_ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
# Word-bounded: matching bare "lga" as a substring would hit unrelated words.
_STATION_HINTS = (
    (re.compile(r"\blaguardia\b|\bla\s+guardia\b|\blga\b", re.IGNORECASE), "KLGA"),
    (re.compile(r"\bknyc\b", re.IGNORECASE), "KNYC"),
)


@dataclass
class DiscoveredEvent:
    """One full Gamma event plus the rows it produces."""

    raw: dict[str, Any]
    fetched_at_utc: datetime
    event_row: tuple | None = None
    market_rows: list[tuple] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        return str(self.raw.get("id", ""))


def _safe_json_list(value: Any) -> list[Any]:
    """Gamma returns several list-valued fields as JSON *strings*."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def detect_station(text: str, *, default: str) -> str:
    """Which station the rules actually name.

    Audited open-source bots shipped with a hardcoded station table that named
    ``KNYC`` while the market resolved on ``KLGA`` (see
    ``docs/open-source-review.md`` §5.3). So the rules text is consulted first
    and the configured default is only a fallback - and a fallback that had to
    be used is visible in the problems list rather than silently applied.
    """
    haystack = text or ""
    for pattern, station in _STATION_HINTS:
        if pattern.search(haystack):
            return station
    for candidate in _ICAO_RE.findall(haystack):
        if candidate.startswith("K"):
            return candidate
    return default


class GammaDiscovery:
    """Read-only Gamma API access for weather events."""

    def __init__(self, http: ResilientHttp, *, default_station: str) -> None:
        self._http = http
        self.default_station = default_station

    def iter_candidates(self, *, limit_per_page: int = 100, max_pages: int = 50) -> Iterator[dict]:
        """Yield candidate event stubs from the weather tag, then keyword search.

        The tag is the primary path; the keyword search is a belt-and-braces
        fallback because some temperature markets hang off other tags, and a
        missed market is silent data loss we would never notice.
        """
        seen: set[str] = set()
        for source in ("series", "tag", "search"):
            for event in self._list_events(source, limit_per_page=limit_per_page, max_pages=max_pages):
                event_id = str(event.get("id", ""))
                if not event_id or event_id in seen:
                    continue
                seen.add(event_id)
                yield event

    def _list_events(self, source: str, *, limit_per_page: int, max_pages: int) -> Iterator[dict]:
        if source == "series":
            # The venue's own series for NYC daily temperature: precise, stable
            # across dates, and not dependent on a tag staying attached.
            base_params: dict[str, Any] = {
                "series_slug": NYC_TEMP_SERIES_SLUG,
                "closed": "false",
                "limit": limit_per_page,
            }
            url = f"{GAMMA_BASE}/events"
            limiter_name = "gamma_events"
        elif source == "tag":
            base_params = {
                "tag_id": WEATHER_TAG_ID,
                "closed": "false",
                "limit": limit_per_page,
                "order": "endDate",
                "ascending": "true",
            }
            url = f"{GAMMA_BASE}/events"
            limiter_name = "gamma_events"
        else:
            base_params = {"q": "highest temperature nyc", "limit": limit_per_page}
            url = f"{GAMMA_BASE}/public-search"
            limiter_name = "gamma_search"

        offset = 0
        for _page in range(max_pages):
            params = {**base_params, "offset": offset}
            payload = self._http.get_json(url, params=params, limiter=limiter_name)
            events = payload if isinstance(payload, list) else (payload.get("data") or payload.get("events") or [])
            if not events:
                return
            yield from events
            if len(events) < limit_per_page:
                return
            offset += limit_per_page

    def fetch_full_event(self, event_id: str) -> dict[str, Any] | None:
        payload = self._http.get_json(f"{GAMMA_BASE}/events/{event_id}", limiter="gamma_events")
        return payload if isinstance(payload, dict) else None

    # -- row construction --------------------------------------------------

    def discover(self) -> Iterator[DiscoveredEvent]:
        """Full pipeline: candidate -> full fetch -> domain rows."""
        for stub in self.iter_candidates():
            if not is_nyc_daily_high_event(stub):
                continue
            event_id = str(stub.get("id", ""))
            try:
                full = self.fetch_full_event(event_id) or stub
            except Exception as exc:
                log.warning("could not fetch event %s: %s", event_id, exc)
                continue
            if not is_nyc_daily_high_event(full):
                continue
            fetched_at = datetime.now(tz=UTC)
            event_row, market_rows, problems = build_market_rows(
                full, fetched_at_utc=fetched_at, default_station=self.default_station
            )
            if event_row is None:
                log.debug("event %s produced no row: %s", event_id, problems)
                continue
            yield DiscoveredEvent(
                raw=full,
                fetched_at_utc=fetched_at,
                event_row=event_row,
                market_rows=market_rows,
                problems=problems,
            )


def build_event_row(event: dict[str, Any], *, fetched_at_utc: datetime) -> tuple | None:
    event_id = str(event.get("id") or "")
    if not event_id:
        return None
    rules = str(event.get("description") or "")
    return (
        event_id,
        str(event.get("slug") or ""),
        str(event.get("title") or ""),
        parse_target_date(event),
        str(event.get("resolutionSource") or ""),
        rules,
        fetched_at_utc,
        json.dumps(event, sort_keys=True, default=str),
        "gamma",
        None,  # run_id, filled in by the worker
    )


def build_market_rows(
    event: dict[str, Any],
    *,
    fetched_at_utc: datetime,
    default_station: str,
) -> tuple[tuple | None, list[tuple], list[str]]:
    """Build ``(event_row, market_rows, problems)`` for one full event.

    Parse quality and lifecycle are kept separate: a closed market keeps its
    parsed brackets so settlement and backtests can still use it, while being
    unambiguously unavailable for new orders.
    """
    event_id = str(event.get("id") or "")
    event_row = build_event_row(event, fetched_at_utc=fetched_at_utc)
    problems: list[str] = []
    if event_row is None:
        return None, [], ["event has no id"]

    rules = str(event.get("description") or "")
    event_resolution = str(event.get("resolutionSource") or "")
    target_date = parse_target_date(event)
    station = detect_station(f"{rules} {event_resolution}", default=default_station)
    if station != default_station:
        problems.append(
            f"rules name station {station} while the configured default is {default_station}; "
            "the venue's station wins and this difference is recorded, not silently absorbed"
        )
    family = resolution_source_family(event)
    if family == "UNKNOWN":
        problems.append("resolution source is neither NOAA nor Weather Underground")
    if target_date is None:
        problems.append("could not determine the target civil date")

    raw_markets = event.get("markets") or []
    if not raw_markets:
        problems.append("event has no child markets")

    rows: list[tuple] = []
    parsed_count = 0
    for market in raw_markets:
        market_id = str(market.get("id") or "")
        if not market_id:
            problems.append("a child market has no id")
            continue

        label = str(market.get("groupItemTitle") or "")
        outcomes = _safe_json_list(market.get("outcomes"))
        token_ids = _safe_json_list(market.get("clobTokenIds"))
        yes_index = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
        yes_token = str(token_ids[yes_index]) if token_ids and yes_index < len(token_ids) else ""
        no_token = str(token_ids[1 - yes_index]) if token_ids and len(token_ids) > 1 else ""

        fee_schedule = market.get("feeSchedule") or {}
        market_rules = str(market.get("description") or rules)
        market_station = detect_station(market_rules, default=station)

        bracket_kind = "range"
        bracket_low: float | None = None
        bracket_high: float | None = None
        bracket_rounding = "nearest_integer_f"
        market_problem: str | None = None

        try:
            bracket = parse_bracket_label(label)
            bracket_kind = bracket.kind.value
            bracket_low = bracket.low
            bracket_high = bracket.high
            bracket_rounding = bracket.rounding.value
            parsed_count += 1
        except NotImplementedError as exc:
            market_problem = str(exc)
        except BracketParseError as exc:
            market_problem = str(exc)

        if not yes_token:
            market_problem = market_problem or "market has no YES clob token id"
        if target_date is None:
            market_problem = market_problem or "no target date"
        if market_problem:
            problems.append(f"{label or market_id}: {market_problem}")

        if bracket_kind == "exact":
            # EXACT is fully described by `low`; leaving `high` NULL keeps the
            # row honest about having only one bound.
            pass

        rows.append(
            (
                market_id,
                event_id,
                str(market.get("conditionId") or ""),
                str(market.get("question") or ""),
                label,
                bracket_kind,
                "degF",
                bracket_low,
                bracket_high,
                True,
                True,
                bracket_rounding,
                yes_token or "",
                no_token or "",
                target_date if target_date else date.min,
                market_station,
                float(market.get("orderPriceMinTickSize") or 0.001),
                float(market.get("orderMinSize") or 5.0),
                _opt_float(fee_schedule.get("rate")),
                _opt_float(fee_schedule.get("exponent")),
                str(market.get("resolutionSource") or event_resolution),
                market_rules,
                bool(market.get("active")),
                bool(market.get("closed")),
                bool(market.get("acceptingOrders")),
                fetched_at_utc,
                json.dumps(market, sort_keys=True, default=str),
                "gamma",
                None,  # run_id
            )
        )

    if parsed_count == 0 and raw_markets:
        problems.append("no market in this event produced a parseable bracket")

    return event_row, rows, problems


def _opt_float(value: Any) -> float | None:
    """Float or ``None``; an absent fee schedule must stay NULL, never 0.

    ``None`` means "the venue did not report a fee schedule", which is a
    different statement from "there is no fee" - conflating them would make
    every edge computed from it wrong.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

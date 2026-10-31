"""Classify raw archive lines into the four kinds the shadow engine can reason about.

The collector archives every frame verbatim, which means the archive contains
several things that all look like "a message about prices" and are not
interchangeable. The distinction this module exists to enforce:

======================  =====================================================
kind                    what it is
======================  =====================================================
``book_snapshot``       a complete L2 depth picture at one instant
``book_delta``          a single level that changed - NOT a book
``trade_print``         one print, no depth at all
``market_metadata``     markets, brackets, tokens, fees, lifecycle
======================  =====================================================

Only ``book_snapshot`` may be walked by the pricer. A ``book_delta`` describes
one level of a book whose other levels it does not carry, and there is no
sequence machinery here (or in the collector) that could safely reassemble
those into state - so a delta is recorded and never priced. Treating one as a
book is the specific error the specification forbids, and it would produce
confidently wrong fills on a book that never existed.

Parsing reuses the collector's own parser (:func:`weatherquant.data.books.
parse_book_payload`) rather than reimplementing level normalisation, so a
shadow book and a collector book of the same payload carry the same
``book_id`` and can be cross-checked by value.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from weatherquant.data.books import parse_book_payload
from weatherquant.data.discovery import detect_station
from weatherquant.markets.models import Bracket, OrderBook
from weatherquant.markets.parsing import (
    BracketParseError,
    is_nyc_daily_high_event,
    parse_bracket_label,
    parse_target_date,
)

__all__ = [
    "BookObservation",
    "CatalogMarket",
    "Envelope",
    "EventKind",
    "classify",
    "market_of_token_table",
    "parse_envelope",
    "parse_gamma",
    "parse_ws_frame",
    "rest_book",
]


class EventKind(StrEnum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE_PRINT = "trade_print"
    MARKET_METADATA = "market_metadata"
    CONTROL = "control"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Envelope:
    """One decoded archive line. ``received_at_utc`` is the *collector's* clock."""

    archive_seq: int
    source: str
    received_at_utc: datetime
    venue_ts_utc: datetime | None
    venue_ts_origin: str
    quality: str
    run_id: str
    payload: Any
    raw_sha256: str


def parse_envelope(line: str) -> Envelope:
    """Decode one archive line.

    Raises ``ValueError`` on anything malformed. A half-written line must
    never reach the tailer (it stops at the last newline precisely so this
    cannot happen), so reaching here means the line *claimed* to be complete
    and was not - that is a data-quality event, not something to skip.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"archive line is not JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"archive line is a {type(obj).__name__}, expected an object")
    missing = [k for k in ("archive_seq", "source", "received_at_utc", "payload") if k not in obj]
    if missing:
        raise ValueError(f"archive envelope is missing field(s): {missing}")

    received = datetime.fromisoformat(str(obj["received_at_utc"]))
    if received.tzinfo is None:
        raise ValueError("received_at_utc must be timezone-aware")

    venue_raw = obj.get("venue_ts_utc")
    venue_ts = datetime.fromisoformat(venue_raw) if venue_raw else None
    return Envelope(
        archive_seq=int(obj["archive_seq"]),
        source=str(obj["source"]),
        received_at_utc=received,
        venue_ts_utc=venue_ts,
        venue_ts_origin=str(obj.get("venue_ts_origin") or ("venue" if venue_ts else "missing")),
        quality=str(obj.get("quality") or ""),
        run_id=str(obj.get("run_id") or ""),
        payload=obj["payload"],
        raw_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
    )


def classify(source: str, payload: Any) -> EventKind:
    """Decide what a payload is. Never infers depth that is not present."""
    if source == "gamma":
        return EventKind.MARKET_METADATA
    if source == "rest":
        # The collector only archives a `rest` line after parsing it into a
        # snapshot, so the presence of a rest line *is* the completeness claim.
        return EventKind.BOOK_SNAPSHOT if isinstance(payload, dict) else EventKind.UNKNOWN
    if source == "ws":
        if isinstance(payload, list):
            # An array frame is the initial full-book dump, one element per token.
            return EventKind.BOOK_SNAPSHOT
        if isinstance(payload, dict):
            kind = str(payload.get("event_type") or "")
            if kind == "book":
                return EventKind.BOOK_SNAPSHOT
            if kind == "price_change":
                return EventKind.BOOK_DELTA
            if kind == "last_trade_price":
                return EventKind.TRADE_PRINT
            if kind == "tick_size_change":
                return EventKind.CONTROL
        return EventKind.UNKNOWN
    return EventKind.UNKNOWN


# --- market metadata -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogMarket:
    """One tradeable outcome, as the shadow engine understands it."""

    market_id: str
    event_id: str
    slug: str
    question: str
    bracket: Bracket | None
    bracket_label: str
    bracket_error: str | None
    token_yes: str
    token_no: str
    target_date: Any
    station: str
    tick_size: float
    min_order_size: float
    fee_rate: float | None
    fee_exponent: float | None
    active: bool
    closed: bool
    accepting_orders: bool
    raw_sha256: str
    source_run_id: str

    @property
    def tradeable(self) -> bool:
        return self.active and self.accepting_orders and not self.closed


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_gamma(
    payload: Any, *, default_station: str, run_id: str = ""
) -> tuple[list[CatalogMarket], list[str]]:
    """Markets from one full Gamma event, plus every problem found.

    Problems are returned, not raised: one unparseable bracket must not cost
    us the other nine markets in the same event, and a market with no bracket
    is carried with ``bracket=None`` so the decision layer can refuse it by
    name instead of never seeing it at all.
    """
    if not isinstance(payload, dict):
        return [], [f"gamma payload is {type(payload).__name__}, expected an object"]
    if not is_nyc_daily_high_event(payload):
        # Deliberately silent: discovery archives candidates it rejected too.
        return [], []

    event_id = str(payload.get("id") or "")
    if not event_id:
        return [], ["event has no id"]

    problems: list[str] = []
    rules = str(payload.get("description") or "")
    resolution = str(payload.get("resolutionSource") or "")
    target_date = parse_target_date(payload)
    station = detect_station(f"{rules} {resolution}", default=default_station)
    if target_date is None:
        problems.append(f"event {event_id}: could not determine the target civil date")

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    markets: list[CatalogMarket] = []
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            problems.append(f"event {event_id}: a child market is not an object")
            continue
        market_id = str(market.get("id") or "")
        if not market_id:
            problems.append(f"event {event_id}: a child market has no id")
            continue

        label = str(market.get("groupItemTitle") or "")
        outcomes = _json_list(market.get("outcomes"))
        token_ids = _json_list(market.get("clobTokenIds"))
        yes_index = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), 0)
        token_yes = str(token_ids[yes_index]) if token_ids and yes_index < len(token_ids) else ""
        token_no = str(token_ids[1 - yes_index]) if token_ids and len(token_ids) > 1 else ""

        bracket: Bracket | None = None
        bracket_error: str | None = None
        try:
            bracket = parse_bracket_label(label)
        except NotImplementedError as exc:
            bracket_error = str(exc)
        except BracketParseError as exc:
            bracket_error = str(exc)
        if bracket_error:
            problems.append(f"{market_id} ({label or 'no label'}): {bracket_error}")
        if not token_yes:
            problems.append(f"{market_id}: no YES token id")

        fee_schedule = market.get("feeSchedule") or {}
        market_rules = str(market.get("description") or rules)
        markets.append(
            CatalogMarket(
                market_id=market_id,
                event_id=event_id,
                slug=str(payload.get("slug") or ""),
                question=str(market.get("question") or ""),
                bracket=bracket,
                bracket_label=label,
                bracket_error=bracket_error,
                token_yes=token_yes,
                token_no=token_no,
                target_date=target_date,
                station=detect_station(market_rules, default=station),
                tick_size=float(market.get("orderPriceMinTickSize") or 0.001),
                min_order_size=float(market.get("orderMinSize") or 5.0),
                fee_rate=_opt_float(fee_schedule.get("rate")),
                fee_exponent=_opt_float(fee_schedule.get("exponent")),
                active=bool(market.get("active")),
                closed=bool(market.get("closed")),
                accepting_orders=bool(market.get("acceptingOrders")),
                raw_sha256=digest,
                source_run_id=run_id,
            )
        )
    return markets, problems


# --- order books -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BookObservation:
    """A complete book from the archive, with the clocks that must stay apart."""

    token_id: str
    market_id: str
    book: OrderBook
    book_id: str
    book_source: str
    venue_ts: datetime | None
    venue_ts_origin: str
    collector_received_at_utc: datetime
    shadow_received_at_utc: datetime
    quality: str
    source_event_id: str | None = None

    @property
    def defects(self) -> list[str]:
        return self.book.defects()

    @property
    def complete(self) -> bool:
        """Both sides present and well-formed - the minimum to price a fill."""
        return not self.defects


def rest_book(
    payload: Any,
    *,
    token_id: str,
    market_id: str,
    collector_received_at_utc: datetime,
    shadow_received_at_utc: datetime,
    run_id: str,
    book_source: str = "rest",
) -> BookObservation:
    """A REST ``/book`` payload (or an equivalent full-depth frame).

    Raises ``ValueError`` when it has no depth. Reuses the collector's parser
    so the resulting ``book_id`` is byte-identical to the one the collector
    would have stored for the same levels - which is what lets a shadow
    observation be cross-checked against the collector's own row by value.
    """
    snapshot = parse_book_payload(
        payload,
        token_id=token_id,
        market_id=market_id,
        fetched_at_utc=collector_received_at_utc,
        run_id=run_id,
        source=book_source,
    )
    return BookObservation(
        token_id=token_id,
        market_id=market_id,
        book=OrderBook(
            market_id=market_id,
            token_id=token_id,
            bids=snapshot.bids,
            asks=snapshot.asks,
            venue_ts=snapshot.venue_ts,
            fetched_at_utc=collector_received_at_utc,
        ),
        book_id=snapshot.content_hash,
        book_source=book_source,
        venue_ts=snapshot.venue_ts,
        venue_ts_origin=snapshot.venue_ts_origin,
        collector_received_at_utc=collector_received_at_utc,
        shadow_received_at_utc=shadow_received_at_utc,
        quality=snapshot.quality,
    )


def parse_ws_frame(
    payload: Any,
    *,
    market_of_token: dict[str, str],
    collector_received_at_utc: datetime,
    shadow_received_at_utc: datetime,
    run_id: str,
) -> tuple[list[BookObservation], list[tuple[EventKind, str]]]:
    """Split one WebSocket frame into snapshots and non-snapshot notes.

    Returns ``(snapshots, other)``. ``other`` carries ``(kind, detail)`` for
    deltas, prints and control frames: they are recorded as seen, and never
    become a book.
    """
    kind = classify("ws", payload)
    if kind is EventKind.UNKNOWN:
        return [], [(EventKind.UNKNOWN, "frame has no recognised shape")]

    elements: list[Any]
    if isinstance(payload, list):
        elements = [e for e in payload if isinstance(e, dict)]
    elif str(payload.get("event_type")) == "book":
        elements = [payload]
    else:
        return [], [
            (
                kind,
                "incremental or one-shot message; no sequence machinery exists to "
                "rebuild depth from it, so it is recorded and not priced"
                if kind is EventKind.BOOK_DELTA
                else f"{kind.value} carries no depth",
            )
        ]

    snapshots: list[BookObservation] = []
    notes: list[tuple[EventKind, str]] = []
    for element in elements:
        if str(element.get("event_type") or "book") != "book":
            notes.append((EventKind.UNKNOWN, f"element event_type={element.get('event_type')!r}"))
            continue
        token_id = str(element.get("asset_id") or "")
        market_id = market_of_token.get(token_id, "")
        if not token_id or not market_id:
            # A frame for a token we have no metadata for cannot be priced, but
            # it must not be mistaken for a parse failure either.
            notes.append(
                (EventKind.UNKNOWN, f"book frame for unknown token {token_id or '<missing>'}")
            )
            continue
        try:
            snapshots.append(
                rest_book(
                    element,
                    token_id=token_id,
                    market_id=market_id,
                    collector_received_at_utc=collector_received_at_utc,
                    shadow_received_at_utc=shadow_received_at_utc,
                    run_id=run_id,
                    book_source="ws",
                )
            )
        except ValueError as exc:
            notes.append((EventKind.UNKNOWN, f"book frame unusable: {exc}"))
    return snapshots, notes


def market_of_token_table(markets: list[CatalogMarket]) -> dict[str, str]:
    """``token_id -> market_id`` for both sides of every market."""
    table: dict[str, str] = {}
    for market in markets:
        if market.token_yes:
            table[market.token_yes] = market.market_id
        if market.token_no:
            table[market.token_no] = market.market_id
    return table

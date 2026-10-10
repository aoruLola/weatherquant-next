"""REST order-book snapshots: the authoritative depth record.

WebSocket book messages are best-effort (they can be missed while reconnecting);
the REST poll is what guarantees a complete depth record at a known instant. The
two cross-check each other: a persistent disagreement between the stream and the
poll is a data-quality problem worth surfacing rather than averaging away.

Normalisation
-------------
Levels are stored **best-first** (bids descending, asks ascending) regardless of
the order the venue returned. That is a normalisation, not an edit: the verbatim
payload is archived through :mod:`weatherquant.data.archive`, so a venue that
returns scrambled levels is still auditable there. Storing sorted levels means
downstream consumers never have to re-derive which end is best - and getting
that backwards is the classic way to price a trade against the wrong side.

Timestamps
----------
``venue_ts`` comes from the payload's own ``timestamp`` field when it has one,
and stays ``NULL`` when it does not. ``fetched_at_utc`` is our clock. The two are
never merged: substituting the capture time for a missing venue time would make
every latency and staleness figure downstream meaningless.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from weatherquant.data.archive import extract_venue_ts
from weatherquant.data.http import ResilientHttp
from weatherquant.data.writer import book_content_hash, canonical_levels_json
from weatherquant.markets.models import BookLevel, OrderBook

log = logging.getLogger(__name__)

__all__ = ["CLOB_BASE", "BookSnapshot", "fetch_book", "parse_book_payload"]

CLOB_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """A parsed book plus the provenance that must travel with it."""

    row: tuple
    venue_ts: datetime | None
    venue_ts_origin: str
    quality: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]

    @property
    def content_hash(self) -> str:
        return self.row[0]


def _sorted_levels(payload: Any, *, descending: bool) -> list[tuple[float, float]]:
    """Extract and order ``(price, size)`` pairs from one side of the payload.

    Malformed entries are dropped rather than raising: one bad level must not
    throw away an otherwise usable snapshot, and the raw archive keeps the
    original for diagnosis. Prices outside ``(0, 1)`` are rejected because a
    probability-shaped price outside the unit interval is corrupt, not extreme.
    """
    raw: list[tuple[float, float]] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                price = float(item["price"])
                size = float(item["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if not 0.0 < price < 1.0 or size <= 0.0:
                continue
            raw.append((price, size))
    return sorted(raw, key=lambda pair: pair[0], reverse=descending)


def parse_book_payload(
    payload: dict[str, Any],
    *,
    token_id: str,
    market_id: str,
    fetched_at_utc: datetime,
    run_id: str,
    source: str = "rest",
) -> BookSnapshot:
    """Turn a venue ``/book`` payload into a database row.

    Raises ``ValueError`` if the payload is not an object or has no usable
    levels on either side - the caller records that as a failed snapshot rather
    than storing an empty row that would later be mistaken for "the market has
    no depth at all".
    """
    if not isinstance(payload, dict):
        raise ValueError(f"book payload is {type(payload).__name__}, expected an object")

    bids_sorted = _sorted_levels(payload.get("bids"), descending=True)
    asks_sorted = _sorted_levels(payload.get("asks"), descending=False)
    if not bids_sorted and not asks_sorted:
        raise ValueError("book payload has no usable levels on either side")

    bids_json = canonical_levels_json(bids_sorted)
    asks_json = canonical_levels_json(asks_sorted)

    # `timestamp` is the venue's event time. Absent -> None, never our clock.
    venue_ts = extract_venue_ts(payload)
    venue_ts_origin = "venue" if venue_ts is not None else "missing"

    book = OrderBook(
        market_id=market_id,
        token_id=token_id,
        bids=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in bids_sorted),
        asks=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in asks_sorted),
        venue_ts=venue_ts,
        fetched_at_utc=fetched_at_utc,
    )
    defects = book.defects()
    quality = "ok" if not defects else "+".join(defects)

    best_bid = max((p for p, _ in bids_sorted), default=None)
    best_ask = min((p for p, _ in asks_sorted), default=None)
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

    content_hash = book_content_hash(token_id, bids_json, asks_json, venue_ts)
    row = (
        content_hash,
        market_id,
        token_id,
        bids_json,
        asks_json,
        best_bid,
        best_ask,
        spread,
        math.fsum(p * s for p, s in bids_sorted),
        math.fsum(p * s for p, s in asks_sorted),
        "+".join(defects),
        venue_ts,
        fetched_at_utc,
        source,
        venue_ts_origin,
        content_hash,
        quality,
        run_id,
    )
    return BookSnapshot(
        row=row,
        venue_ts=venue_ts,
        venue_ts_origin=venue_ts_origin,
        quality=quality,
        bids=book.bids,
        asks=book.asks,
    )


def fetch_book(
    http: ResilientHttp,
    *,
    token_id: str,
    market_id: str,
    fetched_at_utc: datetime,
    run_id: str,
) -> BookSnapshot:
    """Fetch and parse one token's book from the CLOB."""
    payload = http.get_json(
        f"{CLOB_BASE}/book", params={"token_id": token_id}, limiter="clob_book"
    )
    return parse_book_payload(
        payload,
        token_id=token_id,
        market_id=market_id,
        fetched_at_utc=fetched_at_utc,
        run_id=run_id,
    )

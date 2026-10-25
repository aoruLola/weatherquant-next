"""Point-in-time L2 book evidence, resolved from the collector's raw archive.

The paper engine needs the *book the decision was made against* - not the
market's current book, which by fill time has moved. The collector's archive
is the historical record of every book frame, and it is published read-only,
so the paper engine resolves evidence itself instead of asking any other
engine's database for it.

What makes a resolution honest:

**Never future data.** A frame qualifies only if the collector received it
at or before the decision instant. A frame received *after* the decision is
information the decision could not have used; filling against one would let
the simulation know something the trader did not.

**The walk is bounded.** LIVE decisions are contemporary, so the frame that
matches is near the tail of the newest day file. Each file is entered no
deeper than ``tail_bytes`` from its end - a bounded, predictable scan rather
than a full-history search. A frame that falls outside the window is not
found, and "not found" is the honest answer: the engine refuses the fill
rather than filling against a guess.

**Identity is verified, not assumed.** Levels are parsed with the collector's
own parser, so the resolved book's ``book_id`` is the same content hash the
shadow recorded on the decision. When the two disagree, the evidence does
not describe the trade, and the fill is refused rather than approximated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from weatherquant.data.books import parse_book_payload
from weatherquant.markets.models import OrderBook

__all__ = ["ArchiveBookSource", "BookEvidence"]

log = logging.getLogger("weatherquant.paper")

#: The archive's own partitioning (raw/<source>/YYYY-MM-DD.jsonl); a decision
#: may need the previous day's file when it sits shortly after midnight.
_SOURCES = ("ws", "rest")


@dataclass(frozen=True, slots=True)
class BookEvidence:
    """One resolved L2 snapshot, with the clocks that must stay apart."""

    token_id: str
    market_id: str
    book: OrderBook
    book_id: str
    book_source: str  # ws | rest
    venue_ts_utc: datetime | None
    collector_received_at_utc: datetime
    quality: str

    @property
    def defects(self) -> list[str]:
        return self.book.defects()


class ArchiveBookSource:
    """Resolve ``(token_id, decision_time)`` to the book that was current then."""

    def __init__(
        self,
        raw_dir: str | Path,
        *,
        tail_bytes: int = 4 * 1024 * 1024,
        lookback_seconds: float = 3600.0,
    ) -> None:
        if tail_bytes <= 0:
            raise ValueError(f"tail_bytes must be > 0, got {tail_bytes!r}")
        if lookback_seconds <= 0:
            raise ValueError(f"lookback_seconds must be > 0, got {lookback_seconds!r}")
        self.raw_dir = Path(raw_dir)
        self.tail_bytes = int(tail_bytes)
        self.lookback = timedelta(seconds=lookback_seconds)

    def resolve(
        self,
        *,
        token_id: str,
        decision_time: datetime,
    ) -> BookEvidence | None:
        """The newest complete book frame for the token, received no later
        than ``decision_time``. ``None`` when none is in the window.

        Candidate files: the decision day and the day before, per source,
        newest first. The first matching frame wins.
        """
        if not token_id:
            return None
        floor = decision_time - self.lookback
        for day in (decision_time.date(), (decision_time - timedelta(days=1)).date()):
            for source in _SOURCES:
                path = self.raw_dir / source / f"{day.isoformat()}.jsonl"
                if not path.is_file():
                    continue
                found = self._resolve_in_file(path, source, token_id, floor, decision_time)
                if found is not None:
                    return found
        return None

    def _resolve_in_file(
        self,
        path: Path,
        source: str,
        token_id: str,
        floor: datetime,
        decision_time: datetime,
    ) -> BookEvidence | None:
        newest: BookEvidence | None = None
        newest_received: datetime | None = None
        for envelope, payload in _iter_tail(path, self.tail_bytes):
            received = envelope.get("received_at_utc")
            if not isinstance(received, str):
                continue
            try:
                stamp = datetime.fromisoformat(received)
            except ValueError:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            # Future data is excluded, not used: a frame received after the
            # decision was not knowable when the decision was made.
            if stamp > decision_time or stamp < floor:
                continue
            if not _payload_has_book_for(payload, token_id):
                continue
            try:
                evidence = _build_evidence(
                    payload,
                    token_id=token_id,
                    source=source,
                    received_at=stamp,
                )
            except ValueError:
                # An unusable payload for our token: skip it, the previous
                # good frame for the token remains the answer.
                continue
            if newest_received is None or stamp > newest_received:
                newest, newest_received = evidence, stamp
        return newest


def _iter_tail(path: Path, tail_bytes: int):
    """Yield ``(envelope, payload)`` from the last ``tail_bytes`` of a JSONL file.

    The scan starts at a line boundary, so a frame split by the window edge
    is skipped whole rather than half-parsed.
    """
    size = path.stat().st_size
    start = max(size - tail_bytes, 0)
    with path.open("rb") as handle:
        if start:
            handle.seek(start)
            handle.readline()  # discard the partial line the window opened on
        for raw in handle:
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                envelope = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(envelope, dict):
                yield envelope, envelope.get("payload")


def _payload_has_book_for(payload: object, token_id: str) -> bool:
    """Whether this payload carries depth for ``token_id``.

    REST lines are single-token ``/book`` payloads keyed by their own field;
    WebSocket frames carry ``asset_id`` per element, or arrive as an array
    (the initial full-book dump, one element per token).
    """
    if isinstance(payload, list):
        return any(
            isinstance(element, dict)
            and str(element.get("asset_id") or "") == token_id
            and element.get("event_type") in (None, "book")
            for element in payload
        )
    if isinstance(payload, dict):
        payload_token = str(payload.get("asset_id") or payload.get("token_id") or "")
        if payload_token and payload_token != token_id:
            return False
        event_type = payload.get("event_type")
        if event_type not in (None, "book"):
            return False
        return bool(payload.get("bids") or payload.get("asks"))
    return False


def _build_evidence(
    payload: dict,
    *,
    token_id: str,
    source: str,
    received_at: datetime,
) -> BookEvidence:
    """Parse one frame with the collector's own parser.

    Reusing :func:`parse_book_payload` is what makes the resolved book's
    content hash byte-identical to the one the shadow stored for the same
    levels - the property the fill's identity check relies on.
    """
    snapshot = parse_book_payload(
        payload,
        token_id=token_id,
        market_id=str(payload.get("market_id") or payload.get("market") or ""),
        fetched_at_utc=received_at,
        run_id=str(payload.get("run_id") or ""),
        source=source,
    )
    book = OrderBook(
        market_id=snapshot.row[1],
        token_id=token_id,
        bids=snapshot.bids,
        asks=snapshot.asks,
        venue_ts=snapshot.venue_ts,
        fetched_at_utc=received_at,
    )
    return BookEvidence(
        token_id=token_id,
        market_id=book.market_id,
        book=book,
        book_id=snapshot.content_hash,
        book_source=source,
        venue_ts_utc=snapshot.venue_ts,
        collector_received_at_utc=received_at,
        quality=snapshot.quality,
    )

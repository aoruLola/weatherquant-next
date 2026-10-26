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

**Newest across every source.** REST polls and WS pushes are two windows on
the same book; neither owns the truth. The resolver compares every candidate
frame it can reach and returns the newest one received at or before the
decision - it never short-circuits on the first source that happens to
match, which is how a 20-minute-old WS frame once beat a 2-second-old REST
poll.

**The walk is bounded.** LIVE decisions are contemporary, so the frame that
matches is near the tail of the newest day file. Each file is entered no
deeper than ``tail_bytes`` from its end - a bounded, predictable scan rather
than a full-history search. A frame that falls outside the window is not
found, and "not found" is the honest answer: the engine refuses the fill
rather than filling against a guess.

**One bad frame costs one frame.** The WS initial full-book dump is a JSON
*array* (one element per token, verified live - see the collector's
``_iter_frames``), and any payload can be truncated or exotic. Every frame
is parsed inside its own failure boundary: a frame that cannot be used is
skipped, the scan continues, and the newest *good* frame remains the answer.
A resolver that aborts on one frame would stall the whole fill pass with
every signal stranded ACCEPTED - that failure mode shipped once and it is
now a regression test.

**Identity is verified, not assumed.** Levels are parsed with the collector's
own parser, so the resolved book's ``book_id`` is the same content hash the
shadow recorded on the decision. When the two disagree, the evidence does
not describe the trade, and the fill is refused rather than approximated.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
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
        than ``decision_time``, across **all** sources. ``None`` when none
        is in the window.
        """
        if not token_id:
            return None
        floor = decision_time - self.lookback
        newest: BookEvidence | None = None
        newest_received: datetime | None = None
        for day in (decision_time.date(), (decision_time - timedelta(days=1)).date()):
            for source in _SOURCES:
                path = self.raw_dir / source / f"{day.isoformat()}.jsonl"
                if not path.is_file():
                    continue
                for evidence, received in self._frames_in_file(
                    path, source, token_id, floor, decision_time
                ):
                    if newest_received is None or received > newest_received:
                        newest, newest_received = evidence, received
        return newest

    def _frames_in_file(
        self,
        path: Path,
        source: str,
        token_id: str,
        floor: datetime,
        decision_time: datetime,
    ) -> Iterator[tuple[BookEvidence, datetime]]:
        """Yield ``(evidence, received_at)`` for every usable frame in one file.

        Failures are per-frame by construction: a payload that cannot be
        parsed yields nothing and the scan moves on. One corrupted or exotic
        frame must never cost the caller its whole fill pass.
        """
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
            element = _extract_book_element(payload, token_id)
            if element is None:
                continue
            try:
                evidence = _build_evidence(
                    element,
                    token_id=token_id,
                    source=source,
                    received_at=stamp,
                )
            except (ValueError, TypeError, KeyError, IndexError):
                # An unusable payload for our token: skip it, the previous
                # good frame for the token remains the answer.
                log.debug("skipping unusable %s book frame for %s in %s", source, token_id, path)
                continue
            yield evidence, stamp


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


def _extract_book_element(payload: object, token_id: str) -> dict | None:
    """The single token's book element from a frame payload.

    REST lines and single-token WS frames are objects; the WS *initial*
    full-book dump is a JSON **array** with one element per token (verified
    live - see the collector's ``_iter_frames``), so the element carrying
    ``token_id`` is extracted and the rest of the array ignored. Anything
    that is not a usable book for this token returns ``None``.
    """
    candidates: list[object]
    if isinstance(payload, list):
        candidates = payload
    else:
        candidates = [payload]
    for element in candidates:
        if not isinstance(element, dict):
            continue
        if element.get("event_type") not in (None, "book"):
            continue
        element_token = str(element.get("asset_id") or element.get("token_id") or "")
        if element_token and element_token != token_id:
            continue
        if element.get("bids") or element.get("asks"):
            return element
    return None


def _build_evidence(
    element: dict,
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
        element,
        token_id=token_id,
        market_id=str(element.get("market_id") or element.get("market") or ""),
        fetched_at_utc=received_at,
        run_id=str(element.get("run_id") or ""),
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

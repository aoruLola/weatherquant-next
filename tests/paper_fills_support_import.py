"""Real content hash of the fixture book, for event fixtures that must match."""

from __future__ import annotations

from datetime import UTC as _UTC
from datetime import datetime as _dt

from paper_book_helpers import TOKEN, two_level_book


def real_book_id_for_seed() -> str:
    from weatherquant.data.books import parse_book_payload

    snapshot = parse_book_payload(
        two_level_book(),
        token_id=TOKEN,
        market_id="",
        fetched_at_utc=_dt.now(tz=_UTC),
        run_id="",
        source="ws",
    )
    return snapshot.content_hash

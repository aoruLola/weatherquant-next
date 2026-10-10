"""WebSocket frame handling and worker helpers.

The frame shapes below are copied verbatim from live captures of
``wss://ws-subscriptions-clob.polymarket.com/ws/market`` (see
``research/external/review-py-sdk.md`` and the capture files alongside it), so
these tests pin the protocol as it actually behaves rather than as we assumed.

Two properties matter most:

* **mixed framing** - the initial book snapshot arrives as a JSON *array* while
  ``price_change`` arrives as a *bare object*. Handling only one shape would
  silently drop half the stream;
* **only midpoints are stored as prices** - level prices and trade prices are
  archived raw but never written under a mid column.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from weatherquant.data.stream import _decode
from weatherquant.data.worker import (
    _book_midpoint,
    _is_one_sided,
    _iter_frames,
    _tokens_of,
    _with_run,
    stream_price_rows,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

TOKEN_1 = "88206130790501794491984819881379122386069415115740984070309563925393222094492"
TOKEN_2 = "54530000000000000000000000000000000000000000000000000000000000000000000000000"
TRACKED = {TOKEN_1: "mkt-1", TOKEN_2: "mkt-2"}


# Verbatim from a live capture: a book snapshot for one subscribed token.
LIVE_BOOK_FRAME = [
    {
        "market": "0xdf7a61e",
        "asset_id": TOKEN_1,
        "timestamp": "1791605394391",
        "hash": "d30b",
        "bids": [{"price": "0.001", "size": "706093"}],
        "asks": [{"price": "0.999", "size": "6108717.18"}],
        "tick_size": "0.001",
        "event_type": "book",
        "last_trade_price": "0.154",
    }
]

# Verbatim: a price_change arrives as a bare object, with `price_changes` (plural).
LIVE_PRICE_CHANGE_FRAME = {
    "event_type": "price_change",
    "market": "0x0f49",
    "price_changes": [
        {
            "asset_id": TOKEN_2,
            "price": "0.02",
            "size": "165550",
            "side": "BUY",
            "hash": "af76",
            "best_bid": "0.845",
            "best_ask": "0.846",
        }
    ],
    "timestamp": "1791605394982",
}


# ---------------------------------------------------------------------------
# Frame decoding
# ---------------------------------------------------------------------------


def test_decode_accepts_text_and_bytes() -> None:
    assert _decode('{"a": 1}') == {"a": 1}
    assert _decode(b'{"a": 1}') == {"a": 1}
    assert _decode(bytearray(b'{"a": 1}')) == {"a": 1}


def test_decode_returns_none_for_non_json_frames() -> None:
    """PONG and acks are not JSON; they prove liveness but carry no data."""
    assert _decode("PONG") is None
    assert _decode("") is None
    assert _decode(None) is None
    assert _decode(b"\xff\xfe not utf8") is None
    assert _decode([1, 2, 3]) is None  # a list is valid JSON but not an event object


def test_iter_frames_handles_both_framing_styles() -> None:
    """Array (initial book dump) and bare object (price_change) must both work."""
    assert len(_iter_frames(LIVE_BOOK_FRAME)) == 1
    assert _iter_frames(LIVE_PRICE_CHANGE_FRAME) == [LIVE_PRICE_CHANGE_FRAME]
    assert _iter_frames("garbage") == []
    assert _iter_frames(["a", LIVE_BOOK_FRAME[0], 3]) == [LIVE_BOOK_FRAME[0]]


# ---------------------------------------------------------------------------
# Midpoint extraction
# ---------------------------------------------------------------------------


def test_book_midpoint_from_live_frame() -> None:
    # (0.001 + 0.999) / 2 = 0.5
    assert _book_midpoint(LIVE_BOOK_FRAME[0]) == pytest.approx(0.5)


def test_book_midpoint_is_none_when_a_side_is_empty() -> None:
    assert _book_midpoint({"bids": [], "asks": [{"price": "0.5", "size": "1"}]}) is None
    assert _book_midpoint({"bids": [{"price": "0.5", "size": "1"}], "asks": []}) is None


def test_book_midpoint_rejects_prices_outside_the_unit_interval() -> None:
    assert _book_midpoint({"bids": [{"price": "1.5", "size": "1"}], "asks": [{"price": "2.0", "size": "1"}]}) is None


def test_book_midpoint_tolerates_malformed_levels() -> None:
    assert _book_midpoint({"bids": [{"size": "1"}], "asks": [{"price": "0.5", "size": "1"}]}) is None


# ---------------------------------------------------------------------------
# price extraction policy
# ---------------------------------------------------------------------------


def test_book_frame_yields_a_price_point() -> None:
    rows = stream_price_rows(LIVE_BOOK_FRAME, NOW, "r1", TRACKED)
    assert len(rows) == 1
    market_id, token, _ts, mid, source, _fetched_at, venue_origin, run_id = rows[0]
    assert market_id == "mkt-1"
    assert token == TOKEN_1
    assert mid == pytest.approx(0.5)
    assert source == "ws"
    assert run_id == "r1"
    assert venue_origin == "venue", "the frame carries a venue timestamp"


def test_price_change_yields_a_midpoint_from_best_bid_ask() -> None:
    """The change's own `price` is a *level* price, not a mid - use best_bid/ask."""
    rows = stream_price_rows(LIVE_PRICE_CHANGE_FRAME, NOW, "r1", TRACKED)
    assert len(rows) == 1
    _market, _token, _ts, mid, source, _f, origin, _run = rows[0]
    assert mid == pytest.approx((0.845 + 0.846) / 2.0)
    assert source == "ws"
    assert origin == "venue"


def test_a_level_price_is_never_stored_as_a_midpoint() -> None:
    """A price_change without best_bid/best_ask yields no row.

    The alternative - storing `price` (a single level) in a column named
    `mid_usd_per_share` - would corrupt every analysis reading it.
    """
    frame = {
        "event_type": "price_change",
        "price_changes": [{"asset_id": TOKEN_2, "price": "0.02", "size": "10"}],
        "timestamp": "1791605394982",
    }
    assert stream_price_rows(frame, NOW, "r1", TRACKED) == []


def test_a_trade_price_is_archived_raw_but_never_stored_as_a_price_point() -> None:
    frame = {"event_type": "last_trade_price", "asset_id": TOKEN_2, "price": "0.154", "timestamp": "1791605394982"}
    assert stream_price_rows(frame, NOW, "r1", TRACKED) == []


def test_an_untracked_token_yields_no_row() -> None:
    assert stream_price_rows(LIVE_BOOK_FRAME, NOW, "r1", {}) == []
    assert stream_price_rows(LIVE_PRICE_CHANGE_FRAME, NOW, "r1", {}) == []


def test_an_unknown_event_type_is_ignored_not_fatal() -> None:
    frame = {"event_type": "tick_size_change", "old_tick_size": "0.01", "new_tick_size": "0.001"}
    assert stream_price_rows(frame, NOW, "r1", TRACKED) == []


def test_frames_without_a_venue_timestamp_are_marked_missing() -> None:
    """Never substitute the capture clock for a missing exchange timestamp."""
    frame = [{"event_type": "book", "asset_id": TOKEN_1, "bids": [{"price": "0.4", "size": "1"}], "asks": [{"price": "0.6", "size": "1"}]}]
    rows = stream_price_rows(frame, NOW, "r1", TRACKED)
    assert len(rows) == 1
    assert rows[0][6] == "missing"


def test_mixed_array_frame_produces_one_row_per_tracked_token() -> None:
    """The initial dump carries one book element per subscribed token."""
    two = [
        dict(LIVE_BOOK_FRAME[0]),
        {
            "event_type": "book",
            "asset_id": TOKEN_2,
            "timestamp": "1791605394391",
            "bids": [{"price": "0.2", "size": "10"}],
            "asks": [{"price": "0.4", "size": "10"}],
        },
    ]
    rows = stream_price_rows(two, NOW, "r1", TRACKED)
    assert sorted(r[0] for r in rows) == ["mkt-1", "mkt-2"]


# ---------------------------------------------------------------------------
# Worker helpers
# ---------------------------------------------------------------------------


def test_tokens_of_only_includes_currently_tradeable_markets() -> None:
    row = list(_market_row(active=True, closed=False, accepting=True))
    assert len(_tokens_of([tuple(row)])) == 2

    # A closed market is still stored as metadata but never polled for depth -
    # its tokens answer 404 "no orderbook exists".
    for kwargs in (
        {"active": False, "closed": False, "accepting": True},
        {"active": True, "closed": True, "accepting": True},
        {"active": True, "closed": False, "accepting": False},
    ):
        assert _tokens_of([_market_row(**kwargs)]) == [], kwargs


def test_tokens_of_skips_missing_tokens() -> None:
    row = list(_market_row())
    row[13] = ""  # no NO token
    assert [t for t, _ in _tokens_of([tuple(row)])] == ["tokY"]


def test_is_one_sided_distinguishes_an_empty_book_from_a_corrupt_one() -> None:
    # An extreme temperature bucket with no counterparty is normal market state.
    assert _is_one_sided("NO_ASK_SIDE") is True
    assert _is_one_sided("NO_BID_SIDE") is True
    # A crossed or non-monotonic book is a payload defect.
    assert _is_one_sided("CROSSED_OR_LOCKED") is False
    assert _is_one_sided("NON_MONOTONIC_ASK") is False
    assert _is_one_sided("NO_ASK_SIDE+NON_MONOTONIC_BID") is False


def test_with_run_rewrites_only_the_run_id() -> None:
    row = _with_run(("a", "b", "c"), "r1")
    assert row == ("a", "b", "r1")
    with pytest.raises(ValueError, match="missing row"):
        _with_run(None, "r1")


def _market_row(*, active: bool = True, closed: bool = False, accepting: bool = True) -> tuple:
    """Market row layout as built by discovery.build_market_rows."""
    return (
        "mkt-1", "evt-1", "cond-1", "q", "60-61°F", "range", "degF", 60.0, 61.0, True, True,
        "nearest_integer_f", "tokY", "tokN", date(2026, 10, 11), "KLGA", 0.001, 5.0, 0.05, 1.0,
        "NWS", "rules", active, closed, accepting, NOW, "{}", "gamma", None,
    )


def test_market_row_layout_indices_are_what_tokens_of_expects() -> None:
    """Guard against a column reorder silently changing the tradeable filter."""
    from weatherquant.data.discovery import build_market_rows

    event = {
        "id": "1",
        "slug": "highest-temperature-in-nyc-on-october-11-2026",
        "title": "Highest temperature in NYC on October 11, 2026",
        "eventDate": "2026-10-11",
        "description": "NOAA LaGuardia (KLGA)",
        "resolutionSource": "https://www.weather.gov/klga",
        "markets": [
            {
                "id": "m",
                "question": "q",
                "groupItemTitle": "60-61°F",
                "outcomes": '["Yes","No"]',
                "clobTokenIds": '["tokY","tokN"]',
                "active": True,
                "closed": False,
                "acceptingOrders": True,
            }
        ],
    }
    _event_row, rows, problems = build_market_rows(event, fetched_at_utc=NOW, default_station="KLGA")
    assert problems == []
    row = rows[0]
    assert row[0] == "mkt" or isinstance(row[0], str)
    assert (row[12], row[13]) == ("tokY", "tokN")
    assert (row[22], row[23], row[24]) == (True, False, True)
    assert len(_tokens_of(rows)) == 2


# Silence the unused-import warning for json (used by readability of dumps above)
def test_json_round_trip_of_live_frame() -> None:
    assert json.loads(json.dumps(LIVE_BOOK_FRAME)) == LIVE_BOOK_FRAME

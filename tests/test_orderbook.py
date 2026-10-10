"""Order-book records: what is stored vs. what is executable.

A book is captured as-is (including crossed or scrambled levels, so the defect
remains auditable) and only *rejected* at execution time. These tests pin that
split down.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from weatherquant.markets.models import BookLevel, OrderBook, Side

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_best_prices_spread_and_midpoint(make_book) -> None:
    book = make_book(bids=[(0.48, 100.0)], asks=[(0.52, 100.0)])
    assert book.best_bid_usd_per_share == pytest.approx(0.48)
    assert book.best_ask_usd_per_share == pytest.approx(0.52)
    assert book.spread_usd_per_share == pytest.approx(0.04)
    assert book.midpoint_usd_per_share == pytest.approx(0.50)


def test_empty_side_yields_no_prices_instead_of_zero(make_book) -> None:
    book = make_book(bids=[], asks=[(0.52, 100.0)])
    assert book.best_bid_usd_per_share is None
    assert book.spread_usd_per_share is None
    assert book.midpoint_usd_per_share is None


def test_a_healthy_book_has_no_defects(book) -> None:
    assert book.defects() == []


def test_missing_ask_side_is_a_defect(make_book) -> None:
    assert "NO_ASK_SIDE" in make_book(bids=[(0.48, 1.0)], asks=[]).defects()


def test_missing_bid_side_is_a_defect(make_book) -> None:
    assert "NO_BID_SIDE" in make_book(bids=[], asks=[(0.52, 1.0)]).defects()


def test_crossed_book_is_recorded_as_a_defect_not_rejected_on_ingest(make_book) -> None:
    book = make_book(bids=[(0.60, 1.0)], asks=[(0.50, 1.0)])
    assert "CROSSED_OR_LOCKED" in book.defects()
    # ...and the object still exists, so the bad snapshot can be audited later.
    assert book.market_id == "mkt-1"


def test_non_monotonic_levels_are_detected_on_both_sides(make_book) -> None:
    asks_bad = make_book(bids=[(0.40, 1.0)], asks=[(0.50, 1.0), (0.45, 1.0)])
    bids_bad = make_book(bids=[(0.48, 1.0), (0.50, 1.0)], asks=[(0.55, 1.0)])
    assert "NON_MONOTONIC_ASK" in asks_bad.defects()
    assert "NON_MONOTONIC_BID" in bids_bad.defects()


def test_depth_is_summed_as_price_times_size(make_book) -> None:
    book = make_book(bids=[(0.48, 100.0), (0.45, 200.0)], asks=[(0.52, 300.0)])
    assert book.depth_notional_usd(Side.BUY) == pytest.approx(0.52 * 300.0)
    assert book.depth_notional_usd(Side.SELL) == pytest.approx(0.48 * 100.0 + 0.45 * 200.0)


def test_level_notional_is_price_times_shares() -> None:
    level = BookLevel(price_usd_per_share=0.25, size_shares=400.0)
    assert level.notional_usd == pytest.approx(100.0)


def test_price_outside_the_unit_interval_is_rejected() -> None:
    with pytest.raises(ValidationError):
        BookLevel(price_usd_per_share=1.0, size_shares=10.0)
    with pytest.raises(ValidationError):
        BookLevel(price_usd_per_share=0.0, size_shares=10.0)
    with pytest.raises(ValidationError):
        BookLevel(price_usd_per_share=0.5, size_shares=0.0)


def test_venue_timestamp_is_optional_but_fetch_time_is_required(make_book) -> None:
    book = OrderBook(
        market_id="mkt-1",
        token_id="tok",
        bids=(),
        asks=(),
        venue_ts=None,
        fetched_at_utc=NOW,
    )
    assert book.venue_ts is None
    with pytest.raises(ValidationError):
        OrderBook(market_id="mkt-1", token_id="tok", bids=(), asks=(), venue_ts=NOW, fetched_at_utc=None)  # type: ignore[arg-type]


def test_book_is_frozen_so_a_snapshot_cannot_drift_after_capture(make_book) -> None:
    book = make_book(bids=[(0.48, 1.0)], asks=[(0.52, 1.0)])
    with pytest.raises(ValidationError):
        book.fetched_at_utc = datetime(2030, 1, 1, tzinfo=UTC)  # type: ignore[misc]

"""Shared fixtures.

Every factory here builds a *valid* object with explicit units. Tests that need
an invalid object construct it inline so the failure mode stays visible in the
test itself.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.markets.models import (
    BookLevel,
    Bracket,
    BracketKind,
    Event,
    Market,
    OrderBook,
    Side,
)


def make_bracket(low: float = 32.0, high: float = 33.0) -> Bracket:
    return Bracket(
        label=f"{int(low)}-{int(high)}",
        kind=BracketKind.RANGE,
        low=low,
        high=high,
    )


@pytest.fixture
def bracket() -> Bracket:
    return make_bracket()


@pytest.fixture
def market() -> Market:
    return Market(
        market_id="mkt-1",
        event_id="evt-1",
        condition_id="cond-1",
        question="Will the highest temperature in NYC be 32-33 on 2026-10-10?",
        bracket=make_bracket(),
        token_yes="tok-yes",
        token_no="tok-no",
        target_date=date(2026, 10, 10),
        station="KLGA",
        tick_size=0.01,
        min_order_size_shares=5.0,
        fee_rate=0.05,
        fee_exponent=1.0,
        resolution_source="NWS KLGA",
        rules_text="Resolved on the official NWS daily maximum for KLGA.",
        active=True,
        closed=False,
        accepting_orders=True,
        fetched_at_utc=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        raw={"id": "mkt-1"},
    )


@pytest.fixture
def event() -> Event:
    return Event(
        event_id="evt-1",
        slug="highest-temperature-in-nyc-on-october-10-2026",
        title="Highest temperature in NYC on October 10, 2026",
        target_date=date(2026, 10, 10),
        resolution_source="NWS KLGA",
        rules_text="...",
        fetched_at_utc=datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def book() -> OrderBook:
    """A healthy book: spread 0.04, midpoint 0.50, real depth on both sides."""
    return OrderBook(
        market_id="mkt-1",
        token_id="tok-yes",
        bids=(
            BookLevel(price_usd_per_share=0.48, size_shares=100.0),
            BookLevel(price_usd_per_share=0.45, size_shares=400.0),
        ),
        asks=(
            BookLevel(price_usd_per_share=0.52, size_shares=100.0),
            BookLevel(price_usd_per_share=0.57, size_shares=400.0),
        ),
        venue_ts=datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC),
        fetched_at_utc=datetime(2026, 10, 10, 12, 0, 1, tzinfo=UTC),
    )


@pytest.fixture
def make_book():
    """Factory for arbitrary books, so depth tests read as data, not setup noise."""

    def _make(
        *,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        token_id: str = "tok-yes",
    ) -> OrderBook:
        return OrderBook(
            market_id="mkt-1",
            token_id=token_id,
            bids=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in bids),
            asks=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in asks),
            venue_ts=datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC),
            fetched_at_utc=datetime(2026, 10, 10, 12, 0, 1, tzinfo=UTC),
        )

    return _make


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def utc_instant():
    def _make(hour: int, minute: int = 0, day: int = 10) -> datetime:
        return datetime(2026, 10, day, hour, minute, tzinfo=UTC)

    return _make


@pytest.fixture
def side_buy() -> Side:
    return Side.BUY


__all__ = ["make_bracket", "timedelta"]

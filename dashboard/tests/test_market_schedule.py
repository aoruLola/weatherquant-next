"""Trading schedule and open-now, against the field shapes Gamma really sends.

Recorded from the public Gamma API (2026-11-17): a market carries
``endDate: "2026-11-16T12:00:00Z"`` next to a bare ``endDateIso:
"2026-11-16"``; the event carries full ``startDate``/``endDate`` and null
``*Iso`` fields. The same weather markets were still ``acceptingOrders: true``
(Gamma and CLOB) and traded at 14:39Z, i.e. after ``endDate``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from weatherquant.dashboard.builders import _open_now, _summary_from_catalog
from weatherquant.shadow.events import parse_gamma

NOW = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # after endDate, before resolution


def _event(**market_overrides) -> dict:
    market = {
        "id": "m-48",
        "question": "Will the highest temperature in New York City be 48°F on November 16?",
        "groupItemTitle": "48°F",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps(["tok-48-yes", "tok-48-no"]),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "startDate": "2026-11-14T06:59:39.123456Z",
        "startDateIso": "2026-11-14",
        "endDate": "2026-11-16T12:00:00Z",
        "endDateIso": "2026-11-16",
        "description": "Resolution source: KLGA daily max",
        "resolutionSource": "https://example.invalid/klga",
    }
    market.update(market_overrides)
    return {
        "id": "event-1",
        "slug": "highest-temperature-in-nyc-on-november-16-2026",
        "title": "Highest temperature in NYC on November 16?",
        "eventDate": "2026-11-16",
        "startDate": "2026-11-14T06:58:00.5Z",
        "startDateIso": None,
        "endDate": "2026-11-16T12:00:00Z",
        "endDateIso": None,
        "description": "Resolution source: KLGA daily max",
        "resolutionSource": "https://example.invalid/klga",
        "markets": [market],
    }


def _parse(event: dict):
    markets, _problems = parse_gamma(event, default_station="KLGA")
    assert len(markets) == 1
    return markets[0]


def test_real_gamma_shape_prefers_timezone_aware_dates_over_bare_iso_dates():
    market = _parse(_event())
    assert market.start_time_utc == datetime(2026, 11, 14, 6, 59, 39, 123456, tzinfo=UTC)
    assert market.end_time_utc == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert market.schedule_source == "gamma:market.startDate, gamma:market.endDate"
    assert market.schedule_reason is None


def test_bare_market_date_falls_through_to_the_event_timestamp():
    market = _parse(_event(startDate=None, endDate=None))
    assert market.start_time_utc == datetime(2026, 11, 14, 6, 58, 0, 500000, tzinfo=UTC)
    assert market.end_time_utc == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert market.schedule_source == "gamma:event.startDate, gamma:event.endDate"
    assert market.schedule_reason is None


def test_no_timezone_is_reported_only_when_no_aware_value_exists():
    event = _event(startDate=None, endDate="2026-11-16")
    event["startDate"] = None
    event["endDate"] = None
    market = _parse(event)
    assert market.start_time_utc is None and market.end_time_utc is None
    assert "gamma:market.startDateIso has no timezone" in market.schedule_reason
    assert "gamma:market.endDate, gamma:market.endDateIso has no timezone" in market.schedule_reason


def test_open_now_follows_venue_flags_after_the_scheduled_end():
    market = _parse(_event())
    summary = _summary_from_catalog(market, None, NOW, model_reason="fixture")
    assert summary.end_time_utc == datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert summary.open_now is True
    assert summary.open_now_source == "venue flags (active, acceptingOrders, closed)"
    assert "past the scheduled end (gamma:market.startDate, gamma:market.endDate)" in summary.open_now_reason


def test_open_now_is_false_when_the_venue_stops_accepting_orders():
    market = _parse(_event(acceptingOrders=False))
    summary = _summary_from_catalog(market, None, NOW, model_reason="fixture")
    assert summary.open_now is False
    assert summary.open_now_reason == "venue flags do not currently allow order acceptance"


def test_open_now_unknown_flags_stay_unknown():
    end = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    assert _open_now(None, end, NOW, tradeable=None, schedule_source=None, schedule_reason=None) == (
        None,
        None,
        "venue order-acceptance flags are unavailable",
    )
    # Inside the schedule and accepting orders: open, nothing to explain.
    start = datetime(2026, 11, 14, tzinfo=UTC)
    assert _open_now(start, end, datetime(2026, 11, 15, tzinfo=UTC), tradeable=True,
                     schedule_source="s", schedule_reason=None)[0:3:2] == (True, None)

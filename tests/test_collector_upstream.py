"""Upstream layer: HTTP retry policy, book parsing, discovery, bracket labels.

Every expected value here is computed independently of the code under test -
hand-derived from the venue's documented shapes, not echoed back from a prior
run of the same functions.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from weatherquant.data.books import parse_book_payload
from weatherquant.data.discovery import (
    build_event_row,
    build_market_rows,
    detect_station,
)
from weatherquant.data.http import HttpStatusError, ResilientHttp
from weatherquant.data.ratelimit import RateLimitRegistry
from weatherquant.markets.models import BracketKind
from weatherquant.markets.parsing import (
    BracketParseError,
    is_nyc_daily_high_event,
    parse_bracket_label,
    parse_target_date,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# HTTP retry policy
# ---------------------------------------------------------------------------


def _client(handler, **kwargs) -> ResilientHttp:
    return ResilientHttp(
        retries=kwargs.pop("retries", 3),
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        transport=httpx.MockTransport(handler),
        limits=RateLimitRegistry(),
        **kwargs,
    )


def test_successful_get_advances_the_contact_clock() -> None:
    http = _client(lambda _req: httpx.Response(200, json={"ok": True}))
    assert http.last_contact_utc is None
    assert http.get_json("https://example.test/x") == {"ok": True}
    assert http.last_contact_utc is not None
    assert http.requests_ok == 1
    assert http.requests_failed == 0
    http.close()


def test_server_errors_are_retried_then_succeed() -> None:
    calls: list[int] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"n": len(calls)})

    http = _client(handler, retries=3)
    assert http.get_json("https://example.test/x") == {"n": 3}
    assert len(calls) == 3
    assert http.retries_performed == 2
    http.close()


def test_server_errors_exhaust_retries_and_raise() -> None:
    http = _client(lambda _r: httpx.Response(500, text="boom"), retries=2)
    with pytest.raises(HttpStatusError, match="failed after 3 attempt"):
        http.get_json("https://example.test/x")
    assert http.requests_failed >= 1
    http.close()


def test_client_errors_are_not_retried() -> None:
    """A 4xx is our mistake; repeating it just repeats the mistake."""
    calls: list[int] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    http = _client(handler, retries=3)
    with pytest.raises(HttpStatusError, match="HTTP 400"):
        http.get_json("https://example.test/x")
    assert len(calls) == 1, "4xx must be raised immediately, not retried"
    assert http.retries_performed == 0
    http.close()


def test_404_raises_by_default() -> None:
    http = _client(lambda _r: httpx.Response(404, text="nope"), retries=0)
    with pytest.raises(HttpStatusError, match="HTTP 404"):
        http.get_json("https://example.test/x")
    http.close()


def test_404_is_a_definitive_answer_when_allowed() -> None:
    """/book on an archived token: "no orderbook exists" is not a failure."""
    http = _client(lambda _r: httpx.Response(404, json={"error": "No orderbook exists"}), retries=0)
    assert http.get_json("https://example.test/book", allow_not_found=True) is None
    assert http.requests_ok == 1
    assert http.requests_failed == 0
    assert http.last_contact_utc is not None, "the venue still answered - contact happened"
    http.close()


def test_transport_errors_are_retried() -> None:
    calls: list[int] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"ok": 1})

    http = _client(handler, retries=2)
    assert http.get_json("https://example.test/x") == {"ok": 1}
    assert len(calls) == 2
    http.close()


def test_invalid_json_is_retried_rather_than_returned() -> None:
    calls: list[int] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="<html>not json</html>" if len(calls) < 2 else '{"ok":1}')

    http = _client(handler, retries=2)
    assert http.get_json("https://example.test/x") == {"ok": 1}
    http.close()


# ---------------------------------------------------------------------------
# Book parsing
# ---------------------------------------------------------------------------


def _raw_book() -> dict:
    return {
        "market": "0xabc",
        "asset_id": "8820",
        "timestamp": "1791605394391",  # venue time, milliseconds, as a string
        "bids": [{"price": "0.45", "size": "400"}, {"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.57", "size": "400"}, {"price": "0.52", "size": "100"}],
        "tick_size": "0.001",
    }


def test_book_levels_are_stored_best_first_regardless_of_wire_order() -> None:
    snapshot = parse_book_payload(
        _raw_book(), token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    bids = json.loads(snapshot.row[3])
    asks = json.loads(snapshot.row[4])
    # The venue sent bids ascending and asks descending; we store best-first.
    assert [b["price_usd_per_share"] for b in bids] == [0.48, 0.45]
    assert [a["price_usd_per_share"] for a in asks] == [0.52, 0.57]


def test_venue_timestamp_is_parsed_from_millisecond_string() -> None:
    snapshot = parse_book_payload(
        _raw_book(), token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    assert snapshot.venue_ts is not None
    assert snapshot.venue_ts.tzinfo is not None
    # 1791605394391 ms == 1791605394.391 s
    assert snapshot.venue_ts.timestamp() == pytest.approx(1791605394.391)
    assert snapshot.venue_ts_origin == "venue"


def test_a_payload_without_a_timestamp_yields_no_venue_time_not_our_clock() -> None:
    payload = dict(_raw_book())
    del payload["timestamp"]
    snapshot = parse_book_payload(
        payload, token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    assert snapshot.venue_ts is None
    assert snapshot.venue_ts_origin == "missing"
    assert snapshot.venue_ts != NOW
    # And the row carries both clocks separately.
    assert snapshot.row[11] is None
    assert snapshot.row[12] == NOW


def test_derived_book_fields_are_hand_computable() -> None:
    snapshot = parse_book_payload(
        _raw_book(), token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    assert snapshot.row[5] == pytest.approx(0.48)  # best bid = max bids
    assert snapshot.row[6] == pytest.approx(0.52)  # best ask = min asks
    assert snapshot.row[7] == pytest.approx(0.04)  # spread
    assert snapshot.row[8] == pytest.approx(0.45 * 400 + 0.48 * 100)  # 232.0
    assert snapshot.row[9] == pytest.approx(0.52 * 100 + 0.57 * 400)  # 280.0
    assert snapshot.quality == "ok"


def test_a_crossed_book_is_recorded_not_discarded() -> None:
    payload = {
        "timestamp": "1791605394391",
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.50", "size": "10"}],
    }
    snapshot = parse_book_payload(
        payload, token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    assert "CROSSED_OR_LOCKED" in snapshot.quality
    assert snapshot.row[7] < 0  # spread is negative: the defect is visible in the data


def test_a_one_sided_book_is_stored_with_its_defect_named() -> None:
    payload = {"timestamp": "1", "bids": [{"price": "0.48", "size": "10"}], "asks": []}
    snapshot = parse_book_payload(
        payload, token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    assert snapshot.quality == "NO_ASK_SIDE"


def test_a_payload_with_no_usable_levels_is_rejected() -> None:
    with pytest.raises(ValueError, match="no usable levels"):
        parse_book_payload(
            {"bids": [], "asks": []}, token_id="t", market_id="m", fetched_at_utc=NOW, run_id="r1"
        )
    with pytest.raises(ValueError, match="expected an object"):
        parse_book_payload(
            ["not", "a", "dict"], token_id="t", market_id="m", fetched_at_utc=NOW, run_id="r1"
        )


def test_corrupt_levels_are_dropped_rather_than_poisoning_the_snapshot() -> None:
    payload = {
        "timestamp": "1",
        "bids": [
            {"price": "0.48", "size": "10"},
            {"price": "not-a-number", "size": "10"},
            {"price": "0.9", "size": "-5"},  # negative size
            {"price": "1.5", "size": "10"},  # price outside (0, 1)
            "garbage",
        ],
        "asks": [{"price": "0.52", "size": "10"}],
    }
    snapshot = parse_book_payload(
        payload, token_id="tok", market_id="mkt", fetched_at_utc=NOW, run_id="r1"
    )
    bids = json.loads(snapshot.row[3])
    assert len(bids) == 1
    assert bids[0]["price_usd_per_share"] == 0.48


def test_content_hash_is_stable_and_sensitive_to_every_input() -> None:
    a = parse_book_payload(_raw_book(), token_id="tok", market_id="m", fetched_at_utc=NOW, run_id="r1")
    same = parse_book_payload(_raw_book(), token_id="tok", market_id="m", fetched_at_utc=NOW, run_id="r1")
    assert a.content_hash == same.content_hash
    # The capture time is NOT part of identity: polling an unchanged book
    # twice, minutes apart, must dedupe rather than accumulate rows.
    later = parse_book_payload(
        _raw_book(),
        token_id="tok",
        market_id="m",
        fetched_at_utc=NOW + timedelta(minutes=5),
        run_id="r1",
    )
    assert later.content_hash == a.content_hash
    assert later.row[12] == NOW + timedelta(minutes=5), "capture time is still recorded"
    # But a different token is a different observation.
    other = parse_book_payload(_raw_book(), token_id="other", market_id="m", fetched_at_utc=NOW, run_id="r1")
    assert other.content_hash != a.content_hash


# ---------------------------------------------------------------------------
# Discovery / row construction
# ---------------------------------------------------------------------------


def _gamma_event() -> dict:
    return {
        "id": "7001",
        "slug": "highest-temperature-in-nyc-on-october-11-2026",
        "title": "Highest temperature in NYC on October 11, 2026",
        "eventDate": "2026-10-11",
        "seriesSlug": "nyc-daily-weather",
        "description": "Resolved on the official NOAA daily maximum for LaGuardia (KLGA).",
        "resolutionSource": "https://www.weather.gov/klga",
        "markets": [
            {
                "id": "m1",
                "conditionId": "0xcond",
                "question": "Will the highest temperature in NYC be 60-61°F on October 11, 2026?",
                "groupItemTitle": "60-61°F",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["tokY", "tokN"]',
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5.0,
                "feeSchedule": {"rate": 0.05, "exponent": 1},
                "description": "Resolved on the official NOAA daily maximum for LaGuardia (KLGA).",
            },
            {
                "id": "m2",
                "conditionId": "0xcond2",
                "question": "...or 62-63°F?",
                "groupItemTitle": "62-63°F",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["tokY2", "tokN2"]',
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5.0,
                "feeSchedule": {"rate": 0.05, "exponent": 1},
            },
        ],
    }


def test_event_row_keeps_raw_payload_and_capture_time() -> None:
    row = build_event_row(_gamma_event(), fetched_at_utc=NOW)
    assert row is not None
    assert row[0] == "7001"
    assert row[2] == "Highest temperature in NYC on October 11, 2026"
    assert row[3] == date(2026, 10, 11)
    assert row[6] == NOW
    assert json.loads(row[7])["id"] == "7001"  # verbatim venue payload retained
    assert row[8] == "gamma"
    assert row[9] is None  # run_id filled in by the worker


def test_market_rows_parse_brackets_and_expose_both_tokens() -> None:
    event_row, rows, problems = build_market_rows(
        _gamma_event(), fetched_at_utc=NOW, default_station="KLGA"
    )
    assert event_row is not None
    assert len(rows) == 2
    assert problems == []

    first = rows[0]
    assert first[0] == "m1"
    assert first[4] == "60-61°F"
    assert first[5] == "range"
    assert (first[7], first[8]) == (60.0, 61.0)
    assert first[12] == "tokY" and first[13] == "tokN"
    assert first[14] == date(2026, 10, 11)
    assert first[15] == "KLGA"
    assert first[18] == pytest.approx(0.05)  # fee rate preserved, not defaulted to 0
    assert first[22] and not first[23] and first[24]  # active, not closed, accepting


def test_station_is_read_from_the_rules_not_from_a_hardcoded_table() -> None:
    """Audited bots shipped KNYC while the market resolved on KLGA."""
    assert detect_station("Resolved on NOAA daily maximum for LaGuardia (KLGA).", default="KNYC") == "KLGA"
    assert detect_station("Source: KNYC central park", default="KLGA") == "KNYC"
    # When the rules name nothing, the configured default is used (and the
    # caller records that it had to fall back).
    assert detect_station("source: some provider", default="KLGA") == "KLGA"


def test_a_rules_mismatch_is_reported_not_silently_absorbed() -> None:
    event = _gamma_event()
    event["description"] = "Resolved on KNYC."
    _event_row, _rows, problems = build_market_rows(
        event, fetched_at_utc=NOW, default_station="KLGA"
    )
    assert any("KNYC" in p and "KLGA" in p for p in problems)


def test_an_unparseable_bracket_is_reported_per_market() -> None:
    event = _gamma_event()
    event["markets"][1]["groupItemTitle"] = "somewhere around 60"
    _event_row, rows, problems = build_market_rows(
        event, fetched_at_utc=NOW, default_station="KLGA"
    )
    assert len(problems) == 1
    assert "somewhere around 60" in problems[0]
    # The bad market still produces a row, so the venue's payload is retained.
    assert len(rows) == 2


def test_a_market_without_tokens_is_flagged() -> None:
    event = _gamma_event()
    event["markets"][0]["clobTokenIds"] = "[]"
    _event_row, rows, problems = build_market_rows(
        event, fetched_at_utc=NOW, default_station="KLGA"
    )
    assert any("YES clob token" in p for p in problems)
    assert rows[0][12] == ""


def test_missing_target_date_is_flagged() -> None:
    event = _gamma_event()
    del event["eventDate"]
    event["title"] = "Highest temperature in NYC"
    _event_row, _rows, problems = build_market_rows(
        event, fetched_at_utc=NOW, default_station="KLGA"
    )
    assert any("target civil date" in p for p in problems)


def test_unknown_resolution_source_is_flagged() -> None:
    event = _gamma_event()
    event["description"] = "Resolved by some arbitrary website."
    event["resolutionSource"] = "https://example.com"
    _event_row, _rows, problems = build_market_rows(
        event, fetched_at_utc=NOW, default_station="KLGA"
    )
    assert any("neither NOAA nor Weather Underground" in p for p in problems)


# ---------------------------------------------------------------------------
# Event filters and label parsing
# ---------------------------------------------------------------------------


def test_only_nyc_highest_temperature_events_are_candidates() -> None:
    base = {"title": "Highest temperature in NYC on October 11, 2026", "slug": "highest-temperature-in-nyc-on-october-11-2026"}
    assert is_nyc_daily_high_event(dict(base, seriesSlug="nyc-daily-weather")) is True
    # A *lowest* temperature event settles on a different statistic.
    assert is_nyc_daily_high_event(
        dict(base, title="Lowest temperature in NYC on October 11, 2026", slug="lowest-temperature-in-nyc-on-october-11-2026", seriesSlug=None)
    ) is False
    assert is_nyc_daily_high_event(
        dict(base, title="Highest temperature in London", slug="highest-temperature-in-london", seriesSlug=None)
    ) is False


def test_start_date_is_never_used_as_the_target_date() -> None:
    """A market opens before the day it settles on; the start date is wrong."""
    assert parse_target_date({"eventDate": "2026-10-11", "startDate": "2026-10-09"}) == date(2026, 10, 11)
    assert parse_target_date({"startDate": "2026-10-09"}) is None


def test_target_date_falls_back_to_the_title_without_trusting_the_slug_alone() -> None:
    assert parse_target_date(
        {"title": "Highest temperature in NYC on October 11, 2026", "slug": "x-2026"}
    ) == date(2026, 10, 11)


@pytest.mark.parametrize(
    ("label", "kind", "low", "high"),
    [
        ("59°F or below", BracketKind.BELOW, None, 59.0),
        ("≤71°F", BracketKind.BELOW, None, 71.0),
        ("60-61°F", BracketKind.RANGE, 60.0, 61.0),
        ("77 to 78 F", BracketKind.RANGE, 77.0, 78.0),

        # venue labels actually use, not a typographic slip in this file.
        ("32–33°F", BracketKind.RANGE, 32.0, 33.0),  # noqa: RUF001
        ("64°F", BracketKind.EXACT, 64.0, None),
        ("78°F or higher", BracketKind.ABOVE, 78.0, None),
        ("≥86°F", BracketKind.ABOVE, 86.0, None),
    ],
)
def test_live_label_shapes_parse_to_the_expected_bracket(label, kind, low, high) -> None:
    """Shapes confirmed against live Gamma `groupItemTitle` values."""
    bracket = parse_bracket_label(label)
    assert bracket.kind is kind
    assert bracket.low == low
    assert bracket.high == high


def test_the_half_degree_widening_is_applied_to_live_labels() -> None:
    """A live "60-61" bracket covers true temperatures [59.5, 61.5)."""
    lo, hi = parse_bracket_label("60-61°F").probability_interval_f()
    assert lo == pytest.approx(59.5)
    assert hi == pytest.approx(61.5)


@pytest.mark.parametrize("label", ["", "hot", "60-61.5°F", "between ≤70 and ≥80", "60°F to 62°F to 64°F"])
def test_ambiguous_labels_are_refused_rather_than_guessed(label: str) -> None:
    with pytest.raises(BracketParseError):
        parse_bracket_label(label)


def test_deg_celsius_labels_are_refused_not_converted() -> None:
    with pytest.raises(NotImplementedError, match="degC"):
        parse_bracket_label("10-12°C")


# ---------------------------------------------------------------------------
# Legacy import plan still works against the real inventory
# ---------------------------------------------------------------------------


def test_legacy_inventory_still_covers_the_old_tables() -> None:
    from weatherquant.legacy import LEGACY_TABLES, ValueTier

    research = [t for t in LEGACY_TABLES.values() if t.tier is ValueTier.RESEARCH]
    assert len(research) == 10
    assert all(t.target_table for t in research)

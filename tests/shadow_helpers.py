"""Builders for shadow-engine tests.

Kept out of ``conftest.py`` because most of these are *data* rather than
fixtures: a test that inspects a malformed archive line should be able to read
the line it is inspecting, so the helpers live here and each test composes them
inline.

Shapes mirror what the collector actually writes - the gamma, rest and ws
payloads are taken from the fixtures the collector's own tests use, so a shadow
test that passes here is exercising the same bytes the NAS archive contains.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from weatherquant.markets.models import (
    BookLevel,
    Bracket,
    BracketDistribution,
    BracketKind,
    BracketProbability,
    OrderBook,
)
from weatherquant.shadow.decision import Direction
from weatherquant.shadow.events import BookObservation, CatalogMarket
from weatherquant.shadow.forecast import ResolvedForecast
from weatherquant.shadow.inference import PredictionResult

__all__ = [
    "archive_line",
    "book_payload",
    "gamma_event",
    "make_catalog_market",
    "make_forecast",
    "make_observation",
    "make_prediction",
    "write_lines",
]

TARGET_DATE = date(2026, 10, 11)
NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


def archive_line(
    seq: int,
    source: str,
    payload,
    *,
    received: datetime | None = None,
    venue_ts: datetime | None = None,
    quality: str = "ok",
    run_id: str = "collector-run-1",
) -> str:
    received = received or NOW
    return json.dumps(
        {
            "archive_seq": seq,
            "source": source,
            "received_at_utc": received.isoformat(),
            "venue_ts_utc": venue_ts.isoformat() if venue_ts else None,
            "venue_ts_origin": "venue" if venue_ts else "missing",
            "quality": quality,
            "run_id": run_id,
            "payload": payload,
        },
        separators=(",", ":"),
    )


def write_lines(root: Path, source: str, day: date, lines: list[str]) -> Path:
    directory = root / source
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.jsonl"
    text = "".join(line + "\n" for line in lines)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)
    return path


def gamma_event(
    *,
    event_id: str = "evt-1",
    target_date: date = TARGET_DATE,
    labels: list[str] | None = None,
    tradeable: bool = True,
    fee_rate: float | None = 0.05,
    with_description: bool = True,
) -> dict:
    """A full Gamma event, shaped like ``GET /events/{id}``."""
    # Shapes confirmed against live Gamma `groupItemTitle` values - see
    # tests/test_collector_upstream.py. The ladder is open at both ends, which
    # is what makes its probabilities summable without renormalising.
    labels = labels or ["31°F or lower", "32-33°F", "34-35°F", "36°F or higher"]
    markets = []
    for index, label in enumerate(labels):
        markets.append(
            {
                "id": f"{event_id}-m{index}",
                "question": f"Highest temperature in NYC on {target_date}?",
                "groupItemTitle": label,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": json.dumps([f"tok-{index}-yes", f"tok-{index}-no"]),
                "conditionId": f"0xcond{index}",
                "feeSchedule": (
                    {"rate": fee_rate, "exponent": 1} if fee_rate is not None else {}
                ),
                "orderPriceMinTickSize": 0.01,
                "orderMinSize": 5.0,
                "active": tradeable,
                "closed": not tradeable,
                "acceptingOrders": tradeable,
                "description": "Resolved on the official NWS daily maximum for KLGA.",
            }
        )
    event = {
        "id": event_id,
        "slug": f"highest-temperature-in-nyc-on-{target_date.isoformat()}",
        "title": f"Highest temperature in NYC on {target_date.isoformat()}?",
        "seriesSlug": "nyc-daily-weather",
        "eventDate": target_date.isoformat(),
        "resolutionSource": "NOAA",
        "markets": markets,
    }
    if with_description:
        event["description"] = (
            "This market will resolve to the highest temperature recorded at "
            "KLGA (LaGuardia Airport) on the target date."
        )
    return event


def book_payload(
    *,
    token_id: str = "tok-1-yes",
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    venue_ts_ms: int | None = None,
) -> dict:
    bids = bids if bids is not None else [(0.45, 400.0), (0.44, 300.0)]
    asks = asks if asks is not None else [(0.47, 400.0), (0.48, 300.0)]
    payload = {
        "market": "0xcond",
        "asset_id": token_id,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "tick_size": "0.01",
    }
    if venue_ts_ms is not None:
        payload["timestamp"] = str(venue_ts_ms)
    return payload


def make_catalog_market(
    *,
    market_id: str = "m0",
    event_id: str = "evt-1",
    bracket: Bracket | None = None,
    bracket_label: str = "32-33 degF",
    token_yes: str = "tok-0-yes",
    token_no: str = "tok-0-no",
    target_date: date | None = TARGET_DATE,
    fee_rate: float | None = 0.05,
    min_order_size: float = 5.0,
    tradeable: bool = True,
    station: str = "KLGA",
) -> CatalogMarket:
    if bracket is None and bracket_label:
        bracket = Bracket(
            label=bracket_label, kind=BracketKind.RANGE, low=32.0, high=33.0
        )
    return CatalogMarket(
        market_id=market_id,
        event_id=event_id,
        slug="slug",
        question="q",
        bracket=bracket,
        bracket_label=bracket_label,
        bracket_error=None if bracket else "unparseable label",
        token_yes=token_yes,
        token_no=token_no,
        target_date=target_date,
        station=station,
        tick_size=0.01,
        min_order_size=min_order_size,
        fee_rate=fee_rate,
        fee_exponent=1.0 if fee_rate is not None else None,
        active=tradeable,
        closed=not tradeable,
        accepting_orders=tradeable,
        raw_sha256="deadbeef",
        source_run_id="collector-run-1",
    )


_UNSET = object()


def make_observation(
    *,
    token_id: str = "tok-0-yes",
    market_id: str = "m0",
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
    venue_ts: datetime | object | None = _UNSET,
    collector_received: datetime | None = None,
    shadow_received: datetime | None = None,
    book_source: str = "rest",
) -> BookObservation:
    bids = bids if bids is not None else [(0.45, 400.0)]
    asks = asks if asks is not None else [(0.47, 400.0)]
    # A sentinel, not None: passing ``venue_ts=None`` must mean "this book
    # carries no venue timestamp", which is a different fact from "use the
    # default".
    if venue_ts is _UNSET:
        venue_ts = NOW - timedelta(seconds=3)
    if collector_received is None:
        collector_received = (venue_ts + timedelta(seconds=1)) if venue_ts else NOW - timedelta(seconds=1)
    if shadow_received is None:
        shadow_received = collector_received + timedelta(seconds=1)
    book = OrderBook(
        market_id=market_id,
        token_id=token_id,
        bids=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in bids),
        asks=tuple(BookLevel(price_usd_per_share=p, size_shares=s) for p, s in asks),
        venue_ts=venue_ts,
        fetched_at_utc=collector_received,
    )
    defects = book.defects()
    return BookObservation(
        token_id=token_id,
        market_id=market_id,
        book=book,
        book_id=f"book-{token_id}",
        book_source=book_source,
        venue_ts=venue_ts,
        venue_ts_origin="venue" if venue_ts else "missing",
        collector_received_at_utc=collector_received,
        shadow_received_at_utc=shadow_received,
        quality="ok" if not defects else "+".join(defects),
    )


def make_forecast(
    *,
    shadow_model: str = "calibrated",
    run_time: datetime | None = None,
    publishable: datetime | None = None,
    received: datetime | None = None,
    predicted_max_f: float = 32.4,
    target_date: date = TARGET_DATE,
) -> ResolvedForecast:
    run_time = run_time if run_time is not None else NOW - timedelta(hours=14)
    return ResolvedForecast(
        shadow_model=shadow_model,
        source_models=("ecmwf_ifs025",),
        run_time_utc=run_time,
        publishable_at_utc=publishable if publishable is not None else run_time + timedelta(hours=12),
        publishable_basis="estimated",
        received_at_utc=received if received is not None else run_time + timedelta(hours=12),
        target_date=target_date,
        predicted_max_f=predicted_max_f,
        n_hours=24,
        n_missing_hours=0,
        payload_sha256="a" * 64,
        covers_full_local_day=True,
    )


def make_prediction(
    *,
    shadow_model: str = "calibrated",
    probabilities: dict[str, float] | None = None,
    forecast: ResolvedForecast | None = None,
    training_cutoff: datetime | None = None,
    model_version: str = "v1",
) -> PredictionResult:
    forecast = forecast or make_forecast(shadow_model=shadow_model)
    probabilities = probabilities or {"32-33 degF": 0.6, "Below 32 degF": 0.4}
    distribution = BracketDistribution(
        station="KLGA",
        target_date=forecast.target_date,
        model_id=shadow_model,
        forecast_run_time_utc=forecast.run_time_utc,
        probabilities=tuple(
            BracketProbability(label=label, probability=p) for label, p in probabilities.items()
        ),
    )
    return PredictionResult(
        shadow_model=shadow_model,
        model_version=model_version,
        training_cutoff_utc=training_cutoff or (NOW - timedelta(days=5)),
        source_data_hash="b" * 64,
        model_run_id="model-run-1",
        distribution=distribution,
        predicted_max_f=forecast.predicted_max_f,
        lead_hours=forecast.lead_hours,
        from_cache=False,
    )


def all_directions() -> tuple[Direction, Direction]:
    return (Direction.BUY_YES, Direction.SELL_YES)

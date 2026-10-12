"""Builders for the dashboard API tests: one function per producer payload.

Every test builds its own data tree under ``tmp_path`` - no live data, no
shared mutable state - and the payloads below are shaped like the real
producers' (the collector heartbeat mirrors the field set of
``weatherquant/data/heartbeat.py``, the shadow pieces mirror
``shadow/health.py`` and ``shadow/report.py``).

The API tests live in ``dashboard/tests`` rather than in the repository
``tests/`` on purpose: the root CI syncs only the ``dev`` dependency group,
which has no FastAPI in it, and these are the tests that need it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------


def collector_heartbeat(*, now: datetime, status: str = "healthy", age_seconds: float = 5.0, **overrides: Any) -> dict[str, Any]:
    updated = now - timedelta(seconds=age_seconds)
    payload: dict[str, Any] = {
        "status": status,
        "run_id": "run-collector",
        "pid": 4242,
        "hostname": "fixture-host",
        "app_version": "0.1.1",
        "updated_at_utc": iso(updated),
        "started_at_utc": iso(updated - timedelta(hours=2)),
        "uptime_seconds": 7200.0,
        "last_upstream_contact_utc": iso(updated - timedelta(seconds=2)),
        "last_data_change_utc": iso(updated - timedelta(seconds=30)),
        "ws_connected": True,
        "ws_last_message_utc": iso(updated - timedelta(seconds=1)),
        "ws_endpoint": "wss://example.invalid/ws/market",
        "subscribed_tokens": 66,
        "rest_last_success_utc": iso(updated - timedelta(seconds=2)),
        "rest_consecutive_failures": 0,
        "stats": {
            "rest_requests_ok": 1860,
            "rest_requests_failed": 0,
            "ws_messages_received": 9193,
            "ws_reconnects": 0,
            "errors_total": 0,
            "rate_limit_waits": 0,
            "integrity_failures": 0,
            "one_sided_books": 728,
            "retries": 0,
        },
        "db_bytes": 19_148_800,
        "raw_bytes": 12_568_370,
        "disk_free_percent": 42.5,
        "disk_total_bytes": 512_107_737_088,
        "disk_free_bytes": 217_645_788_000,
        "last_defect": None,
        "last_error_utc": None,
        "last_error": None,
        "fatal_error": None,
        # Collector >= v0.1.2 operational telemetry (empty means "not
        # measured", not zero - tests may override with {} to model older
        # collectors).
        "telemetry": {
            "rest_latency_last_sweep_ms": 41.5,
            "rest_latency_recent_ms": {"p50": 38.0, "p95": 96.0, "max": 142.0},
            "rest_timeouts": 0,
            "poll_budget": {"budget_seconds": 3.5, "exceeded": False, "deferred_tokens": 0},
            "ws_queue": {
                "depth": 2,
                "high_water": 11,
                "capacity": 512,
                "frames_dropped": 0,
            },
        },
    }
    payload.update(overrides)
    return payload


def shadow_heartbeat(*, now: datetime, mode: str = "LIVE", age_seconds: float = 5.0, **overrides: Any) -> dict[str, Any]:
    updated = now - timedelta(seconds=age_seconds)
    payload: dict[str, Any] = {
        "status": "running",
        "run_id": "run-shadow",
        "pid": 5252,
        "updated_at_utc": iso(updated),
        "started_at_utc": iso(updated - timedelta(hours=1)),
        "last_decision_at_utc": iso(updated - timedelta(seconds=20)),
        "collector_last_seen_utc": iso(updated - timedelta(seconds=3)),
        "cycles": 120,
        "fatal_error": None,
        "stop_reason": None,
        "thresholds_version": "v3",
        "mode": mode,
        "configured_mode": "auto",
        "primary_model": "calibrated",
        "disk": {"free_percent": 40.0},
        "last_cycle": {
            "cycle_id": "c-120",
            "lines_read": 64,
            "books": 12,
            "markets": 4,
            "quality_events": 0,
            "forecasts_fetched": 0,
            "predictions": 4,
            "decisions": 6,
            "by_state": {"REJECT": 4, "NO_TRADE": 2},
            "primary_reasons": {"EDGE_BELOW_THRESHOLD": 4},
            "duration_ms": 812,
        },
    }
    payload.update(overrides)
    return payload


def shadow_report(*, now: datetime, age_seconds: float = 10.0, mode: str = "LIVE", **overrides: Any) -> dict[str, Any]:
    generated = now - timedelta(seconds=age_seconds)
    payload: dict[str, Any] = {
        "generated_at_utc": iso(generated),
        "source": "shadow-main",
        "schema_version": 1,
        "stale_after_seconds": 300.0,
        "run": {"run_id": "run-shadow", "status": "running", "uptime_seconds": 3600.0},
        "tables": {"shadow_decisions": 42, "market_catalog": 58},
        "liveness": {
            "collector_last_seen_utc": iso(generated - timedelta(seconds=3)),
            "collector_silence_seconds": 3.0,
            "latest_orderbook_venue_age_seconds": 4.0,
            "latest_forecast_age_seconds": 600.0,
        },
        "decisions": {
            "evaluated": 42,
            "by_state": {"ACCEPT": 3, "REJECT": 30, "NO_TRADE": 9},
            "candidates_generated": 40,
            "accepted": 3,
            "rejected": 30,
            "no_trade": 9,
            "data_unavailable": 0,
            "interrupted": 0,
            "by_mode": {"LIVE": {"evaluated": 42, "by_state": {"ACCEPT": 3}, "accepted": 3}},
            "live_accepted": 3,
            "replay_accepted": 0,
            "primary_reasons": {"EDGE_BELOW_THRESHOLD": 30},
            "p95_decision_latency_ms": 42.0,
        },
        "theoretical_edge": {
            "note": "expectation under the model, not a realised return",
            "priced_decisions": 40,
            "mean_usd_per_share": 0.004,
            "positive_count": 12,
        },
        "prediction_quality": {
            "by_model": {
                "calibrated": {"predictions": 120, "events": 3, "latest_run": iso(generated - timedelta(hours=6))},
                "gfs_raw": {"predictions": 120, "events": 3, "latest_run": iso(generated - timedelta(hours=6))},
            },
            "refused": {},
            "scored": False,
        },
        "liquidity": {"snapshots": 200, "mean_spread_usd": 0.02, "mean_ask_depth_usd": 40.0},
        "coverage": {
            "open_target_dates": [str((now + timedelta(days=1)).date()), str((now + timedelta(days=2)).date())],
            "models_tracked": ["calibrated", "fusion", "ecmwf_raw", "gfs_raw"],
            "forecast_coverage": 1.0,
            "open_markets": 33,
            "open_markets_with_a_book": 31,
            "orderbook_coverage": 31 / 33,
        },
        "data_quality": {"by_kind": {}, "data_gaps": 0, "errors": 0},
        "warnings": [],
        "equity_curve": None,
        "equity_curve_note": "null on purpose: shadow mode places no orders",
    }
    if mode == "REPLAY":
        payload["decisions"]["by_mode"] = {
            "REPLAY": {"evaluated": 42, "by_state": {"ACCEPT": 3}, "accepted": 3},
            "LIVE": {"evaluated": 0, "by_state": {}, "accepted": 0},
        }
        payload["decisions"]["live_accepted"] = 0
        payload["decisions"]["replay_accepted"] = 3
        payload["warnings"] = [
            "3 ACCEPT signal(s) are REPLAY, not LIVE: they are NOT contemporary signals",
        ]
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# archive fixtures
# ---------------------------------------------------------------------------


def gamma_event(*, now: datetime, target: str = "2026-10-12") -> dict[str, Any]:
    """One NYC daily-high event with two brackets, shaped like Gamma's own."""
    return {
        "id": "event-1",
        "slug": f"highest-temperature-in-nyc-on-{target}",
        "title": "Highest temperature in New York City on October 12, 2026?",
        "eventDate": target,
        "description": "Resolution source: KLGA daily max",
        "resolutionSource": "https://example.invalid/klga",
        "markets": [
            {
                "id": f"m-{code}",
                "question": f"Will the highest temperature in New York City be {label} on October 12?",
                "groupItemTitle": label,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": json.dumps([f"tok-{code}-yes", f"tok-{code}-no"]),
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5,
                "feeSchedule": {"rate": 0.0, "exponent": 0},
            }
            for code, label in (("81", "81°F"), ("low", "80°F or below"))
        ],
    }


def envelope(source: str, payload: Any, *, now: datetime, seq: int = 1) -> dict[str, Any]:
    return {
        "archive_seq": seq,
        "source": source,
        "received_at_utc": iso(now),
        "venue_ts_utc": None,
        "venue_ts_origin": "missing",
        "quality": "ok",
        "run_id": "run-collector",
        "payload": payload,
    }


def rest_book(
    token_id: str,
    *,
    bid: str = "0.62",
    ask: str = "0.65",
    now: datetime | None = None,
) -> dict[str, Any]:
    # `timestamp` is the venue's own event time in milliseconds - a stale one
    # makes the book stale, which is the point of carrying it, so the fixture
    # stamps it at the moment it pretends to have been captured.
    venue_ms = int((now or utcnow()).timestamp() * 1000)
    return {
        "market": "0xcondition",
        "asset_id": token_id,
        "timestamp": str(venue_ms),
        "hash": "fixture",
        "bids": [{"price": bid, "size": "100"}, {"price": str(float(bid) - 0.01), "size": "50"}],
        "asks": [{"price": ask, "size": "80"}, {"price": str(float(ask) + 0.01), "size": "40"}],
        "min_order_size": "5",
        "tick_size": "0.001",
        "neg_risk": False,
        "last_trade_price": bid,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_jsonl(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# assembled data trees
# ---------------------------------------------------------------------------


def build_tree(
    root: Path,
    *,
    now: datetime | None = None,
    collector: dict[str, Any] | None = True,
    shadow: dict[str, Any] | None = True,
    report: dict[str, Any] | None = True,
    archive: bool = True,
    producer_snapshots: bool = False,
    collector_age_seconds: float = 5.0,
    shadow_mode: str = "LIVE",
    report_mode: str = "LIVE",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Create a complete read-only data tree. ``None`` skips a piece."""
    now = now or utcnow()
    collector_root = root / "collector"
    shadow_root = root / "shadow"

    if collector:
        payload = collector_heartbeat(now=now, age_seconds=collector_age_seconds) if collector is True else collector
        write_json(collector_root / "health" / "heartbeat.json", payload)
    if shadow:
        payload = shadow_heartbeat(now=now, mode=shadow_mode) if shadow is True else shadow
        write_json(shadow_root / "health" / "heartbeat.json", payload)
    if report:
        payload = shadow_report(now=now, mode=report_mode) if report is True else report
        write_json(shadow_root / "reports" / "report.json", payload)
    if archive:
        target = str((now + timedelta(days=1)).date())
        gamma = gamma_event(now=now, target=target)
        lines = [
            envelope("gamma", gamma, now=now - timedelta(seconds=20), seq=1),
            envelope("rest", rest_book("tok-81-yes", now=now - timedelta(seconds=5)), now=now - timedelta(seconds=5), seq=2),
            envelope(
                "rest",
                rest_book("tok-low-yes", bid="0.10", ask="0.13", now=now - timedelta(seconds=4)),
                now=now - timedelta(seconds=4),
                seq=3,
            ),
        ]
        write_jsonl(collector_root / "raw" / "gamma" / f"{now.date().isoformat()}.jsonl", [lines[0]])
        write_jsonl(collector_root / "raw" / "rest" / f"{now.date().isoformat()}.jsonl", lines[1:])
    if producer_snapshots:
        write_producer_snapshots(shadow_root, now=now)
    for rel, payload in (extra or {}).items():
        write_json(root / rel, payload)
    return root


def write_producer_snapshots(shadow_root: Path, *, now: datetime) -> None:
    """The three contract snapshots as the (not yet wired) producer emits them."""
    reports = shadow_root / "reports"
    decisions = {
        "schema_version": 1,
        "kind": "recent-decisions",
        "generated_at_utc": iso(now),
        "source_service": "weatherquant-shadow",
        "source_run_id": "run-shadow",
        "source_version": "0.1.1",
        "stale_after_seconds": 300.0,
        "data": {
            "decisions": [
                {
                    "decision_id": f"d-{i}",
                    "decided_at_utc": iso(now - timedelta(seconds=30 * i)),
                    "market_id": "m-81",
                    "market_question": "Will the highest temperature ... be 81°F ...?",
                    "bracket_label": "81°F",
                    "target_date": str((now + timedelta(days=1)).date()),
                    "direction": "buy_yes",
                    "state": state,
                    "mode": "LIVE",
                    "model_name": "calibrated",
                    "model_version": "v1",
                    "model_probability": 0.41,
                    "market_price": 0.65,
                    "best_bid": 0.62,
                    "best_ask": 0.65,
                    "net_edge_usd": -0.02 if state == "REJECT" else 0.03,
                    "primary_reason": None if state == "ACCEPT" else "EDGE_BELOW_THRESHOLD",
                    "reasons": [] if state == "ACCEPT" else ["EDGE_BELOW_THRESHOLD"],
                    "snapshot_age_ms": 400,
                    "forecast_age_hours": 3.5,
                    "available_depth_usd": 48.0,
                    "thresholds_version": "v3",
                    "run_id": "run-shadow",
                }
                for i, state in enumerate(("ACCEPT", "REJECT", "REJECT", "NO_TRADE"))
            ]
        },
    }
    write_json(reports / "recent-decisions.json", decisions)

    market_snapshot = {
        "schema_version": 1,
        "kind": "market-snapshot",
        "generated_at_utc": iso(now),
        "source_service": "weatherquant-shadow",
        "source_run_id": "run-shadow",
        "source_version": "0.1.1",
        "stale_after_seconds": 300.0,
        "data": {
            "primary_model": "calibrated",
            "markets": [
                {
                    "market_id": "m-81",
                    "event_id": "event-1",
                    "question": "Will the highest temperature in New York City be 81°F on October 12?",
                    "target_date": str((now + timedelta(days=1)).date()),
                    "bracket_label": "81°F",
                    "station": "KLGA",
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "tradeable": True,
                    "token_yes": "tok-81-yes",
                    "book": {
                        "available": True,
                        "best_bid": 0.62,
                        "best_ask": 0.65,
                        "spread": 0.03,
                        "bid_depth_usd": 62.0,
                        "ask_depth_usd": 52.0,
                        "levels_bid": 2,
                        "levels_ask": 2,
                        "defects": [],
                        "quality": "ok",
                        "as_of_utc": iso(now - timedelta(seconds=3)),
                        "venue_ts_utc": None,
                    },
                    "model_probability": 0.41,
                    "net_edge_usd": 0.03,
                    "last_decision": {"state": "ACCEPT", "decided_at_utc": iso(now - timedelta(seconds=30)), "mode": "LIVE"},
                    "updated_at_utc": iso(now - timedelta(seconds=3)),
                }
            ],
        },
    }
    write_json(reports / "market-snapshot.json", market_snapshot)

    forecast_snapshot = {
        "schema_version": 1,
        "kind": "forecast-snapshot",
        "generated_at_utc": iso(now),
        "source_service": "weatherquant-shadow",
        "source_run_id": "run-shadow",
        "source_version": "0.1.1",
        "stale_after_seconds": 600.0,
        "data": {
            "primary_model": "calibrated",
            "distributions": [
                {
                    "model": "calibrated",
                    "version": "v1",
                    "target_date": str((now + timedelta(days=1)).date()),
                    "forecast_run_time_utc": iso(now - timedelta(hours=6)),
                    "forecast_received_at_utc": iso(now - timedelta(hours=6)),
                    "training_cutoff_utc": "2026-09-01T00:00:00+00:00",
                    "predicted_max_f": 81.4,
                    "station": "KLGA",
                    "buckets": [
                        {"label": "79°F", "probability": 0.08},
                        {"label": "80°F", "probability": 0.21},
                        {"label": "81°F", "probability": 0.41},
                        {"label": "82°F", "probability": 0.22},
                        {"label": "83°F", "probability": 0.08},
                    ],
                },
                {
                    "model": "gfs_raw",
                    "version": "v1",
                    "target_date": str((now + timedelta(days=1)).date()),
                    "forecast_run_time_utc": iso(now - timedelta(hours=6)),
                    "forecast_received_at_utc": iso(now - timedelta(hours=6)),
                    "training_cutoff_utc": "2026-09-01T00:00:00+00:00",
                    "predicted_max_f": 80.9,
                    "station": "KLGA",
                    "buckets": [
                        {"label": "79°F", "probability": 0.1},
                        {"label": "80°F", "probability": 0.27},
                        {"label": "81°F", "probability": 0.38},
                        {"label": "82°F", "probability": 0.18},
                        {"label": "83°F", "probability": 0.07},
                    ],
                },
            ],
        },
    }
    write_json(reports / "forecast-snapshot.json", forecast_snapshot)

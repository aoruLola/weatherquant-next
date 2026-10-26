"""Paper engine fixtures: a builder for published decision-event rows.

The rows mirror ``shadow.export.EXPORT_COLUMNS``' names, because that is
what the consumer will actually receive. Building them here (rather than
copy-pasting dicts across tests) keeps the contract under test instead of
the fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def export_row(**overrides: Any) -> dict[str, Any]:
    """A minimal, well-formed LIVE ACCEPT event row, with evidence attached."""
    row: dict[str, Any] = {
        "decision_id": "d" * 32,
        "run_id": "r" * 32,
        "cycle_id": "c" * 16,
        "market_id": "mkt-1",
        "event_id": "evt-1",
        "token_id": "tok-yes",
        "bracket_label": "75-80",
        "target_date": "2026-10-26",
        "station": "KNYC",
        "direction": "buy_yes",
        "state": "ACCEPT",
        "mode": "LIVE",
        "decision_time_utc": T0.isoformat(),
        "snapshot_id": "s" * 32,
        "book_id": "b" * 64,
        "book_source": "ws",
        "venue_ts_utc": datetime(2026, 10, 26, 11, 59, 30, tzinfo=UTC).isoformat(),
        "collector_received_at_utc": datetime(2026, 10, 26, 11, 59, 31, tzinfo=UTC).isoformat(),
        "shadow_received_at_utc": datetime(2026, 10, 26, 11, 59, 31, tzinfo=UTC).isoformat(),
        "snapshot_age_ms": 30000,
        "model_probability": 0.62,
        "market_price": 0.55,
        "best_bid": 0.54,
        "best_ask": 0.56,
        "spread": 0.02,
        "available_depth_usd": 250.0,
        "net_edge": 0.03,
        "estimated_fees_usd": 0.0031,
        "estimated_buffer_usd": 0.14,
        "estimated_cost_usd": 57.4,
        "expected_net_profit_usd": 4.6,
        "proposed_shares": 100.0,
        "estimated_fill_shares": 100.0,
        "estimated_fill_price": 0.56,
        "thresholds_version": "v2",
        "created_at_utc": T0.isoformat(),
        "mode_reason": "auto:archive_age_2s_le_900s",
        "fee_rate": 0.05,
        "fee_exponent": None,
    }
    row.update(overrides)
    return row

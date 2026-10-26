"""The decision-event contract: what a paper engine may act on.

The shadow engine publishes its decision history as append-only JSONL parts
(:mod:`weatherquant.shadow.export`). One published row, decoded and
validated, is a :class:`SignalEvent` - the *only* shape of input this engine
accepts from the shadow, and the reason the engine can promise never to read
the shadow's database: everything it needs to decide and to price is in the
row, or in the collector's archive at the instant the row names.

Three rules the contract enforces, in order of importance:

**Mode is a gate, not metadata.** A ``REPLAY`` row is a decision someone
re-derived from history *now*; its inputs were not knowable at its own
decision instant, and pricing it into a live account would be backtesting
under a live label. :attr:`SignalEvent.tradeable` is False for every REPLAY
row - and for every non-ACCEPT row, because only an ACCEPT was proposed as a
trade in the first place.

**Refusals are records, not exceptions.** An event that fails the contract
is either malformed (``EventError`` - it never becomes a signal) or a valid
event the account must refuse (a recorded refusal with a stable reason).
The distinction matters: the first is a bug in the producer or the reader,
the second is a normal, auditable outcome.

**Evidence travels with the event.** ``book_id``, ``snapshot_id``, the
venue and collector clocks, the fee schedule and the thresholds version are
carried verbatim so every downstream figure can be traced to the exact
market state the decision was made against. The full published row is
additionally stored unmodified by the store layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "DecisionState",
    "Direction",
    "EventError",
    "Mode",
    "RefusalReason",
    "SignalEvent",
    "parse_signal_event",
]


class EventError(ValueError):
    """A published row is not a decodable decision event.

    ``code`` is a stable identifier the consumer records as a data-quality
    event. A malformed row must never become a signal row, and must never be
    silently skipped - both ends of that are how an audit trail goes quiet.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class Mode(StrEnum):
    """Mirrors ``shadow.decision.Mode``; declared here so the paper package
    does not import the shadow's writer-side modules."""

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    UNKNOWN = "UNKNOWN"


class DecisionState(StrEnum):
    """Mirrors ``shadow.decision.State``."""

    CANDIDATE = "CANDIDATE"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NO_TRADE = "NO_TRADE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class Direction(StrEnum):
    """Mirrors ``shadow.decision.Direction``."""

    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"


class RefusalReason(StrEnum):
    """Why a *valid* event did not enter the virtual account.

    This is the paper engine's own vocabulary, disjoint from the shadow's
    decision ``Reason`` set: a shadow rejection never reaches this engine as
    a signal (it is not an ACCEPT), so every reason here answers "why did a
    proposed trade not become a simulated position".

    ``DUPLICATE_SIGNAL`` deserves its comment: the same ``decision_id``
    arriving twice is not an error and not a second trade - it is the same
    fact being re-delivered, and the record's job is to say so on the row
    that already exists.
    """

    NOT_LIVE = "NOT_LIVE"                      # REPLAY or UNKNOWN mode
    NOT_ACCEPT = "NOT_ACCEPT"                  # state is not ACCEPT
    STALE_SIGNAL = "STALE_SIGNAL"              # older than the freshness window at consumption
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"        # market lifecycle says closed at consumption
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"      # already consumed; recorded on the existing row
    NOT_EXECUTABLE = "NOT_EXECUTABLE"          # no point-in-time book evidence: missing or timed out
    NO_BOOK_EVIDENCE = "NO_BOOK_EVIDENCE"      # (pre-WQN-24 spelling of NOT_EXECUTABLE; kept for recorded rows)
    BOOK_EVIDENCE_MISMATCH = "BOOK_EVIDENCE_MISMATCH"  # resolved book is not the referenced one
    BOOK_STALE = "BOOK_STALE"                  # resolved book is older than the freshness limit
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"    # book cannot cover the order
    RISK_BUDGET_EXCEEDED = "RISK_BUDGET_EXCEEDED"      # a configured risk gate refused
    RISK_EXPOSURE_EXCEEDED = "RISK_EXPOSURE_EXCEEDED"  # one market's cumulative cap
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"  # frozen cash + cost exceeds available
    NO_INVENTORY = "NO_INVENTORY"              # a SELL YES with nothing to sell
    UNPRICABLE_EVENT = "UNPRICABLE_EVENT"      # fee schedule or price data unusable
    ENGINE_DISABLED = "ENGINE_DISABLED"        # consuming without an approved account


#: The fields a published row MUST carry to be a decodable event. Everything
#: else is optional evidence, present when the decision had it.
_REQUIRED_FIELDS = (
    "decision_id",
    "run_id",
    "cycle_id",
    "market_id",
    "direction",
    "state",
    "mode",
    "decision_time_utc",
)

#: Numeric evidence fields: present -> must parse as finite floats.
_FLOAT_FIELDS = (
    "net_edge",
    "best_bid",
    "best_ask",
    "spread",
    "available_depth_usd",
    "model_probability",
    "market_price",
    "proposed_shares",
    "estimated_fill_shares",
    "estimated_fill_price",
    "estimated_fees_usd",
    "estimated_buffer_usd",
    "estimated_cost_usd",
    "expected_net_profit_usd",
    "fee_rate",
    "fee_exponent",
    "snapshot_age_ms",
)


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One published decision, decoded. Immutable by construction."""

    decision_id: str
    run_id: str
    cycle_id: str
    market_id: str
    event_id: str | None
    token_id: str | None
    bracket_label: str | None
    direction: Direction
    state: DecisionState
    mode: Mode
    decision_time_utc: datetime

    # Evidence: the market state and the clocks that produced the decision.
    book_id: str | None = None
    snapshot_id: str | None = None
    venue_ts_utc: datetime | None = None
    collector_received_at_utc: datetime | None = None
    snapshot_age_ms: float | None = None

    # The decision's own numbers.
    model_probability: float | None = None
    market_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    available_depth_usd: float | None = None
    net_edge: float | None = None
    proposed_shares: float | None = None
    estimated_fill_shares: float | None = None
    estimated_fill_price: float | None = None
    estimated_fees_usd: float | None = None
    estimated_buffer_usd: float | None = None
    estimated_cost_usd: float | None = None
    expected_net_profit_usd: float | None = None

    # The schedule and the gates the decision was made under. Both are
    # carried so a fill computed later is priced by the rules of the
    # decision's moment, not of the fill's.
    fee_rate: float | None = None
    fee_exponent: float | None = None
    thresholds_version: str | None = None

    # The published row, verbatim. The store persists it unmodified so an
    # audit never depends on this dataclass having kept every field.
    raw: dict[str, Any] | None = None

    @property
    def tradeable(self) -> bool:
        """Whether the contract even allows this event to be considered.

        LIVE + ACCEPT, and nothing else. The risk gates, the fill engine and
        the account all come later; this is the boundary between "the shadow
        proposed a live trade" and everything that can still say no.
        """
        return self.state is DecisionState.ACCEPT and self.mode is Mode.LIVE

    def to_json(self) -> str:
        return json.dumps(self.raw or {}, sort_keys=True, default=str)


def parse_signal_event(row: Any) -> SignalEvent:
    """Decode one published decisions-export row.

    Raises :class:`EventError` with a stable code for anything that is not a
    complete, well-typed event. A row that decodes but is not *tradeable*
    (REPLAY, or a non-ACCEPT state) decodes fine - refusing it is the
    consumer's recorded decision, not a parse failure.
    """
    if not isinstance(row, dict):
        raise EventError("MALFORMED_EVENT", f"event row is {type(row).__name__}, expected an object")

    missing = [field for field in _REQUIRED_FIELDS if row.get(field) in (None, "")]
    if missing:
        raise EventError("MALFORMED_EVENT", f"event row is missing field(s): {missing}")

    try:
        direction = Direction(str(row["direction"]))
    except ValueError:
        raise EventError(
            "MALFORMED_EVENT",
            f"unknown direction {row['direction']!r}",
        ) from None
    try:
        state = DecisionState(str(row["state"]))
    except ValueError:
        raise EventError(
            "MALFORMED_EVENT",
            f"unknown decision state {row['state']!r}",
        ) from None
    try:
        mode = Mode(str(row["mode"]))
    except ValueError:
        raise EventError(
            "MALFORMED_EVENT",
            f"unknown mode {row['mode']!r}",
        ) from None

    decision_time = _require_tz_datetime(row, "decision_time_utc")

    floats: dict[str, float | None] = {}
    for name in _FLOAT_FIELDS:
        value = row.get(name)
        if value is None or value == "":
            floats[name] = None
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise EventError(
                "MALFORMED_EVENT",
                f"field {name!r} is {value!r}, expected a number",
            ) from None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            raise EventError("MALFORMED_EVENT", f"field {name!r} is not finite")
        floats[name] = parsed

    return SignalEvent(
        decision_id=str(row["decision_id"]),
        run_id=str(row["run_id"]),
        cycle_id=str(row["cycle_id"]),
        market_id=str(row["market_id"]),
        event_id=_opt_str(row.get("event_id")),
        token_id=_opt_str(row.get("token_id")),
        bracket_label=_opt_str(row.get("bracket_label")),
        direction=direction,
        state=state,
        mode=mode,
        decision_time_utc=decision_time,
        book_id=_opt_str(row.get("book_id")),
        snapshot_id=_opt_str(row.get("snapshot_id")),
        venue_ts_utc=_opt_datetime(row.get("venue_ts_utc")),
        collector_received_at_utc=_opt_datetime(row.get("collector_received_at_utc")),
        snapshot_age_ms=floats["snapshot_age_ms"],
        model_probability=floats["model_probability"],
        market_price=floats["market_price"],
        best_bid=floats["best_bid"],
        best_ask=floats["best_ask"],
        spread=floats["spread"],
        available_depth_usd=floats["available_depth_usd"],
        net_edge=floats["net_edge"],
        proposed_shares=floats["proposed_shares"],
        estimated_fill_shares=floats["estimated_fill_shares"],
        estimated_fill_price=floats["estimated_fill_price"],
        estimated_fees_usd=floats["estimated_fees_usd"],
        estimated_buffer_usd=floats["estimated_buffer_usd"],
        estimated_cost_usd=floats["estimated_cost_usd"],
        expected_net_profit_usd=floats["expected_net_profit_usd"],
        fee_rate=floats["fee_rate"],
        fee_exponent=floats["fee_exponent"],
        thresholds_version=_opt_str(row.get("thresholds_version")),
        raw=dict(row),
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_tz_datetime(row: dict[str, Any], name: str) -> datetime:
    value = row.get(name)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise EventError(
            "MALFORMED_EVENT",
            f"field {name!r} is {value!r}, expected an ISO-8601 timestamp",
        ) from None
    if parsed.tzinfo is None:
        raise EventError(
            "MALFORMED_EVENT",
            f"field {name!r} is timezone-naive; a decision instant without a "
            "timezone cannot be compared against books or settlements",
        )
    return parsed


def _opt_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed

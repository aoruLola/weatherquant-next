"""The decision state machine: one market, one direction, one verdict.

State definitions (strict, so they are testable rather than decorative)
----------------------------------------------------------------------
``CANDIDATE``
    Written when an evaluation starts and not yet finished. A row still in
    this state after a restart is evidence of an interrupted cycle - which is
    the point: it is the only way a crash between "began evaluating" and
    "recorded the verdict" becomes visible in the data.

``NO_TRADE``
    The market itself is not open to us (``MARKET_CLOSED``: inactive, not
    accepting orders, closed, or its target day has already ended). Nothing
    was refused - there was nothing to consider.

``DATA_UNAVAILABLE``
    A **required input is absent**: the feed is silent, there is no book, no
    publishable forecast, no model artifact, or the metadata needed to price
    the market could not be parsed. No edge is computed at all, because there
    is nothing honest to compute it from.

``REJECT``
    The inputs were **present** and a gate refused the trade: the book is
    stale or one-sided, the fee schedule is unknown, depth will not cover the
    size, the clocks disagree, or the edge is negative or too small. An edge
    figure may still be recorded - it is evidence about *why* the refusal
    happened, not a signal.

``ACCEPT``
    Every gate passed and ``net_edge >= min_net_edge_usd_per_share``. In shadow
    mode this is an observation, never an order.

The dividing line between the two failures is deliberately mechanical:
**absent -> DATA_UNAVAILABLE, present-but-refused -> REJECT.** Ambiguity there
is what makes a report unreadable, so it is decided by a rule, not per call.

Primary reason
--------------
All reasons that apply are stored, normalised into :data:`CAUSAL_ORDER`. The
*primary* one is the first in that order - the earliest broken precondition -
so the primary reason is stable regardless of which gate happened to fire
first. "The market was closed and the feed was also down" reports
``MARKET_CLOSED``, because that is what would have blocked the trade even if
the feed had been perfect.

Never a bare error
------------------
Every refusal carries a reason from :class:`Reason`. An unexpected exception
becomes ``EVALUATION_ERROR`` with the exception text in ``detail`` - specific
and greppable, not a ``generic_error`` that says nothing and hides everything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from weatherquant.execution.pricing import (
    ExecutionRefused,
    FeeStructureUnknown,
    build_fill_plan,
    edge_usd_per_share,
    edge_usd_per_share_sell,
    execution_cost,
    expected_net_profit_usd,
    expected_net_profit_usd_sell,
)
from weatherquant.markets.models import Side
from weatherquant.shadow.config import Thresholds
from weatherquant.shadow.forecast import ResolvedForecast
from weatherquant.shadow.inference import ModelRefusal, PredictionResult

__all__ = [
    "CAUSAL_ORDER",
    "DATA_UNAVAILABLE_REASONS",
    "Decision",
    "Direction",
    "Mode",
    "Reason",
    "State",
    "evaluate",
]


class State(StrEnum):
    CANDIDATE = "CANDIDATE"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NO_TRADE = "NO_TRADE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class Mode(StrEnum):
    """What kind of data this decision was made against.

    The distinction is not cosmetic. A REPLAY decision whose inputs were only
    obtainable *now* is not a decision anyone could have made at that moment,
    and counting it beside a LIVE one would make the log say something that
    never happened.
    """

    LIVE = "LIVE"
    REPLAY = "REPLAY"
    UNKNOWN = "UNKNOWN"


class Reason(StrEnum):
    NO_FORECAST = "NO_FORECAST"
    FORECAST_NOT_YET_AVAILABLE = "FORECAST_NOT_YET_AVAILABLE"
    NO_VALID_MODEL = "NO_VALID_MODEL"
    MODEL_STALE = "MODEL_STALE"
    NO_ORDERBOOK = "NO_ORDERBOOK"
    STALE_ORDERBOOK = "STALE_ORDERBOOK"
    INCOMPLETE_ORDERBOOK = "INCOMPLETE_ORDERBOOK"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    UNKNOWN_FEE = "UNKNOWN_FEE"
    NEGATIVE_NET_EDGE = "NEGATIVE_NET_EDGE"
    EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
    TIME_MISMATCH = "TIME_MISMATCH"
    MARKET_CLOSED = "MARKET_CLOSED"
    COLLECTOR_DISCONNECTED = "COLLECTOR_DISCONNECTED"
    DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"
    EVALUATION_ERROR = "EVALUATION_ERROR"


#: Causal order: the earliest broken precondition is the primary reason.
CAUSAL_ORDER: tuple[Reason, ...] = (
    Reason.MARKET_CLOSED,
    Reason.COLLECTOR_DISCONNECTED,
    Reason.DATA_QUALITY_FAILURE,
    Reason.NO_ORDERBOOK,
    Reason.NO_FORECAST,
    Reason.FORECAST_NOT_YET_AVAILABLE,
    Reason.NO_VALID_MODEL,
    Reason.MODEL_STALE,
    Reason.STALE_ORDERBOOK,
    Reason.TIME_MISMATCH,
    Reason.INCOMPLETE_ORDERBOOK,
    Reason.UNKNOWN_FEE,
    Reason.INSUFFICIENT_LIQUIDITY,
    Reason.NEGATIVE_NET_EDGE,
    Reason.EDGE_BELOW_THRESHOLD,
    Reason.EVALUATION_ERROR,
)

#: Reasons that mean "the input is not there" rather than "a gate said no".
DATA_UNAVAILABLE_REASONS = frozenset(
    {
        Reason.COLLECTOR_DISCONNECTED,
        Reason.NO_FORECAST,
        Reason.FORECAST_NOT_YET_AVAILABLE,
        Reason.NO_VALID_MODEL,
        Reason.NO_ORDERBOOK,
        Reason.DATA_QUALITY_FAILURE,
        Reason.EVALUATION_ERROR,
    }
)

#: Reasons that mean "there was nothing to consider".
NO_TRADE_REASONS = frozenset({Reason.MARKET_CLOSED})

#: A missing input that makes the arithmetic meaningless, so no edge is derived.
_BLOCKING = frozenset(
    {
        Reason.NO_ORDERBOOK,
        Reason.DATA_QUALITY_FAILURE,
        Reason.NO_VALID_MODEL,
        Reason.NO_FORECAST,
        Reason.FORECAST_NOT_YET_AVAILABLE,
        Reason.COLLECTOR_DISCONNECTED,
        Reason.MARKET_CLOSED,
    }
)


class Direction(StrEnum):
    BUY_YES = "buy_yes"
    SELL_YES = "sell_yes"

    @property
    def side(self) -> Side:
        # A shadow sell of YES is priced from the bid - the side a seller
        # actually hits - and never from the ask or the midpoint. Both
        # directions read the YES token's book.
        return Side.BUY if self is Direction.BUY_YES else Side.SELL


@dataclass(frozen=True, slots=True)
class Decision:
    """One verdict, with every input that produced it attached."""

    cycle_id: str
    market_id: str
    event_id: str | None
    token_id: str | None
    bracket_label: str | None
    target_date: object | None
    station: str
    direction: Direction
    state: State
    reasons: tuple[Reason, ...]
    details: dict[str, str]
    decision_time_utc: datetime
    thresholds_version: str
    mode: Mode = Mode.UNKNOWN
    mode_reason: str | None = None
    market_closed: bool = False

    model_name: str | None = None
    model_version: str | None = None
    model_run_id: str | None = None
    forecast_run_time_utc: datetime | None = None
    forecast_age_hours: float | None = None

    snapshot_id: str | None = None
    book_id: str | None = None
    book_source: str | None = None
    venue_ts_utc: datetime | None = None
    collector_received_at_utc: datetime | None = None
    shadow_received_at_utc: datetime | None = None
    snapshot_age_ms: int | None = None

    model_probability: float | None = None
    market_price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    available_depth_usd: float | None = None
    proposed_shares: float | None = None
    estimated_fill_shares: float | None = None
    estimated_fill_price: float | None = None
    estimated_fees_usd: float | None = None
    estimated_buffer_usd: float | None = None
    estimated_cost_usd: float | None = None
    expected_net_profit_usd: float | None = None
    net_edge: float | None = None
    decision_latency_ms: int | None = None

    @property
    def primary_reason(self) -> Reason | None:
        return self.reasons[0] if self.reasons else None

    @property
    def reason_codes(self) -> list[str]:
        return [r.value for r in self.reasons]

    def summary(self) -> str:
        primary = f" [{self.primary_reason.value}]" if self.primary_reason else ""
        return (
            f"[{self.mode.value}] {self.market_id} {self.direction.value} "
            f"{self.state.value}{primary}"
        )


def evaluate(
    *,
    cycle_id: str,
    market,
    direction: Direction,
    prediction: PredictionResult | ModelRefusal | None,
    forecast: ResolvedForecast | None,
    snapshot,
    thresholds: Thresholds,
    buffer_fraction: float,
    decision_time: datetime,
    collector_last_seen: datetime | None,
    collector_silence_seconds: float,
    max_forecast_age_hours: float,
    market_closed: bool,
    closed_detail: str = "",
    # UNKNOWN, not LIVE: a caller that forgets to state the mode must not be
    # handed a row that claims to be contemporary. The runner always passes it.
    mode: Mode = Mode.UNKNOWN,
    mode_reason: str | None = None,
) -> Decision:
    """Run every gate that can run and produce one verdict.

    Gates run as far as the data allows rather than stopping at the first
    failure, so a record can carry several reasons. What is *never* recorded is
    a fabricated number: when a gate means the arithmetic would be meaningless
    (no fee rate, no probability, no book), the derived fields stay ``None``
    instead of being filled with an estimate.

    ``mode`` changes *when* the clocks are read against, not what is true:

    * ``LIVE`` - every input must already be in hand at ``decision_time``.
    * ``REPLAY`` - ``decision_time`` is the instant the archive represents, so
      this process's own receipt times legitimately fall *after* it; those two
      checks are waived because they are an artefact of replaying, not a leak.
      The venue-vs-collector clock check is **not** waived: it compares two
      upstream clocks and holds in either mode. Silence is not a fault either,
      because no live feed was expected.
    """
    reasons: list[Reason] = []
    details: dict[str, str] = {}

    def fail(reason: Reason, detail: str) -> None:
        reasons.append(reason)
        # Several gates can fail with the *same* reason (two liquidity checks,
        # for instance). Concatenating rather than overwriting keeps every
        # explanation - dropping the first would hide half of why it failed.
        key = reason.value
        details[key] = f"{details[key]} | {detail}" if key in details else detail

    # --- 1. the market itself -------------------------------------------
    if market_closed:
        fail(Reason.MARKET_CLOSED, closed_detail or "market is not open for new orders")

    # --- 2. the feed ------------------------------------------------------
    # In replay there is no feed to be silent: the archive is the input, and
    # its age is already expressed by decision_time being the archive's own
    # instant. Reporting COLLECTOR_DISCONNECTED here would misdescribe a
    # deliberate re-read as an outage.
    if mode is not Mode.REPLAY and collector_last_seen is None:
        fail(Reason.COLLECTOR_DISCONNECTED, "no archive line has ever been read from the collector")
    elif mode is not Mode.REPLAY:
        silence = (decision_time - collector_last_seen).total_seconds()
        if silence > collector_silence_seconds:
            fail(
                Reason.COLLECTOR_DISCONNECTED,
                f"collector last seen {silence:.0f}s before this decision "
                f"(limit {collector_silence_seconds:.0f}s); its view of the market "
                "may predate this instant",
            )

    # --- 3. metadata ------------------------------------------------------
    if market.bracket is None:
        fail(
            Reason.DATA_QUALITY_FAILURE,
            market.bracket_error or f"market {market.market_id} has no usable bracket",
        )
    if market.target_date is None:
        fail(Reason.DATA_QUALITY_FAILURE, "market has no target civil date")

    # --- 4. the book ------------------------------------------------------
    snapshot_age_ms: int | None = None
    if market.fee_rate is None:
        # A market that never reported a schedule is recorded as such even when
        # no forecast exists yet, so an unknown fee can never pass unnoticed.
        fail(Reason.UNKNOWN_FEE, "market did not report a fee schedule")

    book = None
    if snapshot is None:
        fail(Reason.NO_ORDERBOOK, f"no order book observed for token {market.token_yes}")
    else:
        book = snapshot.book
        snapshot_age_ms = _snapshot_age_ms(snapshot, decision_time)
        blocked = _blocking_defect(snapshot, direction)
        if blocked:
            fail(
                Reason.INCOMPLETE_ORDERBOOK,
                f"book {snapshot.book_id} cannot price a {direction.value}: {blocked}",
            )
        if snapshot_age_ms is not None and snapshot_age_ms > thresholds.max_snapshot_age_ms:
            fail(
                Reason.STALE_ORDERBOOK,
                f"book is {snapshot_age_ms}ms old at decision time (limit "
                f"{thresholds.max_snapshot_age_ms:.0f}ms)",
            )
        elif snapshot_age_ms is None:
            fail(
                Reason.STALE_ORDERBOOK,
                "book carries no venue timestamp, so its age cannot be established "
                "and it cannot be shown to be fresh",
            )
        _check_clocks(snapshot, decision_time, fail, mode)

    # --- 5. the forecast --------------------------------------------------
    q: float | None = None
    if isinstance(prediction, ModelRefusal):
        fail(_map_refusal(prediction.reason), prediction.detail)
    elif prediction is None:
        fail(Reason.NO_FORECAST, "no forecast was resolved for this market's target date")
    elif prediction.training_cutoff_utc > decision_time:
        # Checked again here, not only when the artifact was loaded: a
        # prediction whose parameters postdate the decision is the leak itself,
        # and this is the last place it can be caught.
        fail(
            Reason.NO_VALID_MODEL,
            f"model {prediction.shadow_model}@{prediction.model_version} was fitted on data "
            f"up to {prediction.training_cutoff_utc.isoformat()}, after the decision instant "
            f"{decision_time.isoformat()}",
        )
    elif forecast is None:
        fail(Reason.NO_FORECAST, "a prediction exists but its forecast is missing")
    else:
        ok, why = forecast.usable_at(decision_time, max_age_hours=max_forecast_age_hours)
        if not ok:
            fail(_map_forecast(why), why or "forecast refused")
        elif market.bracket is not None:
            label = market.bracket.label
            probs = prediction.probabilities
            if label not in probs:
                fail(
                    Reason.DATA_QUALITY_FAILURE,
                    f"bracket {label!r} is not in the prediction (has {sorted(probs)[:8]})",
                )
            else:
                q = probs[label]
        if mode is not Mode.REPLAY and forecast.received_at_utc > decision_time:
            fail(
                Reason.TIME_MISMATCH,
                f"forecast was received at {forecast.received_at_utc.isoformat()}, after "
                f"the decision instant {decision_time.isoformat()}",
            )

    # --- 6. pricing gates -------------------------------------------------
    fill = cost = edge = profit = None
    if q is not None and book is not None and not (set(reasons) & _BLOCKING):
        try:
            fill, cost, edge, profit = _price(
                market=market,
                snapshot=snapshot,
                direction=direction,
                q=q,
                thresholds=thresholds,
                buffer_fraction=buffer_fraction,
            )
        except FeeStructureUnknown as exc:
            fail(Reason.UNKNOWN_FEE, str(exc))
        except ExecutionRefused as exc:
            _map_refusal_error(exc, fail)
        except Exception as exc:
            fail(Reason.EVALUATION_ERROR, f"{type(exc).__name__}: {exc}")

    if fill is not None and thresholds.require_complete_fill and not fill.complete:
        fail(
            Reason.INSUFFICIENT_LIQUIDITY,
            f"only {fill.filled_shares:.1f} of {fill.requested_shares:.1f} shares "
            f"fillable across {fill.levels_consumed} level(s)",
        )

    if market.min_order_size and thresholds.proposed_shares < market.min_order_size:
        # An ACCEPT that could not have been placed is a lie about what the
        # system observed, so the venue's own minimum is a gate.
        fail(
            Reason.INSUFFICIENT_LIQUIDITY,
            f"proposed {thresholds.proposed_shares:g} shares is below the market's "
            f"minimum order size {market.min_order_size:g}",
        )

    depth = _side_depth(snapshot, direction)
    if depth is not None and depth < thresholds.min_depth_usd:
        fail(
            Reason.INSUFFICIENT_LIQUIDITY,
            f"side depth {depth:.2f} USD < required {thresholds.min_depth_usd} USD",
        )

    if edge is not None:
        if edge <= 0.0:
            fail(
                Reason.NEGATIVE_NET_EDGE,
                f"net edge {edge:.4f} USD/share after fill, fees and buffer",
            )
        elif edge < thresholds.min_net_edge_usd_per_share:
            fail(
                Reason.EDGE_BELOW_THRESHOLD,
                f"net edge {edge:.4f} < required {thresholds.min_net_edge_usd_per_share} "
                f"USD/share (thresholds {thresholds.version})",
            )

    ordered = _dedupe_causal(reasons)
    return Decision(
        cycle_id=cycle_id,
        market_id=market.market_id,
        event_id=market.event_id,
        token_id=market.token_yes,
        bracket_label=market.bracket_label or None,
        target_date=market.target_date,
        station=market.station,
        direction=direction,
        state=_final_state(ordered),
        reasons=tuple(ordered),
        details=details,
        decision_time_utc=decision_time,
        thresholds_version=thresholds.version,
        mode=mode,
        mode_reason=mode_reason,
        market_closed=market_closed,
        model_name=getattr(prediction, "shadow_model", None),
        model_version=getattr(prediction, "model_version", None),
        model_run_id=getattr(prediction, "model_run_id", None),
        forecast_run_time_utc=forecast.run_time_utc if forecast else None,
        forecast_age_hours=(
            (decision_time - forecast.run_time_utc).total_seconds() / 3600.0 if forecast else None
        ),
        snapshot_id=_snapshot_id(snapshot, decision_time) if snapshot else None,
        book_id=snapshot.book_id if snapshot else None,
        book_source=snapshot.book_source if snapshot else None,
        venue_ts_utc=snapshot.venue_ts if snapshot else None,
        collector_received_at_utc=snapshot.collector_received_at_utc if snapshot else None,
        shadow_received_at_utc=snapshot.shadow_received_at_utc if snapshot else None,
        snapshot_age_ms=snapshot_age_ms,
        model_probability=q,
        market_price=_market_price(book, direction),
        best_bid=book.best_bid_usd_per_share if book else None,
        best_ask=book.best_ask_usd_per_share if book else None,
        spread=book.spread_usd_per_share if book else None,
        available_depth_usd=depth,
        proposed_shares=thresholds.proposed_shares if fill else None,
        estimated_fill_shares=fill.filled_shares if fill else None,
        estimated_fill_price=fill.avg_price_usd_per_share if fill else None,
        estimated_fees_usd=cost.fees_usd if cost else None,
        estimated_buffer_usd=cost.execution_buffer_usd if cost else None,
        estimated_cost_usd=_charged_costs(cost),
        expected_net_profit_usd=profit,
        net_edge=edge,
    )


# --- pricing ---------------------------------------------------------------


def _price(*, market, snapshot, direction, q, thresholds, buffer_fraction):
    """Walk the book and cost the fill. Every figure comes from pricing."""
    fill = build_fill_plan(snapshot.book, side=direction.side, shares=thresholds.proposed_shares)
    cost = execution_cost(fill, fee_rate=market.fee_rate, buffer_fraction=buffer_fraction)
    if direction is Direction.BUY_YES:
        profit = expected_net_profit_usd(q=q, filled_shares=fill.filled_shares, cost=cost)
        edge = edge_usd_per_share(q=q, filled_shares=fill.filled_shares, cost=cost)
    else:
        profit = expected_net_profit_usd_sell(q=q, filled_shares=fill.filled_shares, cost=cost)
        edge = edge_usd_per_share_sell(q=q, filled_shares=fill.filled_shares, cost=cost)
    return fill, cost, edge, profit


def _charged_costs(cost) -> float | None:
    """Fees plus execution buffer: the terms charged on top of the fill.

    The fill itself is not included, because for a *sell* that term is money
    received rather than money spent. Keeping one column that means "what the
    fill cost beyond its own notional" avoids a column that silently changes
    meaning by direction; the fill total is ``estimated_fill_price *
    estimated_fill_shares``.
    """
    if cost is None:
        return None
    return cost.fees_usd + cost.execution_buffer_usd


def _map_refusal_error(exc: ExecutionRefused, fail) -> None:
    if exc.code == "NO_LIQUIDITY":
        fail(Reason.INSUFFICIENT_LIQUIDITY, exc.detail or exc.code)
    elif "CROSSED" in exc.code or "NO_ASK_SIDE" in exc.code or "NO_BID_SIDE" in exc.code or "NON_MONOTONIC" in exc.code:
        fail(Reason.INCOMPLETE_ORDERBOOK, f"{exc.code}: {exc.detail}")
    else:
        fail(Reason.DATA_QUALITY_FAILURE, f"{exc.code}: {exc.detail}")


# --- gates -----------------------------------------------------------------


def _blocking_defect(snapshot, direction: Direction) -> str | None:
    """Defects that make *this side* unpriceable, before any arithmetic.

    A buy consumes the ask side, so a scrambled bid side is recorded but does
    not block; a crossed book blocks both, because neither side's prices mean
    anything in that state. This mirrors ``build_fill_plan``'s own rule, so the
    gate and the pricer can never disagree about what is executable.
    """
    blocking = {"CROSSED_OR_LOCKED"}
    blocking.add("NO_ASK_SIDE" if direction is Direction.BUY_YES else "NO_BID_SIDE")
    blocking.add("NON_MONOTONIC_ASK" if direction is Direction.BUY_YES else "NON_MONOTONIC_BID")
    found = [d for d in snapshot.defects if d in blocking]
    return "+".join(sorted(found)) if found else None


def _check_clocks(snapshot, decision_time: datetime, fail, mode: Mode = Mode.UNKNOWN) -> None:
    """No input may originate after the decision instant, and no delta may invert.

    In ``REPLAY`` our own two receipt stamps are excluded: this process read
    the line *now* on purpose, which is what replaying means. The venue check
    stays - a venue timestamp ahead of the decision instant is a clock problem
    in either mode - and the negative-latency check stays, because it compares
    the collector's clock against the venue's and never involves ours.
    """
    if snapshot.venue_ts is not None and snapshot.venue_ts > decision_time:
        fail(
            Reason.TIME_MISMATCH,
            f"venue timestamp {snapshot.venue_ts.isoformat()} is after the decision "
            f"instant {decision_time.isoformat()}",
        )
    if mode is not Mode.REPLAY and snapshot.collector_received_at_utc > decision_time:
        fail(
            Reason.TIME_MISMATCH,
            f"the collector received this book at "
            f"{snapshot.collector_received_at_utc.isoformat()}, after the decision "
            f"instant - it was not knowable then",
        )
    if (
        mode is not Mode.REPLAY
        and snapshot.shadow_received_at_utc is not None
        and snapshot.shadow_received_at_utc > decision_time
    ):
        fail(
            Reason.TIME_MISMATCH,
            f"this process read the book at "
            f"{snapshot.shadow_received_at_utc.isoformat()}, after the decision instant",
        )
    if snapshot.venue_ts is not None:
        latency = (snapshot.collector_received_at_utc - snapshot.venue_ts).total_seconds()
        if latency < 0:
            fail(
                Reason.TIME_MISMATCH,
                f"collector receipt precedes the venue timestamp by {-latency:.3f}s; "
                "the clocks disagree",
            )


def _snapshot_age_ms(snapshot, decision_time: datetime) -> int | None:
    """Age against the venue's own clock. ``None`` when it carries no timestamp -
    a missing venue time is not a zero age, and is not a fresh one either."""
    if snapshot.venue_ts is None:
        return None
    return int((decision_time - snapshot.venue_ts).total_seconds() * 1000)


def _snapshot_id(snapshot, decision_time: datetime) -> str:
    basis = (
        f"{snapshot.book_id}|{snapshot.collector_received_at_utc.isoformat()}"
        f"|{decision_time.isoformat()}"
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _side_depth(snapshot, direction: Direction) -> float | None:
    if snapshot is None:
        return None
    side = Side.BUY if direction is Direction.BUY_YES else Side.SELL
    return snapshot.book.depth_notional_usd(side)


def _market_price(book, direction: Direction) -> float | None:
    """The price this direction would transact at: ask for a buy, bid for a sell."""
    if book is None:
        return None
    return (
        book.best_ask_usd_per_share
        if direction is Direction.BUY_YES
        else book.best_bid_usd_per_share
    )


# --- classification --------------------------------------------------------


def _map_refusal(code: str) -> Reason:
    try:
        return Reason(code)
    except ValueError:
        return Reason.DATA_QUALITY_FAILURE


def _map_forecast(why: str | None) -> Reason:
    text = (why or "").lower()
    if "publishable" in text or "has not happened" in text:
        return Reason.FORECAST_NOT_YET_AVAILABLE
    if "old" in text or "past the" in text:
        return Reason.MODEL_STALE
    return Reason.NO_FORECAST


def _dedupe_causal(reasons: list[Reason]) -> list[Reason]:
    """Unique and ordered, so the primary reason does not depend on gate order."""
    seen = set(reasons)
    ordered = [r for r in CAUSAL_ORDER if r in seen]
    ordered.extend(r for r in reasons if r not in CAUSAL_ORDER and r not in ordered)
    return ordered


def _final_state(ordered: list[Reason]) -> State:
    """Closed wins, then absent, then refused - in that order, always.

    * ``NO_TRADE``: the market is not open. Nothing was evaluated, so nothing
      could have been refused.
    * ``DATA_UNAVAILABLE``: at least one *required input is absent*. Even if a
      gate also fired, the edge could not have been computed from inputs that
      are not there, so "we could not tell" is the honest verdict.
    * ``REJECT``: every input was present and a gate said no.
    * ``ACCEPT``: no reason at all.

    The precedence is what makes the state a function of the reason *set* rather
    than of the order the gates happened to run in.
    """
    if not ordered:
        return State.ACCEPT
    unique = set(ordered)
    if unique & NO_TRADE_REASONS:
        return State.NO_TRADE
    if unique & DATA_UNAVAILABLE_REASONS:
        return State.DATA_UNAVAILABLE
    return State.REJECT

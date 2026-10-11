"""The decision state machine: every state, every reason, the primary-reason rule.

These tests are the executable half of ``docs/shadow-risk-rules.md``. If a gate
changes behaviour, a state boundary blurs, or a reason degrades into something
unspecific, one of them fails.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from shadow_helpers import (
    NOW,
    TARGET_DATE,
    make_catalog_market,
    make_forecast,
    make_observation,
    make_prediction,
)
from weatherquant.execution.pricing import build_fill_plan, execution_cost, taker_fee_usd
from weatherquant.markets.models import Bracket, BracketKind, Side
from weatherquant.shadow.config import load_thresholds
from weatherquant.shadow.decision import (
    CAUSAL_ORDER,
    DATA_UNAVAILABLE_REASONS,
    Direction,
    Mode,
    Reason,
    State,
    evaluate,
)

THRESHOLDS = load_thresholds("configs/shadow-thresholds.json")
BUFFER = 0.005


def _stale_book():
    """A book older than the configured gate, by exactly one millisecond.

    Derived from THRESHOLDS rather than a literal so a gate change moves the
    test instead of silently leaving it testing the wrong side of the line.
    """
    return make_observation(venue_ts=NOW - timedelta(milliseconds=THRESHOLDS.max_snapshot_age_ms + 1))


def _evaluate(**overrides):
    base = dict(
        cycle_id="cycle-1",
        market=make_catalog_market(),
        direction=Direction.BUY_YES,
        prediction=make_prediction(),
        forecast=make_forecast(),
        snapshot=make_observation(),
        thresholds=THRESHOLDS,
        buffer_fraction=BUFFER,
        decision_time=NOW,
        collector_last_seen=NOW - timedelta(seconds=5),
        collector_silence_seconds=900.0,
        max_forecast_age_hours=THRESHOLDS.max_forecast_age_hours,
        market_closed=False,
    )
    base.update(overrides)
    return evaluate(**base)


# --- states ---------------------------------------------------------------


def test_a_healthy_evaluation_accepts_and_records_no_reason() -> None:
    decision = _evaluate()
    assert decision.state is State.ACCEPT
    assert decision.reasons == ()
    assert decision.primary_reason is None
    assert decision.net_edge is not None and decision.net_edge > 0


def test_a_closed_market_is_no_trade_not_a_rejection() -> None:
    decision = _evaluate(market_closed=True, closed_detail="the venue reports this market as closed")
    assert decision.state is State.NO_TRADE
    assert decision.reasons == (Reason.MARKET_CLOSED,)
    # Nothing was priced: there was nothing to price.
    assert decision.net_edge is None
    assert decision.model_probability is None or decision.model_probability >= 0


def test_an_absent_book_is_data_unavailable_not_a_rejection() -> None:
    decision = _evaluate(snapshot=None)
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.reasons == (Reason.NO_ORDERBOOK,)
    assert decision.net_edge is None
    assert decision.estimated_fill_shares is None


def test_a_present_but_stale_book_is_a_rejection() -> None:
    decision = _evaluate(snapshot=_stale_book())
    assert decision.state is State.REJECT
    assert decision.primary_reason is Reason.STALE_ORDERBOOK
    # The edge is still computed - it is evidence about the refusal, not a signal.
    assert decision.net_edge is not None


def test_a_silent_collector_is_data_unavailable() -> None:
    decision = _evaluate(collector_last_seen=NOW - timedelta(minutes=30))
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.reasons == (Reason.COLLECTOR_DISCONNECTED,)


def test_never_seen_a_collector_line_at_all() -> None:
    decision = _evaluate(collector_last_seen=None)
    assert decision.state is State.DATA_UNAVAILABLE
    assert Reason.COLLECTOR_DISCONNECTED in decision.reasons


def test_every_state_the_spec_names_is_reachable() -> None:
    reached = {
        _evaluate().state,
        _evaluate(market_closed=True).state,
        _evaluate(snapshot=None).state,
        _evaluate(snapshot=_stale_book()).state,
        State.CANDIDATE,  # written by the runner at the start of an evaluation
    }
    assert reached == {State.ACCEPT, State.NO_TRADE, State.DATA_UNAVAILABLE, State.REJECT, State.CANDIDATE}


# --- reasons --------------------------------------------------------------


def test_all_specified_reason_codes_exist() -> None:
    required = {
        "NO_FORECAST",
        "FORECAST_NOT_YET_AVAILABLE",
        "NO_VALID_MODEL",
        "MODEL_STALE",
        "NO_ORDERBOOK",
        "STALE_ORDERBOOK",
        "INCOMPLETE_ORDERBOOK",
        "INSUFFICIENT_LIQUIDITY",
        "UNKNOWN_FEE",
        "NEGATIVE_NET_EDGE",
        "EDGE_BELOW_THRESHOLD",
        "TIME_MISMATCH",
        "MARKET_CLOSED",
        "COLLECTOR_DISCONNECTED",
        "DATA_QUALITY_FAILURE",
    }
    assert required <= {r.value for r in Reason}


def test_no_reason_is_a_generic_catch_all() -> None:
    assert "GENERIC_ERROR" not in {r.value for r in Reason}
    assert all("generic" not in r.value.lower() for r in Reason)


def test_causal_order_covers_every_reason() -> None:
    assert set(CAUSAL_ORDER) == set(Reason)


# --- time consistency -----------------------------------------------------


def test_a_forecast_that_is_not_yet_publishable_is_refused() -> None:
    """Publishability is checked against the decision instant, never assumed.

    Holding a run whose ``publishable_at`` is still ahead of the decision also
    means its receipt stamp is ahead of it, so TIME_MISMATCH fires too. Both
    are recorded; FORECAST_NOT_YET_AVAILABLE is primary because it comes first
    in the causal order - "we do not have this run yet" is the reason that
    would have blocked the trade even if the clocks agreed.
    """
    forecast = make_forecast(run_time=NOW - timedelta(hours=4))  # publishable at +12h
    decision = _evaluate(forecast=forecast, prediction=make_prediction(forecast=forecast))
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.primary_reason is Reason.FORECAST_NOT_YET_AVAILABLE
    assert Reason.TIME_MISMATCH in decision.reasons
    assert "after the decision instant" in decision.details["FORECAST_NOT_YET_AVAILABLE"]
    assert decision.net_edge is None


def test_a_publishable_run_received_in_the_past_passes_the_availability_gate() -> None:
    forecast = make_forecast(run_time=NOW - timedelta(hours=14))  # publishable at -2h
    decision = _evaluate(forecast=forecast, prediction=make_prediction(forecast=forecast))
    assert Reason.FORECAST_NOT_YET_AVAILABLE not in decision.reasons
    assert Reason.TIME_MISMATCH not in decision.reasons
    assert decision.state is State.ACCEPT


def test_a_forecast_from_the_future_is_refused() -> None:
    forecast = make_forecast(run_time=NOW + timedelta(hours=6))
    decision = _evaluate(forecast=forecast, prediction=make_prediction(forecast=forecast))
    assert Reason.FORECAST_NOT_YET_AVAILABLE in decision.reasons


def test_an_ancient_forecast_is_stale_not_absent() -> None:
    forecast = make_forecast(run_time=NOW - timedelta(hours=72))
    decision = _evaluate(forecast=forecast, prediction=make_prediction(forecast=forecast))
    assert decision.primary_reason is Reason.MODEL_STALE
    assert Reason.MODEL_STALE not in DATA_UNAVAILABLE_REASONS
    assert decision.state is State.REJECT


def test_a_book_the_collector_received_after_the_decision_is_refused() -> None:
    """Received-after-decide cannot have been knowable, whatever the venue stamp says."""
    future_receipt = make_observation(
        venue_ts=NOW - timedelta(seconds=2),
        collector_received=NOW + timedelta(seconds=5),
        shadow_received=NOW + timedelta(seconds=6),
    )
    decision = _evaluate(snapshot=future_receipt)
    assert decision.state is State.REJECT
    assert decision.primary_reason is Reason.TIME_MISMATCH
    assert "after the decision instant" in decision.details["TIME_MISMATCH"]


def test_a_venue_timestamp_in_the_future_is_refused() -> None:
    future_venue = make_observation(
        venue_ts=NOW + timedelta(seconds=30),
        collector_received=NOW + timedelta(seconds=31),
        shadow_received=NOW + timedelta(seconds=32),
    )
    decision = _evaluate(snapshot=future_venue)
    assert Reason.TIME_MISMATCH in decision.reasons


def test_collector_receipt_before_the_venue_timestamp_is_a_clock_disagreement() -> None:
    inverted = make_observation(
        venue_ts=NOW,
        collector_received=NOW - timedelta(seconds=10),
        shadow_received=NOW - timedelta(seconds=9),
    )
    decision = _evaluate(snapshot=inverted)
    assert Reason.TIME_MISMATCH in decision.reasons
    assert "clocks disagree" in decision.details["TIME_MISMATCH"]


def test_a_book_with_no_venue_timestamp_cannot_be_shown_fresh() -> None:
    no_venue = make_observation(
        venue_ts=None,
        collector_received=NOW - timedelta(seconds=1),
        shadow_received=NOW,
    )
    decision = _evaluate(snapshot=no_venue)
    assert Reason.STALE_ORDERBOOK in decision.reasons
    assert "no venue timestamp" in decision.details["STALE_ORDERBOOK"]
    assert decision.snapshot_age_ms is None


def test_a_forecast_received_after_the_decision_is_refused() -> None:
    late = make_forecast(received=NOW + timedelta(seconds=10))
    decision = _evaluate(forecast=late, prediction=make_prediction(forecast=late))
    assert Reason.TIME_MISMATCH in decision.reasons


# --- model version / leakage ---------------------------------------------


def test_an_artifact_fitted_after_the_decision_is_refused() -> None:
    """Parameters trained on data from after the decision are the leak itself."""
    future_cutoff = make_prediction(training_cutoff=NOW + timedelta(days=1))
    decision = _evaluate(prediction=future_cutoff)
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.primary_reason is Reason.NO_VALID_MODEL
    assert "after the decision instant" in decision.details["NO_VALID_MODEL"]


def test_a_missing_model_is_reported_by_name_not_as_an_error() -> None:
    from weatherquant.shadow.inference import ModelRefusal

    refusal = ModelRefusal("NO_VALID_MODEL", "model 'gfs_raw' is not loaded (loaded: [])")
    decision = _evaluate(prediction=refusal)
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.reasons == (Reason.NO_VALID_MODEL,)
    assert "not loaded" in decision.details["NO_VALID_MODEL"]


def test_an_unrecognised_refusal_code_degrades_to_data_quality_not_a_blanket() -> None:
    from weatherquant.shadow.inference import ModelRefusal

    decision = _evaluate(prediction=ModelRefusal("SOMETHING_NEW", "detail"))
    assert decision.reasons == (Reason.DATA_QUALITY_FAILURE,)


# --- order book -----------------------------------------------------------


def test_a_book_missing_its_ask_side_blocks_a_buy_but_not_a_sell() -> None:
    """Only the side being traded has to exist - and the rule is symmetric."""
    askless = make_observation(bids=[(0.45, 400.0)], asks=[])

    buy = _evaluate(snapshot=askless, direction=Direction.BUY_YES)
    assert buy.primary_reason is Reason.INCOMPLETE_ORDERBOOK
    assert "NO_ASK_SIDE" in buy.details["INCOMPLETE_ORDERBOOK"]
    assert buy.net_edge is None  # never priced against a side that is not there

    # The bid side is intact, so a sell can still be priced from it.
    sell = _evaluate(snapshot=askless, direction=Direction.SELL_YES)
    assert Reason.INCOMPLETE_ORDERBOOK not in sell.reasons
    assert sell.estimated_fill_price == pytest.approx(0.45)


def test_a_book_missing_its_bid_side_blocks_a_sell_but_not_a_buy() -> None:
    bidless = make_observation(bids=[], asks=[(0.47, 400.0)])

    buy = _evaluate(snapshot=bidless, direction=Direction.BUY_YES)
    assert Reason.INCOMPLETE_ORDERBOOK not in buy.reasons
    assert buy.estimated_fill_price == pytest.approx(0.47)

    sell = _evaluate(snapshot=bidless, direction=Direction.SELL_YES)
    assert sell.primary_reason is Reason.INCOMPLETE_ORDERBOOK
    assert "NO_BID_SIDE" in sell.details["INCOMPLETE_ORDERBOOK"]
    assert sell.net_edge is None


def test_a_crossed_book_is_refused_for_both_directions() -> None:
    crossed = make_observation(bids=[(0.50, 100.0)], asks=[(0.45, 100.0)])
    for direction in (Direction.BUY_YES, Direction.SELL_YES):
        decision = _evaluate(snapshot=crossed, direction=direction)
        assert Reason.INCOMPLETE_ORDERBOOK in decision.reasons, direction
        assert decision.net_edge is None


def test_stale_threshold_is_the_configured_one() -> None:
    just_inside = make_observation(
        venue_ts=NOW - timedelta(milliseconds=THRESHOLDS.max_snapshot_age_ms - 200)
    )
    assert Reason.STALE_ORDERBOOK not in _evaluate(snapshot=just_inside).reasons

    just_outside = make_observation(
        venue_ts=NOW - timedelta(milliseconds=THRESHOLDS.max_snapshot_age_ms + 200)
    )
    assert Reason.STALE_ORDERBOOK in _evaluate(snapshot=just_outside).reasons


# --- fees and liquidity ---------------------------------------------------


def test_a_market_with_no_fee_schedule_is_refused_rather_than_assumed_free() -> None:
    decision = _evaluate(market=make_catalog_market(fee_rate=None))
    assert decision.primary_reason is Reason.UNKNOWN_FEE
    assert decision.net_edge is None  # without a schedule there is no net edge to state
    assert decision.estimated_fees_usd is None


def test_depth_below_the_floor_is_insufficient_liquidity() -> None:
    thin = make_observation(bids=[(0.45, 10.0)], asks=[(0.47, 10.0)])
    decision = _evaluate(snapshot=thin)
    assert Reason.INSUFFICIENT_LIQUIDITY in decision.reasons
    assert decision.state is State.REJECT


def test_a_partial_fill_is_refused_when_the_threshold_requires_a_complete_one() -> None:
    assert THRESHOLDS.require_complete_fill is True
    shallow = make_observation(bids=[(0.45, 400.0)], asks=[(0.47, 5.0)])
    decision = _evaluate(snapshot=shallow)
    assert Reason.INSUFFICIENT_LIQUIDITY in decision.reasons
    assert "5.0 of 100.0 shares" in decision.details["INSUFFICIENT_LIQUIDITY"]


def test_below_the_markets_own_minimum_order_size_is_not_executable() -> None:
    decision = _evaluate(market=make_catalog_market(min_order_size=500.0))
    assert Reason.INSUFFICIENT_LIQUIDITY in decision.reasons
    assert "minimum order size" in decision.details["INSUFFICIENT_LIQUIDITY"]


# --- edge -----------------------------------------------------------------


def test_a_negative_edge_is_rejected_with_the_number_recorded() -> None:
    # Model says 0.10, the ask is 0.47: buying is clearly wrong.
    prediction = make_prediction(probabilities={"32-33 degF": 0.10, "Below 32 degF": 0.90})
    decision = _evaluate(prediction=prediction)
    assert decision.state is State.REJECT
    assert decision.primary_reason is Reason.NEGATIVE_NET_EDGE
    assert decision.net_edge is not None and decision.net_edge < 0
    assert "USD/share" in decision.details["NEGATIVE_NET_EDGE"]


def test_a_positive_but_small_edge_is_below_threshold_not_negative() -> None:
    # Edge lands between 0 and the configured floor.
    prediction = make_prediction(probabilities={"32-33 degF": 0.492, "Below 32 degF": 0.508})
    decision = _evaluate(prediction=prediction)
    assert decision.net_edge is not None and decision.net_edge > 0
    assert decision.state is State.REJECT
    assert decision.primary_reason is Reason.EDGE_BELOW_THRESHOLD
    assert decision.details["EDGE_BELOW_THRESHOLD"].startswith("net edge")


def test_the_threshold_version_travels_with_the_decision() -> None:
    decision = _evaluate()
    assert decision.thresholds_version == THRESHOLDS.version


# --- direction ------------------------------------------------------------


def test_a_buy_prices_against_the_ask_and_a_sell_against_the_bid() -> None:
    snapshot = make_observation(bids=[(0.40, 400.0)], asks=[(0.60, 400.0)])
    prediction = make_prediction(probabilities={"32-33 degF": 0.5, "Below 32 degF": 0.5})

    buy = _evaluate(snapshot=snapshot, prediction=prediction, direction=Direction.BUY_YES)
    sell = _evaluate(snapshot=snapshot, prediction=prediction, direction=Direction.SELL_YES)

    assert buy.market_price == pytest.approx(0.60)
    assert sell.market_price == pytest.approx(0.40)
    assert buy.estimated_fill_price == pytest.approx(0.60)
    assert sell.estimated_fill_price == pytest.approx(0.40)


def test_buy_and_sell_costs_match_hand_computed_values() -> None:
    """Expected values derived from the published fee formula, not re-called."""
    snapshot = make_observation(bids=[(0.45, 400.0)], asks=[(0.47, 400.0)])
    shares = THRESHOLDS.proposed_shares

    # --- buy: ask 0.47 ---------------------------------------------------
    buy_plan = build_fill_plan(snapshot.book, side=Side.BUY, shares=shares)
    buy_cost = execution_cost(buy_plan, fee_rate=0.05, buffer_fraction=BUFFER)
    expected_fee = round(shares * 0.05 * 0.47 * 0.53, 5)  # C * rate * p * (1-p)
    assert buy_cost.fees_usd == pytest.approx(expected_fee, abs=1e-9)
    assert buy_cost.execution_buffer_usd == pytest.approx(0.47 * shares * BUFFER, abs=1e-9)
    assert buy_cost.total_cost_usd == pytest.approx(
        0.47 * shares + expected_fee + 0.47 * shares * BUFFER, abs=1e-9
    )

    # --- sell: bid 0.45 --------------------------------------------------
    sell_plan = build_fill_plan(snapshot.book, side=Side.SELL, shares=shares)
    sell_cost = execution_cost(sell_plan, fee_rate=0.05, buffer_fraction=BUFFER)
    assert sell_cost.fill_cost_usd == pytest.approx(0.45 * shares, abs=1e-9)
    assert sell_cost.net_proceeds_usd == pytest.approx(
        0.45 * shares - round(shares * 0.05 * 0.45 * 0.55, 5) - 0.45 * shares * BUFFER,
        abs=1e-9,
    )


def test_the_fee_used_is_the_markets_own_reported_rate() -> None:
    market = make_catalog_market(fee_rate=0.0)
    decision = _evaluate(market=market)
    assert decision.estimated_fees_usd == pytest.approx(0.0)
    # A zero fee raises the edge, but the buffer and threshold still apply.
    assert decision.net_edge is not None


def test_a_hand_computed_buy_edge_matches_what_the_gate_saw() -> None:
    shares = THRESHOLDS.proposed_shares
    q = 0.6
    fill = 0.47 * shares
    fee = taker_fee_usd(filled_shares=shares, price_usd_per_share=0.47, fee_rate=0.05)
    buffer = fill * BUFFER
    expected = (q * shares - fill - fee - buffer) / shares

    prediction = make_prediction(probabilities={"32-33 degF": 0.6, "Below 32 degF": 0.4})
    decision = _evaluate(prediction=prediction, direction=Direction.BUY_YES)
    assert decision.net_edge == pytest.approx(expected, abs=1e-9)
    assert decision.expected_net_profit_usd == pytest.approx(expected * shares, abs=1e-6)


def test_a_hand_computed_sell_edge_matches_what_the_gate_saw() -> None:
    shares = THRESHOLDS.proposed_shares
    q = 0.6
    proceeds = 0.45 * shares
    fee = taker_fee_usd(filled_shares=shares, price_usd_per_share=0.45, fee_rate=0.05)
    buffer = proceeds * BUFFER
    expected = (proceeds - fee - buffer - q * shares) / shares

    prediction = make_prediction(probabilities={"32-33 degF": 0.6, "Below 32 degF": 0.4})
    decision = _evaluate(prediction=prediction, direction=Direction.SELL_YES)
    assert decision.net_edge == pytest.approx(expected, abs=1e-9)


# --- multiple reasons and the primary ------------------------------------


def test_every_applicable_reason_is_kept_and_the_primary_is_the_first_causal_one() -> None:
    """A stale book on a silent feed reports both, primary by causal order."""
    decision = _evaluate(snapshot=_stale_book(), collector_last_seen=NOW - timedelta(minutes=30))

    assert set(decision.reasons) == {Reason.STALE_ORDERBOOK, Reason.COLLECTOR_DISCONNECTED}
    # COLLECTOR_DISCONNECTED precedes STALE_ORDERBOOK in the causal order.
    assert decision.primary_reason is Reason.COLLECTOR_DISCONNECTED
    assert decision.reason_codes[0] == decision.primary_reason.value
    assert decision.state is State.DATA_UNAVAILABLE


def test_primary_reason_is_stable_whatever_order_the_gates_fire_in() -> None:
    """Ordering comes from CAUSAL_ORDER, not from the order conditions arose."""
    closed_and_stale = _evaluate(
        market_closed=True,
        snapshot=_stale_book(),
    )
    assert closed_and_stale.reason_codes == [
        Reason.MARKET_CLOSED.value,
        Reason.STALE_ORDERBOOK.value,
    ]
    assert closed_and_stale.primary_reason is Reason.MARKET_CLOSED


def test_a_closed_market_stays_no_trade_even_when_other_gates_also_fail() -> None:
    decision = _evaluate(market_closed=True, snapshot=None)
    assert decision.state is State.NO_TRADE
    assert Reason.MARKET_CLOSED in decision.reasons
    assert Reason.NO_ORDERBOOK in decision.reasons


def test_an_unexpected_exception_is_recorded_as_a_specific_reason() -> None:
    """Not swallowed, not generic: the exception text becomes the detail.

    The state is DATA_UNAVAILABLE because an evaluation that threw produced no
    edge - the input we needed (a computable cost) was not obtained.
    """
    decision = _evaluate(buffer_fraction=-0.01)
    assert decision.state is State.DATA_UNAVAILABLE
    assert decision.primary_reason is Reason.EVALUATION_ERROR
    assert decision.details["EVALUATION_ERROR"]
    assert "generic" not in decision.details["EVALUATION_ERROR"].lower()
    assert decision.net_edge is None


def test_an_unparseable_fill_is_a_data_quality_failure_with_the_code() -> None:
    decision = _evaluate(
        thresholds=dataclasses.replace(THRESHOLDS, proposed_shares=-5.0),
    )
    assert decision.primary_reason is Reason.DATA_QUALITY_FAILURE
    assert "INVALID_ORDER_SIZE" in decision.details["DATA_QUALITY_FAILURE"]


def test_decision_carries_the_full_provenance_chain() -> None:
    decision = _evaluate()
    assert decision.cycle_id == "cycle-1"
    assert decision.market_id
    assert decision.book_id
    assert decision.book_source == "rest"
    assert decision.venue_ts_utc is not None
    assert decision.collector_received_at_utc is not None
    assert decision.shadow_received_at_utc is not None
    assert decision.snapshot_age_ms is not None and decision.snapshot_age_ms >= 0
    assert decision.model_name == "calibrated"
    assert decision.model_version == "v1"
    assert decision.model_run_id
    assert decision.forecast_run_time_utc is not None
    assert decision.thresholds_version == THRESHOLDS.version
    assert decision.target_date == TARGET_DATE


def test_data_quality_failure_when_the_bracket_is_missing() -> None:
    market = make_catalog_market(bracket=None, bracket_label="")
    decision = _evaluate(market=market)
    assert decision.primary_reason is Reason.DATA_QUALITY_FAILURE
    assert decision.state is State.DATA_UNAVAILABLE


def test_data_quality_failure_when_the_bracket_is_not_in_the_prediction() -> None:
    market = make_catalog_market(
        bracket=Bracket(label="99-100°F", kind=BracketKind.RANGE, low=99, high=100)
    )
    decision = _evaluate(market=market)
    assert Reason.DATA_QUALITY_FAILURE in decision.reasons
    assert "not in the prediction" in decision.details["DATA_QUALITY_FAILURE"]


# --- LIVE vs REPLAY -------------------------------------------------------
#
# A decision made against a historical archive is a different claim from one
# made against a feed being written now. These tests pin down which checks are
# mode-dependent - and, more importantly, which are NOT.


def test_a_decision_records_the_mode_it_was_made_in() -> None:
    live = _evaluate(mode=Mode.LIVE, mode_reason="configured:live")
    assert live.mode is Mode.LIVE
    assert live.mode_reason == "configured:live"

    replay = _evaluate(mode=Mode.REPLAY, mode_reason="auto:archive_age_900s_gt_900s")
    assert replay.mode is Mode.REPLAY
    assert "archive_age" in replay.mode_reason

    # Default is UNKNOWN, never a silent LIVE: an undecided mode must not be
    # read as contemporary signal after the fact.
    default = evaluate(
        cycle_id="c",
        market=make_catalog_market(),
        direction=Direction.BUY_YES,
        prediction=make_prediction(),
        forecast=make_forecast(),
        snapshot=make_observation(),
        thresholds=THRESHOLDS,
        buffer_fraction=BUFFER,
        decision_time=NOW,
        collector_last_seen=NOW,
        collector_silence_seconds=900.0,
        max_forecast_age_hours=30.0,
        market_closed=False,
    )
    assert default.mode is Mode.UNKNOWN


def test_live_refuses_a_forecast_received_after_the_decision() -> None:
    late = make_forecast(received=NOW + timedelta(seconds=30))
    decision = _evaluate(
        forecast=late, prediction=make_prediction(forecast=late), mode=Mode.LIVE
    )
    assert Reason.TIME_MISMATCH in decision.reasons


def test_replay_waives_our_own_receipt_time_but_not_publishability() -> None:
    """Two different questions, only one of which replay makes meaningless.

    Our receipt stamp is *supposed* to postdate a historical decision - that is
    what replaying means - so checking it would refuse every replay row for an
    artefact of the exercise. Whether the forecast had been *published* by then
    is a property of the forecast, not of us, and it is still refused.
    """
    late_received = make_forecast(received=NOW + timedelta(seconds=30))
    waived = _evaluate(
        forecast=late_received,
        prediction=make_prediction(forecast=late_received),
        mode=Mode.REPLAY,
    )
    assert Reason.TIME_MISMATCH not in waived.reasons

    not_yet_published = make_forecast(publishable=NOW + timedelta(hours=6))
    refused = _evaluate(
        forecast=not_yet_published,
        prediction=make_prediction(forecast=not_yet_published),
        mode=Mode.REPLAY,
    )
    assert refused.primary_reason is Reason.FORECAST_NOT_YET_AVAILABLE
    assert refused.net_edge is None


def test_replay_does_not_report_a_silent_collector() -> None:
    """There is no feed to be silent: the archive is the input.

    Reporting COLLECTOR_DISCONNECTED for a deliberate re-read would describe an
    outage that is not happening.
    """
    live = _evaluate(
        collector_last_seen=NOW - timedelta(hours=6), mode=Mode.LIVE
    )
    assert Reason.COLLECTOR_DISCONNECTED in live.reasons
    assert live.state is State.DATA_UNAVAILABLE

    replay = _evaluate(
        collector_last_seen=NOW - timedelta(hours=6), mode=Mode.REPLAY
    )
    assert Reason.COLLECTOR_DISCONNECTED not in replay.reasons


def test_replay_still_checks_the_two_upstream_clocks() -> None:
    """The venue-vs-collector comparison involves neither of our clocks.

    A venue timestamp ahead of the decision instant, or a collector receipt
    that precedes it, is a clock problem in either mode - waiving these would
    let replay hide a genuinely broken pair of upstream clocks.
    """
    future_venue = make_observation(
        venue_ts=NOW + timedelta(seconds=30),
        collector_received=NOW + timedelta(seconds=31),
        shadow_received=NOW + timedelta(days=3),  # our clock: expected in replay
    )
    decision = _evaluate(snapshot=future_venue, mode=Mode.REPLAY)
    assert Reason.TIME_MISMATCH in decision.reasons
    assert "venue timestamp" in decision.details["TIME_MISMATCH"]

    inverted = make_observation(
        venue_ts=NOW,
        collector_received=NOW - timedelta(seconds=20),
        shadow_received=NOW + timedelta(days=3),
    )
    skew = _evaluate(snapshot=inverted, mode=Mode.REPLAY)
    assert Reason.TIME_MISMATCH in skew.reasons
    assert "clocks disagree" in skew.details["TIME_MISMATCH"]


def test_the_mode_changes_nothing_about_the_pricing() -> None:
    """Replay must not become a cheaper way to ACCEPT: same book, same costs."""
    live = _evaluate(mode=Mode.LIVE)
    replay = _evaluate(mode=Mode.REPLAY)
    assert live.state is replay.state
    assert live.net_edge == pytest.approx(replay.net_edge, abs=1e-12)
    assert live.estimated_fees_usd == pytest.approx(replay.estimated_fees_usd, abs=1e-12)

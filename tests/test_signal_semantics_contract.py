"""WQN-27: the executable half of ``docs/signal-semantics-audit.md``.

These tests pin the four semantics a SELL YES signal touches:

* Shadow prices BUY YES from the ask and SELL YES from the bid, never the
  midpoint, and never treats SELL YES as a naked short or a BUY NO order.
* ``net_edge_usd_per_share`` is USD per **filled** share, after fees and the
  execution buffer; a gross edge above the threshold can still fail after
  costs.
* Repeated ACCEPTs for one five-field key are one opportunity; a different
  direction or mode is a different opportunity.
* Paper only reduces existing YES inventory on SELL YES, never below zero,
  and never maps the signal to a NO token. Shadow and Paper stay independent:
  Paper refusals/fills do not rewrite Shadow decisions, and redelivery does
  not create a second trade.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from paper_book_helpers import T_DECISION, TOKEN, two_level_book, write_archive, ws_line
from paper_fills_support import build_engine, open_ledger_if_needed
from paper_helpers import T0, export_row
from shadow_helpers import (
    NOW as SHADOW_NOW,
)
from shadow_helpers import (
    make_catalog_market,
    make_forecast,
    make_observation,
    make_prediction,
)
from weatherquant.execution.pricing import (
    build_fill_plan,
    edge_usd_per_share,
    edge_usd_per_share_sell,
    execution_cost,
    expected_net_profit_usd,
    expected_net_profit_usd_sell,
)
from weatherquant.markets.models import BookLevel, OrderBook, Side
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.consumer import CURSOR_KEY, SignalConsumer
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.store import PaperStore
from weatherquant.performance.episodes import build_episodes, summarize
from weatherquant.shadow.config import load_thresholds
from weatherquant.shadow.decision import Direction, Reason, State, evaluate
from weatherquant.storage.paper import PaperDatabase

THRESHOLDS = load_thresholds("configs/shadow-thresholds.json")
BUFFER = 0.005

# A two-sided, one-level book. The midpoint (0.46) must never be used.
BIDS = [(0.44, 400.0)]
ASKS = [(0.48, 400.0)]


def _evaluate(
    *,
    direction: Direction = Direction.BUY_YES,
    probabilities: dict[str, float] | None = None,
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
):
    return evaluate(
        cycle_id="cycle-1",
        market=make_catalog_market(),
        direction=direction,
        prediction=make_prediction(probabilities=probabilities),
        forecast=make_forecast(),
        snapshot=make_observation(bids=bids if bids is not None else BIDS, asks=asks if asks is not None else ASKS),
        thresholds=THRESHOLDS,
        buffer_fraction=BUFFER,
        decision_time=SHADOW_NOW,
        collector_last_seen=SHADOW_NOW - timedelta(seconds=5),
        collector_silence_seconds=900.0,
        max_forecast_age_hours=THRESHOLDS.max_forecast_age_hours,
        market_closed=False,
    )


def _probabilities(q: float) -> dict[str, float]:
    return {"32-33 degF": q, "Below 32 degF": 1.0 - q}


# --- Shadow price side -----------------------------------------------------


def test_buy_prices_from_ask_and_sell_prices_from_bid_never_midpoint() -> None:
    buy = _evaluate(probabilities=_probabilities(0.6))
    sell = _evaluate(direction=Direction.SELL_YES, probabilities=_probabilities(0.4))

    midpoint = 0.46
    assert buy.market_price == pytest.approx(0.48)
    assert buy.estimated_fill_price == pytest.approx(0.48)
    assert sell.market_price == pytest.approx(0.44)
    assert sell.estimated_fill_price == pytest.approx(0.44)

    # The midpoint is analysis-only; it is never an execution price.
    assert buy.market_price != pytest.approx(midpoint)
    assert sell.market_price != pytest.approx(midpoint)
    assert buy.estimated_fill_price != pytest.approx(midpoint)
    assert sell.estimated_fill_price != pytest.approx(midpoint)


# --- net_edge formula, units and gates -------------------------------------


def test_hand_computed_buy_and_sell_edges_include_fees_and_buffer() -> None:
    buy = _evaluate(probabilities=_probabilities(0.6))
    # fill = 100 * 0.48 = 48.00
    # fee  = 100 * 0.05 * 0.48 * 0.52 = 1.248
    # buff = 48.00 * 0.005 = 0.24
    # E[net] = 60 - 48 - 1.248 - 0.24 = 10.512
    assert buy.state is State.ACCEPT
    assert buy.estimated_fill_shares == pytest.approx(100.0)
    assert buy.estimated_fees_usd == pytest.approx(1.248)
    assert buy.estimated_buffer_usd == pytest.approx(0.24)
    assert buy.expected_net_profit_usd == pytest.approx(10.512)
    assert buy.net_edge == pytest.approx(0.10512)  # USD per filled share

    sell = _evaluate(direction=Direction.SELL_YES, probabilities=_probabilities(0.4))
    # proceeds = 100 * 0.44 = 44.00
    # fee      = 100 * 0.05 * 0.44 * 0.56 = 1.232
    # buff     = 44.00 * 0.005 = 0.22
    # E[net]   = 44 - 1.232 - 0.22 - 40 = 2.548
    assert sell.state is State.ACCEPT
    assert sell.estimated_fees_usd == pytest.approx(1.232)
    assert sell.estimated_buffer_usd == pytest.approx(0.22)
    assert sell.expected_net_profit_usd == pytest.approx(2.548)
    assert sell.net_edge == pytest.approx(0.02548)

    # Unit check: per-share edge multiplied by filled shares is the USD figure.
    assert buy.net_edge * buy.estimated_fill_shares == pytest.approx(buy.expected_net_profit_usd)
    assert sell.net_edge * sell.estimated_fill_shares == pytest.approx(sell.expected_net_profit_usd)


def test_gross_edge_above_threshold_but_net_below_is_not_accept() -> None:
    decision = _evaluate(probabilities=_probabilities(0.505))

    threshold = THRESHOLDS.min_net_edge_usd_per_share
    gross_edge = 0.505 - 0.48
    assert gross_edge > threshold  # the naive, pre-cost view says "go"
    assert decision.net_edge is not None
    assert 0.0 < decision.net_edge < threshold  # costs and buffer eat the edge
    assert decision.net_edge == pytest.approx(0.01012)
    assert decision.state is State.REJECT
    assert decision.primary_reason is Reason.EDGE_BELOW_THRESHOLD


def test_sell_edge_sign_flips_with_model_probability() -> None:
    positive = _evaluate(direction=Direction.SELL_YES, probabilities=_probabilities(0.4))
    assert positive.net_edge == pytest.approx(0.02548)
    assert positive.state is State.ACCEPT

    negative = _evaluate(direction=Direction.SELL_YES, probabilities=_probabilities(0.6))
    assert negative.net_edge == pytest.approx(-0.17452)
    assert negative.state is State.REJECT
    assert negative.primary_reason is Reason.NEGATIVE_NET_EDGE


# --- partial fills ---------------------------------------------------------


def _partial_book() -> OrderBook:
    return OrderBook(
        market_id="m0",
        token_id="tok-0-yes",
        bids=(BookLevel(price_usd_per_share=0.44, size_shares=400.0),),
        asks=(BookLevel(price_usd_per_share=0.48, size_shares=60.0),),
        venue_ts=SHADOW_NOW,
        fetched_at_utc=SHADOW_NOW,
    )


def test_net_edge_is_per_filled_share_for_partial_fills() -> None:
    plan = build_fill_plan(_partial_book(), side=Side.BUY, shares=100.0)
    cost = execution_cost(plan, fee_rate=0.05, buffer_fraction=BUFFER)

    assert plan.filled_shares == pytest.approx(60.0)
    assert plan.unfilled_shares == pytest.approx(40.0)

    profit = expected_net_profit_usd(q=0.6, filled_shares=plan.filled_shares, cost=cost)
    edge = edge_usd_per_share(q=0.6, filled_shares=plan.filled_shares, cost=cost)

    # The divisor is what actually filled, not what was proposed.
    assert edge == pytest.approx(profit / 60.0)
    assert edge * 60.0 == pytest.approx(profit)
    assert edge * 100.0 != pytest.approx(profit)
    assert edge == pytest.approx(0.10512)

    # Mirror check for a partial sell.
    sell_plan = build_fill_plan(
        OrderBook(
            market_id="m0",
            token_id="tok-0-yes",
            bids=(BookLevel(price_usd_per_share=0.44, size_shares=40.0),),
            asks=(BookLevel(price_usd_per_share=0.48, size_shares=400.0),),
            venue_ts=SHADOW_NOW,
            fetched_at_utc=SHADOW_NOW,
        ),
        side=Side.SELL,
        shares=100.0,
    )
    sell_cost = execution_cost(sell_plan, fee_rate=0.05, buffer_fraction=BUFFER)
    sell_profit = expected_net_profit_usd_sell(q=0.4, filled_shares=sell_plan.filled_shares, cost=sell_cost)
    sell_edge = edge_usd_per_share_sell(q=0.4, filled_shares=sell_plan.filled_shares, cost=sell_cost)
    assert sell_edge == pytest.approx(sell_profit / 40.0)


def test_partial_fill_is_sized_down_but_edge_recorded_per_filled_share() -> None:
    decision = _evaluate(
        probabilities=_probabilities(0.6),
        asks=[(0.48, 60.0)],
    )
    # 60 shares is above the venue minimum, so this is a valid sized-down
    # trade rather than a refusal. The edge is still computed against the
    # shares that would actually fill, not the 100 requested.
    assert decision.state is State.ACCEPT
    assert Reason.INSUFFICIENT_LIQUIDITY not in decision.reasons
    assert decision.proposed_shares == pytest.approx(100.0)
    assert decision.estimated_fill_shares == pytest.approx(60.0)
    assert decision.net_edge == pytest.approx(0.10512)


# --- episode dedup ---------------------------------------------------------


BASE = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def _episode_row(
    minute: int,
    *,
    direction: str = "buy_yes",
    mode: str = "LIVE",
    state: str = "ACCEPT",
    edge: float = 0.03,
    market_id: str = "mkt-1",
) -> dict:
    return {
        "market_id": market_id,
        "target_date": "2026-10-26",
        "model_name": "calibrated",
        "direction": direction,
        "mode": mode,
        "state": state,
        "decision_time_utc": (BASE + timedelta(minutes=minute)).isoformat(),
        "net_edge": edge,
        "market_price": 0.5,
        "model_probability": 0.6,
        "available_depth_usd": 200.0,
        "fee_rate": 0.05,
    }


def test_repeated_accepts_are_one_episode_and_direction_mode_separate() -> None:
    rows = [
        _episode_row(0),
        _episode_row(1),
        _episode_row(2),
        _episode_row(0, direction="sell_yes"),
        _episode_row(1, direction="sell_yes"),
        _episode_row(0, mode="REPLAY"),
        _episode_row(1, mode="REPLAY"),
    ]
    episodes, counters = build_episodes(rows)

    assert counters["episodes_built"] == 3
    by_key = {(episode.key.direction, episode.key.mode): episode for episode in episodes}
    assert set(by_key) == {("buy_yes", "LIVE"), ("sell_yes", "LIVE"), ("buy_yes", "REPLAY")}
    assert by_key[("buy_yes", "LIVE")].observation_count == 3
    assert by_key[("sell_yes", "LIVE")].observation_count == 2
    assert by_key[("buy_yes", "REPLAY")].observation_count == 2

    summary = summarize(episodes)
    assert summary["opportunity_episodes"] == 3
    assert summary["accept_observations"] == 7
    assert summary["unique_opportunity_edge_sum"] == pytest.approx(0.09)
    assert summary["observation_edge_sum"] == pytest.approx(0.21)


# --- Paper SELL YES --------------------------------------------------------


def _seed_accepted(store: PaperStore, row: dict, *, consumed_at: datetime = T0) -> None:
    store.record_signal(
        parse_signal_event(row),
        tradeable=True,
        outcome="ACCEPTED",
        consumed_at_utc=consumed_at,
    )


def test_sell_yes_without_inventory_is_refused_before_pricing(tmp_path) -> None:
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=T0)
        open_ledger_if_needed(store, now=T0)
        _seed_accepted(
            store,
            export_row(
                decision_id="sell-no-inventory",
                direction="sell_yes",
                token_id=TOKEN,
                book_id=None,
            ),
        )

        # No archive at all: the refusal must happen before a book is needed.
        engine = build_engine(store, ArchiveBookSource(tmp_path / "raw"), clock=lambda: T0)
        result = engine.fill_pending()

        assert result.filled == 0
        assert result.refused == 1
        assert result.by_reason == {"NO_INVENTORY": 1}
        assert store.signal("sell-no-inventory")["outcome"] == "REFUSED"
        assert store.signal("sell-no-inventory")["refusal_reason"] == "NO_INVENTORY"
        assert store.fill_of("sell-no-inventory") is None


def test_sell_yes_only_reduces_inventory_and_never_opens_a_no_position(tmp_path) -> None:
    raw = tmp_path / "raw"
    write_archive(
        raw,
        source="ws",
        day="2026-10-26",
        lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
    )
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=T0)
        open_ledger_if_needed(store, now=T0)
        for decision_id, direction, when in (
            ("z-buy", "buy_yes", T_DECISION),
            ("a-sell", "sell_yes", T_DECISION + timedelta(seconds=1)),
        ):
            _seed_accepted(
                store,
                export_row(
                    decision_id=decision_id,
                    direction=direction,
                    token_id=TOKEN,
                    decision_time_utc=when.isoformat(),
                    book_id=None,
                    proposed_shares=10.0,
                ),
            )

        result = build_engine(store, ArchiveBookSource(raw), clock=lambda: T0).fill_pending()
        assert result.filled == 2, result.as_dict()

        buy_fill = store.fill_of("z-buy")
        sell_fill = store.fill_of("a-sell")
        assert buy_fill is not None and sell_fill is not None
        buy_side = db.execute(
            "SELECT side, direction, token_id FROM paper_fills WHERE decision_id = ?", ["z-buy"]
        ).fetchone()
        sell_side = db.execute(
            "SELECT side, direction, token_id FROM paper_fills WHERE decision_id = ?", ["a-sell"]
        ).fetchone()
        assert buy_side == ("buy", "buy_yes", TOKEN)
        assert sell_side == ("sell", "sell_yes", TOKEN)
        assert sell_fill["net_proceeds_usd"] is not None
        assert sell_fill["total_cost_usd"] is None

        # Inventory closed exactly, never negative, and only the YES token.
        assert db.execute("SELECT SUM(shares) FROM paper_positions").fetchone()[0] == 0
        assert db.execute("SELECT MIN(shares) FROM paper_positions").fetchone()[0] >= 0
        token_ids = {row[0] for row in db.execute("SELECT DISTINCT token_id FROM paper_positions").fetchall()}
        assert token_ids <= {TOKEN}
        assert db.execute(
            "SELECT DISTINCT direction FROM paper_fills ORDER BY direction"
        ).fetchall() == [("buy_yes",), ("sell_yes",)]


# --- Shadow / Paper independence and redelivery ----------------------------


def _write_part(directory, rows: list[dict]) -> None:
    parts = directory / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    path = parts / "part-000000001.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str))
            handle.write("\n")


def test_shadow_accept_and_paper_outcomes_are_independent(tmp_path) -> None:
    raw = tmp_path / "raw"
    write_archive(
        raw,
        source="ws",
        day="2026-10-26",
        lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
    )
    events_dir = tmp_path / "decisions-export"
    rows = [
        export_row(
            decision_id="a-sell",
            direction="sell_yes",
            token_id=TOKEN,
            decision_time_utc=T0.isoformat(),
            book_id=None,
            proposed_shares=10.0,
        ),
        export_row(
            decision_id="z-buy",
            direction="buy_yes",
            token_id=TOKEN,
            decision_time_utc=(T0 + timedelta(seconds=1)).isoformat(),
            book_id=None,
            proposed_shares=10.0,
        ),
    ]
    _write_part(events_dir, rows)
    export_file = events_dir / "parts" / "part-000000001.jsonl"
    before_bytes = export_file.read_bytes()

    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=T0)
        open_ledger_if_needed(store, now=T0)

        consumer = SignalConsumer(
            store,
            events_dir=events_dir,
            max_signal_age_seconds=600.0,
            clock=lambda: T0 + timedelta(minutes=5),
        )
        consumed = consumer.consume_once()
        assert consumed.signals_recorded == 2
        assert consumed.accepted == 2

        filled = build_engine(store, ArchiveBookSource(raw), clock=lambda: T0).fill_pending()
        # Every exported row is a Shadow ACCEPT. Paper still refuses the first
        # (sell, no inventory) and fills the second (buy) for its own reasons.
        assert filled.filled == 1
        assert filled.by_reason == {"NO_INVENTORY": 1}
        assert store.signal("a-sell")["outcome"] == "REFUSED"
        assert store.signal("z-buy")["outcome"] == "FILLED"
        assert store.fill_of("a-sell") is None

        # Paper never writes back into the Shadow export.
        assert export_file.read_bytes() == before_bytes
        exported = [json.loads(line) for line in export_file.read_text(encoding="utf-8").splitlines()]
        assert {row["state"] for row in exported} == {"ACCEPT"}
        assert {row["decision_id"] for row in exported} == {"a-sell", "z-buy"}

        # Redelivery of both decisions: no second signal row, no second trade.
        counts_before = store.signal_counts()
        store.db.execute("DELETE FROM paper_state WHERE key = ?", [CURSOR_KEY])
        redelivered = consumer.consume_once()
        assert redelivered.duplicates == 2
        assert redelivered.signals_recorded == 0
        assert store.signal_counts() == counts_before

        again = build_engine(store, ArchiveBookSource(raw), clock=lambda: T0).fill_pending()
        assert again.caught_up is True
        assert db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1

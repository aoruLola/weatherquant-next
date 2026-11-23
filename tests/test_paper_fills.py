"""WQN-41: the simulated taker fill.

Every acceptance criterion is a test: a missing or stale book never becomes
a trade, a thin book is sized down only to the venue minimum, future data is
refused, and the fill row carries the full evidence (consumed levels, average
price, fee, identity).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from paper_book_helpers import TOKEN, book_payload, two_level_book, write_archive, ws_line
from paper_fills_support import make_risk
from paper_helpers import export_row
from weatherquant.execution.fees import DatedFeeSchedule, FeeScheduleEntry
from weatherquant.execution.pricing import ExecutionCost, edge_usd_per_share, taker_fee_usd
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.consumer import SignalConsumer
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.risk import RiskGate, RiskRules
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


@pytest.fixture()
def engine_env(tmp_path):
    """An enabled store with an account; events and archive directories."""
    events_dir = tmp_path / "decisions-export"
    raw_dir = tmp_path / "raw"
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        # Fills settle through the ledger, so the account's books must be open.
        Ledger(store).open_account(initial_capital_usdc=100.0, now=NOW)
        yield store, events_dir, raw_dir, db


def real_book_id(payload=None) -> str:
    """The content hash the collector's own parser assigns to a fixture book."""
    from datetime import datetime as _dt

    from weatherquant.data.books import parse_book_payload

    snapshot = parse_book_payload(
        payload if payload is not None else two_level_book(),
        token_id=TOKEN,
        market_id="",
        fetched_at_utc=_dt.now(tz=UTC),
        run_id="",
        source="ws",
    )
    return snapshot.content_hash


def accept_signal(
    raw_dir,
    *,
    decision_id="d" * 32,
    token_id=TOKEN,
    direction="buy_yes",
    book=None,
    **row_overrides,
):
    """An ACCEPTED signal whose book evidence exists in the archive.

    The event's book_id is the frame's real content hash, exactly as the
    shadow would have recorded it for the same levels.
    """
    book = book if book is not None else two_level_book()
    row_overrides.setdefault("book_id", real_book_id(book))
    row_overrides.setdefault("proposed_shares", 100.0)
    row_overrides.setdefault("estimated_fill_shares", 100.0)
    row = export_row(
        decision_id=decision_id,
        token_id=token_id,
        direction=direction,
        **row_overrides,
    )
    event = parse_signal_event(row)
    write_archive(
        raw_dir,
        source="ws",
        day=event.decision_time_utc.date().isoformat(),
        lines=[ws_line(book, received=event.decision_time_utc - timedelta(seconds=20))],
    )
    return event


def seed_accepted(store, raw_dir, **kwargs):
    event = accept_signal(raw_dir, **kwargs)
    store.record_signal(
        event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW
    )
    return event


def make_engine(store, raw_dir, **kwargs):
    return FillEngine(
        store,
        book_source=ArchiveBookSource(raw_dir),
        buffer_fraction=0.005,
        clock=lambda: NOW,
        risk=kwargs.pop("risk", None) or make_risk(store),
        **kwargs,
    )


class TestHappyPath:
    def test_buy_walks_asks_across_levels(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = seed_accepted(store, raw_dir)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        fill = store.fill_of(event.decision_id)
        # 60 shares at 0.55 + 40 shares at 0.57 = 55.80 for 100 shares.
        assert fill["filled_shares"] == pytest.approx(100.0)
        assert fill["avg_price_usd_per_share"] == pytest.approx(0.558)
        assert fill["levels_consumed"] == 2
        assert fill["complete"] is True
        assert fill["total_cost_usd"] is not None
        assert fill["net_proceeds_usd"] is None
        # The fee is THE fee function's output for the average price - never
        # a local re-derivation.
        assert fill["fees_usd"] == pytest.approx(
            taker_fee_usd(filled_shares=100.0, price_usd_per_share=0.558, fee_rate=0.05)
        )

    def test_consumed_levels_reproduce_the_cost(self, engine_env):
        # The evidence row must be sufficient to recompute the fill: sum of
        # taken*price == fill_cost, sum of taken == filled.
        store, _events, raw_dir, _db = engine_env
        event = seed_accepted(store, raw_dir)
        make_engine(store, raw_dir).fill_pending()
        fill = store.fill_of(event.decision_id)
        levels = fill["consumed_levels"]
        assert len(levels) == 2
        assert sum(level["size_shares_taken"] for level in levels) == pytest.approx(fill["filled_shares"])
        assert sum(
            level["price_usd_per_share"] * level["size_shares_taken"] for level in levels
        ) == pytest.approx(fill["avg_price_usd_per_share"] * fill["filled_shares"])
        assert levels[0]["price_usd_per_share"] == 0.55
        assert levels[0]["size_shares_taken"] == 60.0

    def test_slippage_is_average_over_best(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = seed_accepted(store, raw_dir)
        make_engine(store, raw_dir).fill_pending()
        fill = store.fill_of(event.decision_id)
        # Best ask 0.55, average 0.558 -> 0.008 per share of slippage.
        assert fill["slippage_usd_per_share"] == pytest.approx(0.008, abs=1e-9)
        single = store.db.execute(
            "SELECT slippage_usd_per_share, best_price_usd_per_share FROM paper_fills WHERE decision_id = ?",
            [event.decision_id],
        ).fetchone()
        assert single[1] == 0.55

    def test_sell_walks_bids_and_nets_proceeds(self, engine_env):
        # A sell needs inventory (WQN-43's rule, enforced by the ledger since
        # WQN-42): buy first, then sell, then inspect the sell's fill row.
        store, _events, raw_dir, _db = engine_env
        seed_accepted(store, raw_dir, decision_id="b" * 32)
        event = seed_accepted(store, raw_dir, decision_id="s" * 32, direction="sell_yes")
        make_engine(store, raw_dir).fill_pending()
        fill = store.fill_of(event.decision_id)
        # SELL hits the bid side: 500 shares resting at 0.52 fill all 100.
        assert fill["avg_price_usd_per_share"] == pytest.approx(0.52)
        assert fill["levels_consumed"] == 1
        assert fill["total_cost_usd"] is None
        # net = fill - fees - buffer, from the same three-term decomposition
        # the buy side sums; never a fourth term.
        assert fill["net_proceeds_usd"] == pytest.approx(
            100.0 * 0.52 - fill["fees_usd"] - _buffer(fill)
        )

    def test_fill_row_and_outcome_commit_together(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = seed_accepted(store, raw_dir)
        make_engine(store, raw_dir).fill_pending()
        row = store.signal(event.decision_id)
        assert row["outcome"] == "FILLED"
        assert store.fill_of(event.decision_id) is not None

    def test_fill_is_idempotent(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        seed_accepted(store, raw_dir)
        make_engine(store, raw_dir).fill_pending()
        # A second pass finds no ACCEPTED signals and writes nothing.
        second = make_engine(store, raw_dir).fill_pending()
        assert second.caught_up is True
        assert second.filled == 0
        assert store.signal_counts() == {"FILLED": 1}


def _buffer(fill) -> float:
    # The buffer is not stored as its own column on the read-back dict; the
    # invariant under test is total/net against the stored fee figure.
    return fill["avg_price_usd_per_share"] * fill["filled_shares"] * 0.005


class TestEvidenceGates:
    def test_missing_book_is_refused_not_faked(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = parse_signal_event(export_row(decision_id="d" * 32, proposed_shares=100.0))
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.refused == 1
        assert result.by_reason == {"NOT_EXECUTABLE": 1}
        assert store.fill_of(event.decision_id) is None
        row = store.signal(event.decision_id)
        assert row["outcome"] == "REFUSED"
        assert "no archived L2 frame" in row["refusal_detail"]

    def test_book_identity_mismatch_is_refused(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = accept_signal(raw_dir, decision_id="d" * 32)
        # The decision claims a different book than the archive resolves.
        event = parse_signal_event(export_row(decision_id="d" * 32, token_id=TOKEN, book_id="f" * 64))
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.by_reason == {"BOOK_EVIDENCE_MISMATCH": 1}
        assert store.fill_of(event.decision_id) is None

    def test_stale_book_is_refused(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = accept_signal(raw_dir, decision_id="d" * 32)
        # The frame the archive holds is 20s before the decision; a 5s age
        # limit refuses it.
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir, max_book_age_seconds=5.0).fill_pending()
        assert result.by_reason == {"BOOK_STALE": 1}
        assert store.fill_of(event.decision_id) is None

    def test_book_without_venue_timestamp_cannot_show_freshness(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        # Rewrite the archive frame with the venue timestamp stripped; the
        # event references THAT book (its hash), so the gate reached is
        # freshness, not identity.
        frame = two_level_book()
        frame.pop("timestamp")
        event = accept_signal(raw_dir, decision_id="d" * 32, book_id=real_book_id(frame))
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(frame, received=event.decision_time_utc - timedelta(seconds=20))],
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.by_reason == {"BOOK_STALE": 1}

    def test_only_future_frames_means_no_evidence(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = parse_signal_event(export_row(decision_id="d" * 32, proposed_shares=100.0))
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(two_level_book(), received=event.decision_time_utc + timedelta(seconds=5))],
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.by_reason == {"NOT_EXECUTABLE": 1}


class TestCapacityAndFees:
    def test_a_thin_book_is_sized_down_to_what_it_offers(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        thin = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 40.0)])
        event = parse_signal_event(
            export_row(
                decision_id="d" * 32,
                token_id=TOKEN,
                proposed_shares=100.0,
                book_id=real_book_id(thin),
            )
        )
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[
                ws_line(thin, received=event.decision_time_utc - timedelta(seconds=20))
            ],
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        fill = store.fill_of(event.decision_id)
        assert fill["complete"] is False
        assert fill["filled_shares"] == pytest.approx(40.0)
        assert store.db.execute(
            "SELECT requested_shares FROM paper_fills WHERE decision_id = ?",
            [event.decision_id],
        ).fetchone()[0] == pytest.approx(100.0)

    def test_partial_fill_uses_its_actual_vwap_for_net_edge(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        # The book cannot complete the proposal. Its actual 60-share VWAP,
        # rather than the proposed size or top-of-book price, carries the edge.
        thin = book_payload(
            bids=[(0.52, 500.0)],
            asks=[(0.55, 40.0), (0.57, 20.0)],
        )
        event = seed_accepted(
            store,
            raw_dir,
            book=thin,
            estimated_fill_shares=60.0,
            estimated_fill_price=33.4 / 60.0,
        )
        result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        fill = store.fill_of(event.decision_id)
        assert fill["complete"] is False
        assert fill["filled_shares"] == pytest.approx(60.0)
        assert fill["avg_price_usd_per_share"] == pytest.approx(33.4 / 60.0)
        cost = ExecutionCost(
            filled_shares=fill["filled_shares"],
            fill_cost_usd=fill["filled_shares"] * fill["avg_price_usd_per_share"],
            fees_usd=fill["fees_usd"],
            execution_buffer_usd=fill["execution_buffer_usd"],
        )
        assert edge_usd_per_share(
            q=event.model_probability,
            filled_shares=fill["filled_shares"],
            cost=cost,
        ) >= 0.02

    def test_partial_fill_is_capped_by_risk_size_before_book_depth(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        risk = RiskGate(
            Ledger(store),
            RiskRules(
                version="test-sized",
                basis="",
                max_opportunity_fraction=0.10,
                max_market_exposure_fraction=0.20,
            ),
            initial_capital_usdc=100.0,
        )
        thin = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 10.0)])
        event = seed_accepted(store, raw_dir, book=thin)
        result = make_engine(store, raw_dir, risk=risk).fill_pending()
        assert result.filled == 1
        row = store.db.execute(
            "SELECT requested_shares, filled_shares, complete FROM paper_fills WHERE decision_id = ?",
            [event.decision_id],
        ).fetchone()
        assert 5.0 < row[1] == 10.0
        assert 10.0 < row[0] < 100.0
        assert row[2] is False

    @pytest.mark.parametrize(("depth", "fills"), [(5.0, True), (4.99, False)])
    def test_venue_minimum_boundary_is_inclusive(self, engine_env, depth, fills):
        store, _events, raw_dir, _db = engine_env
        boundary = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, depth)])
        event = seed_accepted(store, raw_dir, book=boundary)
        result = make_engine(store, raw_dir).fill_pending()
        assert (result.filled == 1) is fills
        if fills:
            assert store.fill_of(event.decision_id)["filled_shares"] == pytest.approx(5.0)
        else:
            assert result.by_reason == {"INSUFFICIENT_CAPACITY": 1}
            assert store.fill_of(event.decision_id) is None

    def test_a_fill_below_the_venue_minimum_is_refused(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        dust = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 3.0)])
        event = parse_signal_event(
            export_row(
                decision_id="d" * 32,
                token_id=TOKEN,
                proposed_shares=100.0,
                book_id=real_book_id(dust),
            )
        )
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[
                ws_line(dust, received=event.decision_time_utc - timedelta(seconds=20))
            ],
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        result = make_engine(store, raw_dir).fill_pending()
        assert result.by_reason == {"INSUFFICIENT_CAPACITY": 1}
        assert "below the venue minimum order of 5" in (
            store.signal(event.decision_id)["refusal_detail"] or ""
        )
        assert store.fill_of(event.decision_id) is None

    @pytest.mark.parametrize(
        ("decision_time", "expected_rate"),
        [
            ("2026-11-20T23:58:59+00:00", 0.05),
            ("2026-11-20T23:59:00+00:00", 0.01),
        ],
    )
    def test_a_missing_fee_uses_the_dated_fallback_and_warns(
        self, engine_env, caplog, decision_time, expected_rate
    ):
        store, _events, raw_dir, _db = engine_env
        decision = datetime.fromisoformat(decision_time)
        book = book_payload(
            bids=[(0.52, 500.0)],
            asks=[(0.55, 60.0), (0.57, 200.0)],
            timestamp=(decision - timedelta(seconds=20)).isoformat(),
        )
        event = seed_accepted(
            store,
            raw_dir,
            book=book,
            decision_time_utc=decision_time,
            fee_rate=None,
        )
        with caplog.at_level("WARNING", logger="weatherquant.paper"):
            result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        fill = store.fill_of(event.decision_id)
        assert fill["fees_usd"] == pytest.approx(
            taker_fee_usd(
                filled_shares=fill["filled_shares"],
                price_usd_per_share=fill["avg_price_usd_per_share"],
                fee_rate=expected_rate,
            )
        )
        stored_rate = store.db.execute(
            "SELECT fee_rate FROM paper_fills WHERE decision_id = ?", [event.decision_id]
        ).fetchone()[0]
        assert stored_rate == pytest.approx(expected_rate)
        assert "fee fallback used" in caplog.text
        assert event.decision_id in caplog.text
        # The fallback prices the fill without rewriting the published event.
        original = store.db.execute(
            "SELECT fee_rate, event_json FROM paper_signals WHERE decision_id = ?",
            [event.decision_id],
        ).fetchone()
        assert original[0] is None
        assert json.loads(original[1])["fee_rate"] is None

    def test_a_venue_zero_fee_wins_over_the_fallback_without_warning(
        self, engine_env, caplog
    ):
        store, _events, raw_dir, _db = engine_env
        decision = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
        book = book_payload(
            bids=[(0.52, 500.0)],
            asks=[(0.55, 60.0), (0.57, 200.0)],
            timestamp=(decision - timedelta(seconds=20)).isoformat(),
        )
        event = seed_accepted(
            store,
            raw_dir,
            book=book,
            decision_time_utc=decision.isoformat(),
            fee_rate=0.0,
        )
        with caplog.at_level("WARNING", logger="weatherquant.paper"):
            result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        assert store.fill_of(event.decision_id)["fees_usd"] == 0.0
        assert store.db.execute(
            "SELECT fee_rate FROM paper_fills WHERE decision_id = ?", [event.decision_id]
        ).fetchone()[0] == 0.0
        assert "fee fallback used" not in caplog.text

    def test_a_missing_fee_without_a_covering_schedule_is_refused(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        event = accept_signal(raw_dir, fee_rate=None)
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        future_only = DatedFeeSchedule(
            version="test-future",
            venue="polymarket",
            category="weather",
            role="taker",
            entries=(FeeScheduleEntry(effective_from_utc=datetime(2027, 1, 1, tzinfo=UTC), rate=0.01),),
        )
        result = make_engine(store, raw_dir, fee_schedule=future_only).fill_pending()
        assert result.by_reason == {"UNPRICABLE_EVENT": 1}
        assert store.fill_of(event.decision_id) is None

    def test_buy_and_sell_never_double_charge_the_fee(self, engine_env):
        # One fill, one fee. The sell's net proceeds derive from the SAME
        # three-term decomposition the buy's total cost uses.
        store, _events, raw_dir, _db = engine_env
        buy = seed_accepted(store, raw_dir, decision_id="b" * 32)
        sell = seed_accepted(store, raw_dir, decision_id="s" * 32, direction="sell_yes")
        make_engine(store, raw_dir).fill_pending()
        buy_fee = store.fill_of(buy.decision_id)["fees_usd"]
        sell_fill = store.fill_of(sell.decision_id)
        # Sell at a single bid level: same notional, same fee arithmetic.
        assert sell_fill["fees_usd"] == pytest.approx(
            taker_fee_usd(
                filled_shares=100.0,
                price_usd_per_share=sell_fill["avg_price_usd_per_share"],
                fee_rate=0.05,
            )
        )
        assert buy_fee > 0


class TestEndToEnd:
    def test_consume_then_fill_over_the_published_channel(self, engine_env):
        """Export part -> consume -> fill, with real archive evidence."""
        store, events_dir, raw_dir, _db = engine_env
        event = parse_signal_event(export_row(proposed_shares=100.0, token_id=TOKEN))
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(two_level_book(), received=event.decision_time_utc - timedelta(seconds=20))],
        )
        parts = events_dir / "parts"
        parts.mkdir(parents=True)
        with (parts / "part-000000001.jsonl").open("w") as handle:
            handle.write(
                json.dumps(
                    {
                        **export_row(
                            proposed_shares=100.0, token_id=TOKEN, book_id=real_book_id()
                        )
                    },
                    default=str,
                )
                + "\n"
            )

        consumer = SignalConsumer(
            store,
            events_dir=events_dir,
            max_signal_age_seconds=600.0,
            clock=lambda: NOW,
        )
        assert consumer.consume_once().accepted == 1
        result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1
        assert store.signal_counts() == {"FILLED": 1}
        fill = store.fill_of(event.decision_id)
        assert fill["avg_price_usd_per_share"] == pytest.approx(0.558)


class TestVenueClockSkew:
    """Paper must not refuse a book the shadow accepted for the same skew."""

    def _seed(self, store, raw_dir, venue_ts: str):
        frame = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 60.0), (0.57, 200.0)], timestamp=venue_ts)
        event = accept_signal(raw_dir, decision_id="d" * 32, book=frame)
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        return event

    def test_a_venue_stamp_one_second_ahead_still_fills(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        self._seed(store, raw_dir, "2026-10-26T12:00:01Z")
        result = make_engine(store, raw_dir).fill_pending()
        assert result.filled == 1, result.by_reason

    def test_a_venue_stamp_beyond_the_tolerance_is_refused(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        self._seed(store, raw_dir, "2026-10-26T12:00:03Z")
        result = make_engine(store, raw_dir).fill_pending()
        assert result.by_reason == {"NOT_EXECUTABLE": 1}


class TestRepeatedSignalsOnOneBook:
    """Shadow re-ACCEPTs every cycle while a book is current; its depth is finite."""

    def test_depth_taken_by_one_fill_is_not_filled_again(self, engine_env):
        store, _events, raw_dir, _db = engine_env
        ids = [c * 32 for c in "abcde"]
        thin = book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 60.0), (0.57, 40.0)])
        for decision_id in ids:
            seed_accepted(
                store,
                raw_dir,
                decision_id=decision_id,
                book=thin,
                proposed_shares=30.0,
                estimated_fill_shares=30.0,
            )
        result = make_engine(store, raw_dir).fill_pending()
        # Asks 60 @ 0.55, 40 @ 0.57 -> 30, 30, 30, 10, then nothing.
        fills = [store.fill_of(i) for i in ids]
        filled = [f["filled_shares"] if f else 0.0 for f in fills]
        assert sorted(filled, reverse=True) == pytest.approx([30.0, 30.0, 30.0, 10.0, 0.0])
        assert sum(filled) == pytest.approx(100.0)
        assert result.filled == 4 and result.by_reason == {"INSUFFICIENT_CAPACITY": 1}
        prices = sorted(f["avg_price_usd_per_share"] for f in fills if f)
        assert prices == pytest.approx([0.55, 0.55, 0.57, 0.57])

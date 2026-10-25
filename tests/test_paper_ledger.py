"""WQN-42: the double-sided ledger, positions and capital occupation.

Under test: separate buckets that never collapse, buy/sell both accounted,
no negative balance, no double debit, event-by-event auditability, and a
valuation that says MARK_UNAVAILABLE instead of inventing a number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paper_book_helpers import TOKEN, two_level_book, write_archive, ws_line
from paper_fills_support import make_fill
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import (
    InsufficientFunds,
    InsufficientInventory,
    Ledger,
)
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path):
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        ledger = Ledger(store)
        ledger.open_account(initial_capital_usdc=100.0, now=NOW)
        yield store, ledger, db


class TestOpening:
    def test_open_entry_and_balances(self, env):
        _store, ledger, _db = env
        balances = ledger.balances()
        assert balances["available_usdc"] == 100.0
        assert balances["occupied_usdc"] == 0.0
        assert balances["realized_pnl_usdc"] == 0.0
        entries = ledger.ledger_entries()
        assert [e["kind"] for e in entries] == ["OPEN"]
        assert entries[0]["balance_after_usdc"] == 100.0

    def test_open_is_idempotent(self, env):
        _store, ledger, _db = env
        assert ledger.open_account(initial_capital_usdc=100.0, now=NOW) is False
        assert len(ledger.ledger_entries()) == 1


class TestBuySide:
    def test_buy_moves_cash_out_and_position_in(self, env):
        _store, ledger, db = env
        fill = make_fill(decision_id="b1", fees_usd=0.279)
        with db.transaction():
            assert ledger.apply_buy(fill, now=NOW) is True
        balances = ledger.balances()
        # Cash out = fill (55.80) + fees (0.279): the venue-real figure.
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079)
        assert balances["occupied_usdc"] == pytest.approx(56.079)
        positions = ledger.positions()
        assert len(positions) == 1
        assert positions[0].shares == pytest.approx(100.0)
        assert positions[0].avg_cost_usd_per_share == pytest.approx(0.56079)
        entry = next(e for e in ledger.ledger_entries() if e["kind"] == "BUY_SETTLE")
        assert entry["amount_usdc"] == pytest.approx(-56.079)
        assert entry["balance_after_usdc"] == pytest.approx(100.0 - 56.079)
        assert entry["detail"]["shares"] == 100.0

    def test_two_buys_average_the_cost(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        later = NOW + timedelta(minutes=1)
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="b2", shares=20.0, fill_cost=11.4, fees_usd=0.057, price=0.57),
                now=later,
            )
        positions = ledger.positions()
        assert positions[0].shares == pytest.approx(120.0)
        assert positions[0].cost_basis_usdc == pytest.approx(56.079 + 11.457)
        assert positions[0].avg_cost_usd_per_share == pytest.approx((56.079 + 11.457) / 120.0)

    def test_buy_beyond_cash_is_refused_and_writes_nothing(self, env):
        _store, ledger, db = env
        # 100 USDC cannot buy 200 shares at ~0.56.
        with pytest.raises(InsufficientFunds), db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="big", shares=200.0, fill_cost=111.6, price=0.558),
                now=NOW,
            )
        assert ledger.balances()["available_usdc"] == 100.0
        assert ledger.positions() == []
        assert [e["kind"] for e in ledger.ledger_entries()] == ["OPEN"]

    def test_double_apply_never_debits_twice(self, env):
        _store, ledger, db = env
        fill = make_fill(decision_id="b1", fees_usd=0.279)
        with db.transaction():
            assert ledger.apply_buy(fill, now=NOW) is True
            # The same settle replayed inside the same batch: no second debit.
            assert ledger.apply_buy(fill, now=NOW) is False
        assert ledger.balances()["available_usdc"] == pytest.approx(100.0 - 56.079)
        kinds = [e["kind"] for e in ledger.ledger_entries()]
        assert kinds.count("BUY_SETTLE") == 1


class TestSellSide:
    def _buy(self, ledger, db):
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)

    def test_sell_books_realised_pnl_and_frees_capital(self, env):
        _store, ledger, db = env
        self._buy(ledger, db)
        # Sell the whole position: 100 shares at 0.60 fill (60.00), fee 0.30.
        with db.transaction():
            assert ledger.apply_sell(
                make_fill(decision_id="s1", shares=100.0, fill_cost=60.0, fees_usd=0.30, price=0.60, side="sell"),
                now=NOW + timedelta(minutes=2),
            ) is True
        balances = ledger.balances()
        # net in = 60.00 - 0.30 = 59.70; realised = 59.70 - 56.079 = 3.621.
        assert balances["realized_pnl_usdc"] == pytest.approx(59.70 - 56.079)
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079 + 59.70)
        assert balances["occupied_usdc"] == pytest.approx(0.0)
        # The position is closed, not deleted: shares 0, basis 0.
        assert all(p.shares == 0.0 for p in ledger.positions())

    def test_partial_sell_keeps_remaining_basis(self, env):
        _store, ledger, db = env
        self._buy(ledger, db)
        with db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s1", shares=40.0, fill_cost=24.0, fees_usd=0.12, price=0.60, side="sell"),
                now=NOW + timedelta(minutes=2),
            )
        positions = [p for p in ledger.positions() if p.shares > 0]
        assert len(positions) == 1
        assert positions[0].shares == pytest.approx(60.0)
        assert positions[0].cost_basis_usdc == pytest.approx(56.079 * 0.6)
        # realised = (24 - 0.12) - 0.56079*40
        expected = 23.88 - 0.56079 * 40
        assert ledger.balances()["realized_pnl_usdc"] == pytest.approx(expected)

    def test_sell_without_inventory_is_refused(self, env):
        _store, ledger, db = env
        with pytest.raises(InsufficientInventory), db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s0", shares=100.0, fill_cost=52.0, fees_usd=0.26, price=0.52, side="sell"),
                now=NOW,
            )
        assert ledger.balances()["realized_pnl_usdc"] == 0.0

    def test_sell_cannot_exceed_held_shares(self, env):
        _store, ledger, db = env
        self._buy(ledger, db)
        with pytest.raises(InsufficientInventory), db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s1", shares=150.0, fill_cost=78.0, fees_usd=0.39, price=0.52, side="sell"),
                now=NOW + timedelta(minutes=2),
            )


class TestConservation:
    def test_available_plus_occupied_equals_initial_plus_realised(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        with db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s1", shares=50.0, fill_cost=30.0, fees_usd=0.15, price=0.60, side="sell"),
                now=NOW + timedelta(minutes=1),
            )
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="b2", shares=20.0, fill_cost=11.0, fees_usd=0.055, price=0.55),
                now=NOW + timedelta(minutes=2),
            )
        b = ledger.balances()
        assert b["available_usdc"] + b["occupied_usdc"] == pytest.approx(100.0 + b["realized_pnl_usdc"])

    def test_verify_replays_the_ledger_to_the_same_state(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        with db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s1", shares=40.0, fill_cost=24.0, fees_usd=0.12, price=0.60, side="sell"),
                now=NOW + timedelta(minutes=1),
            )
        report = ledger.verify()
        assert report["consistent"] is True
        assert report["balances_match"] is True
        assert report["positions_match"] is True
        assert report["running_balance_consistent"] is True

    def test_verify_detects_tampered_balances(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        # Someone edits a number outside the ledger: replay must notice.
        db.execute("UPDATE paper_balances SET available_usdc = 99.0 WHERE account_id = 'paper-main'")
        report = ledger.verify()
        assert report["consistent"] is False
        assert report["balances_match"] is False


class TestValuation:
    def test_marked_positions_and_equity(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        report = ledger.mark_valuation(lambda token: 0.60 if token == TOKEN else None)
        assert report["all_marked"] is True
        assert report["positions"][0]["value_usdc"] == pytest.approx(60.0)
        assert report["equity_usdc"] == pytest.approx((100.0 - 56.079) + 60.0)
        # Unrealised P&L is derived on read, never booked as realised.
        assert report["realized_pnl_usdc"] == 0.0

    def test_missing_mark_is_mark_unavailable_never_zero(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        report = ledger.mark_valuation(lambda token: None)
        assert report["all_marked"] is False
        assert report["marks_unavailable"] == ["mkt-1"]
        position = report["positions"][0]
        assert position["mark_status"] == "MARK_UNAVAILABLE"
        assert position["value_usdc"] is None
        # Equity states what it could mark - it does not silently shrink.
        assert report["equity_usdc"] == pytest.approx(100.0 - 56.079)
        assert report["marked_positions_value_usdc"] == 0.0


class TestFillEngineIntegration:
    def test_fill_debits_cash_and_builds_position_atomically(self, tmp_path):
        raw_dir = tmp_path / "raw"
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            Ledger(store).open_account(initial_capital_usdc=100.0, now=NOW)
            _seed_accepted(store, raw_dir)
            engine = FillEngine(
                store,
                book_source=ArchiveBookSource(raw_dir),
                buffer_fraction=0.005,
                clock=lambda: NOW,
            )
            assert engine.fill_pending().filled == 1
            ledger = Ledger(store)
            balances = ledger.balances()
            fill = store.fill_of("d" * 32)
            assert balances["available_usdc"] == pytest.approx(
                100.0 - (fill["avg_price_usd_per_share"] * 100.0 + fill["fees_usd"])
            )
            assert ledger.positions()[0].shares == pytest.approx(100.0)
            assert ledger.verify()["consistent"] is True

    def test_ledger_refusal_leaves_no_half_trade(self, tmp_path):
        """A crash injected mid-settle rolls back fill, ledger and position."""
        raw_dir = tmp_path / "raw"
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            Ledger(store).open_account(initial_capital_usdc=100.0, now=NOW)
            _seed_accepted(store, raw_dir, decision_id="e" * 32, market_id="mkt-big")
            engine = FillEngine(
                store,
                book_source=ArchiveBookSource(raw_dir),
                buffer_fraction=0.005,
                clock=lambda: NOW,
                ledger=ExplodingLedger(store),
            )
            result = engine.fill_pending()
            # The injected failure is contained: the signal is recorded as
            # failed-to-fill (still ACCEPTED for a later pass), and nothing
            # half-landed.
            assert result.filled == 0
            ledger = Ledger(store)
            assert ledger.balances()["available_usdc"] == 100.0
            assert ledger.positions() == []
            assert store.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0


def _seed_accepted(store, raw_dir, **kwargs):
    from datetime import timedelta as _td

    from paper_fills_support_import import real_book_id_for_seed
    from paper_helpers import export_row
    from weatherquant.paper.events import parse_signal_event

    row = export_row(
        decision_id=kwargs.get("decision_id", "d" * 32),
        token_id=TOKEN,
        proposed_shares=100.0,
        estimated_fill_shares=100.0,
        book_id=real_book_id_for_seed(),
    )
    event = parse_signal_event(row)
    write_archive(
        raw_dir,
        source="ws",
        day="2026-10-26",
        lines=[ws_line(two_level_book(), received=event.decision_time_utc - _td(seconds=20))],
    )
    store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
    return event


class ExplodingLedger(Ledger):
    """A ledger whose first buy settlement dies mid-transaction."""

    def apply_buy(self, fill, *, now):
        raise RuntimeError("injected: disk full between debit and fill row")

"""WQN-44: verified settlement, capital release and correction events.

Under test: only RESOLVED {0,1} payouts move the ledger; unsettled states
never book realised P&L; corrections compensate instead of editing; redelivered
settlements are idempotent; and everything a human must decide is flagged,
not guessed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from paper_fills_support import make_fill
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.settlements import (
    SettlementParseError,
    parse_settlement_record,
    read_settlement_feed,
)
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path):
    """An account that has bought the fixture position and still holds it."""
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        ledger = Ledger(store)
        ledger.open_account(initial_capital_usdc=100.0, now=NOW)
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        yield store, ledger, db


def settlement(market_id="mkt-1", revision=1, status="RESOLVED", yes_payout=1.0, **overrides):
    row = {
        "market_id": market_id,
        "revision": revision,
        "status": status,
        "yes_payout": yes_payout,
        "settled_at_utc": NOW.isoformat(),
        "transaction_hash": "0xabc",
        "source": "data-api.polymarket.com/v2/resolutions",
    }
    row.update(overrides)
    return row


def apply(ledger, db, row, **kwargs):
    with db.transaction():
        return ledger.apply_settlement(parse_settlement_record(row), now=kwargs.pop("now", NOW))


class TestParsing:
    def test_resolved_with_valid_payout(self):
        event = parse_settlement_record(settlement())
        assert event.is_payout is True
        assert event.yes_payout == 1.0
        assert event.evidence["transaction_hash"] == "0xabc"

    @pytest.mark.parametrize("status", ["DISPUTED", "PENDING", "UNAVAILABLE", "UNVERIFIABLE"])
    def test_non_resolution_states_are_not_payouts(self, status):
        event = parse_settlement_record(settlement(status=status, yes_payout=None))
        assert event.is_payout is False
        assert event.yes_payout is None

    def test_resolved_without_payout_is_malformed(self):
        with pytest.raises(SettlementParseError, match="yes_payout"):
            parse_settlement_record(settlement(yes_payout=None))

    def test_fractional_payout_is_malformed_never_exotic(self):
        # 0.5 is a proposal in flight, not a resolution: the platform's
        # answer is a share of the $1 collateral, nothing between.
        with pytest.raises(SettlementParseError, match="proposal"):
            parse_settlement_record(settlement(yes_payout=0.5))

    def test_non_resolution_with_payout_is_malformed(self):
        # A payout on a PENDING record would fabricate profit at parse time.
        with pytest.raises(SettlementParseError, match="must not carry yes_payout"):
            parse_settlement_record(settlement(status="PENDING", yes_payout=1.0))

    def test_unknown_status_is_malformed(self):
        with pytest.raises(SettlementParseError, match="unknown status"):
            parse_settlement_record(settlement(status="CANCELLED"))

    def test_missing_fields_name_them(self):
        with pytest.raises(SettlementParseError, match="market_id"):
            parse_settlement_record({"revision": 1, "status": "RESOLVED", "yes_payout": 1.0})

    def test_revision_below_one_is_malformed(self):
        with pytest.raises(SettlementParseError, match="revision"):
            parse_settlement_record(settlement(revision=0))


class TestPayout:
    def test_settlement_releases_capital_and_books_realised(self, env):
        _store, ledger, db = env
        outcome = apply(ledger, db, settlement())
        assert outcome["action"] == "APPLIED"
        balances = ledger.balances()
        # Cost basis 56.079 (55.80 fill + 0.279 fee); payout 1.0 x 100 shares.
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079 + 100.0)
        assert balances["occupied_usdc"] == pytest.approx(0.0)
        assert balances["realized_pnl_usdc"] == pytest.approx(100.0 - 56.079)
        # The position is closed, not deleted: shares 0, basis 0.
        assert all(p.shares == 0.0 for p in ledger.positions())
        assert ledger.verify()["consistent"] is True

    def test_zero_payout_releases_nothing_and_books_the_loss(self, env):
        _store, ledger, db = env
        outcome = apply(ledger, db, settlement(yes_payout=0.0))
        assert outcome["action"] == "APPLIED"
        balances = ledger.balances()
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079)
        assert balances["occupied_usdc"] == pytest.approx(0.0)
        assert balances["realized_pnl_usdc"] == pytest.approx(-56.079)

    def test_unsettled_states_record_but_never_book_profit(self, env):
        _store, ledger, db = env
        for i, status in enumerate(("PENDING", "DISPUTED", "UNAVAILABLE", "UNVERIFIABLE"), start=1):
            outcome = apply(ledger, db, settlement(revision=i, status=status, yes_payout=None))
            assert outcome["action"] == "RECORDED"
            b = ledger.balances()
            assert b["realized_pnl_usdc"] == 0.0
            assert b["occupied_usdc"] == pytest.approx(56.079)
        # All four are in the audit table; none was applied.
        applied = ledger.db.execute("SELECT COUNT(*) FROM paper_settlements WHERE applied").fetchone()[0]
        assert applied == 0

    def test_settlement_without_a_position_is_recorded(self, tmp_path):
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            ledger = Ledger(store)
            ledger.open_account(initial_capital_usdc=100.0, now=NOW)
            outcome = apply(ledger, db, settlement(market_id="never-traded"))
            assert outcome["action"] == "RECORDED"
            assert "no open position" in outcome["detail"]
            assert ledger.balances()["available_usdc"] == 100.0

    def test_settlement_after_a_full_sell_touches_nothing(self, env):
        _store, ledger, db = env
        with db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s1", shares=100.0, fill_cost=60.0, fees_usd=0.30, price=0.60, side="sell"),
                now=NOW + timedelta(minutes=1),
            )
        realised_at_sale = ledger.balances()["realized_pnl_usdc"]
        outcome = apply(ledger, db, settlement(yes_payout=1.0))
        assert outcome["action"] == "RECORDED"
        assert ledger.balances()["realized_pnl_usdc"] == pytest.approx(realised_at_sale)


class TestIdempotency:
    def test_same_record_twice_is_one_movement(self, env):
        _store, ledger, db = env
        first = apply(ledger, db, settlement())
        second = apply(ledger, db, settlement())
        assert first["action"] == "APPLIED"
        assert second["action"] == "NOOP"
        assert ledger.balances()["available_usdc"] == pytest.approx(100.0 - 56.079 + 100.0)
        kinds = [e["kind"] for e in ledger.ledger_entries()]
        assert kinds.count("SETTLE_PAYOUT") == 1

    def test_rereading_the_whole_feed_changes_nothing(self, env, tmp_path):
        _store, ledger, db = env
        feed = tmp_path / "settlements.jsonl"
        feed.write_text("\n".join(json.dumps(settlement()) for _ in [1]) + "\n", encoding="utf-8")
        for _ in range(2):
            events = read_settlement_feed(feed)
            with db.transaction():
                for event in events:
                    ledger.apply_settlement(event, now=NOW)
        assert ledger.balances()["realized_pnl_usdc"] == pytest.approx(100.0 - 56.079)
        kinds = [e["kind"] for e in ledger.ledger_entries()]
        assert kinds.count("SETTLE_PAYOUT") == 1

    def test_lower_revision_after_higher_is_superseded(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=2))
        outcome = apply(ledger, db, settlement(revision=1))
        assert outcome["action"] == "NOOP"
        assert "superseded" in outcome["detail"]

    def test_redundant_confirmation_writes_no_entry(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=1, yes_payout=1.0))
        outcome = apply(ledger, db, settlement(revision=2, yes_payout=1.0))
        assert outcome["action"] == "NOOP"
        kinds = [e["kind"] for e in ledger.ledger_entries()]
        assert kinds.count("SETTLE_PAYOUT") == 1
        assert "CORRECTION" not in kinds


class TestCorrections:
    def test_changed_payout_compensates_and_preserves_history(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=1, yes_payout=1.0))
        outcome = apply(ledger, db, settlement(revision=2, yes_payout=0.0))
        assert outcome["action"] == "APPLIED"
        balances = ledger.balances()
        # The 100 USDC paid out at revision 1 is clawed back; realised goes
        # from +43.921 to -56.079, exactly the corrected truth.
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079)
        assert balances["realized_pnl_usdc"] == pytest.approx(-56.079)
        # History, not an edit: the payout and the correction both stand.
        kinds = [e["kind"] for e in ledger.ledger_entries()]
        assert kinds == ["OPEN", "BUY_SETTLE", "SETTLE_PAYOUT", "CORRECTION"]
        correction = ledger.ledger_entries()[-1]
        assert correction["amount_usdc"] == pytest.approx(-100.0)
        assert correction["detail"]["old_payout"] == 1.0
        assert correction["detail"]["new_payout"] == 0.0
        assert ledger.verify()["consistent"] is True

    def test_upward_correction_pays_the_difference(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=1, yes_payout=0.0))
        outcome = apply(ledger, db, settlement(revision=2, yes_payout=1.0))
        assert outcome["action"] == "APPLIED"
        assert ledger.balances()["available_usdc"] == pytest.approx(100.0 - 56.079 + 100.0)
        assert ledger.verify()["consistent"] is True

    def test_clawback_beyond_cash_is_flagged_not_applied(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=1, yes_payout=1.0))
        # Spend the settlement proceeds on a new position.
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="b2", shares=100.0, fill_cost=90.0, fees_usd=0.45, price=0.90),
                now=NOW + timedelta(minutes=1),
            )
        outcome = apply(ledger, db, settlement(revision=2, yes_payout=0.0))
        assert outcome["action"] == "REVIEW_REQUIRED"
        # Nothing moved: the clawback that would drive cash negative waits.
        balances = ledger.balances()
        assert balances["available_usdc"] == pytest.approx(143.921 - 90.45)
        assert balances["realized_pnl_usdc"] == pytest.approx(43.921)
        reviews = ledger.review_required()
        assert len(reviews) == 1
        assert reviews[0]["market_id"] == "mkt-1"
        assert "claw back" in reviews[0]["review_reason"]

    def test_dispute_after_payout_is_flagged_and_touches_nothing(self, env):
        _store, ledger, db = env
        apply(ledger, db, settlement(revision=1, yes_payout=1.0))
        outcome = apply(ledger, db, settlement(revision=2, status="DISPUTED", yes_payout=None))
        assert outcome["action"] == "REVIEW_REQUIRED"
        # The dispute does not keep the payout as profit (that would
        # fabricate realised P&L) and does not reverse it either (that would
        # fabricate the opposite). A human decides; the balances wait.
        assert ledger.balances()["realized_pnl_usdc"] == pytest.approx(43.921)
        assert ledger.review_required()[0]["status"] == "DISPUTED"


@pytest.mark.parametrize("payouts", [(1.0, 1.0, 0.0), (1.0, 0.0, 1.0, 0.0)])
def test_confirmations_and_multiple_corrections_keep_original_shares(env, payouts):
    _store, ledger, db = env
    for revision, payout in enumerate(payouts, start=1):
        apply(ledger, db, settlement(revision=revision, yes_payout=payout))
        balances = ledger.balances()
        assert balances["available_usdc"] == pytest.approx(100.0 - 56.079 + 100 * payout)
        assert balances["realized_pnl_usdc"] == pytest.approx(100 * payout - 56.079)
        assert ledger._applied_settlement("mkt-1")["settled_shares"] == 100.0
        assert ledger.verify()["consistent"]

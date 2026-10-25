"""WQN-43: the risk gates.

Under test: per-opportunity budget, cumulative per-market exposure, no
leverage, no naked shorts, sizing instead of blanket refusal, and rule
files that refuse to load silently incomplete.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from paper_book_helpers import TOKEN, two_level_book, write_archive, ws_line
from paper_fills_support import make_fill
from paper_helpers import export_row
from weatherquant.paper.books import ArchiveBookSource, BookEvidence
from weatherquant.paper.events import EventError, parse_signal_event
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.risk import RiskGate, RiskRules, load_risk_rules
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


class TestRulesLoading:
    def test_mvp_file_loads(self):
        rules = load_risk_rules("configs/paper-risk.json")
        assert rules.version == "mvp-v1"
        assert rules.max_opportunity_fraction == 0.10
        assert rules.max_market_exposure_fraction == 0.20

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_risk_rules(tmp_path / "absent.json")

    @pytest.mark.parametrize("drop", ["max_opportunity_fraction", "max_market_exposure_fraction"])
    def test_missing_key_is_an_error_never_a_default(self, tmp_path, drop):
        payload = {
            "version": "t",
            "thresholds": {
                "max_opportunity_fraction": {"value": 0.1},
                "max_market_exposure_fraction": {"value": 0.2},
            },
        }
        del payload["thresholds"][drop]
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(payload))
        # A permissive default here is a cap that is off without anyone
        # turning it off.
        with pytest.raises(ValueError, match=drop):
            load_risk_rules(path)

    def test_fraction_outside_unit_interval_is_an_error(self, tmp_path):
        payload = {
            "version": "t",
            "thresholds": {
                "max_opportunity_fraction": {"value": 1.5},
                "max_market_exposure_fraction": {"value": 0.2},
            },
        }
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError, match="unit interval"):
            load_risk_rules(path)


@pytest.fixture()
def env(tmp_path):
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        ledger = Ledger(store)
        ledger.open_account(initial_capital_usdc=100.0, now=NOW)
        gate = RiskGate(
            ledger,
            RiskRules(
                version="mvp-test",
                basis="test: shadow mvp caps",
                max_opportunity_fraction=0.10,
                max_market_exposure_fraction=0.20,
            ),
            initial_capital_usdc=100.0,
        )
        yield store, ledger, gate, db


class TestSizing:
    def test_buy_is_sized_down_to_the_budget(self, env):
        _store, _ledger, gate, _db = env

        evidence = _evidence()
        event = parse_signal_event(export_row(token_id=TOKEN, proposed_shares=100.0))
        shares, verdict = gate.size(event, evidence)
        assert verdict.allowed
        # 10 USDC budget at best ask 0.55 with the fee margin reserved.
        assert shares < 100.0
        assert shares * 0.55 * 1.0125 <= 10.0 + 1e-9

    def test_buy_never_exceeds_the_proposal(self, env):
        # A huge budget still fills at most what the shadow proposed.
        _store, ledger, gate, _db = env
        gate = RiskGate(
            ledger,
            RiskRules(version="t", basis="", max_opportunity_fraction=1.0, max_market_exposure_fraction=1.0),
            initial_capital_usdc=1_000_000.0,
        )
        event = parse_signal_event(export_row(token_id=TOKEN, proposed_shares=30.0))
        shares, verdict = gate.size(event, _evidence())
        assert verdict.allowed
        assert shares == 30.0

    def test_zero_headroom_refuses_with_the_exposure_reason(self, env):
        _store, ledger, gate, db = env
        # Fill the market's exposure cap with an existing position.
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="e1", shares=34.0, fill_cost=19.0, fees_usd=0.095, price=0.558),
                now=NOW,
            )
        verdict = gate.before_fill(parse_signal_event(export_row(token_id=TOKEN)))
        assert verdict.allowed  # a buy passes before_fill
        _shares, sized = gate.size(parse_signal_event(export_row(token_id=TOKEN, proposed_shares=100.0)), _evidence())
        # 19 USDC occupied against a 20 cap: under 1 USDC of headroom sizes a
        # 1.6-share fill, below the venue's minimum order - refused, because a
        # sub-minimum fill is one the venue could never have accepted.
        assert not sized.allowed
        assert sized.reason == "RISK_BUDGET_EXCEEDED"
        assert "minimum order" in sized.detail

    def test_market_fully_climbed_refuses(self, env):
        _store, ledger, gate, db = env
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="e1", shares=34.0, fill_cost=19.5, fees_usd=0.098, price=0.558),
                now=NOW,
            )
            ledger.apply_buy(
                make_fill(decision_id="e2", shares=1.0, fill_cost=0.6, fees_usd=0.003, price=0.558),
                now=NOW,
            )
        _shares, verdict = gate.size(parse_signal_event(export_row(token_id=TOKEN, proposed_shares=100.0)), _evidence())
        assert not verdict.allowed
        assert verdict.reason == "RISK_BUDGET_EXCEEDED"
        assert "mvp-test" in verdict.detail

    def test_sell_is_sized_to_what_is_held(self, env):
        _store, ledger, gate, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        with db.transaction():
            # Sell 40 of the 100 held: the sell sized to the proposal is 100
            # capped by... nothing, but a sell of 150 is capped at 100.
            event = parse_signal_event(export_row(token_id=TOKEN, direction="sell_yes", proposed_shares=150.0))
            shares, verdict = gate.size(event, None)
        assert verdict.allowed
        assert shares == pytest.approx(100.0)


def _evidence():
    """The fixture book as BookEvidence, bypassing the archive for unit tests."""
    from weatherquant.data.books import parse_book_payload
    from weatherquant.markets.models import OrderBook

    snapshot = parse_book_payload(
        two_level_book(),
        token_id=TOKEN,
        market_id="mkt-1",
        fetched_at_utc=NOW,
        run_id="",
        source="ws",
    )
    return BookEvidence(
        token_id=TOKEN,
        market_id="mkt-1",
        book=OrderBook(
            market_id="mkt-1",
            token_id=TOKEN,
            bids=snapshot.bids,
            asks=snapshot.asks,
            venue_ts=snapshot.venue_ts,
            fetched_at_utc=NOW,
        ),
        book_id=snapshot.content_hash,
        book_source="ws",
        venue_ts_utc=snapshot.venue_ts,
        collector_received_at_utc=NOW,
        quality="ok",
    )


class TestCheckPriced:
    def test_over_budget_outflow_is_refused(self, env):
        _store, _ledger, gate, _db = env
        fill = make_fill(shares=100.0, price=0.558, fees_usd=0.279)
        verdict = gate.check_priced(parse_signal_event(export_row(token_id=TOKEN)), fill)
        assert not verdict.allowed
        assert verdict.reason == "RISK_BUDGET_EXCEEDED"
        assert "10.00" in verdict.detail

    def test_within_budget_passes(self, env):
        _store, _ledger, gate, _db = env
        fill = make_fill(shares=10.0, price=0.558, fees_usd=0.0279)
        verdict = gate.check_priced(parse_signal_event(export_row(token_id=TOKEN)), fill)
        assert verdict.allowed

    def test_market_exposure_is_cumulative_across_fills(self, env):
        # With a 10 USDC budget and a 20 USDC market cap, climbing the cap
        # takes three fills - which is the point: small repeated buys are
        # one exposure, not several.
        _store, ledger, gate, db = env
        for i in ("m1", "m2"):
            with db.transaction():
                ledger.apply_buy(
                    make_fill(decision_id=i, shares=17.0, fill_cost=9.5, fees_usd=0.048, price=0.553),
                    now=NOW,
                )
        third = make_fill(decision_id="m3", shares=17.0, fill_cost=9.5, fees_usd=0.048, price=0.553)
        verdict = gate.check_priced(parse_signal_event(export_row(token_id=TOKEN)), third)
        assert not verdict.allowed
        assert verdict.reason == "RISK_EXPOSURE_EXCEEDED"

    def test_other_markets_have_their_own_cap(self, env):
        _store, ledger, gate, db = env
        with db.transaction():
            ledger.apply_buy(
                make_fill(decision_id="m1", shares=17.0, fill_cost=9.4, fees_usd=0.047, price=0.553),
                now=NOW,
            )
        fill = make_fill(
            decision_id="other",
            market_id="mkt-2",
            token_id="tok-other",
            shares=17.0,
            fill_cost=9.4,
            fees_usd=0.047,
            price=0.553,
        )
        event = parse_signal_event(export_row(token_id=TOKEN, market_id="mkt-2"))
        assert gate.check_priced(event, fill).allowed

    def test_no_leverage_on_the_priced_outflow(self, env):
        _store, ledger, gate, db = env
        # Ten 9.5 USDC buys (one per market, each inside every cap) leave
        # 4.5 USDC; a further 5 USDC fill must be refused, not levered.
        for i in range(10):
            with db.transaction():
                ledger.apply_buy(
                    make_fill(
                        decision_id=f"m{i}",
                        shares=17.0,
                        fill_cost=9.5,
                        fees_usd=0.047,
                        price=0.553,
                        market_id=f"mkt-{i}",
                        token_id=f"tok-{i}",
                    ),
                    now=NOW,
                )
        available = ledger.balances()["available_usdc"]
        assert available < 5.0
        big = make_fill(shares=10.0, price=0.558, fees_usd=0.0279, market_id="mkt-11", token_id="tok-11")
        verdict = gate.check_priced(parse_signal_event(export_row(token_id=TOKEN, market_id="mkt-11")), big)
        assert not verdict.allowed
        assert verdict.reason == "INSUFFICIENT_FUNDS"


class TestNoNakedShorts:
    def test_sell_with_no_inventory_refused_before_pricing(self, env):
        _store, _ledger, gate, _db = env
        event = parse_signal_event(export_row(token_id=TOKEN, direction="sell_yes"))
        verdict = gate.before_fill(event)
        assert not verdict.allowed
        assert verdict.reason == "NO_INVENTORY"

    def test_sell_with_inventory_passes_the_gate(self, env):
        _store, ledger, gate, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        event = parse_signal_event(export_row(token_id=TOKEN, direction="sell_yes"))
        assert gate.before_fill(event).allowed

    def test_ledger_refuses_even_a_sized_over_sell(self, env):
        # Two locks on one door: sizing would cap a 150-share sell at the 100
        # held, and if anything beyond that ever reached the ledger, the
        # ledger's own inventory check refuses it.
        _store, ledger, gate, db = env
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        event = parse_signal_event(export_row(token_id=TOKEN, direction="sell_yes", proposed_shares=150.0))
        shares, verdict = gate.size(event, None)
        assert verdict.allowed and shares == 100.0  # sized down, not refused
        with pytest.raises(Exception, match="holds"), db.transaction():
            ledger.apply_sell(
                make_fill(decision_id="s0", shares=150.0, fill_cost=78.0, fees_usd=0.39, price=0.52, side="sell"),
                now=NOW,
            )


class TestBuyNoIsNotReachable:
    def test_buy_no_cannot_be_expressed_by_the_channel(self):
        # The shadow's vocabulary has no buy_no direction; a row claiming one
        # is malformed at the contract boundary, long before any risk gate.
        with pytest.raises(EventError, match="direction"):
            parse_signal_event(export_row(token_id=TOKEN, direction="buy_no"))


class TestIntegration:
    def _engine(self, store, raw_dir, gate):
        return FillEngine(
            store,
            book_source=ArchiveBookSource(raw_dir),
            buffer_fraction=0.005,
            clock=lambda: NOW,
            risk=gate,
        )

    def _seed(self, store, raw_dir, **kwargs):
        from paper_fills_support_import import real_book_id_for_seed

        event = parse_signal_event(
            export_row(
                decision_id=kwargs.get("decision_id", "d" * 32),
                token_id=TOKEN,
                proposed_shares=kwargs.get("proposed_shares", 100.0),
                book_id=real_book_id_for_seed(),
            )
        )
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(two_level_book(), received=event.decision_time_utc - timedelta(seconds=20))],
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        return event

    def test_engine_sizes_a_100_share_proposal_to_the_budget(self, tmp_path):
        raw_dir = tmp_path / "raw"
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            ledger = Ledger(store)
            ledger.open_account(initial_capital_usdc=100.0, now=NOW)
            gate = RiskGate(
                ledger,
                load_risk_rules("configs/paper-risk.json"),
                initial_capital_usdc=100.0,
            )
            self._seed(store, raw_dir)
            result = self._engine(store, raw_dir, gate).fill_pending()
            assert result.filled == 1
            fill = store.fill_of("d" * 32)
            # The proposal was 100 shares; the budget bought less.
            assert fill["filled_shares"] < 100.0
            cash_out = fill["avg_price_usd_per_share"] * fill["filled_shares"] + fill["fees_usd"]
            assert cash_out <= 10.0 + 1e-9
            assert ledger.verify()["consistent"] is True

    def test_engine_refuses_clearly_when_out_of_funds(self, tmp_path):
        raw_dir = tmp_path / "raw"
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=1.0, now=NOW)
            ledger = Ledger(store)
            ledger.open_account(initial_capital_usdc=1.0, now=NOW)
            gate = RiskGate(
                ledger,
                load_risk_rules("configs/paper-risk.json"),
                initial_capital_usdc=1.0,
            )
            self._seed(store, raw_dir)
            result = self._engine(store, raw_dir, gate).fill_pending()
            # Even one share of the fixture book costs ~0.56 USDC, inside a
            # 1 USDC account's 10% budget of 0.1 -> nothing affordable, and
            # the refusal says so in dollars and cents.
            assert result.filled == 0
            row = store.signal("d" * 32)
            assert row["outcome"] == "REFUSED"
            assert row["refusal_reason"] in ("RISK_BUDGET_EXCEEDED", "INSUFFICIENT_FUNDS")
            assert ledger.balances()["available_usdc"] == 1.0

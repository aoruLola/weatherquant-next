from datetime import timedelta

from paper_helpers import T0, export_row
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase


def test_pending_signals_keep_business_order_inside_one_consumer_batch(tmp_path):
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        for decision, direction, when in (
            ("z-buy", "buy_yes", T0),
            ("a-sell", "sell_yes", T0 + timedelta(seconds=1)),
        ):
            event = parse_signal_event(
                export_row(
                    decision_id=decision,
                    direction=direction,
                    decision_time_utc=when.isoformat(),
                )
            )
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=T0)
        pending = store.signals_with_outcome("ACCEPTED")
        assert [row["decision_id"] for row in pending] == ["z-buy", "a-sell"]


def test_buy_then_sell_in_one_batch_settles_in_business_order(tmp_path):
    from paper_book_helpers import T_DECISION, TOKEN, two_level_book, write_archive, ws_line
    from paper_fills_support import build_engine, open_ledger_if_needed
    from weatherquant.paper.books import ArchiveBookSource

    raw = tmp_path / "raw"
    write_archive(
        raw,
        source="ws",
        day="2026-10-26",
        lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
    )
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100, now=T0)
        open_ledger_if_needed(store, now=T0)
        for decision, direction, when in (
            ("z-buy", "buy_yes", T_DECISION),
            ("a-sell", "sell_yes", T_DECISION + timedelta(seconds=1)),
        ):
            event = parse_signal_event(
                export_row(
                    decision_id=decision,
                    direction=direction,
                    token_id=TOKEN,
                    decision_time_utc=when.isoformat(),
                    book_id=None,
                    proposed_shares=10,
                )
            )
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=T0)
        engine = build_engine(store, ArchiveBookSource(raw), clock=lambda: T0)
        result = engine.fill_pending()
        assert result.filled == 2, result.as_dict()
        assert store.signal("a-sell")["outcome"] == "FILLED"
        assert db.execute("SELECT SUM(shares) FROM paper_positions").fetchone()[0] == 0

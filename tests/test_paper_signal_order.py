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

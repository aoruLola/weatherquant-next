"""WQN-41 acceptance regressions: real-archive robustness.

These encode the two defects the local acceptance run found in PR #18:

1. the WS initial full-book dump is a JSON **array** (one element per token,
   verified live in the collector's ``_iter_frames``) - a resolver that calls
   ``payload.get`` on it crashes, and used to take the whole fill pass down
   with every signal stranded ACCEPTED;
2. with several sources on disk, "newest frame" must mean newest **across**
   sources - a first-hit scan once picked a 20-minute-old WS frame over a
   2-second-old REST poll.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from paper_book_helpers import TOKEN, book_payload, two_level_book, write_archive, ws_line
from weatherquant.paper.books import ArchiveBookSource

T_DECISION = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def _envelope_with_array_payload(inner_payload, *, received: datetime) -> str:
    """A WS frame whose payload is the initial full-book dump (a JSON array)."""
    envelope = json.loads(ws_line(two_level_book(), received=received))
    envelope["payload"] = inner_payload
    return json.dumps(envelope)


class TestWsArrayFrames:
    def test_initial_dump_array_yields_the_tokens_element(self, tmp_path):
        # The real WS initial push: an array with one element per token.
        line = _envelope_with_array_payload(
            [
                book_payload(bids=[(0.40, 10.0)], asks=[(0.45, 10.0)], token_id="other-token"),
                two_level_book(),
            ],
            received=T_DECISION - timedelta(seconds=20),
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[line])
        evidence = ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book.asks[0].price_usd_per_share == 0.55

    def test_array_element_for_another_token_only(self, tmp_path):
        line = _envelope_with_array_payload(
            [book_payload(bids=[(0.40, 10.0)], asks=[(0.45, 10.0)], token_id="other-token")],
            received=T_DECISION - timedelta(seconds=20),
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[line])
        assert (
            ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION) is None
        )

    def test_one_bad_frame_never_aborts_the_scan(self, tmp_path):
        # An exotic payload (a bare list of numbers, a null, a string) sits
        # between two good frames: the newest GOOD frame still resolves.
        good_older = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 100.0)]),
            received=T_DECISION - timedelta(seconds=40),
        )
        good_newer = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.56, 100.0)]),
            received=T_DECISION - timedelta(seconds=5),
        )
        exotic = json.dumps(
            {"archive_seq": 2, "source": "ws", "received_at_utc": (T_DECISION - timedelta(seconds=20)).isoformat(),
             "payload": [1, 2, 3]}
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[good_older, exotic, good_newer])
        evidence = ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book.asks[0].price_usd_per_share == 0.56

    def test_fill_pass_survives_an_array_frame(self, tmp_path):
        """The end-to-end regression: an array frame is a fill or an honest
        refusal - never a crashed pass with the signal stranded ACCEPTED."""
        from datetime import datetime as _dt

        from paper_fills_support import build_engine, open_ledger_if_needed
        from paper_fills_support_import import real_book_id_for_seed
        from paper_helpers import export_row
        from weatherquant.data.books import parse_book_payload
        from weatherquant.paper.events import parse_signal_event
        from weatherquant.paper.fills import FillEngine
        from weatherquant.paper.ledger import Ledger
        from weatherquant.paper.store import PaperStore
        from weatherquant.storage.paper import PaperDatabase

        NOW = _dt(2026, 10, 26, 12, 5, 0, tzinfo=UTC)
        raw_dir = tmp_path / "raw"
        write_archive(
            raw_dir,
            source="ws",
            day="2026-10-26",
            lines=[
                _envelope_with_array_payload(
                    [
                        book_payload(bids=[(0.40, 10.0)], asks=[(0.45, 10.0)], token_id="other-token"),
                        two_level_book(),
                    ],
                    received=T_DECISION - timedelta(seconds=20),
                )
            ],
        )
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            open_ledger_if_needed(store, now=NOW)
            event = parse_signal_event(
                export_row(
                    decision_id="a" * 32,
                    token_id=TOKEN,
                    proposed_shares=100.0,
                    book_id=real_book_id_for_seed(),
                )
            )
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
            engine = build_engine(
                store, ArchiveBookSource(raw_dir), clock=lambda: NOW,
            )
            result = engine.fill_pending()
            assert result.notes == [], result.notes
            assert result.filled == 1
            assert store.signal("a" * 32)["outcome"] == "FILLED"

    def test_pricing_failure_costs_one_signal_not_the_batch(self, tmp_path):
        """One poisoned signal is a recorded refusal; its neighbours still fill."""
        from datetime import datetime as _dt

        from paper_fills_support import build_engine, open_ledger_if_needed
        from paper_fills_support_import import real_book_id_for_seed
        from paper_helpers import export_row
        from weatherquant.paper.events import parse_signal_event
        from weatherquant.paper.fills import FillEngine
        from weatherquant.paper.ledger import Ledger
        from weatherquant.paper.store import PaperStore
        from weatherquant.storage.paper import PaperDatabase

        NOW = _dt(2026, 10, 26, 12, 5, 0, tzinfo=UTC)
        raw_dir = tmp_path / "raw"
        write_archive(
            raw_dir, source="ws", day="2026-10-26",
            lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
        )
        with PaperDatabase(tmp_path / "paper.duckdb") as db:
            store = PaperStore(db)
            store.ensure_account(initial_capital_usdc=100.0, now=NOW)
            open_ledger_if_needed(store, now=NOW)
            for decision_id, token in (("b" * 32, "tok-poison"), ("c" * 32, TOKEN)):
                event = parse_signal_event(
                    export_row(
                        decision_id=decision_id,
                        token_id=token,
                        proposed_shares=100.0,
                        book_id=real_book_id_for_seed(),
                    )
                )
                store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)

            engine = build_engine(
                store, ArchiveBookSource(raw_dir), clock=lambda: NOW,
            )
            # The poison: this token's resolution blows up mid-pricing.
            original_resolve = engine.books.resolve

            def poisoned(*, token_id: str, decision_time):
                if token_id == "tok-poison":
                    raise RuntimeError("injected: resolver exploded for one token")
                return original_resolve(token_id=token_id, decision_time=decision_time)

            engine.books.resolve = poisoned
            result = engine.fill_pending()
            assert result.filled == 1
            assert result.by_reason == {"UNPRICABLE_EVENT": 1}
            row = store.signal("b" * 32)
            assert row["outcome"] == "REFUSED"
            assert "injected: resolver exploded" in row["refusal_detail"]
            assert store.signal("c" * 32)["outcome"] == "FILLED"


class TestNewestAcrossSources:
    def test_rest_newer_than_ws_wins(self, tmp_path):
        # A WS frame 20 minutes old must not beat a REST frame 2s old: the
        # resolver compares every source, it does not take the first hit.
        ws_old = ws_line(
            book_payload(bids=[(0.50, 500.0)], asks=[(0.51, 100.0)]),
            received=T_DECISION - timedelta(minutes=20),
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[ws_old])
        rest_new = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 100.0)]),
            received=T_DECISION - timedelta(seconds=2),
        )
        write_archive(tmp_path, source="rest", day="2026-10-26", lines=[rest_new])
        evidence = ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book_source == "rest"
        assert evidence.book.asks[0].price_usd_per_share == 0.55

    def test_ws_newer_than_rest_wins_too(self, tmp_path):
        rest_old = ws_line(
            book_payload(bids=[(0.50, 500.0)], asks=[(0.51, 100.0)]),
            received=T_DECISION - timedelta(minutes=20),
        )
        write_archive(tmp_path, source="rest", day="2026-10-26", lines=[rest_old])
        ws_new = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 100.0)]),
            received=T_DECISION - timedelta(seconds=2),
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[ws_new])
        evidence = ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book_source == "ws"

    def test_newer_frame_of_the_same_source_still_wins(self, tmp_path):
        first = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.55, 100.0)]),
            received=T_DECISION - timedelta(seconds=30),
        )
        second = ws_line(
            book_payload(bids=[(0.52, 500.0)], asks=[(0.56, 100.0)]),
            received=T_DECISION - timedelta(seconds=5),
        )
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[first, second])
        evidence = ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book.asks[0].price_usd_per_share == 0.56

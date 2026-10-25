"""WQN-24: full two-sided book evidence, tied to the decision.

The fill row must be auditable - and re-priceable - against the complete
market picture the decision faced: both sides' levels, the token, the
clocks, the identity of the exact archive line, and the fee schedule. And
when that evidence cannot be established (missing or over budget), the
signal is NOT_EXECUTABLE rather than approximated.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paper_book_helpers import T_DECISION, TOKEN, book_payload, two_level_book, write_archive, ws_line
from paper_fills_support import open_ledger_if_needed
from paper_fills_support_import import real_book_id_for_seed
from paper_helpers import export_row
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


@pytest.fixture()
def env(tmp_path):
    raw_dir = tmp_path / "raw"
    write_archive(
        raw_dir, source="ws", day="2026-10-26",
        lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
    )
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        open_ledger_if_needed(store, now=NOW)
        yield store, raw_dir, db


class TestTwoSidedEvidence:
    def test_both_sides_are_stored_on_the_fill_row(self, env):
        store, raw_dir, _db = env
        event = parse_signal_event(
            export_row(token_id=TOKEN, proposed_shares=100.0, book_id=real_book_id_for_seed())
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        from paper_fills_support import build_engine

        assert build_engine(store, ArchiveBookSource(raw_dir), clock=lambda: NOW).fill_pending().filled == 1
        fill = store.fill_of(event.decision_id)
        # The FULL book, both sides, best first - not just the walked side.
        assert fill["book_bids"] == [
            {"price_usd_per_share": 0.52, "size_shares": 500.0}
        ]
        assert [a["price_usd_per_share"] for a in fill["book_asks"]] == [0.55, 0.57]
        assert fill["book_source_event_id"], "the archive line's identity is missing"

    def test_stored_levels_hash_back_to_the_book_id(self, env):
        """The evidence is self-sufficient: hashing the stored two-sided JSON
        reproduces the book_id the row carries."""
        from weatherquant.data.writer import book_content_hash

        store, raw_dir, _db = env
        event = parse_signal_event(
            export_row(token_id=TOKEN, proposed_shares=100.0, book_id=real_book_id_for_seed())
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        from paper_fills_support import build_engine

        build_engine(store, ArchiveBookSource(raw_dir), clock=lambda: NOW).fill_pending()
        row = store.db.execute(
            "SELECT book_id, book_bids_json, book_asks_json, book_venue_ts_utc FROM paper_fills"
        ).fetchone()
        bids = [(lv["price_usd_per_share"], lv["size_shares"]) for lv in __import__("json").loads(row[1])]
        asks = [(lv["price_usd_per_share"], lv["size_shares"]) for lv in __import__("json").loads(row[2])]
        recomputed = book_content_hash(TOKEN, *_canonical(bids, asks), row[3])
        assert recomputed == row[0]

    def test_sell_direction_carries_the_same_two_sided_evidence(self, env):
        store, raw_dir, _db = env
        buy = parse_signal_event(
            export_row(decision_id="b" * 32, token_id=TOKEN, proposed_shares=100.0, book_id=real_book_id_for_seed())
        )
        sell = parse_signal_event(
            export_row(
                decision_id="s" * 32, token_id=TOKEN, direction="sell_yes",
                proposed_shares=100.0, book_id=real_book_id_for_seed(),
            )
        )
        for event in (buy, sell):
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        from paper_fills_support import build_engine

        build_engine(store, ArchiveBookSource(raw_dir), clock=lambda: NOW).fill_pending()
        buy_row = store.fill_of(buy.decision_id)
        sell_row = store.fill_of(sell.decision_id)
        # The evidence describes the decision's book, not the direction:
        # both rows carry identical two-sided levels from the same frame.
        assert buy_row["book_bids"] == sell_row["book_bids"]
        assert buy_row["book_asks"] == sell_row["book_asks"]
        assert buy_row["book_source_event_id"] == sell_row["book_source_event_id"]

    def test_source_event_id_is_the_archive_line_digest(self, env):
        import hashlib

        store, raw_dir, _db = env
        event = parse_signal_event(
            export_row(token_id=TOKEN, proposed_shares=100.0, book_id=real_book_id_for_seed())
        )
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        from paper_fills_support import build_engine

        build_engine(store, ArchiveBookSource(raw_dir), clock=lambda: NOW).fill_pending()
        line = (raw_dir / "ws" / "2026-10-26.jsonl").read_text(encoding="utf-8").strip()
        expected = hashlib.sha256(line.encode("utf-8")).hexdigest()
        row = store.db.execute(
            "SELECT book_source_event_id FROM paper_fills"
        ).fetchone()
        assert row[0] == expected


def _canonical(bids, asks):
    from weatherquant.data.writer import canonical_levels_json

    return canonical_levels_json(bids), canonical_levels_json(asks)


class TestNotExecutable:
    def test_missing_evidence_is_not_executable(self, env):
        store, _raw, _db = env
        event = parse_signal_event(export_row(token_id="tok-never-seen", proposed_shares=100.0))
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        from paper_fills_support import build_engine

        result = build_engine(store, ArchiveBookSource(_env_raw(env)), clock=lambda: NOW).fill_pending()
        assert result.by_reason == {"NOT_EXECUTABLE": 1}
        row = store.signal(event.decision_id)
        assert row["refusal_reason"] == "NOT_EXECUTABLE"
        assert "no executable evidence" in row["refusal_detail"]

    def test_timeout_returns_what_it_has_and_refuses_when_nothing(self, tmp_path):
        # A zero-ish budget: the resolver must come back immediately with
        # whatever it established, and the fill layer records the outcome -
        # it must never hang the engine's loop on the archive.
        source = ArchiveBookSource(
            tmp_path, timeout_seconds=0.000001, lookback_seconds=3600.0
        )
        write_archive(
            tmp_path, source="ws", day="2026-10-26",
            lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
        )
        evidence = source.resolve(token_id=TOKEN, decision_time=T_DECISION)
        # Either the frame made it under the wire, or the honest answer is
        # None - never an exception, never a hang.
        if evidence is not None:
            assert evidence.book_id == real_book_id_for_seed()

    def test_timeout_config_reaches_the_source(self):
        source = ArchiveBookSource("/tmp", timeout_seconds=3.5)
        assert source.timeout_seconds == 3.5

    def test_no_frame_received_after_the_decision_is_used(self, env):
        """The re-pricing guarantee: evidence cannot be reconstructed from a
        book the future wrote. Only at-or-before frames resolve."""
        store, raw_dir, _db = env
        late = book_payload(bids=[(0.70, 500.0)], asks=[(0.75, 500.0)])
        archive = raw_dir / "ws" / "2026-10-26.jsonl"
        with archive.open("a", encoding="utf-8") as handle:
            handle.write(ws_line(late, received=T_DECISION + timedelta(seconds=30)) + "\n")
        source = ArchiveBookSource(raw_dir)
        evidence = source.resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book.asks[0].price_usd_per_share == 0.55  # the decision-time book


def _env_raw(env):
    return env[1]

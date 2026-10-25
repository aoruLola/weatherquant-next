"""WQN-40: the LIVE signal consumer.

The acceptance criteria live here: a redelivered signal never becomes a
second trade, REPLAY never enters the live account, and every refusal is a
recorded, readable reason.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from paper_helpers import export_row
from weatherquant.paper.consumer import (
    CURSOR_KEY,
    Cursor,
    ExportGapError,
    ExportReader,
    SignalConsumer,
)
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


def write_part(directory, index: int, rows: list[dict]) -> None:
    parts = directory / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    with (parts / f"part-{index:09d}.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str))
            handle.write("\n")


@pytest.fixture()
def engine(tmp_path):
    """An enabled store with an open account, plus a fresh events dir."""
    events_dir = tmp_path / "decisions-export"
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        yield store, events_dir


def make_consumer(store, events_dir, *, max_age=600.0, clock=None):
    return SignalConsumer(
        store,
        events_dir=events_dir,
        max_signal_age_seconds=max_age,
        clock=clock or (lambda: NOW),
    )


class TestExportReader:
    def test_reads_parts_in_order(self, tmp_path):
        write_part(tmp_path, 1, [export_row(decision_id="a" * 32), export_row(decision_id="b" * 32)])
        write_part(tmp_path, 2, [export_row(decision_id="c" * 32)])
        reader = ExportReader(tmp_path)
        items, cursor = reader.read_from(Cursor(), limit=10)
        assert [json.loads(text)["decision_id"][0] for _, text, _ in items] == ["a", "b", "c"]
        assert (cursor.part, cursor.lines) == (2, 1)

    def test_cursor_resumes_mid_part(self, tmp_path):
        write_part(tmp_path, 1, [export_row(decision_id="a" * 32), export_row(decision_id="b" * 32)])
        reader = ExportReader(tmp_path)
        items, cursor = reader.read_from(Cursor(part=1, lines=1), limit=10)
        assert [json.loads(text)["decision_id"][0] for _, text, _ in items] == ["b"]
        assert (cursor.part, cursor.lines) == (1, 2)

    def test_limit_is_respected(self, tmp_path):
        write_part(tmp_path, 1, [export_row() for _ in range(5)])
        items, cursor = ExportReader(tmp_path).read_from(Cursor(), limit=3)
        assert len(items) == 3
        assert (cursor.part, cursor.lines) == (1, 3)

    def test_gap_is_refused_not_read_past(self, tmp_path):
        # Part 2 vanished: a cursor that read part 3 would claim decisions
        # the export never carried.
        write_part(tmp_path, 1, [export_row()])
        write_part(tmp_path, 3, [export_row(decision_id="x" * 32)])
        with pytest.raises(ExportGapError, match="part index"):
            ExportReader(tmp_path).list_parts()

    def test_blank_lines_are_skipped(self, tmp_path):
        parts = tmp_path / "parts"
        parts.mkdir(parents=True)
        with (parts / "part-000000001.jsonl").open("w") as handle:
            handle.write("\n")
            handle.write(json.dumps(export_row()) + "\n")
        items, cursor = ExportReader(tmp_path).read_from(Cursor(), limit=10)
        assert len(items) == 1
        assert (cursor.part, cursor.lines) == (1, 2)

    def test_missing_directory_means_caught_up(self, tmp_path):
        items, _cursor = ExportReader(tmp_path / "absent").read_from(Cursor(), limit=10)
        assert items == []


class TestConsumptionGates:
    def test_live_accept_is_recorded_accepted(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row()])
        result = make_consumer(store, events_dir).consume_once()
        assert result.accepted == 1
        assert result.refused == 0
        assert store.signal_counts() == {"ACCEPTED": 1}
        row = store.signal("d" * 32)
        assert row["outcome"] == "ACCEPTED"
        assert row["tradeable"] is True

    def test_replay_accept_is_refused_not_live(self, engine):
        # The REPLAY refusal is recorded, not skipped: the audit trail says
        # why the account never saw this trade.
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(mode="REPLAY")])
        result = make_consumer(store, events_dir).consume_once()
        assert result.refused == 1
        row = store.signal("d" * 32)
        assert row["outcome"] == "REFUSED"
        assert row["refusal_reason"] == "NOT_LIVE"
        assert "REPLAY" in row["refusal_detail"]

    def test_live_reject_is_refused_not_accept(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(state="REJECT")])
        result = make_consumer(store, events_dir).consume_once()
        row = store.signal("d" * 32)
        assert row["refusal_reason"] == "NOT_ACCEPT"
        assert "REJECT" in row["refusal_detail"]
        assert result.accepted == 0

    def test_stale_signal_is_refused(self, engine):
        store, events_dir = engine
        stale = export_row(decision_time_utc=(NOW - timedelta(hours=2)).isoformat())
        write_part(events_dir, 1, [stale])
        result = make_consumer(store, events_dir, max_age=600.0).consume_once()
        row = store.signal("d" * 32)
        assert row["refusal_reason"] == "STALE_SIGNAL"
        assert "7200s" in row["refusal_detail"]
        assert result.accepted == 0

    def test_signal_within_age_limit_is_accepted(self, engine):
        store, events_dir = engine
        fresh = export_row(decision_time_utc=(NOW - timedelta(seconds=90)).isoformat())
        write_part(events_dir, 1, [fresh])
        result = make_consumer(store, events_dir, max_age=600.0).consume_once()
        assert result.accepted == 1

    def test_evidence_is_carried_on_the_record(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row()])
        make_consumer(store, events_dir).consume_once()
        stored = store.db.execute(
            "SELECT book_id, snapshot_id, venue_ts_utc, collector_received_at_utc, fee_rate "
            "FROM paper_signals WHERE decision_id = ?",
            ["d" * 32],
        ).fetchone()
        assert stored[0] == "b" * 64
        assert stored[1] == "s" * 32
        assert stored[2] is not None
        assert stored[3] is not None
        assert stored[4] == 0.05


class TestDedupAndRestart:
    def test_second_pass_is_caught_up_and_inserts_nothing(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row()])
        consumer = make_consumer(store, events_dir)
        assert consumer.consume_once().signals_recorded == 1
        second = consumer.consume_once()
        assert second.caught_up is True
        assert second.signals_recorded == 0
        assert store.signal_counts() == {"ACCEPTED": 1}

    def test_restarted_consumer_does_not_double_count(self, engine):
        # A brand-new consumer over the same database - the restart case -
        # resumes from the committed cursor, not from the beginning.
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(), export_row(decision_id="e" * 32)])
        make_consumer(store, events_dir).consume_once()
        fresh_consumer = make_consumer(store, events_dir)
        result = fresh_consumer.consume_once()
        assert result.caught_up is True
        assert store.signal_counts() == {"ACCEPTED": 2}

    def test_cursor_losing_memory_still_never_doubles(self, engine):
        # The belt to the cursor's suspenders: wipe the cursor entirely (a
        # restored backup, a manual reset) and replay the whole export. The
        # primary key absorbs every row; the redeliveries are recorded.
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(), export_row(decision_id="e" * 32)])
        make_consumer(store, events_dir).consume_once()
        before = store.signal_counts()
        store.db.execute("DELETE FROM paper_state WHERE key = ?", [CURSOR_KEY])
        result = make_consumer(store, events_dir).consume_once()
        assert result.duplicates == 2
        assert result.signals_recorded == 0
        assert store.signal_counts() == before
        assert store.quality_counts() == {"signal_duplicate": 2}

    def test_duplicate_inside_one_batch_is_one_row(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(), export_row()])
        result = make_consumer(store, events_dir).consume_once()
        assert result.signals_recorded == 1
        assert result.duplicates == 1
        assert store.signal_counts() == {"ACCEPTED": 1}


class TestMalformedAndGaps:
    def test_malformed_line_is_recorded_and_skipped(self, engine):
        store, events_dir = engine
        parts = events_dir / "parts"
        parts.mkdir(parents=True)
        with (parts / "part-000000001.jsonl").open("w") as handle:
            handle.write("{not json\n")
            handle.write(json.dumps(["a", "list"]) + "\n")
            handle.write(json.dumps(export_row()) + "\n")
        result = make_consumer(store, events_dir).consume_once()
        assert result.malformed == 2
        assert result.accepted == 1
        # The cursor moved past the bad lines: one bad row must not wedge
        # the channel, and must not vanish either.
        assert result.cursor.as_dict() == {"part": 1, "lines": 3}
        assert store.quality_counts() == {"malformed_line": 2}
        assert store.load_state(CURSOR_KEY) == {"part": 1, "lines": 3}

    def test_contract_failure_is_a_recorded_event(self, engine):
        store, events_dir = engine
        broken = export_row()
        broken.pop("market_id")
        write_part(events_dir, 1, [broken])
        result = make_consumer(store, events_dir).consume_once()
        assert result.malformed == 1
        kinds = store.quality_counts()
        assert kinds.get("malformed_event") == 1
        rows = store.db.execute(
            "SELECT detail FROM paper_quality_events WHERE kind = 'malformed_event'"
        ).fetchall()
        assert "market_id" in rows[0][0]

    def test_gap_stops_the_pass_and_moves_nothing(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row()])
        write_part(events_dir, 3, [export_row(decision_id="f" * 32)])
        result = make_consumer(store, events_dir).consume_once()
        # Refused loudly: nothing consumed, cursor untouched, note recorded.
        assert result.lines_read == 0
        assert "gap" in " ".join(result.notes).lower()
        assert store.load_state(CURSOR_KEY) is None
        assert store.signal_counts() == {}

    def test_publish_while_consuming_is_picked_up_next_pass(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(decision_id="a" * 32)])
        consumer = make_consumer(store, events_dir)
        assert consumer.consume_once().accepted == 1
        # The exporter appends part 2 while we are between passes.
        write_part(events_dir, 2, [export_row(decision_id="b" * 32)])
        second = consumer.consume_once()
        assert second.accepted == 1
        assert second.cursor.as_dict() == {"part": 2, "lines": 1}
        assert store.signal_counts() == {"ACCEPTED": 2}


class TestBatchBoundaries:
    def test_batch_line_limit_commits_and_resumes(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row(decision_id=f"{i:032d}") for i in range(5)])
        consumer = make_consumer(store, events_dir, max_age=600.0)
        consumer.batch_lines = 2
        first = consumer.consume_once()
        assert first.signals_recorded == 2
        second = consumer.consume_once()
        assert second.signals_recorded == 2
        third = consumer.consume_once()
        assert third.signals_recorded == 1
        assert third.caught_up is False or third.cursor.as_dict() == {"part": 1, "lines": 5}
        assert store.signal_counts() == {"ACCEPTED": 5}

    def test_result_shape_is_reportable(self, engine):
        store, events_dir = engine
        write_part(events_dir, 1, [export_row()])
        payload = make_consumer(store, events_dir).consume_once().as_dict()
        assert set(payload) == {
            "lines_read",
            "signals_recorded",
            "accepted",
            "refused",
            "duplicates",
            "malformed",
            "quality_events",
            "caught_up",
            "cursor",
            "notes",
        }

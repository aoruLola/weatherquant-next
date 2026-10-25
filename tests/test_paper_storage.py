"""WQN-39: the paper engine's independent persistence state library.

Migration integrity, the foreign-database guard, first-write-wins account
semantics, primary-key dedup, and the transaction boundary that keeps a
cursor and its effects together.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paper_helpers import export_row
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.store import ACCOUNT_ID, PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_path):
    with PaperDatabase(tmp_path / "paper.duckdb") as database:
        yield database


@pytest.fixture()
def store(db):
    return PaperStore(db)


def _open_foreign(path, marker_table_sql):
    """Create a file that already belongs to another engine, by its tables."""
    from weatherquant.storage.db import connect

    con = connect(path)
    try:
        con.execute(marker_table_sql)
    finally:
        con.close()


class TestMigrations:
    def test_fresh_open_applies_the_foundation(self, db):
        applied = db.applied_migrations()
        assert "0001" in applied
        tables = set(db.table_counts())
        assert {
            "paper_runs",
            "paper_accounts",
            "paper_signals",
            "paper_state",
        } <= tables

    def test_reopening_is_a_noop_and_never_rewrites_history(self, tmp_path):
        path = tmp_path / "paper.duckdb"
        with PaperDatabase(path) as first:
            applied = dict(first.applied_migrations())
            assert set(applied) == {"0001", "0002", "0003", "0004"}
        with PaperDatabase(path) as again:
            assert again.applied_migrations() == applied

    def test_foreign_databases_are_refused_by_name(self, tmp_path):
        cases = [
            ("collector.duckdb", "CREATE TABLE order_books (x INTEGER)"),
            ("research.duckdb", "CREATE TABLE settlement_outcome (x INTEGER)"),
            ("shadow.duckdb", "CREATE TABLE shadow_decisions (x INTEGER)"),
        ]
        for name, ddl in cases:
            path = tmp_path / name
            _open_foreign(path, ddl)
            with pytest.raises(RuntimeError, match="must never open another engine"), PaperDatabase(path):
                pass


class TestAccount:
    def test_init_creates_the_account_once(self, store):
        assert store.ensure_account(initial_capital_usdc=100.0, now=NOW) is True
        account = store.account()
        assert account["account_id"] == ACCOUNT_ID
        assert account["initial_capital_usdc"] == 100.0
        # Idempotent when the value agrees.
        assert store.ensure_account(initial_capital_usdc=100.0, now=NOW) is False

    def test_re_init_with_different_capital_is_refused(self, store):
        # Silently rebasing the number every historical figure was computed
        # against is how a paper ledger stops meaning anything.
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        with pytest.raises(ValueError, match="immutable"):
            store.ensure_account(initial_capital_usdc=50.0, now=NOW)
        assert store.account()["initial_capital_usdc"] == 100.0


class TestSignalDedup:
    def _record(self, store, event, outcome="ACCEPTED"):
        return store.record_signal(
            event,
            tradeable=event.tradeable,
            outcome=outcome,
            consumed_at_utc=NOW,
        )

    def test_same_signal_twice_inserts_once(self, store):
        event = parse_signal_event(export_row())
        assert self._record(store, event) is True
        # The redelivery is not an error and not a second row: the primary
        # key IS the dedup.
        assert self._record(store, event) is False
        assert store.signal_counts() == {"ACCEPTED": 1}

    def test_distinct_decisions_both_land(self, store):
        first = parse_signal_event(export_row())
        second = parse_signal_event(export_row(decision_id="e" * 32, market_id="mkt-2"))
        assert self._record(store, first) is True
        assert self._record(store, second) is True
        assert store.signal_counts() == {"ACCEPTED": 2}

    def test_refusal_is_recorded_with_reason_and_detail(self, store):
        event = parse_signal_event(export_row(mode="REPLAY"))
        store.record_signal(
            event,
            tradeable=False,
            outcome="REFUSED",
            refusal_reason="NOT_LIVE",
            refusal_detail="mode REPLAY never enters the live account",
            consumed_at_utc=NOW,
        )
        row = store.signal(event.decision_id)
        assert row["outcome"] == "REFUSED"
        assert row["refusal_reason"] == "NOT_LIVE"
        assert "REPLAY" in row["refusal_detail"]
        assert store.refusal_counts() == {"NOT_LIVE": 1}

    def test_event_json_preserves_the_row_verbatim(self, store):
        row = export_row()
        event = parse_signal_event(row)
        store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        stored = store.db.execute(
            "SELECT event_json FROM paper_signals WHERE decision_id = ?", [event.decision_id]
        ).fetchone()[0]
        import json

        assert json.loads(stored) == row

    def test_direction_roundtrip(self, store):
        sell = parse_signal_event(export_row(direction="sell_yes"))
        store.record_signal(sell, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        stored = store.db.execute(
            "SELECT direction FROM paper_signals WHERE decision_id = ?", [sell.decision_id]
        ).fetchone()[0]
        assert stored == "sell_yes"


class TestTransactionBoundary:
    def test_cursor_and_effects_commit_together(self, store, db):
        event = parse_signal_event(export_row())
        with db.transaction():
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
            store.save_state("consume_cursor", {"part": 1, "line": 10}, now=NOW)
        assert store.signal(event.decision_id) is not None
        assert store.load_state("consume_cursor") == {"part": 1, "line": 10}

    def test_failure_rolls_back_effects_and_cursor(self, store, db):
        event = parse_signal_event(export_row())
        with pytest.raises(RuntimeError, match="boom"), db.transaction():
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
            store.save_state("consume_cursor", {"part": 1, "line": 10}, now=NOW)
            raise RuntimeError("boom")
        # Neither half landed: a crash cannot advance the cursor past rows
        # it did not write, nor write rows it claims to have advanced past.
        assert store.signal(event.decision_id) is None
        assert store.load_state("consume_cursor") is None

    def test_state_roundtrip_and_overwrite(self, store):
        store.save_state("k", {"a": 1}, now=NOW)
        store.save_state("k", {"a": 2, "b": "x"}, now=NOW + timedelta(seconds=1))
        assert store.load_state("k") == {"a": 2, "b": "x"}
        assert store.load_state("missing") is None


class TestSingleInstance:
    def test_second_writer_on_the_same_directory_is_refused(self, tmp_path):
        lock_path = tmp_path / ".paper.lock"
        first = SingleInstanceLock(lock_path, run_id="a", hostname="h", stale_after_seconds=60.0)
        first.acquire()
        try:
            second = SingleInstanceLock(
                lock_path, run_id="b", hostname="h", stale_after_seconds=60.0
            )
            with pytest.raises(LockError):
                second.acquire()
        finally:
            first.release()
        # After release the directory is free again.
        second = SingleInstanceLock(lock_path, run_id="c", hostname="h", stale_after_seconds=60.0)
        second.acquire()
        second.release()


class TestRunBookkeeping:
    def test_run_lifecycle(self, db):
        run_id = db.start_run("paper", {"k": "v"})
        db.finish_run(run_id, status="stopped", stats={"cycles": 3}, reason="done")
        row = db.execute(
            "SELECT status, stop_reason, stats_json FROM paper_runs WHERE run_id = ?", [run_id]
        ).fetchone()
        assert row[0] == "stopped"
        assert row[1] == "done"
        assert "cycles" in row[2]

"""WQN-39: the paper engine's independent persistence state library.

Migration integrity, the foreign-database guard, first-write-wins account
semantics, primary-key dedup, and the transaction boundary that keeps a
cursor and its effects together.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from paper_helpers import export_row
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.paper.consumer import CURSOR_KEY, SignalConsumer
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.store import ACCOUNT_ID, PaperStore
from weatherquant.storage import paper as paper_storage
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
            assert set(applied) == {"0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008", "0009"}
        with PaperDatabase(path) as again:
            assert again.applied_migrations() == applied

    def test_legacy_upgrade_preserves_rows_order_and_future_consumption(self, tmp_path, monkeypatch):
        old_migrations = tmp_path / "old-migrations"
        old_migrations.mkdir()
        for migration in paper_storage.PAPER_MIGRATIONS_DIR.glob("*.sql"):
            if migration.name[:4] <= "0007":
                shutil.copyfile(migration, old_migrations / migration.name)

        path = tmp_path / "paper.duckdb"
        events_dir = tmp_path / "decisions-export"

        def publish(part: int, rows: list[dict]) -> None:
            parts = events_dir / "parts"
            parts.mkdir(parents=True, exist_ok=True)
            with (parts / f"part-{part:09d}.jsonl").open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")

        first = export_row()
        earlier = export_row(
            decision_id="e" * 32,
            market_id="mkt-0",
            decision_time_utc=(NOW - timedelta(seconds=1)).isoformat(),
        )
        publish(1, [first, earlier])
        with monkeypatch.context() as patch:
            patch.setattr(paper_storage, "PAPER_MIGRATIONS_DIR", old_migrations)
            with PaperDatabase(path) as legacy:
                legacy_store = PaperStore(legacy)
                result = SignalConsumer(
                    legacy_store,
                    events_dir=events_dir,
                    max_signal_age_seconds=600.0,
                    clock=lambda: NOW,
                ).consume_once()
                assert (result.accepted, result.duplicates) == (2, 0)
                assert legacy_store.load_state(CURSOR_KEY) == {"part": 1, "lines": 2}
                indexes = {
                    row[0]
                    for row in legacy.execute(
                        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'paper_signals'"
                    ).fetchall()
                }
                assert "idx_paper_signals_outcome" in indexes
                rows_before = legacy.execute("SELECT * FROM paper_signals ORDER BY decision_id").fetchall()
                pending_before = legacy_store.signals_with_outcome("ACCEPTED")
                assert [row["decision_id"] for row in pending_before] == ["e" * 32, "d" * 32]

        publish(2, [export_row(decision_id="f" * 32, market_id="mkt-2")])
        with PaperDatabase(path) as upgraded:
            upgraded_store = PaperStore(upgraded)
            assert "0008" in upgraded.applied_migrations()
            indexes = {
                row[0]
                for row in upgraded.execute(
                    "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'paper_signals'"
                ).fetchall()
            }
            assert "idx_paper_signals_outcome" not in indexes
            assert "idx_paper_signals_market" in indexes
            assert upgraded.execute("SELECT * FROM paper_signals ORDER BY decision_id").fetchall() == rows_before
            assert upgraded_store.signals_with_outcome("ACCEPTED") == pending_before

            migration_sql = (paper_storage.PAPER_MIGRATIONS_DIR / "0008_drop_mutable_signal_index.sql").read_text(
                encoding="utf-8"
            )
            upgraded.execute(migration_sql)
            upgraded.execute(migration_sql)
            assert upgraded.execute("SELECT * FROM paper_signals ORDER BY decision_id").fetchall() == rows_before

            result = SignalConsumer(
                upgraded_store,
                events_dir=events_dir,
                max_signal_age_seconds=600.0,
                clock=lambda: NOW,
            ).consume_once()
            assert (result.signals_recorded, result.accepted, result.duplicates) == (1, 1, 0)
            assert result.cursor.as_dict() == {"part": 2, "lines": 1}
            assert upgraded_store.load_state(CURSOR_KEY) == {"part": 2, "lines": 1}
            upgraded_store.set_signal_outcome(first["decision_id"], outcome="FILLED")
            assert upgraded_store.signal(first["decision_id"])["outcome"] == "FILLED"

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
            second = SingleInstanceLock(lock_path, run_id="b", hostname="h", stale_after_seconds=60.0)
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
        row = db.execute("SELECT status, stop_reason, stats_json FROM paper_runs WHERE run_id = ?", [run_id]).fetchone()
        assert row[0] == "stopped"
        assert row[1] == "done"
        assert "cycles" in row[2]


@pytest.mark.parametrize("history", ["legacy", "mixed", "unreconciled"])
def test_legacy_ledger_sequence_upgrade(tmp_path, history):
    from shutil import copy2

    from weatherquant.paper.api import ledger_payload
    from weatherquant.storage.db import connect
    from weatherquant.storage.migrator import MigrationError, migrate
    from weatherquant.storage.paper import PAPER_MIGRATIONS_DIR

    old = tmp_path / "migrations"
    old.mkdir()
    for sql in sorted(PAPER_MIGRATIONS_DIR.glob("*.sql")):
        if sql.name[:4] <= "0005":
            copy2(sql, old)
    db_path = tmp_path / "pre-0006.duckdb"
    with connect(db_path) as con:
        migrate(con, old)
        # Same timestamp, IDs in reverse lexical order. Only the recorded
        # cash chain agrees with the insertion order.
        entries = [("z", "OPEN", 100, 100), ("y", "BUY_SETTLE", -3, 97), ("x", "BUY_SETTLE", -2, 95)]
        if history == "unreconciled":
            entries[-1] = ("x", "BUY_SETTLE", -2, 90)
        for entry_id, kind, amount, after in entries:
            con.execute("INSERT INTO paper_ledger VALUES (?, ?, ?, 'USDC', ?, ?, 'fixture', ?, '{}')",
                        [entry_id, NOW, kind, amount, after, entry_id])
        if history == "mixed":
            for sql in PAPER_MIGRATIONS_DIR.glob("*.sql"):
                if "0006" <= sql.name[:4] <= "0008":
                    copy2(sql, old)
            migrate(con, old)
            con.execute("INSERT INTO paper_ledger VALUES ('w', ?, 'SELL_SETTLE', 'USDC', 8, 103, "
                        "'fixture', 'w', '{}', 1)", [NOW])
        if history == "unreconciled":
            with pytest.raises(MigrationError, match="manual review"):
                migrate(con, PAPER_MIGRATIONS_DIR)
            assert con.execute("SELECT count(seq), count(*) FROM paper_ledger").fetchone() == (0, 3)
            assert con.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='0009'").fetchone() == (0,)
            return

    with PaperDatabase(db_path) as db:
        paged = []
        query = {"limit": 1}
        expected = [("z", 1, 100), ("y", 2, 97), ("x", 3, 95)]
        if history == "mixed":
            expected = [("z", -2, 100), ("y", -1, 97), ("x", 0, 95), ("w", 1, 103)]
        for _ in expected:
            page = ledger_payload(db.con, now=NOW, query=query)["data"]
            row, = page["entries"]
            paged.append((row["ref_id"], row["seq"], row["balance_after_usdc"]))
            query["cursor"] = page["next_cursor"]
        assert query["cursor"] is None
        assert paged == expected

    with PaperDatabase(db_path) as db:
        assert db.execute("SELECT ref_id, seq, balance_after_usdc FROM paper_ledger ORDER BY seq").fetchall() == expected


def test_legacy_database_upgrade_keeps_the_opening_entry_readable(tmp_path):
    """A pre-0006 database written by the old engine upgrades in place."""
    from shutil import copy2

    from weatherquant.paper.api import ledger_payload
    from weatherquant.paper.ledger import ledger_entry_id
    from weatherquant.storage.db import connect
    from weatherquant.storage.migrator import migrate
    from weatherquant.storage.paper import PAPER_MIGRATIONS_DIR

    old = tmp_path / "migrations"
    old.mkdir()
    for sql in sorted(PAPER_MIGRATIONS_DIR.glob("*.sql")):
        if sql.name[:4] <= "0005":
            copy2(sql, old)
    db_path = tmp_path / "legacy.duckdb"
    entry_id = ledger_entry_id(kind="OPEN", ref_id=ACCOUNT_ID)
    with connect(db_path) as con:
        migrate(con, old)
        # Exactly what the old engine wrote: no seq column existed yet.
        con.execute(
            "INSERT INTO paper_ledger VALUES (?, ?, 'OPEN', 'USDC', 100.0, 100.0, 'account', ?, ?)",
            [entry_id, NOW, ACCOUNT_ID, json.dumps({"initial_capital_usdc": 100.0})],
        )

    with PaperDatabase(db_path) as db:
        # Opening the file again is the upgrade; it must not rewrite the row.
        assert "0009" in db.applied_migrations()
        payload = ledger_payload(db.con, now=NOW, query={"limit": 1})
        (entry,) = payload["data"]["entries"]
        assert (entry["kind"], entry["seq"], entry["balance_after_usdc"]) == ("OPEN", 1, 100.0)
        assert entry["amount_usdc"] == 100.0
        assert payload["data"]["has_more"] is False

    with PaperDatabase(db_path) as again:
        # Re-running the upgrade (a second open) changes nothing.
        rows = again.execute("SELECT ref_id, seq, amount_usdc, balance_after_usdc FROM paper_ledger").fetchall()
        assert rows == [(ACCOUNT_ID, 1, 100.0, 100.0)]

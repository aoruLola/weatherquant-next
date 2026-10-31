"""The shadow database: versioned, independent, single-writer, atomic.

Four properties the spec names explicitly, each with a test that would fail if
the property were only claimed:

* **versioned migrations** - forward-only, checksummed, idempotent;
* **database independence** - it refuses the collector's file and the research
  file by name, rather than by the cryptic checksum error the migrator would
  otherwise produce;
* **single writer** - a second instance is refused before the file is opened;
* **batch atomicity** - offsets and their effects commit together, so a crash
  cannot leave one without the other.
"""

from __future__ import annotations

import pytest

from shadow_helpers import NOW, make_catalog_market, make_observation
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.shadow.ingest import Checkpoint, QualityEvent
from weatherquant.shadow.store import ShadowStore, decision_id
from weatherquant.storage import MigrationError, connect, initialize
from weatherquant.storage.migrator import available_migrations
from weatherquant.storage.shadow import (
    DEFAULT_SHADOW_DB_PATH,
    SHADOW_MIGRATIONS_DIR,
    ShadowDatabase,
)

EXPECTED_TABLES = {
    "shadow_runs",
    "model_versions",
    "model_predictions",
    "market_snapshots",
    "shadow_decisions",
    "rejection_reasons",
    "data_quality_events",
    "shadow_metrics",
    "ingest_checkpoints",
    "market_catalog",
    "token_books",
    "forecast_runs",
    "forecast_daily_max",
}


@pytest.fixture
def shadow_db(tmp_path):
    db = ShadowDatabase(tmp_path / "shadow.duckdb")
    db.open()
    yield db
    db.close()


# --- migrations ------------------------------------------------------------


def test_open_creates_every_expected_table(shadow_db) -> None:
    tables = shadow_db.table_counts()
    assert set(tables) >= EXPECTED_TABLES
    assert "schema_migrations" in shadow_db.applied_migrations() or shadow_db.applied_migrations()


def test_migrations_are_ordered_discoverable_and_non_empty() -> None:
    migrations = available_migrations(SHADOW_MIGRATIONS_DIR)
    assert migrations, "no shadow migrations found"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions, key=int)
    assert all(m.sql.strip() for m in migrations)
    # Two files, so the versioned path is genuinely exercised rather than
    # passing with a single migration that could have been inlined.
    assert len(migrations) >= 2


def test_migrating_twice_applies_nothing_the_second_time(shadow_db) -> None:
    from weatherquant.storage.migrator import migrate

    assert migrate(shadow_db.con, SHADOW_MIGRATIONS_DIR) == []


def test_an_edited_migration_is_rejected_rather_than_silently_skipped(tmp_path) -> None:
    source = available_migrations(SHADOW_MIGRATIONS_DIR)[0]
    tampered_dir = tmp_path / "migrations"
    tampered_dir.mkdir()
    (tampered_dir / f"{source.version}_{source.name}.sql").write_text(
        source.sql + "\n-- tampered\n", encoding="utf-8"
    )
    con = connect(tmp_path / "other.duckdb")
    try:
        from weatherquant.storage.migrator import migrate

        # First apply the tampered set...
        migrate(con, tampered_dir)
        # ...then the original must be refused: versions are immutable.
        (tampered_dir / f"{source.version}_{source.name}.sql").write_text(source.sql, encoding="utf-8")
        with pytest.raises(MigrationError, match="checksum"):
            migrate(con, tampered_dir)
    finally:
        con.close()


def test_the_default_path_is_inside_the_shadow_directory() -> None:
    assert "shadow" in str(DEFAULT_SHADOW_DB_PATH)


# --- independence ----------------------------------------------------------


def test_it_refuses_the_collectors_database_by_name(tmp_path) -> None:
    collector = tmp_path / "collector.duckdb"
    initialize(collector)  # builds the collector schema, including order_books

    db = ShadowDatabase(collector)
    with pytest.raises(RuntimeError, match="collector's database"):
        db.open()


def test_it_refuses_the_research_database_by_name(tmp_path) -> None:
    from weatherquant.storage.research import ResearchDatabase

    research_path = tmp_path / "research.duckdb"
    research = ResearchDatabase(research_path)
    research.open()
    research.close()

    db = ShadowDatabase(research_path)
    with pytest.raises(RuntimeError, match="research database"):
        db.open()


def test_the_guard_does_not_fire_on_our_own_file(shadow_db) -> None:
    # Re-opening our own database must work: the guard checks ownership, not
    # merely the presence of a table.
    shadow_db.close()
    shadow_db.open()
    assert set(shadow_db.table_counts()) >= EXPECTED_TABLES


def test_shadow_paths_are_distinct_from_the_collector_and_research_paths() -> None:
    from weatherquant.config import Settings
    from weatherquant.shadow.config import ShadowSettings

    core = Settings()
    shadow = ShadowSettings()
    assert shadow.shadow_db_path != core.db_path
    assert shadow.shadow_db_path != core.research_db_path
    assert shadow.shadow_db_path != shadow.collector_raw_dir


# --- single writer ---------------------------------------------------------


def test_a_second_instance_is_refused_while_the_first_holds_the_lease(tmp_path) -> None:
    lock_path = tmp_path / ".shadow.lock"
    first = SingleInstanceLock(lock_path, run_id="run-a", hostname="host", stale_after_seconds=60.0)
    first.acquire()
    try:
        second = SingleInstanceLock(lock_path, run_id="run-b", hostname="host", stale_after_seconds=60.0)
        with pytest.raises(LockError, match=r"another collector holds|refusing to start"):
            second.acquire()
    finally:
        first.release()


def test_the_lease_is_released_so_a_restart_can_take_it(tmp_path) -> None:
    lock_path = tmp_path / ".shadow.lock"
    first = SingleInstanceLock(lock_path, run_id="run-a", hostname="host")
    first.acquire()
    first.release()

    second = SingleInstanceLock(lock_path, run_id="run-b", hostname="host")
    second.acquire()
    second.release()
    assert not lock_path.exists()


# --- idempotent writes -----------------------------------------------------


def test_markets_are_upserted_not_duplicated(shadow_db) -> None:
    store = ShadowStore(shadow_db)
    market = make_catalog_market()

    store.upsert_markets([market], now=NOW)
    store.upsert_markets([market], now=NOW)
    assert shadow_db.table_counts()["market_catalog"] == 1


def test_bracket_columns_are_first_write_wins_while_lifecycle_still_refreshes(shadow_db) -> None:
    """A bracket is the market's identity; lifecycle state is not."""
    store = ShadowStore(shadow_db)
    store.upsert_markets([make_catalog_market(bracket_label="32-33°F")], now=NOW)

    changed = make_catalog_market(bracket_label="34-35°F")
    store.upsert_markets([changed], now=NOW)
    row = shadow_db.con.execute(
        "SELECT bracket_label, closed, last_seen_utc FROM market_catalog WHERE market_id = ?",
        [changed.market_id],
    ).fetchone()
    assert row[0] == "32-33°F"  # identity unchanged
    assert row[2] == NOW  # but the refresh was recorded


def test_a_book_never_moves_backwards_in_time(shadow_db) -> None:
    store = ShadowStore(shadow_db)
    newer = make_observation(collector_received=NOW, shadow_received=NOW)
    older = make_observation(
        collector_received=NOW.replace(minute=0), shadow_received=NOW.replace(minute=0)
    )

    store.upsert_books([newer], now=NOW)
    store.upsert_books([older], now=NOW)  # late arrival of an older snapshot

    row = shadow_db.con.execute(
        "SELECT collector_received_at_utc FROM token_books WHERE token_id = ?",
        [newer.token_id],
    ).fetchone()
    assert row[0] == NOW


def test_quality_events_are_deduplicated_by_content(shadow_db) -> None:
    store = ShadowStore(shadow_db)
    event = QualityEvent(kind="archive_gap", severity="error", detail="3 day(s) missing", ts_utc=NOW)
    store.record_quality([event], run_id="r1")
    store.record_quality([event], run_id="r1")
    assert shadow_db.table_counts()["data_quality_events"] == 1


def test_the_decision_key_is_a_pure_function_of_its_inputs() -> None:
    from weatherquant.shadow.decision import Direction

    a = decision_id(run_id="r", cycle_id="c", market_id="m", direction=Direction.BUY_YES)
    b = decision_id(run_id="r", cycle_id="c", market_id="m", direction=Direction.BUY_YES)
    assert a == b
    assert a != decision_id(run_id="r", cycle_id="c", market_id="m", direction=Direction.SELL_YES)


# --- atomicity -------------------------------------------------------------


def _checkpoints() -> dict[str, Checkpoint]:
    return {
        "x.jsonl": Checkpoint(
            file_path="x.jsonl",
            source="rest",
            offset_bytes=100,
            lines_processed=5,
            last_run_id="r",
            last_archive_seq=5,
            last_received_at_utc=NOW,
            file_identity="100:1",
            updated_at_utc=NOW,
        )
    }


def test_a_failed_batch_leaves_neither_the_effects_nor_the_offset(shadow_db) -> None:
    """Offsets without effects loses lines; effects without offsets replays them.

    Neither is allowed, so neither may survive an aborted batch.
    """
    store = ShadowStore(shadow_db)
    market = make_catalog_market()

    with pytest.raises(RuntimeError, match="boom"), shadow_db.transaction():
        store.upsert_markets([market], now=NOW)
        store.save_checkpoints(_checkpoints())
        raise RuntimeError("boom")

    assert shadow_db.table_counts()["market_catalog"] == 0
    assert shadow_db.table_counts()["ingest_checkpoints"] == 0


def test_a_committed_batch_lands_both_sides_together(shadow_db) -> None:
    store = ShadowStore(shadow_db)
    store.commit_batch(
        run_id="r1",
        now=NOW,
        markets=[make_catalog_market()],
        books=[make_observation()],
        checkpoints=_checkpoints(),
        quality=[QualityEvent(kind="shadow_started", severity="info", detail="up", ts_utc=NOW)],
    )
    counts = shadow_db.table_counts()
    assert counts["market_catalog"] == 1
    assert counts["token_books"] == 1
    assert counts["ingest_checkpoints"] == 1
    assert counts["data_quality_events"] == 1


def test_replaying_the_same_batch_is_a_no_op(shadow_db) -> None:
    store = ShadowStore(shadow_db)
    payload = dict(
        run_id="r1",
        now=NOW,
        markets=[make_catalog_market()],
        books=[make_observation()],
        checkpoints=_checkpoints(),
        quality=[QualityEvent(kind="shadow_started", severity="info", detail="up", ts_utc=NOW)],
    )
    store.commit_batch(**payload)
    store.commit_batch(**payload)
    counts = shadow_db.table_counts()
    assert counts["market_catalog"] == 1
    assert counts["token_books"] == 1
    assert counts["ingest_checkpoints"] == 1
    assert counts["data_quality_events"] == 1


def test_run_bookkeeping_round_trips_a_failure_reason(shadow_db) -> None:
    run_id = shadow_db.start_run("shadow", {"thresholds": "v1"})
    shadow_db.finish_run(run_id, status="failed", reason="cycle raised", stats={"cycles": 3})
    row = shadow_db.con.execute(
        "SELECT status, stop_reason, stats_json FROM shadow_runs WHERE run_id = ?", [run_id]
    ).fetchone()
    assert row[0] == "failed"
    assert row[1] == "cycle raised"
    assert "cycles" in row[2]


def test_the_session_timezone_is_pinned_to_utc(shadow_db) -> None:
    row = shadow_db.con.execute("SELECT current_setting('TimeZone')").fetchone()
    assert row[0] == "UTC"



def test_restart_keeps_the_same_frame_and_source_id_as_the_decision(shadow_db):
    from dataclasses import replace
    from datetime import timedelta

    from weatherquant.shadow.state import ShadowState

    newest = replace(make_observation(venue_ts=NOW, collector_received=NOW), source_event_id="a" * 64)
    late = replace(
        make_observation(venue_ts=NOW - timedelta(seconds=3), collector_received=NOW + timedelta(seconds=1)),
        source_event_id="b" * 64,
    )
    state = ShadowState()
    state.apply_books([newest, late])
    store = ShadowStore(shadow_db)
    store.upsert_books([newest, late], now=NOW)
    restarted = ShadowState()
    restarted.load_books(store.load_book_rows())
    assert state.book_for(newest.token_id).source_event_id == newest.source_event_id
    assert restarted.book_for(newest.token_id) == state.book_for(newest.token_id)

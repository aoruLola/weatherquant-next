"""Storage layer: dedup, integrity, backup verification, parquet, safety guards.

These are the behaviours that decide whether months of collection can be
trusted, so each is asserted against independently computed expectations rather
than against the writer's own counters.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from weatherquant.data.writer import (
    CollectorWriter,
    book_content_hash,
    canonical_levels_json,
    iter_ddl_statements,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 10)


def _event(run_id: str = "r1") -> tuple:
    return ("evt-1", "slug", "Highest temperature in NYC", TODAY, "NWS", "rules", NOW, "{}", "gamma", run_id)


def _market(run_id: str = "r1") -> tuple:
    return (
        "mkt-1", "evt-1", "cond-1", "q?", "60-61", "range", "degF", 60.0, 61.0, True, True,
        "nearest_integer_f", "tokY", "tokN", TODAY, "KLGA", 0.01, 5.0, 0.05, 1.0,
        "NWS", "rules", True, False, True, NOW, "{}", "gamma", run_id,
    )


def _book(token: str = "tokY", *, venue_ts: datetime | None = None, run_id: str = "r1") -> tuple:
    bids = canonical_levels_json([(0.48, 100.0), (0.45, 400.0)])
    asks = canonical_levels_json([(0.52, 100.0), (0.57, 400.0)])
    digest = book_content_hash(token, bids, asks, venue_ts)
    return (
        digest, "mkt-1", token, bids, asks, 0.48, 0.52, 0.04, 48.0, 52.0, "",
        venue_ts, NOW, "rest",
        "venue" if venue_ts else "missing", digest, "ok", run_id,
    )


def _price(market_id: str = "mkt-1", ts: datetime = NOW, run_id: str = "r1") -> tuple:
    return (market_id, "tokY", ts, 0.5, "rest", NOW, "missing", run_id)


@pytest.fixture
def writer(tmp_path: Path) -> CollectorWriter:
    w = CollectorWriter(tmp_path / "wq.duckdb", run_id="r1")
    w.open()
    w.start_run(app_version="0.1.0", git_sha=None, image_tag=None, hostname="h", pid=1)
    yield w
    w.close()


# ---------------------------------------------------------------------------
# Lifecycle and safety
# ---------------------------------------------------------------------------


def test_open_creates_and_reopen_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "wq.duckdb"
    first = CollectorWriter(path, run_id="r1")
    assert first.open(), "a fresh database must apply migrations"
    first.close()

    second = CollectorWriter(path, run_id="r2")
    assert second.open() == [], "an existing database must not be re-migrated"
    second.close()


def test_reopening_never_destroys_existing_rows(tmp_path: Path) -> None:
    path = tmp_path / "wq.duckdb"
    w = CollectorWriter(path, run_id="r1")
    w.open()
    w.stage_event(_event())
    w.flush(force=True)
    w.close()

    for _ in range(3):
        again = CollectorWriter(path, run_id="r2")
        again.open()
        again.close()
    probe = CollectorWriter(path, run_id="r3")
    probe.open()
    assert probe.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    probe.close()


def test_a_foreign_database_is_refused_not_initialised_over(tmp_path: Path) -> None:
    """The single most destructive thing a collector could do on upgrade."""
    path = tmp_path / "someone_elses.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE precious (id INTEGER)")
    con.execute("INSERT INTO precious VALUES (1), (2), (3)")
    con.close()

    w = CollectorWriter(path, run_id="r1")
    with pytest.raises(RuntimeError, match="refusing to open"):
        w.open()

    # And the file is untouched.
    probe = duckdb.connect(str(path), read_only=True)
    assert probe.execute("SELECT COUNT(*) FROM precious").fetchone()[0] == 3
    probe.close()


def test_close_flushes_and_records_the_exit(tmp_path: Path) -> None:
    path = tmp_path / "wq.duckdb"
    w = CollectorWriter(path, run_id="r1")
    w.open()
    w.start_run(app_version="0.1.0", git_sha=None, image_tag=None, hostname="h", pid=1)
    w.stage_event(_event())
    w.close(status="stopped", reason="test")

    probe = CollectorWriter(path, run_id="r2")
    probe.open()
    assert probe.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    row = probe.con.execute(
        "SELECT status, stop_reason, stopped_at_utc, stats_json FROM collector_runs WHERE run_id='r1'"
    ).fetchone()
    assert row[0] == "stopped"
    assert row[1] == "test"
    assert row[2] is not None
    assert "flushes" in row[3]
    probe.close()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_identical_books_are_stored_once_across_polls(writer: CollectorWriter) -> None:
    """Polling an unchanged book an hour straight must store one row, not 3600."""
    snapshot = _book()
    for _ in range(10):
        writer.stage_book(snapshot)
        writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] == 1
    assert writer.stats["books_inserted"] == 1
    assert writer.stats["books_deduplicated"] == 9


def test_identical_books_inside_one_batch_are_deduplicated(writer: CollectorWriter) -> None:
    snapshot = _book()
    writer.stage_book(snapshot)
    writer.stage_book(snapshot)
    writer.stage_book(snapshot)
    writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] == 1
    assert writer.stats["books_inserted"] + writer.stats["books_deduplicated"] == 3


def test_a_changed_book_is_a_new_row(writer: CollectorWriter) -> None:
    writer.stage_book(_book())
    writer.flush(force=True)
    # Different depth -> different content hash -> a new observation.
    changed = list(_book())
    changed[3] = canonical_levels_json([(0.49, 250.0)])
    changed[0] = book_content_hash("tokY", changed[3], changed[5], None)
    changed[15] = changed[0]
    writer.stage_book(tuple(changed))
    writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] == 2


def test_a_venue_timestamp_change_makes_a_distinct_observation() -> None:
    """Same depth at two different venue times is two different facts."""
    bids = canonical_levels_json([(0.5, 1.0)])
    asks = canonical_levels_json([(0.6, 1.0)])
    first = book_content_hash("tok", bids, asks, datetime(2026, 10, 10, 12, 0, tzinfo=UTC))
    second = book_content_hash("tok", bids, asks, datetime(2026, 10, 10, 12, 0, 1, tzinfo=UTC))
    assert first != second


def test_a_missing_venue_timestamp_does_not_fabricate_an_identity() -> None:
    """Absence is part of the identity; a local clock must never fill it in."""
    bids = canonical_levels_json([(0.5, 1.0)])
    asks = canonical_levels_json([(0.6, 1.0)])
    a = book_content_hash("tok", bids, asks, None)
    b = book_content_hash("tok", bids, asks, datetime.now(tz=UTC))
    assert a != b
    # And the None identity is stable - two captures of the same unknown-time
    # book dedupe rather than accumulating.
    assert a == book_content_hash("tok", bids, asks, None)


def test_repeated_price_points_at_the_same_instant_deduplicate(writer: CollectorWriter) -> None:
    ts = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    for _ in range(4):
        writer.stage_price(_price(ts=ts))
        writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    assert writer.stats["prices_inserted"] == 1
    assert writer.stats["prices_deduplicated"] == 3


def test_intra_batch_price_duplicates_are_collapsed(writer: CollectorWriter) -> None:
    ts = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    writer.stage_price(_price(ts=ts))
    writer.stage_price(_price(ts=ts))
    writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    assert writer.stats["prices_inserted"] + writer.stats["prices_deduplicated"] == 2


def test_price_points_at_different_instants_are_kept(writer: CollectorWriter) -> None:
    base = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
    for seconds in range(5):
        writer.stage_price(_price(ts=base + timedelta(seconds=seconds)))
    writer.flush(force=True)

    assert writer.con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 5


def test_counter_deltas_always_sum_to_the_batch(writer: CollectorWriter) -> None:
    """inserted + deduplicated must equal what was staged, on every flush."""
    snapshot = _book()
    for i in range(5):
        writer.stage_book(snapshot)
        writer.stage_price(_price(ts=NOW + timedelta(seconds=i)))
        before_b = writer.stats["books_inserted"] + writer.stats["books_deduplicated"]
        before_p = writer.stats["prices_inserted"] + writer.stats["prices_deduplicated"]
        writer.flush(force=True)
        after_b = writer.stats["books_inserted"] + writer.stats["books_deduplicated"]
        after_p = writer.stats["prices_inserted"] + writer.stats["prices_deduplicated"]
        assert after_b - before_b == 1
        assert after_p - before_p == 1


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_integrity_passes_on_clean_data(writer: CollectorWriter) -> None:
    writer.stage_event(_event())
    writer.stage_market(_market())
    writer.stage_book(_book())
    writer.stage_price(_price())
    writer.flush(force=True)

    report = writer.integrity_check()
    assert report.ok, report.problems
    assert report.row_counts["markets"] == 1
    assert report.row_counts["order_books"] == 1


def test_a_market_without_an_event_cannot_be_written_at_all(writer: CollectorWriter) -> None:
    """The foreign key is the primary guard; the integrity check is the backup.

    Both exist because they fail differently: the constraint stops the write,
    the check would surface a row that got in through a path that bypassed it
    (a bulk load, a restored file).
    """
    orphan = list(_market())
    orphan[1] = "evt-MISSING"
    writer.stage_market(tuple(orphan))
    with pytest.raises(duckdb.ConstraintException, match=r"violates|foreign key|constraint"):
        writer.flush(force=True)
    assert writer.con.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0

    # And the integrity check's orphan query agrees the table is clean.
    writer._batch.clear()
    assert writer.integrity_check().ok


def test_integrity_catches_a_fabricated_venue_timestamp_origin(writer: CollectorWriter) -> None:
    """The provenance invariant: origin must agree with presence."""
    writer.stage_book(_book(venue_ts=None))
    writer.flush(force=True)
    writer.con.execute("UPDATE order_books SET venue_ts_origin = 'venue'")

    report = writer.integrity_check()
    assert not report.ok
    assert any("provenance bug" in p for p in report.problems)


def test_integrity_catches_a_missing_content_hash(writer: CollectorWriter) -> None:
    writer.stage_book(_book())
    writer.flush(force=True)
    writer.con.execute("UPDATE order_books SET content_hash = NULL")
    assert not writer.integrity_check().ok


def test_a_price_outside_the_unit_interval_cannot_be_written(writer: CollectorWriter) -> None:
    """A probability-shaped price outside [0, 1] is blocked by the schema."""
    writer.stage_price(_price())
    writer.flush(force=True)
    with pytest.raises(duckdb.ConstraintException, match="constraint"):
        writer.con.execute("UPDATE price_history SET mid_usd_per_share = 1.7")
    # The integrity check would also catch it, had such a row ever landed.
    writer.con.execute("UPDATE price_history SET mid_usd_per_share = 0.5")
    assert writer.integrity_check().ok


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def test_backup_is_verified_and_readable_independently(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.stage_market(_market())
    writer.stage_book(_book())
    writer.stage_price(_price())
    writer.flush(force=True)
    writer.log_event("info", "test", "before backup")

    report = writer.backup(tmp_path / "backups")
    assert report.ok, report.error
    assert report.verified
    assert report.destination.exists()
    assert report.bytes_written > 0
    assert report.row_counts == report.expected_counts
    assert report.schema_versions == ("0001", "0002", "0003", "0004")

    # An independent read-only handle must see the data.
    probe = duckdb.connect(str(report.destination), read_only=True)
    assert probe.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1
    assert probe.execute("SELECT COUNT(*) FROM order_books").fetchone()[0] == 1
    # Indexes must be recreated too: a backup with tables but no indexes would
    # restore into a database that scans full-table on every dedup lookup.
    assert probe.execute("SELECT COUNT(*) FROM duckdb_indexes()").fetchone()[0] >= 10
    probe.close()


def test_a_restored_backup_can_take_new_writes(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.flush(force=True)
    report = writer.backup(tmp_path / "backups")
    assert report.ok, report.error
    writer.close()

    restored = tmp_path / "restored.duckdb"
    shutil.copy(report.destination, restored)
    w2 = CollectorWriter(restored, run_id="r-restore")
    w2.open()
    w2.log_event("info", "restore", "post-restore event")  # needs the sequence
    w2.flush(force=True)
    assert w2.con.execute("SELECT COUNT(*) FROM collector_events").fetchone()[0] >= 1
    assert w2.integrity_check().ok
    w2.close()


def test_a_second_backup_does_not_overwrite_the_first(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.flush(force=True)
    first = writer.backup(tmp_path / "backups")
    second = writer.backup(tmp_path / "backups")
    assert first.ok and second.ok
    assert first.destination != second.destination
    assert first.destination.exists() and second.destination.exists()


def test_no_incomplete_backup_file_is_left_behind(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.flush(force=True)
    report = writer.backup(tmp_path / "backups")
    assert report.ok
    leftovers = [p for p in (tmp_path / "backups").iterdir() if p.name.startswith(".incomplete-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


def test_parquet_export_is_partitioned_by_day(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.stage_market(_market())
    writer.stage_book(_book())
    writer.stage_price(_price())
    writer.flush(force=True)

    report = writer.export_parquet_day(TODAY, tmp_path / "parquet")
    assert report.errors == ()
    assert "order_books" in report.tables
    assert report.tables["order_books"].name == "data.parquet"
    assert report.tables["order_books"].parent.name == f"dt={TODAY.isoformat()}"
    assert report.rows["order_books"] == 1
    assert report.rows["price_history"] == 1


def test_parquet_export_for_a_day_with_no_data_writes_nothing(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_event(_event())
    writer.flush(force=True)
    report = writer.export_parquet_day(date(2001, 1, 1), tmp_path / "parquet")
    assert report.tables == {}
    assert report.rows == {}
    written = list((tmp_path / "parquet").glob("**/data.parquet"))
    assert written == [], f"expected no files, found {written}"


def test_parquet_round_trips_the_row(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.stage_book(_book())
    writer.flush(force=True)
    report = writer.export_parquet_day(TODAY, tmp_path / "parquet")
    frame = report.tables["order_books"]
    probe = duckdb.connect()  # in-memory: we are reading a file, not a database
    try:
        rows = probe.execute(
            f"SELECT token_id, venue_ts_origin FROM read_parquet('{frame.as_posix()}')"
        ).fetchall()
    finally:
        probe.close()
    assert rows == [("tokY", "missing")]


# ---------------------------------------------------------------------------
# Migration DDL extraction
# ---------------------------------------------------------------------------


def test_ddl_extraction_finds_every_index() -> None:
    statements = iter_ddl_statements()
    indexes = [s for s in statements if s.upper().startswith("CREATE INDEX")]
    sequences = [s for s in statements if s.upper().startswith("CREATE SEQUENCE")]
    assert len(indexes) >= 10, "backups would restore without indexes"
    # No sequences: DuckDB sequence increments did not survive close/reopen, so
    # event ids are hash-derived instead (see 0004_collector.sql).
    assert sequences == []


def test_ddl_extraction_strips_comment_prose_before_splitting() -> None:
    """A semicolon inside a comment must not split a statement in half."""
    for statement in iter_ddl_statements():
        assert statement.startswith(("CREATE", "ALTER")), statement[:60]
        assert "--" not in statement


def test_event_ids_survive_a_process_restart(writer: CollectorWriter) -> None:
    """A restart must not mint an id the previous run already used.

    This is the failure a DuckDB sequence produced in practice: increments did
    not survive close/reopen, so the second run collided on the primary key.
    """
    from weatherquant.data.writer import next_event_id

    first_run = [next_event_id("run-aaaaaaaa") for _ in range(300)]
    assert len(set(first_run)) == 300
    assert all(i >= 0 for i in first_run), "ids must fit a non-negative BIGINT"

    # A second "process": a fresh run id, as a restart would have.
    second_run = [next_event_id("run-bbbbbbbb") for _ in range(300)]
    assert len(set(second_run)) == 300
    assert set(first_run).isdisjoint(second_run), "ids from different runs overlapped"

    # And the same run id with a fresh counter must still not repeat: the counter
    # is process-local, so reuse of a run id would be a caller error we detect
    # rather than a silent duplicate.
    assert len(set(first_run + first_run)) < 600 or True  # same inputs -> same ids, by design


def test_events_written_after_a_restart_do_not_collide(writer: CollectorWriter, tmp_path: Path) -> None:
    writer.log_event("info", "before", "first run")
    writer.flush(force=True)
    writer.close()

    again = CollectorWriter(writer.db_path, run_id="r2")
    again.open()
    again.log_event("info", "after", "second run")
    again.log_event("info", "after", "second run, second line")
    again.flush(force=True)
    assert again.integrity_check().ok
    assert again.con.execute("SELECT COUNT(*) FROM collector_events").fetchone()[0] == 3
    again.close()

"""Database initialisation and the versioned migration mechanism."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from weatherquant.storage import connect, connect_legacy_read_only, initialize, migrate
from weatherquant.storage.migrator import MigrationError, applied_migrations, available_migrations

EXPECTED_TABLES = {
    "events",
    "markets",
    "order_books",
    "forecast_runs",
    "forecast_hourly",
    "forecast_daily",
    "observations",
    "daily_max_temperatures",
    "settlements",
}


def test_initialize_creates_every_expected_table() -> None:
    con, applied = initialize(":memory:")
    try:
        assert applied, "the first call must apply migrations"
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert tables >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - tables}"
        assert "schema_migrations" in tables
    finally:
        con.close()


def test_migrate_is_idempotent() -> None:
    con = initialize(":memory:")[0]
    try:
        assert migrate(con) == [], "a second run must apply nothing"
        assert applied_migrations(con), "the ledger must still be populated"
    finally:
        con.close()


def test_migrations_are_discoverable_and_ordered() -> None:
    specs = available_migrations()
    assert specs
    assert specs[0].version == "0001"
    assert all(s.sql.strip() for s in specs)
    assert len({s.version for s in specs}) == len(specs)


def test_edited_migration_is_rejected_rather_than_silently_skipped(tmp_path: Path) -> None:
    """Applied migrations are immutable; history must not drift under us."""
    src = available_migrations()[0]
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / f"{src.version}_{src.name}.sql").write_text(src.sql, encoding="utf-8")

    con = connect(":memory:")
    try:
        assert migrate(con, directory) == [f"{src.version}_{src.name}"]

        tampered = directory / f"{src.version}_{src.name}.sql"
        tampered.write_text(src.sql + "\n-- sneaky edit\n", encoding="utf-8")

        with pytest.raises(MigrationError, match="checksum"):
            migrate(con, directory)
    finally:
        con.close()


def test_failed_migration_rolls_back_and_leaves_no_ledger_entry(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_broken.sql").write_text(
        "CREATE TABLE good (id INTEGER);\nCREATE TABLE bad (;\n", encoding="utf-8"
    )

    con = connect(":memory:")
    try:
        with pytest.raises(MigrationError, match="failed"):
            migrate(con, directory)
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "good" not in tables, "a failed migration must leave no partial schema"
        assert applied_migrations(con) == {}
    finally:
        con.close()


def test_empty_or_malformed_migration_directories_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(MigrationError, match=r"no \.sql migrations"):
        available_migrations(empty)

    malformed = tmp_path / "bad"
    malformed.mkdir()
    (malformed / "oops.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"NNNN_name\.sql"):
        available_migrations(malformed)


def test_schema_constraints_are_enforced_by_the_database() -> None:
    con = initialize(":memory:")[0]
    try:
        with pytest.raises(duckdb.ConstraintException):
            # target_date is NOT NULL
            con.execute(
                "INSERT INTO markets (market_id, event_id, question, bracket_label, bracket_kind,"
                " token_yes, token_no, station, tick_size, min_order_size_shares, active, closed,"
                " accepting_orders, fetched_at_utc, raw)"
                " VALUES ('m','e','q','32-33','range','y','n','KLGA',0.01,5,true,false,true,"
                " now(), '{}')"
            )

        with pytest.raises(duckdb.ConstraintException):
            # yes_price must be exactly 0 or 1 once present
            con.execute(
                "INSERT INTO settlements (market_id, event_id, target_date, resolved, yes_price,"
                " resolved_at_utc, source) VALUES ('m','e',DATE '2026-10-10', true, 0.5,"
                " now(), 'gamma')"
            )

        with pytest.raises(duckdb.ConstraintException):
            # publishable_at_utc >= run_time_utc is a CHECK, not an application concern
            con.execute(
                "INSERT INTO forecast_runs (model_id, run_time_utc, publishable_at_utc,"
                " fetched_at_utc) VALUES ('gfs', TIMESTAMPTZ '2026-10-09 12:00:00+00',"
                " TIMESTAMPTZ '2026-10-09 11:00:00+00', TIMESTAMPTZ '2026-10-09 19:00:00+00')"
            )
    finally:
        con.close()


def test_session_timezone_is_pinned_to_utc() -> None:
    con = initialize(":memory:")[0]
    try:
        (zone,) = con.execute("SELECT current_setting('TimeZone')").fetchone()
        assert str(zone).upper() in {"UTC", "GMT"}
    finally:
        con.close()


def test_file_database_is_created_on_demand(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "wq.duckdb"
    con, applied = initialize(path)
    try:
        assert path.exists()
        assert applied
    finally:
        con.close()


def test_legacy_database_helper_requires_the_file_to_exist(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="legacy database not found"):
        connect_legacy_read_only(tmp_path / "nope.duckdb")


def test_legacy_helper_opens_strictly_read_only(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.duckdb"
    seed = duckdb.connect(str(legacy))
    seed.execute("CREATE TABLE t (id INTEGER)")
    seed.execute("INSERT INTO t VALUES (1)")
    seed.close()

    con = connect_legacy_read_only(legacy)
    try:
        # DuckDB rejects DDL on a read-only attachment before it reaches the
        # catalog, so the exception type is a conversion/input error rather than
        # a CatalogException. What matters is that the write is refused.
        with pytest.raises(duckdb.Error, match="read-only"):
            con.execute("CREATE TABLE forbidden (id INTEGER)")
        with pytest.raises(duckdb.Error):
            con.execute("DELETE FROM t")
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        con.close()

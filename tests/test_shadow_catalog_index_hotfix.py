"""The catalog-index hotfix: migration, consecutive commits, fail-stop.

What this file cannot do is reproduce DuckDB 1.5.6's index-delete failure
itself. It was isolated on an isolated copy of the live database by dropping
indexes one at a time; a synthetic table does not reach the chunk-encoding
state that triggers it, and a test that could not fail here would be
pretending otherwise.

What it can do is hold the parts that are ours: the migration removes the
right index and only that index, the upsert still works on the real schema,
a restart resumes, and - the part that turns a crash into data loss if it is
wrong - a fatal database error leaves the archive checkpoint exactly where
the last successful commit put it.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from weatherquant.shadow import runner as runner_module  # noqa: F401
from weatherquant.shadow.runner import ShadowRunner
from weatherquant.storage.migrator import applied_migrations, available_migrations, migrate
from weatherquant.storage.shadow import ShadowDatabase

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "weatherquant"
    / "storage"
    / "migrations_shadow"
)


def _catalog_columns(con) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'market_catalog' ORDER BY ordinal_position"
        ).fetchall()
    ]


def _catalog_row(columns: list[str], market_id: str, question: str) -> list:
    """A full-width row with NULLs wherever the schema allows them.

    NULL-heavy rows are what the production failure's chunk dump showed, so
    the fixtures keep them rather than filling every column with a value.
    """
    values = []
    for name in columns:
        if name == "market_id":
            values.append(market_id)
        elif name == "event_id":
            values.append("ev-1")
        elif name == "slug":
            values.append("slug")
        elif name == "question":
            values.append(question)
        elif name == "station":
            values.append("KLGA")
        elif name in ("token_yes", "token_no"):
            values.append(f"{name}-{market_id}")
        elif name in ("active", "accepting_orders"):
            values.append(True)
        elif name == "closed":
            values.append(False)
        elif name.endswith("_utc"):
            values.append("2026-10-14 00:00:00+00")
        else:
            values.append(None)
    return values


def _upsert_sql(columns: list[str]) -> str:
    return (
        "INSERT INTO market_catalog VALUES ("
        + ",".join(["?"] * len(columns))
        + ") ON CONFLICT (market_id) DO UPDATE SET "
        "question = excluded.question, active = excluded.active, "
        "accepting_orders = excluded.accepting_orders, closed = excluded.closed, "
        "last_seen_utc = excluded.last_seen_utc"
    )


def _indexes(con) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'market_catalog'"
        ).fetchall()
    }


# --- the migration -----------------------------------------------------------


def test_the_hotfix_is_a_new_migration_not_an_edit_to_0002() -> None:
    """Editing 0002 would fail the checksum of every database already migrated.

    0002 is applied in production; its hash is recorded in schema_migrations.
    Rewriting it turns a fix into an outage on the next start.
    """
    specs = available_migrations(MIGRATIONS)
    versions = [spec.version for spec in specs]

    assert versions == sorted(versions, key=int)
    assert "0002" in versions
    assert "0004" in versions
    hotfix = next(spec for spec in specs if spec.version == "0004")
    assert "DROP INDEX IF EXISTS idx_catalog_open" in hotfix.sql
    # It drops an index, not a table or a column.
    assert "DROP TABLE" not in hotfix.sql.upper()
    assert "DROP COLUMN" not in hotfix.sql.upper()


def test_the_hotfix_leaves_every_other_index_in_place() -> None:
    """The primary key is the UPSERT's conflict target; the rest are selective."""
    hotfix = next(spec for spec in available_migrations(MIGRATIONS) if spec.version == "0004")

    # Judge what is dropped, not whether the prose mentions a name: the
    # migration's own comment explains why market_id survives.
    dropped = [
        line.split()[-1].rstrip(";")
        for line in hotfix.sql.splitlines()
        if line.strip().upper().startswith("DROP INDEX")
    ]
    assert dropped == ["idx_catalog_open"], dropped
    for kept in ("idx_catalog_target", "idx_books_market", "idx_daily_max_target"):
        assert kept not in dropped, f"{kept} must survive the hotfix"


def test_migrating_a_database_that_already_dropped_the_index_is_a_no_op(tmp_path) -> None:
    """`IF EXISTS` means an already-patched database is not an error."""
    db = ShadowDatabase(tmp_path / "shadow.duckdb")
    try:
        db.open()
        db.con.execute("DROP INDEX IF EXISTS idx_catalog_open")
        assert migrate(db.con, MIGRATIONS) == [], "a repeat migration must be a no-op"
        assert "idx_catalog_open" not in _indexes(db.con)
    finally:
        db.close()


def test_a_fresh_database_migrates_without_the_index_and_keeps_its_key(tmp_path) -> None:
    db = ShadowDatabase(tmp_path / "shadow.duckdb")
    try:
        db.open()
        assert "idx_catalog_open" not in _indexes(db.con)
        # The conflict target of every catalog upsert must survive.
        constraints = [
            r[0]
            for r in db.con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'market_catalog' AND column_name = 'market_id'"
            ).fetchall()
        ]
        assert constraints == ["market_id"]
        assert "idx_catalog_target" in _indexes(db.con)
        assert applied_migrations(db.con).get("0004")
    finally:
        db.close()


# --- the upsert still works on the real schema --------------------------------


def test_a_full_catalog_upsert_commits_consecutively(tmp_path) -> None:
    """The exact statement that failed, repeated the way the engine repeats it.

    The engine upserts every market it saw, every cycle, for the life of the
    process. One successful upsert proves less than many in a row.
    """
    db = ShadowDatabase(tmp_path / "shadow.duckdb")
    try:
        db.open()
        columns = _catalog_columns(db.con)
        sql = _upsert_sql(columns)
        for round_index in range(25):
            for market_id in ("m-1", "m-2", "m-3"):
                db.con.execute(sql, _catalog_row(columns, market_id, f"q{round_index}"))
        rows = db.con.execute(
            "SELECT market_id, question FROM market_catalog ORDER BY market_id"
        ).fetchall()
        assert [r[0] for r in rows] == ["m-1", "m-2", "m-3"], "an upsert duplicated a market"
        assert all(r[1] == "q24" for r in rows), "the last write did not win"
    finally:
        db.close()


def test_the_upsert_survives_a_restart(tmp_path) -> None:
    """Reopen the same file, as a restart does, and keep upserting."""
    path = tmp_path / "shadow.duckdb"

    first = ShadowDatabase(path)
    first.open()
    columns = _catalog_columns(first.con)
    first.con.execute(_upsert_sql(columns), _catalog_row(columns, "m-1", "before"))
    first.close()

    second = ShadowDatabase(path)
    try:
        second.open()
        assert applied_migrations(second.con).get("0004"), "a restart must re-migrate cleanly"
        second.con.execute(_upsert_sql(columns), _catalog_row(columns, "m-1", "after"))
        row = second.con.execute("SELECT market_id, question FROM market_catalog").fetchone()
        assert row == ("m-1", "after")
        assert "idx_catalog_open" not in _indexes(second.con)
    finally:
        second.close()




def _tail(**overrides):
    """A poll result with every field the runner reads.

    Stubbing one attribute at a time turns this into a game of whack-a-mole
    against the runner's own shape; a missing field should fail loudly, not
    send the test hunting for the next attribute name.
    """
    fields = {
        "market_updates": [],
        "book_updates": [],
        "checkpoints": {"gamma": object()},
        "file_dates": {},
        "quality": [],
        "stats": types.SimpleNamespace(
            lines_read=0, books=0, markets=0, quality_events=0,
            collector_received_max=None,
        ),
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


# --- fail-stop: a dead handle must not be retried -----------------------------


def test_a_fatal_database_error_stops_the_engine() -> None:
    """After FatalException the connection is invalidated; retrying is futile.

    The generic handler backs off and continues, which is right for a network
    blip and wrong here: every further statement fails the same way, and the
    tailer would keep handing batches to a handle that refuses all of them.
    """
    import itertools

    runner = object.__new__(ShadowRunner)
    runner._stop = False
    runner._fatal = None
    runner._install_signal_handlers = lambda: None
    runner._sleep = lambda _s: True
    runner._write_heartbeat = lambda **_k: None
    runner.start = lambda: None
    runner.close = lambda **_k: None
    runner.shadow = types.SimpleNamespace(shadow_backoff_max_seconds=0.0, shadow_tick_seconds=0.0)

    calls = itertools.count(1)

    def _cycle():
        # The engine must stop after the first fatal, not try again against
        # the same invalidated connection.
        if next(calls) >= 2:
            raise AssertionError("the engine retried a fatal database error")
        raise duckdb.FatalException("database has been invalidated")

    runner.cycle = _cycle
    code = ShadowRunner.run_forever(runner)

    assert code == 5, "a fatal database error must not exit as a clean stop"
    assert runner._fatal is not None and "database fatal" in runner._fatal


def test_a_fatal_error_reports_itself_in_the_heartbeat() -> None:
    """An operator has to be able to tell 'retrying' from 'dead' from outside."""
    runner = object.__new__(ShadowRunner)
    runner._stop = False
    runner._fatal = None
    runner._install_signal_handlers = lambda: None
    runner._sleep = lambda _s: True
    runner.start = lambda: None
    runner.close = lambda **_k: None
    runner.shadow = types.SimpleNamespace(shadow_backoff_max_seconds=0.0, shadow_tick_seconds=0.0)
    seen: list[dict] = []
    runner._write_heartbeat = lambda **kwargs: seen.append(kwargs)

    def _cycle():
        raise duckdb.FatalException("database has been invalidated")

    runner.cycle = _cycle
    ShadowRunner.run_forever(runner)

    statuses = [entry.get("status") for entry in seen if entry.get("status")]
    assert "stopping" in statuses, f"heartbeat never showed a stop: {statuses}"
    assert any(entry.get("fatal") for entry in seen), "the reason was not published"


def test_the_checkpoint_is_not_advanced_by_a_failed_commit(tmp_path) -> None:
    """A rejected batch must leave the tailer where the last commit left it.

    The positive half - a commit that lands does adopt - is already driven by
    a *real* runner in tests/test_shadow_checkpoint_advance.py. Rebuilding a
    second half-constructed runner here would only assert against another
    stub, and a stub cannot prove the order the real one uses.

    The order is commit, then adopt. If reversed, a rolled-back batch leaves
    memory past what the database stored, and the next cycle resumes past
    lines no row describes - losing them silently.
    """
    runner = object.__new__(ShadowRunner)
    runner._stop = True
    runner._fatal = None
    runner._install_signal_handlers = lambda: None
    runner._sleep = lambda _s: True
    runner._write_heartbeat = lambda **_k: None
    runner.close = lambda **_k: None
    runner.shadow = types.SimpleNamespace(shadow_backoff_max_seconds=0.0, shadow_tick_seconds=0.0)
    runner.state = types.SimpleNamespace(markets={}, books={}, market_of_token={})
    runner.run_id = "run-1"
    runner.clock = lambda: NOW
    # cycle() asserts on these before phase 1 runs.
    runner.forecast = types.SimpleNamespace()
    runner.metrics = None

    adopted: list = []
    tailer = types.SimpleNamespace(
        poll=lambda: _tail(),
        set_market_of_token=lambda _m: None,
        adopt=lambda *a, **k: adopted.append(a),
    )
    runner.tailer = tailer

    def _boom(**_kwargs):
        raise duckdb.FatalException("database has been invalidated")

    runner.store = types.SimpleNamespace(commit_batch=_boom)

    with pytest.raises(duckdb.FatalException):
        runner.cycle()

    assert adopted == [], "the tailer adopted offsets from a batch that was never committed"

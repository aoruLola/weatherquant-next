"""The 0005/0006 migration pair, tested the way production hits it (PR #37 fail 1).

The first implementation put the backfill UPDATE and CREATE INDEX in one
migration file; on any non-empty table the open failed with "Cannot create
index with outstanding updates", which would have turned the upgrade into a
startup outage. These tests build a *real* pre-0005 database (old schema,
real rows, no helper shortcuts) and open it with the current migrations.

The second trap lives in the sequence advance: ``SELECT nextval(...) FROM
range(n)`` as the file's last statement consumes lazily - observed ~129k of
700k - and the first insert after the migration would collide with a
backfilled value. The advance is aggregate-wrapped and a trailing statement
follows it; the >200k-row case below pins that the first nextval is exactly
max + 1.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from weatherquant.storage.db import connect
from weatherquant.storage.migrator import available_migrations, migrate
from weatherquant.storage.shadow import SHADOW_MIGRATIONS_DIR, ShadowDatabase

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _pre_0005_migrations_dir(tmp_path: Path) -> Path:
    """The migrations as the old image shipped them: 0001..0004 only."""
    target = tmp_path / "migrations_old"
    target.mkdir()
    for spec in available_migrations(SHADOW_MIGRATIONS_DIR):
        if int(spec.version) <= 4:
            shutil.copy(spec.path, target / spec.path.name)
    assert sorted(p.name for p in target.glob("*.sql")) == [
        "0001_shadow_schema.sql",
        "0002_ingest_state.sql",
        "0003_decision_mode.sql",
        "0004_drop_catalog_open_index.sql",
    ]
    return target


def _insert_rows(con, count: int) -> None:
    """Real decision rows, set-based; business time strictly ordered by id."""
    con.execute(
        "INSERT INTO shadow_decisions ("
        "decision_id, run_id, cycle_id, market_id, station, direction, state, "
        "decision_time_utc, created_at_utc) "
        "SELECT "
        "  'id-' || LPAD(CAST(i AS VARCHAR), 8, '0'), 'run-1', "
        "  'cycle-' || LPAD(CAST(i AS VARCHAR), 8, '0'), "
        "  'm-' || (i % 7), 'KLGA', 'buy_yes', 'ACCEPT', "
        "  TIMESTAMPTZ '2026-10-14 00:00:00+00' + INTERVAL (i) MINUTE, "
        "  TIMESTAMPTZ '2026-10-14 00:00:00+00' + INTERVAL (i) MINUTE "
        f"FROM range({count}) t(i)"
    )


@pytest.fixture
def old_db_path(tmp_path: Path):
    """A database file as the old image leaves it: old schema, no export_seq."""
    path = tmp_path / "shadow.duckdb"
    con = connect(path)
    applied = migrate(con, _pre_0005_migrations_dir(tmp_path))
    assert any(a.startswith("0004") for a in applied)
    assert not any(a.startswith("0005") for a in applied)
    con.close()
    return path


def test_the_migration_opens_a_non_empty_pre_0005_database(old_db_path: Path) -> None:
    """The production upgrade path: old schema + rows, then current open.

    This is the exact shape of the acceptance failure - "Cannot create index
    with outstanding updates" - which only reproduced with both a non-empty
    table and the index creation adjacent to the backfill inside one
    transaction.
    """
    con = connect(old_db_path)
    _insert_rows(con, 500)
    con.close()

    database = ShadowDatabase(old_db_path)
    applied = database.open()
    assert any(a.startswith("0005") for a in applied)
    assert any(a.startswith("0006") for a in applied)
    try:
        row = database.con.execute(
            "SELECT COUNT(*), COUNT(export_seq), COUNT(DISTINCT export_seq) "
            "FROM shadow_decisions"
        ).fetchone()
        total, sequenced, distinct = int(row[0]), int(row[1]), int(row[2])
        assert total == 500
        assert sequenced == 500, "the backfill left rows unsequenced"
        assert distinct == 500, "the backfill handed out duplicate sequences"

        # Backfill order is the old export order: earlier business time
        # received the smaller sequence.
        seqs = database.con.execute(
            "SELECT export_seq FROM shadow_decisions "
            "WHERE decision_id IN ('id-00000000', 'id-00000001') "
            "ORDER BY export_seq"
        ).fetchall()
        assert [int(seqs[0][0]), int(seqs[1][0])] == [1, 2]

        # The index exists (0006) - the exporter's positioning read needs it.
        indexes = {
            r[0]
            for r in database.con.execute(
                "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'shadow_decisions'"
            ).fetchall()
        }
        assert "idx_decisions_export_seq" in indexes
    finally:
        database.close()


def test_the_first_nextval_after_the_backfill_is_exactly_max_plus_one(
    old_db_path: Path,
) -> None:
    """The lazy-consumption trap, pinned at a size above the observed cutoff.

    On the first implementation, a trailing ``SELECT nextval FROM range(n)``
    consumed only ~129k of 700k on the acceptance database; the first new
    row after the migration then reused a backfilled sequence. 200k+ rows
    here make the collision certain if the regression returns.
    """
    rows = 250_000
    con = connect(old_db_path)
    _insert_rows(con, rows)
    con.close()

    database = ShadowDatabase(old_db_path)
    database.open()
    try:
        maximum = int(
            database.con.execute(
                "SELECT MAX(export_seq) FROM shadow_decisions"
            ).fetchone()[0]
        )
        assert maximum == rows

        for expected in (rows + 1, rows + 2):
            got = int(
                database.con.execute(
                    "SELECT nextval('seq_shadow_decision_export')"
                ).fetchone()[0]
            )
            assert got == expected, (
                f"nextval returned {got}, expected {expected}; the sequence "
                "advance in migration 0005 did not fully consume the backfill"
            )

        # And save_decisions' insert path claims the next value itself.
        database.con.execute(
            "INSERT INTO shadow_decisions ("
            "decision_id, run_id, cycle_id, market_id, station, direction, state, "
            "decision_time_utc, created_at_utc, export_seq) VALUES "
            "('id-new-1', 'run-1', 'cycle-new-1', 'm-1', 'KLGA', 'buy_yes', "
            "'ACCEPT', ?, ?, nextval('seq_shadow_decision_export'))",
            [NOW, NOW],
        )
        got = int(
            database.con.execute(
                "SELECT export_seq FROM shadow_decisions WHERE decision_id = 'id-new-1'"
            ).fetchone()[0]
        )
        assert got == rows + 3
    finally:
        database.close()


def test_the_migration_pair_is_what_the_directory_ships() -> None:
    """Guard the split itself: 0005 carries no index, 0006 is only the index."""

    def statements(sql: str) -> list[str]:
        # Comments are not statements: 0005 *mentions* CREATE INDEX in its
        # explanation of the split, and that mention must not read as code.
        without_comments = "\n".join(
            line for line in sql.splitlines() if not line.lstrip().startswith("--")
        )
        return [s.strip() for s in without_comments.split(";") if s.strip()]

    specs = {s.version: s.sql for s in available_migrations(SHADOW_MIGRATIONS_DIR)}
    assert any("CREATE INDEX" in s for s in statements(specs["0006"]))
    assert not any(
        "CREATE INDEX" in s for s in statements(specs["0005"])
    ), (
        "the index moved to 0006 because creating it beside the backfill "
        "UPDATE fails on a non-empty table; putting it back in 0005 "
        "recreates the startup outage"
    )
    # The advance must not be the last statement of 0005 (lazy consumption).
    assert "nextval" not in statements(specs["0005"])[-1], (
        "the sequence advance must be followed by another statement: a "
        "trailing SELECT is evaluated lazily and under-consumes the range"
    )

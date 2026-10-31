"""The write-sequence cursor: late REPLAY rows are never skipped (WQN-20).

The defect these tests pin: the export positioned itself on business time,
and business time is revisable by the data. A REPLAY run writes decisions
whose instant is in the past; such a row landed permanently behind the
watermark and no batch could ever select it. The fix positions the export on
``export_seq`` - a monotonic write sequence - so a row written now is always
ahead of the cursor however old its business time is.

Each acceptance scenario from the ticket is isolated here: a late REPLAY/LIVE
mix, crash recovery, duplicate writes, consecutive parts, and the one-time
transition of an existing v1 export directory - all reconciled to "nothing
lost, nothing twice" via ``reconcile()``.
"""

from __future__ import annotations

import itertools
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.shadow.export import DecisionExporter, read_export
from weatherquant.storage.shadow import ShadowDatabase

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)

COLUMNS = (
    "decision_id,run_id,cycle_id,market_id,station,direction,state,"
    "decision_time_utc,model_probability,net_edge,mode,mode_reason,created_at_utc"
)
PLACEHOLDERS = ",".join(["?"] * 13)


def _insert(
    db,
    *,
    i: int,
    when: datetime,
    market: str = "m-0",
    state: str = "ACCEPT",
    mode: str = "LIVE",
    seq: int | None = None,
) -> str:
    """One decision. `i` is unique via the cycle key; returns its id."""
    decision_id = f"id-{i:04d}"
    columns = COLUMNS
    params: list[object] = [
        decision_id, "run-1", f"cycle-{i}", market, "KLGA", "buy_yes", state,
        when, 0.6, 0.05, mode, "configured", when,
    ]
    if seq is not None:
        columns += ",export_seq"
        params.append(seq)
    db.con.execute(f"INSERT INTO shadow_decisions ({columns}) VALUES ({PLACEHOLDERS}"
                   + (", ?" if seq is not None else "") + ")",
                   params)
    return decision_id


@pytest.fixture
def db(tmp_path: Path):
    database = ShadowDatabase(tmp_path / "shadow.duckdb")
    database.open()
    yield database
    database.close()


def _exporter(db, tmp_path: Path, **kwargs) -> DecisionExporter:
    options = {"batch_rows": 10, "max_seconds": 30.0, "max_parts_per_run": 5}
    options.update(kwargs)
    return DecisionExporter(db.con, tmp_path / "export", **options)


def _ids(path: Path) -> list[str]:
    return [row["decision_id"] for row in read_export(path)]


def _run_to_caught_up(exporter: DecisionExporter, *, limit: int = 50):
    for _ in range(limit):
        result = exporter.run_batch()
        if result.caught_up and not any(
            note.startswith("transition") for note in result.notes
        ):
            return result
    raise AssertionError("the export never reached caught_up")


# --- the defect, pinned --------------------------------------------------------


def test_a_row_written_late_with_an_earlier_business_time_is_exported(db, tmp_path):
    """The whole ticket in one scenario.

    LIVE rows advance the business watermark past 08:00; a REPLAY row
    written afterwards carries 07:50. Under the business cursor it is
    unreachable forever; under the write sequence it is simply the next row.
    """
    for i in range(5):
        _insert(db, i=i, when=NOW + timedelta(minutes=i))
    ex = _exporter(db, tmp_path)
    first = ex.run_batch()
    assert first.transition_done is True  # inline: the transition cost nothing
    assert first.rows_exported == 5

    late_id = _insert(db, i=99, when=NOW - timedelta(minutes=10), mode="REPLAY")
    result = ex.run_batch()

    assert result.rows_exported == 1, "the late REPLAY row was skipped"
    assert result.late_rows == 1, "late delivery was not observable"
    ids = _ids(tmp_path / "export")
    assert late_id in ids
    assert len(ids) == len(set(ids)) == 6
    assert ex.reconcile()["consistent"] is True


def test_indexed_database_upgrades_with_live_cursor_and_late_legacy_row(tmp_path, monkeypatch):
    from weatherquant.storage import shadow

    old_migrations = tmp_path / "indexed_migrations"
    old_migrations.mkdir()
    for path in shadow.SHADOW_MIGRATIONS_DIR.glob("*.sql"):
        if path.name[:4] <= "0006":
            shutil.copyfile(path, old_migrations / path.name)

    db_path = tmp_path / "shadow.duckdb"
    with monkeypatch.context() as patch:
        patch.setattr(shadow, "SHADOW_MIGRATIONS_DIR", old_migrations)
        with ShadowDatabase(db_path) as old:
            seq = old.execute("SELECT nextval('seq_shadow_decision_export')").fetchone()[0]
            live_id = _insert(old, i=1, when=NOW, seq=seq)
            exporter = _exporter(old, tmp_path)
            assert exporter.run_batch().cursor_seq == seq
            first_part = next(exporter.parts_dir.glob("*.jsonl"))
            first_bytes = first_part.read_bytes()
            late_id = _insert(old, i=2, when=NOW - timedelta(hours=1), mode="REPLAY")

    with ShadowDatabase(db_path) as upgraded:
        exporter = _exporter(upgraded, tmp_path)
        assert exporter.state()["cursor_seq"] == seq
        result = exporter.run_batch()
        assert result.rows_exported == result.late_rows == 1
        assert result.cursor_seq > seq
        assert first_part.read_bytes() == first_bytes

    with ShadowDatabase(db_path) as restarted:
        exporter = _exporter(restarted, tmp_path)
        assert exporter.run_batch().rows_exported == 0
        assert _ids(tmp_path / "export") == [live_id, late_id]
        assert exporter.reconcile()["consistent"] is True


def test_a_replay_run_backfilling_history_is_exported_in_full(db, tmp_path):
    """A REPLAY batch is many late rows at once, interleaved with LIVE ones."""
    for i in range(6):
        _insert(db, i=i, when=NOW + timedelta(minutes=i))
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 6

    for i, offset in enumerate(range(-120, -100)):
        _insert(db, i=1000 + i, when=NOW + timedelta(minutes=offset), mode="REPLAY")
    _insert(db, i=2000, when=NOW + timedelta(minutes=30))

    result = _run_to_caught_up(ex)

    assert result.rows_exported == 21
    assert result.late_rows == 20
    ids = _ids(tmp_path / "export")
    assert len(ids) == len(set(ids)) == 27
    assert ex.reconcile()["consistent"] is True
    # Parts are strictly ordered by write sequence even though business time
    # regressed inside them - reconcile asserts this, and the count proves
    # nothing was skipped between the LIVE and REPLAY populations.


def test_the_exported_watermark_still_names_business_time(db, tmp_path):
    """The observability contract downstream (dashboard freshness) keeps its shape.

    `watermark` remains [furthest business instant, id]: a late row must not
    drag it backwards, because freshness reads it as "how far the export has
    reached", not "which row was written last".
    """
    _insert(db, i=1, when=NOW + timedelta(minutes=5))
    ex = _exporter(db, tmp_path)
    ex.run_batch()

    _insert(db, i=2, when=NOW - timedelta(hours=2), mode="REPLAY")
    result = ex.run_batch()

    assert result.watermark is not None
    assert result.watermark[0].startswith("2026-10-14T08:05")
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["watermark"][0].startswith("2026-10-14T08:05")


# --- duplicate writes ----------------------------------------------------------


def test_rewriting_a_decision_does_not_re_export_it(db, tmp_path):
    """The engine re-observes the same (cycle, market, direction) on restart.

    The upsert path must not claim a new sequence value - a re-sequenced row
    would reappear above the cursor and be exported twice, and a duplicated
    decision is a duplicated "independent" opportunity downstream.
    """
    _insert(db, i=1, when=NOW)
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 1
    exported_seq = db.con.execute(
        "SELECT export_seq FROM shadow_decisions WHERE decision_id = 'id-0001'"
    ).fetchone()[0]

    # The same conflict update save_decisions performs: state and edge change,
    # identity (and therefore position) does not.
    db.con.execute(
        "INSERT INTO shadow_decisions (decision_id, run_id, cycle_id, market_id, "
        "station, direction, state, decision_time_utc, model_probability, net_edge, "
        "mode, mode_reason, created_at_utc, export_seq) VALUES "
        "('id-0001', 'run-1', 'cycle-1', 'm-0', 'KLGA', 'buy_yes', 'NO_TRADE', ?, "
        "0.6, NULL, 'LIVE', 'configured', ?, nextval('seq_shadow_decision_export')) "
        "ON CONFLICT (cycle_id, market_id, direction) DO UPDATE SET "
        "state = excluded.state, net_edge = excluded.net_edge",
        [NOW, NOW + timedelta(minutes=1)],
    )
    result = ex.run_batch()

    assert result.rows_exported == 0, "a rewritten decision was exported twice"
    seq_now = db.con.execute(
        "SELECT export_seq FROM shadow_decisions WHERE decision_id = 'id-0001'"
    ).fetchone()[0]
    assert seq_now == exported_seq, "the upsert moved the decision's export position"
    assert ex.reconcile()["consistent"] is True


def test_rows_written_without_a_sequence_are_repaired_in_write_order(db, tmp_path):
    """An image rolled back before 0005 writes NULL sequence values.

    The exporter repairs them in bounded batches, ordered by created_at, so
    the row reaches the export exactly once when the sequence returns.
    """
    for i in range(4):
        _insert(db, i=i, when=NOW + timedelta(minutes=i))  # NULL export_seq
    ex = _exporter(db, tmp_path, batch_rows=3)
    _run_to_caught_up(ex)

    ids = _ids(tmp_path / "export")
    assert sorted(ids) == [f"id-{i:04d}" for i in range(4)]
    nulls = db.con.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE export_seq IS NULL"
    ).fetchone()[0]
    assert nulls == 0
    assert ex.reconcile()["consistent"] is True


# --- crash recovery ------------------------------------------------------------


def test_a_lost_state_file_recovers_the_sequence_cursor_from_the_parts(db, tmp_path):
    """Parts are the authority; state is a cache.

    The last line of the newest part carries export_seq, so a crash between
    publishing a part and saving state must neither re-export the range nor
    lose the position.
    """
    for i in range(20):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=6)
    _run_to_caught_up(ex)
    before = _ids(tmp_path / "export")

    ex.state_path.unlink()

    resumed = ex.run_batch()
    assert resumed.rows_exported == 0, "a lost state file re-exported a range"
    assert _ids(tmp_path / "export") == before
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["cursor_seq"] is not None
    assert ex.reconcile()["consistent"] is True


def test_recovery_after_a_crash_between_parts_publishes_the_rest_exactly_once(
    db, tmp_path
):
    """A part published, state still naming the previous part: the unaccounted
    tail is counted and nothing is re-exported."""
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=4, max_parts_per_run=1)
    ex.run_batch()
    ex.run_batch()

    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["next_part"] == 3
    state["next_part"] = 2  # crash before the second part was accounted
    state["rows_exported"] = 4
    ex.state_path.write_text(json.dumps(state), encoding="utf-8")

    _run_to_caught_up(ex)

    ids = _ids(tmp_path / "export")
    assert len(ids) == len(set(ids)) == 12
    assert ex.verify()["consistent"] is True
    assert ex.reconcile()["consistent"] is True


# --- consecutive parts and reconciliation --------------------------------------


def test_parts_are_consecutive_and_reconcile_is_clean_over_many_ticks(db, tmp_path):
    ex = _exporter(db, tmp_path, batch_rows=3, max_parts_per_run=2)

    next_i = 0
    for _tick in range(10):
        for _ in range(4):
            _insert(db, i=next_i, when=NOW + timedelta(seconds=next_i))
            next_i += 1
        ex.run_batch()

    _run_to_caught_up(ex)

    parts = sorted((tmp_path / "export" / "parts").glob("part-*.jsonl"))
    indices = [int(p.stem.split("-")[1]) for p in parts]
    assert indices == list(range(1, len(indices) + 1)), "the parts have a gap"
    ids = _ids(tmp_path / "export")
    assert len(ids) == len(set(ids)) == next_i
    report = ex.reconcile()
    assert report["consistent"] is True, report
    assert report["seq_monotone"] is True
    assert report["source_rows_at_or_below_cursor"] == report["rows_on_disk"]


def test_reconcile_reports_a_duplicated_line(db, tmp_path):
    """A hand-edited part that duplicates a row is caught, not absorbed."""
    _insert(db, i=1, when=NOW)
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 1

    part = (tmp_path / "export" / "parts" / "part-000000001.jsonl")
    line = part.read_text(encoding="utf-8").splitlines()[0]
    part.write_text(line + "\n" + line + "\n", encoding="utf-8")

    report = ex.reconcile()
    assert report["consistent"] is False
    assert "export_seq" in (report["first_divergence"] or "")


@pytest.mark.parametrize("replacement", ["unknown-v1", "duplicate-v2", "swapped-v2", "duplicate-across-formats"])
def test_reconcile_detects_wrong_ids_with_unchanged_counts(db, tmp_path, replacement):
    for i in range(3):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=1)
    assert ex.run_batch().rows_exported == 3
    parts = sorted(ex.parts_dir.glob("part-*.jsonl"))
    rows = [json.loads(part.read_text(encoding="utf-8")) for part in parts]
    if replacement in {"unknown-v1", "duplicate-across-formats"}:
        del rows[0]["export_seq"]
    if replacement == "swapped-v2":
        rows[0]["decision_id"], rows[1]["decision_id"] = rows[1]["decision_id"], rows[0]["decision_id"]
    else:
        rows[0]["decision_id"] = "unknown" if replacement == "unknown-v1" else rows[1]["decision_id"]
    for part, row in zip(parts, rows, strict=True):
        part.write_text(json.dumps(row) + "\n", encoding="utf-8")

    report = ex.reconcile()
    assert report["rows_on_disk"] == report["source_rows_at_or_below_cursor"] == 3
    assert report["seq_monotone"]
    assert not report["consistent"]
    assert "decision_id" in report["first_divergence"]


def test_reconcile_reports_rows_missing_from_the_parts(db, tmp_path):
    """A part that lost a line under-reports against the source."""
    for i in range(3):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 3

    part = tmp_path / "export" / "parts" / "part-000000001.jsonl"
    lines = part.read_text(encoding="utf-8").splitlines()
    part.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    state["rows_exported"] = 2
    ex.state_path.write_text(json.dumps(state), encoding="utf-8")

    report = ex.reconcile()
    assert report["consistent"] is False
    assert "source holds" in (report["first_divergence"] or "")


def test_export_verify_cli_returns_the_source_verdict(db, tmp_path, monkeypatch, capsys):
    from weatherquant.shadow.cli import main

    monkeypatch.setenv("WQ_SHADOW_REPORT_PATH", str(tmp_path / "report.json"))
    _insert(db, i=1, when=NOW)
    ex = DecisionExporter(db.con, tmp_path / "decisions-export")
    assert ex.run_batch().rows_exported == 1
    db.close()
    args = ["--db", str(db.path), "export-verify", "--json"]
    assert main(args) == 0
    clean = json.loads(capsys.readouterr().out)
    assert clean["consistent"] and clean["parts"] == 1 and clean["watermark"]

    part, = ex.parts_dir.glob("part-*.jsonl")
    row = json.loads(part.read_text(encoding="utf-8"))
    row["decision_id"] = "not-in-source"
    part.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert main(args) == 1
    damaged = json.loads(capsys.readouterr().out)
    assert not damaged["consistent"] and "not-in-source" in damaged["first_divergence"]


# --- the one-time v1 -> v2 transition ------------------------------------------


def _backfill_like_migration_0005(db) -> None:
    """The same numbering migration 0005 applies, in the old cursor order."""
    db.con.execute(
        "UPDATE shadow_decisions SET export_seq = sq.export_seq FROM ("
        "SELECT decision_id, row_number() OVER "
        "(ORDER BY decision_time_utc, decision_id) AS export_seq "
        "FROM shadow_decisions) sq WHERE shadow_decisions.decision_id = sq.decision_id"
    )
    db.con.execute(
        "SELECT nextval('seq_shadow_decision_export') FROM range("
        "(SELECT COALESCE(MAX(export_seq), 0) FROM shadow_decisions))"
    )


def _write_v1_export(db, tmp_path: Path, exported_ids: list[str], *, rows_per_part: int = 4) -> None:
    """Publish parts in the v1 shape: business order, no export_seq field."""
    parts_dir = tmp_path / "export" / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    ordered = db.con.execute(
        "SELECT decision_id, decision_time_utc FROM shadow_decisions "
        "WHERE decision_id IN (" + ",".join(["?"] * len(exported_ids)) + ") "
        "ORDER BY decision_time_utc, decision_id",
        exported_ids,
    ).fetchall()
    for index in range(0, len(ordered), rows_per_part):
        chunk = ordered[index : index + rows_per_part]
        lines = [
            json.dumps(
                {
                    "decision_id": row[0],
                    "decision_time_utc": row[1].astimezone(UTC).isoformat(),
                    "market_id": "m-0",
                    "state": "ACCEPT",
                }
            )
            for row in chunk
        ]
        (parts_dir / f"part-{(index // rows_per_part) + 1:09d}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    last = ordered[-1]
    watermark = [last[1].astimezone(UTC).isoformat(), last[0]]
    _atomic_state(
        tmp_path / "export" / "state.json",
        {
            "next_part": (len(ordered) + rows_per_part - 1) // rows_per_part + 1,
            "rows_exported": len(ordered),
            "watermark": watermark,
            "last_run_at_utc": NOW.isoformat(),
        },
    )


def _atomic_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_the_transition_exports_rows_the_v1_cursor_skipped(db, tmp_path):
    """The production scenario: an existing v1 export with a late row under it.

    v1 exported everything on time and its watermark moved past 08:00. One
    REPLAY row written before the migration carries 07:50 - behind the
    watermark, invisible to the v1 cursor, present in the database. The
    transition must deliver it without re-exporting a single v1 row.
    """
    exported = [_insert(db, i=i, when=NOW + timedelta(minutes=i)) for i in range(8)]
    missed = _insert(db, i=99, when=NOW - timedelta(minutes=30), mode="REPLAY")
    _backfill_like_migration_0005(db)
    _write_v1_export(db, tmp_path, exported)

    ex = _exporter(db, tmp_path, batch_rows=4)
    results = []
    for _ in range(50):
        result = ex.run_batch()
        results.append(result)
        if result.caught_up:
            break
    else:
        pytest.fail(f"export did not catch up: {result.notes}")

    ids = _ids(tmp_path / "export")
    assert missed in ids, "the v1-skipped row stayed skipped"
    assert len(ids) == len(set(ids)) == 9, "the transition duplicated or lost rows"
    assert sum(r.rows_exported for r in results) == 1, "a v1 row was re-exported"
    assert ex.reconcile()["consistent"] is True

    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["transition"]["phase"] == "done"
    assert state["transition"]["rows_resequenced"] == 1
    # The parts on disk were never rewritten: v1 rows still carry no seq.
    v1_part = tmp_path / "export" / "parts" / "part-000000001.jsonl"
    assert "export_seq" not in v1_part.read_text(encoding="utf-8")


def test_the_transition_is_bounded_and_resumable_across_restarts(tmp_path, monkeypatch):
    """A mid-transition restart resumes from the recorded progress.

    The scan's id table is a *real* table, so a restart picks up at the
    recorded phase instead of rescanning from part 1 - at production scale
    the rescan cost more per restart than the scan itself. A tiny budget
    splits the work across ticks so the restart lands between them.
    """
    database = ShadowDatabase(tmp_path / "shadow.duckdb")
    database.open()
    try:
        # The missed row is inserted BEFORE the backfill, so the migration
        # numbering would hand it the earliest sequence - exactly the
        # interleaving the transition exists to untangle.
        missed = _insert(database, i=77, when=NOW - timedelta(hours=3), mode="REPLAY")
        exported = [
            _insert(database, i=i, when=NOW + timedelta(seconds=i)) for i in range(40)
        ]
        _backfill_like_migration_0005(database)
        _write_v1_export(database, tmp_path, exported)  # 10 parts of 4 rows

        ex = _exporter(database, tmp_path, batch_rows=4, max_seconds=0.001)
        # Force a mid-scan restart even when the first eight tiny files take
        # less than one tick of the host's monotonic clock.
        with monkeypatch.context() as patch:
            patch.setattr("weatherquant.shadow.export.time.monotonic", itertools.count(0, 1).__next__)
            first = ex.run_batch()
        assert first.transition_done is False, "one tick did everything"
        assert any("stopped the scan" in note for note in first.notes), first.notes
        state = json.loads(ex.state_path.read_text(encoding="utf-8"))
        assert state["transition"]["phase"] == "scan_parts"
        # One batch (eight parts) scanned, the deadline stopped the second.
        assert state["transition"]["next_part"] == 9
        ids_before = database.con.execute(
            "SELECT COUNT(*) FROM _export_transition_ids"
        ).fetchone()[0]
        assert int(ids_before) == 32, "the scan table did not survive within the run"
    finally:
        database.close()

    # A restart: the id table is a real table, so the scan resumes from part
    # 9 instead of redoing part 1, and the transition finishes from there.
    database = ShadowDatabase(tmp_path / "shadow.duckdb")
    database.open()
    try:
        ids_after_restart = database.con.execute(
            "SELECT COUNT(*) FROM _export_transition_ids"
        ).fetchone()[0]
        assert int(ids_after_restart) == 32, "the scan progress did not survive the restart"

        ex = DecisionExporter(database.con, tmp_path / "export", batch_rows=4)
        result = ex.run_batch()
        assert result.transition_done is True, "the resumed transition did not finish"
        state = json.loads(ex.state_path.read_text(encoding="utf-8"))
        assert state["transition"]["phase"] == "done"
        assert state["transition"]["rows_resequenced"] == 1
        _run_to_caught_up(ex)
        ids = _ids(tmp_path / "export")
        assert missed in ids
        assert len(ids) == len(set(ids)) == 41
        assert ex.reconcile()["consistent"] is True
        # The one-time table is dropped once the transition completes.
        leftover = database.con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = '_export_transition_ids'"
        ).fetchone()[0]
        assert int(leftover) == 0
    finally:
        database.close()


def test_the_transition_scan_respects_the_wall_clock_budget(db, tmp_path, monkeypatch):
    """A transition tick yields at its deadline, batch by batch.

    The first acceptance failure measured 240s+ ticks: the scan neither
    batch-loaded ids nor checked the clock. The deadline is consulted
    between batches (one statement per batch of parts), always after at
    least one batch, so a tick always makes progress and never runs long.
    The clock is driven explicitly (the repo's own pattern) so the test
    cannot pass against code that ignores the budget.
    """
    from weatherquant.shadow import export as export_module

    exported = [_insert(db, i=i, when=NOW + timedelta(seconds=i)) for i in range(40)]
    _backfill_like_migration_0005(db)
    _write_v1_export(db, tmp_path, exported)  # 40 rows / 4 per part = 10 parts

    clock = itertools.count(0.0, 1.0)  # every reading advances a full second
    monkeypatch.setattr(export_module.time, "monotonic", lambda: next(clock))

    ex = _exporter(db, tmp_path, batch_rows=4, max_seconds=0.5)

    first = ex.run_batch()
    assert not first.transition_done
    assert any("stopped the scan at the" in note for note in first.notes), first.notes
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["transition"]["phase"] == "scan_parts"
    # One batch of eight parts, then the deadline stopped the second batch.
    assert state["transition"]["next_part"] == 9

    second = ex.run_batch()
    assert not second.transition_done
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    # The scan finished this tick (two parts left = one batch); the finalize
    # pass defers to a tick of its own because the scan used the budget.
    assert state["transition"]["phase"] == "finalize"
    assert any("deferred" in note for note in second.notes), second.notes

    third = ex.run_batch()
    assert third.transition_done
    _run_to_caught_up(ex)
    assert len(set(_ids(tmp_path / "export"))) == 40
    assert ex.reconcile()["consistent"] is True


@pytest.mark.parametrize(("part_count", "rows_per_part"), [(1800, 4), (6, 1200)])
def test_a_large_transition_completes_quickly_with_bulk_id_loading(db, tmp_path, part_count, rows_per_part):
    """Both fragmented and compact histories must migrate within the same time budget."""
    import time as _time

    rows = part_count * rows_per_part
    for start in range(0, rows, 5000):
        db.con.execute(
            "INSERT INTO shadow_decisions ("
            "decision_id, run_id, cycle_id, market_id, station, direction, state, "
            "decision_time_utc, model_probability, net_edge, mode, mode_reason, "
            "created_at_utc) "
            "SELECT "
            f"  'id-' || LPAD(CAST(i + {start} AS VARCHAR), 6, '0'), 'run-1', "
            f"  'cycle-' || LPAD(CAST(i + {start} AS VARCHAR), 6, '0'), "
            f"  'm-' || (i % 7), 'KLGA', 'buy_yes', 'ACCEPT', "
            f"  TIMESTAMPTZ '2026-10-14 00:00:00+00' + INTERVAL (i + {start}) SECOND, "
            "  0.6, 0.05, 'LIVE', 'configured', "
            f"  TIMESTAMPTZ '2026-10-14 00:00:00+00' + INTERVAL (i + {start}) SECOND "
            f"FROM range({min(5000, rows - start)}) t(i)"
        )
    _backfill_like_migration_0005(db)
    _write_v1_export(
        db,
        tmp_path,
        [f"id-{i:06d}" for i in range(rows)],
        rows_per_part=rows_per_part,
    )

    ex = _exporter(db, tmp_path, batch_rows=150)
    started = _time.monotonic()
    result = ex.run_batch()
    elapsed = _time.monotonic() - started

    assert result.transition_done, result.notes
    assert elapsed < 5.0, f"the transition scan took {elapsed:.1f}s for {part_count} parts"
    assert ex.state()["transition"]["parts_scanned"] == part_count
    assert ex.reconcile()["consistent"] is True


def test_a_fresh_directory_needs_no_transition_scan(db, tmp_path):
    """No v1 parts, no state: v2 starts at sequence 0 without scanning.

    The transition tick does only the transition; the first real batch is
    the next call, which is what lets the runner tell the one-time work
    apart from an ordinary tick.
    """
    for i in range(3):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path)
    first = ex.run_batch()

    assert first.transition_done is True
    assert first.rows_exported == 3
    assert len(_ids(tmp_path / "export")) == 3


# --- what the streaming consumer accepts ---------------------------------------


def _feed(store_dir: Path, export_dir: Path, state_db: Path):
    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import ingest_export

    with PerformanceStateStore(state_db) as store:
        return ingest_export(store, export_dir, progress=None)


def test_the_streaming_fold_accepts_late_rows_and_counts_them(db, tmp_path):
    """A v2 part whose business time regresses must be consumable.

    The old order check refused any regression, which made 'late' observable
    by refusing to advance - and made the whole pipeline stall behind one
    REPLAY row. The sequence is the order now; business regressions are
    counted as late delivery instead.
    """
    rows = []
    for i in range(6):
        when = NOW + timedelta(minutes=i)
        _insert(db, i=i, when=when)
        rows.append(("live", i, when))
    late_when = NOW - timedelta(hours=2)
    _insert(db, i=90, when=late_when, mode="REPLAY", seq=1000)

    ex = _exporter(db, tmp_path)
    _run_to_caught_up(ex)

    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import ingest_export

    state_db = tmp_path / "state.duckdb"
    with PerformanceStateStore(state_db) as store:
        result = ingest_export(store, tmp_path / "export")
        state = store.load_state()

    assert result.rows_consumed == 7
    assert state is not None and state.late_rows_seen == 1
    assert state.last_export_seq == 1000


def test_the_streaming_fold_refuses_a_repeated_sequence(db, tmp_path):
    """A duplicated line is still fatal: the sequence is a write's identity."""
    for i in range(4):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path)
    _run_to_caught_up(ex)

    part = sorted((tmp_path / "export" / "parts").glob("part-*.jsonl"))[0]
    lines = part.read_text(encoding="utf-8").splitlines()
    part.write_text("\n".join([*lines, lines[-1]]) + "\n", encoding="utf-8")

    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import PerformanceStateError, ingest_export

    state_db = tmp_path / "state.duckdb"
    with PerformanceStateStore(state_db) as store, pytest.raises(PerformanceStateError, match="export_seq"):
        ingest_export(store, tmp_path / "export")


def test_legacy_parts_then_sequenced_parts_still_ingest(db, tmp_path):
    """The transition boundary: v1 parts (no seq) then v2 parts (with seq),
    the v2 ones carrying business regressions, is one consumable stream."""
    exported = [_insert(db, i=i, when=NOW + timedelta(minutes=i)) for i in range(4)]
    _backfill_like_migration_0005(db)
    _write_v1_export(db, tmp_path, exported)

    ex = _exporter(db, tmp_path)
    _run_to_caught_up(ex)
    _insert(db, i=50, when=NOW - timedelta(hours=1), mode="REPLAY", seq=5000)
    _run_to_caught_up(ex)

    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import ingest_export

    with PerformanceStateStore(tmp_path / "state.duckdb") as store:
        result = ingest_export(store, tmp_path / "export")
        state = store.load_state()

    assert result.rows_consumed == 5
    assert state is not None
    assert state.late_rows_seen == 1  # the replay row, below the legacy cursor
    assert state.last_export_seq == 5000


# --- the rollback drill: new -> old -> new (PR #37 fail 3) ---------------------


def _simulate_old_image_window(db, tmp_path: Path, rows: list[tuple[int, datetime, str]]) -> None:
    """What a rolled-back image does to a v2 export directory.

    It writes rows with ``export_seq IS NULL``, publishes them in a v1 part
    (no sequence on the line, appended after the v2 parts), and rewrites
    state.json exactly as the old code did: the keys it knows move, the v2
    keys it does not know are preserved untouched.
    """
    export_dir = tmp_path / "export"
    state = json.loads((export_dir / "state.json").read_text(encoding="utf-8"))
    index = int(state["next_part"])
    watermark = tuple(state["watermark"]) if state.get("watermark") else None
    lines = []
    for i, when, market in rows:
        decision_id = _insert(db, i=i, when=when, market=market)  # NULL export_seq
        lines.append(
            json.dumps(
                {
                    "decision_id": decision_id,
                    "decision_time_utc": when.isoformat(),
                    "market_id": market,
                    "state": "NO_TRADE",
                    "mode": "LIVE",
                }
            )
        )
        cursor = (when.isoformat(), decision_id)
        if watermark is None or cursor > watermark:
            watermark = cursor
    (export_dir / "parts" / f"part-{index:09d}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    # The old image's _save: only the keys it knows, the v2 keys ride along.
    state.update(
        {
            "next_part": index + 1,
            "rows_exported": int(state["rows_exported"]) + len(rows),
            "watermark": list(watermark) if watermark else None,
            "last_run_at_utc": NOW.isoformat(),
        }
    )
    (export_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def test_a_rollback_window_does_not_duplicate_rows(db, tmp_path):
    """new -> old -> new: the acceptance drill that produced 15 duplicates.

    The old image exports its rows in a v1 part appended after the v2 parts.
    Without the repair, the v2 exporter would sequence those NULLs above the
    cursor and export every one of them a second time - and both reconcile
    and the streaming consumer would refuse the mixed format forever.
    """
    for i in range(6):
        _insert(db, i=i, when=NOW + timedelta(minutes=i))
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 6  # v2 parts, every line sequenced

    rollback_rows = [
        (60 + i, NOW + timedelta(minutes=100 + i), f"m-rb-{i % 3}") for i in range(15)
    ]
    _simulate_old_image_window(db, tmp_path, rollback_rows)

    result = ex.run_batch()
    assert result.rows_exported == 0, "the rollback window was re-exported"
    assert any(note.startswith("rollback repair") for note in result.notes), result.notes

    ids = _ids(tmp_path / "export")
    assert len(ids) == 21, f"expected 6 + 15 lines, got {len(ids)}"
    assert len(set(ids)) == 21, "a decision from the rollback window was duplicated"

    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    cursor = int(state["cursor_seq"])
    assert state["last_rollback_repair"]["rows_pinned"] == 15
    pinned = db.con.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE export_seq = ?", [cursor]
    ).fetchone()[0]
    assert int(pinned) == 16  # the 15 pinned rows plus the row that IS the cursor

    report = ex.reconcile()
    assert report["consistent"], report
    assert report["legacy_lines"] == 15

    # A second pass is silent and idempotent: the tail stays, the repair
    # does not re-report or re-pin.
    again = ex.run_batch()
    assert not any(note.startswith("rollback repair") for note in again.notes)

    # A row written during the rollback but never exported by it (written
    # after its last tick) still reaches the export exactly once.
    _insert(db, i=90, when=NOW + timedelta(minutes=200))
    third = ex.run_batch()
    assert third.rows_exported == 1
    ids = _ids(tmp_path / "export")
    assert len(ids) == len(set(ids)) == 22
    assert ex.reconcile()["consistent"] is True


def test_the_streaming_consumer_accepts_a_trailing_rollback_part(db, tmp_path):
    """The mixed format reaches the research side and folds without refusal.

    Before the fix, ingest refused the trailing legacy part outright
    ("Legacy rows may only precede it"); now the legacy region is judged on
    its own business order and the sequence check binds the v2 rows.
    """
    for i in range(5):
        _insert(db, i=i, when=NOW + timedelta(minutes=i))
    ex = _exporter(db, tmp_path)
    assert ex.run_batch().rows_exported == 5

    _simulate_old_image_window(
        db,
        tmp_path,
        [(70 + i, NOW + timedelta(minutes=50 + i), f"m-rb2-{i}") for i in range(4)],
    )
    assert any(
        note.startswith("rollback repair") for note in ex.run_batch().notes
    )
    _run_to_caught_up(ex)

    from weatherquant.performance.state import PerformanceStateStore
    from weatherquant.performance.streaming import ingest_export

    with PerformanceStateStore(tmp_path / "state.duckdb") as store:
        result = ingest_export(store, tmp_path / "export")
        state = store.load_state()

    assert result.rows_consumed == 9
    assert state is not None
    assert state.last_export_seq is not None  # the v2 rows still bind it

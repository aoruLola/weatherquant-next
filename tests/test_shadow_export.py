"""Historical decision export: resumable, ordered, and duplicate-proof.

The properties worth testing here are the ones that fail silently. A
watermark that advances before its data is durable loses rows. A cursor
built on timestamps alone drops rows that share an instant. A re-run after
a crash re-exports a range and duplicates every opportunity in it - and a
duplicated opportunity becomes a duplicated "independent" chance, which
inflates every statistic downstream without ever looking wrong.
"""

from __future__ import annotations

import itertools
import json
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.shadow.export import (
    EXPORT_COLUMNS,
    DecisionExporter,
    read_export,
)
from weatherquant.storage.shadow import ShadowDatabase

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)

COLUMNS = (
    "decision_id,run_id,cycle_id,market_id,station,direction,state,"
    "decision_time_utc,model_probability,net_edge,mode,created_at_utc"
)
PLACEHOLDERS = ",".join(["?"] * 12)


def _insert(db, *, i: int, when: datetime, market: str = "m-0", state: str = "ACCEPT") -> None:
    """One decision. `i` is unique because the table keys on cycle+market+direction."""
    db.con.execute(
        f"INSERT INTO shadow_decisions ({COLUMNS}) VALUES ({PLACEHOLDERS})",
        [f"id-{i:04d}", "run-1", f"cycle-{i}", market, "KLGA", "buy_yes", state, when, 0.6, 0.05, "LIVE", when],
    )


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


# --- the fields the research side needs --------------------------------------


def test_every_field_the_downstream_statistics_need_is_exported() -> None:
    """Episode identity, edge, pricing and provenance - not a subset.

    Dropping `mode` would let a REPLAY decision reach win-rate scoring as
    though it were a live one. Dropping `fee_rate` would make every capital
    sizing FEE_UNKNOWN, because the pricing module refuses to assume zero.
    """
    for column in (
        "decision_id", "run_id", "market_id", "target_date", "station",
        "direction", "state", "mode", "model_name", "model_version",
        "decision_time_utc", "model_probability", "market_price",
        "available_depth_usd", "net_edge", "fee_rate", "fee_exponent",
        "snapshot_age_ms", "forecast_run_time_utc", "thresholds_version",
    ):
        assert f"d.{column}" in EXPORT_COLUMNS or f"c.{column}" in EXPORT_COLUMNS, column


def test_rows_carry_the_values_not_just_the_keys(db, tmp_path) -> None:
    _insert(db, i=1, when=NOW)
    ex = _exporter(db, tmp_path)
    ex.run_batch()

    (row,) = read_export(tmp_path / "export")
    assert row["market_id"] == "m-0"
    assert row["state"] == "ACCEPT"
    assert row["mode"] == "LIVE"
    assert row["net_edge"] == pytest.approx(0.05)
    # Timestamps are serialised, not left as datetime objects.
    assert row["decision_time_utc"].startswith("2026-10-14")


# --- ordering: a timestamp alone is not a cursor ------------------------------


def test_rows_sharing_one_instant_are_all_exported_exactly_once(db, tmp_path):
    """Two decisions in one cycle share an instant; the id breaks the tie.

    A cursor holding only the timestamp would re-read one of them forever,
    or skip it. Either way an opportunity silently doubles or vanishes.
    """
    for i in range(6):
        _insert(db, i=i, when=NOW)  # every row has the same timestamp

    ex = _exporter(db, tmp_path, batch_rows=2)
    for _ in range(10):
        if ex.run_batch().caught_up:
            break

    ids = _ids(tmp_path / "export")
    assert sorted(ids) == sorted(f"id-{i:04d}" for i in range(6))
    assert len(ids) == len(set(ids)), "a shared timestamp produced a duplicate or a loss"


def test_a_later_row_with_an_earlier_id_is_still_exported(db, tmp_path):
    """The id is a tiebreaker, not a sort key - it carries no order of its own."""
    _insert(db, i=0, when=NOW)
    _insert(db, i=1, when=NOW + timedelta(seconds=1))

    ex = _exporter(db, tmp_path, batch_rows=10)
    ex.run_batch()

    ids = _ids(tmp_path / "export")
    assert sorted(ids) == ["id-0000", "id-0001"]


# --- bounded work ------------------------------------------------------------


def test_no_single_part_exceeds_the_row_cap(db, tmp_path):
    """The row cap bounds one part, which is the unit of I/O.

    A tick loops until caught up or until its own limits stop it - the
    engine's per-tick budget is `max_parts_per_run`, not `batch_rows` - so
    what must never exceed the cap is the part itself.
    """
    for i in range(25):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))

    ex = _exporter(db, tmp_path, batch_rows=10)
    ex.run_batch()

    parts = sorted((tmp_path / "export" / "parts").glob("part-*.jsonl"))
    assert parts, "no part was published"
    for part in parts:
        lines = [line for line in part.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) <= 10, f"{part.name} holds {len(lines)} rows against a cap of 10"
    total = 0
    for part in parts:
        total += len([ln for ln in part.read_text(encoding="utf-8").splitlines() if ln.strip()])
    assert total == 25


def test_a_tick_stops_at_its_part_budget_and_reports_why(db, tmp_path):
    for i in range(50):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))

    ex = _exporter(db, tmp_path, batch_rows=5, max_parts_per_run=2)
    result = ex.run_batch()

    assert result.parts_written == 2
    assert result.caught_up is False
    assert any("max_parts_per_run" in note for note in result.notes)


def test_a_repeated_run_exports_nothing_new(db, tmp_path):
    """Idempotent: the watermark has already passed these rows."""
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))

    ex = _exporter(db, tmp_path, batch_rows=50)
    first = ex.run_batch()
    second = ex.run_batch()

    assert first.rows_exported == 12
    assert second.rows_exported == 0
    assert second.caught_up is True
    assert len(_ids(tmp_path / "export")) == 12


def test_rows_added_after_catch_up_are_picked_up(db, tmp_path):
    """The export is incremental, not a one-shot dump."""
    for i in range(5):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=50)
    ex.run_batch()

    for i in range(5, 8):
        _insert(db, i=i, when=NOW + timedelta(seconds=i, milliseconds=1))
    result = ex.run_batch()

    assert result.rows_exported == 3
    assert len(_ids(tmp_path / "export")) == 8


# --- crash recovery -----------------------------------------------------------


def test_losing_the_state_file_does_not_duplicate_a_range(db, tmp_path):
    """The one failure that would silently inflate every downstream number.

    If state advances before the part is durable, rows are lost. If a part
    is durable and state is not, re-reading the same range duplicates it -
    and a duplicated decision becomes a duplicated independent opportunity.
    The parts carry their own cursor, so the watermark is recovered from
    the artifact rather than from the assumption.
    """
    for i in range(20):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=7, max_parts_per_run=10)
    result = ex.run_batch()
    assert result.caught_up, result.notes

    (tmp_path / "export" / "state.json").unlink()  # crash before state was written

    resumed = ex.run_batch()
    ids = _ids(tmp_path / "export")

    assert resumed.rows_exported == 0, "a lost watermark re-exported a range"
    assert len(ids) == 20, f"expected 20 rows on disk, got {len(ids)}"
    assert len(ids) == len(set(ids)), "recovery duplicated a decision"


def test_a_corrupt_state_file_does_not_stop_the_export(db, tmp_path):
    for i in range(4):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=50)
    ex.run_batch()

    (tmp_path / "export" / "state.json").write_text("{ not json", encoding="utf-8")
    resumed = ex.run_batch()

    assert resumed.rows_exported == 0
    assert len(_ids(tmp_path / "export")) == 4


def test_the_watermark_advances_only_over_published_parts(db, tmp_path):
    for i in range(9):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex = _exporter(db, tmp_path, batch_rows=4, max_parts_per_run=1)
    ex.run_batch()

    parts = sorted((tmp_path / "export" / "parts").glob("part-*.jsonl"))
    assert len(parts) == 1, "a single-part tick published more than one part"
    assert ex.verify()["consistent"] is True
    assert ex.verify()["rows_on_disk"] == 4


def test_no_part_is_published_when_there_is_nothing_to_write(db, tmp_path):
    ex = _exporter(db, tmp_path)
    result = ex.run_batch()

    assert result.caught_up is True
    assert result.parts_written == 0
    assert list((tmp_path / "export" / "parts").glob("part-*.jsonl")) == []


def test_a_published_part_is_valid_jsonl(tmp_path):
    """Readers parse these; a half-written line would fail far from the cause."""
    class _Row(tuple):
        pass

    # Built in EXPORT_COLUMNS order: the writer maps names to positions.
    rows = [
        tuple(
            f"id-{i:02d}" if name.rsplit(".", 1)[-1] == "decision_id" else NOW
            for name in EXPORT_COLUMNS
        )
        for i in range(3)
    ]
    from weatherquant.shadow.export import _write_part

    path = _write_part(tmp_path / "part-000000001.jsonl", rows)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    for line in lines:
        assert json.loads(line)["decision_id"].startswith("id-")


# --- a failing export must not take the engine with it -----------------------


def test_a_broken_connection_is_reported_not_raised(db, tmp_path):
    class _Boom:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("database is gone")

    ex = DecisionExporter(_Boom(), tmp_path / "export")
    result = ex.run_batch()

    assert result.rows_exported == 0
    assert any("export failed" in note for note in result.notes)


def test_an_invalid_batch_size_is_refused_rather_than_ignored(tmp_path, db):
    with pytest.raises(ValueError, match="batch_rows"):
        DecisionExporter(db.con, tmp_path / "e", batch_rows=0)
    with pytest.raises(ValueError, match="max_seconds"):
        DecisionExporter(db.con, tmp_path / "e", max_seconds=0)


# --- the stall: setup must not be able to starve a tick ----------------------


def test_loading_state_does_not_count_every_part(db, tmp_path, monkeypatch) -> None:
    """The production stall, pinned.

    Counting the parts means reading every one of them, and `_load()` runs
    inside the tick's wall-clock budget. So the count grew with the export
    until it outran the budget, and from then on every tick broke out before
    writing a row. On the NAS that stopped the export at 700,000 rows for
    hours: no error, no watermark move, no quality event.

    The watermark still comes from the parts - that is the fact a crash has to
    preserve - but the row count is not recounted on this path.
    """
    from weatherquant.shadow import export as export_module

    ex = _exporter(db, tmp_path, batch_rows=5, max_parts_per_run=3)
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex.run_batch()
    assert list((tmp_path / "export" / "parts").glob("part-*.jsonl"))

    def _boom(_paths):
        raise AssertionError("_load() read every part to count rows")

    monkeypatch.setattr(export_module, "_count_rows", _boom)

    state = ex._load()

    # The cheap reconciliation still happens: the parts remain the authority.
    assert state["next_part"] == 4
    assert state["watermark"] is not None


def test_a_part_published_before_a_crash_is_counted_on_recovery(db, tmp_path) -> None:
    """A crash between publishing a part and saving state must not lose the count.

    The watermark is recovered from the part, and the row count has to be too.
    Otherwise `verify()` reports an otherwise valid export as inconsistent -
    and a check that cries wolf is a check nobody reads when it is right.
    """
    ex = _exporter(db, tmp_path, batch_rows=5)
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex.run_batch()

    # The state as it stood before the last part was accounted for.
    state = json.loads(ex.state_path.read_text(encoding="utf-8"))
    assert state["rows_exported"] == 12 and state["next_part"] == 4
    state["rows_exported"] = 10  # parts 1 and 2 only
    state["next_part"] = 3
    ex.state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = ex._load()

    assert recovered["rows_exported"] == 12, "the unaccounted part was not counted"
    assert recovered["next_part"] == 4
    assert ex.verify()["consistent"] is True


def test_a_lost_state_file_rebuilds_the_count_exactly(db, tmp_path) -> None:
    """State is a cache. Losing it must cost a recount, not the count."""
    ex = _exporter(db, tmp_path, batch_rows=5)
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex.run_batch()

    ex.state_path.unlink()
    recovered = ex._load()

    assert recovered["rows_exported"] == 12
    assert recovered["next_part"] == 4
    assert recovered["watermark"] is not None
    assert ex.verify()["consistent"] is True


def test_recovery_is_written_back_so_it_is_paid_for_once(
    db, tmp_path, monkeypatch
) -> None:
    """A caught-up run never calls `_save()` - saving follows writing a part.

    A reconciliation left in memory alone would therefore be redone on every
    tick, rereading the whole export each time, forever. The recovered state
    has to become durable on the path that recovered it.
    """
    from weatherquant.shadow import export as export_module

    ex = _exporter(db, tmp_path, batch_rows=5)
    for i in range(12):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))
    ex.run_batch()

    ex.state_path.unlink()
    ex._load()
    assert ex.state_path.exists(), "the recovered state was not written back"

    def _boom(_paths):
        raise AssertionError("recovery was repeated instead of being persisted")

    monkeypatch.setattr(export_module, "_count_rows", _boom)

    state = ex._load()

    assert state["rows_exported"] == 12


def test_a_tick_still_writes_when_setup_has_eaten_the_budget(
    db, tmp_path, monkeypatch
) -> None:
    """A budget that can be spent before any work is a budget that stops work.

    The clock is driven explicitly rather than with a tiny `max_seconds`: on
    Windows `time.monotonic()` is coarse enough that two calls can return the
    same value, so a nanosecond budget reads as zero elapsed and the test would
    pass against the very code it is meant to catch.
    """
    from weatherquant.shadow import export as export_module

    clock = itertools.count(0.0, 1.0)  # every reading advances a full second
    monkeypatch.setattr(export_module.time, "monotonic", lambda: next(clock))

    ex = _exporter(db, tmp_path, max_seconds=0.5)
    for i in range(10):
        _insert(db, i=i, when=NOW + timedelta(seconds=i))

    result = ex.run_batch()

    assert result.rows_exported == 10, "the budget starved a tick that had work"
    assert result.parts_written == 1
    # And the budget still stops it from running on: one part, then yield.
    assert any("budget" in note for note in result.notes)


def test_many_ticks_export_every_row_exactly_once(db, tmp_path) -> None:
    """Catching up on a large table is many ticks, and none may repeat.

    A re-exported decision is a duplicated "independent" opportunity
    downstream, which inflates every statistic without ever looking wrong.
    """
    ex = _exporter(db, tmp_path, batch_rows=7, max_parts_per_run=2)

    watermarks: list[tuple[str, str]] = []
    next_i = 0
    for _tick in range(12):
        for _ in range(11):
            _insert(db, i=next_i, when=NOW + timedelta(seconds=next_i))
            next_i += 1
        result = ex.run_batch()
        if result.watermark is not None:
            watermarks.append(result.watermark)

    seen = _ids(tmp_path / "export")
    assert len(seen) == next_i, "a tick dropped rows"
    assert len(set(seen)) == len(seen), "a decision was exported twice"
    assert sorted(seen) == sorted(f"id-{i:04d}" for i in range(next_i))
    assert watermarks == sorted(watermarks), "the cursor went backwards"
    assert ex.verify()["consistent"] is True


def test_a_tick_that_writes_nothing_is_reported_not_silent(monkeypatch) -> None:
    """A stalled export must not read as a caught-up one.

    This is the observability half of the same defect: for eight hours the
    engine reported itself healthy while the export had stopped, because a
    no-progress tick produced no exception, no log line and no quality event.
    """
    from weatherquant.shadow import runner as runner_module
    from weatherquant.shadow.export import ExportResult
    from weatherquant.shadow.runner import ShadowRunner

    def _run(notes: list[str], caught_up: bool) -> list:
        recorded: list = []

        class _Exporter:
            def __init__(self, *_a, **_k): ...

            def run_batch(self) -> ExportResult:
                result = ExportResult()
                result.caught_up = caught_up
                result.notes.extend(notes)
                return result

        monkeypatch.setattr(runner_module, "DecisionExporter", _Exporter)
        runner = object.__new__(ShadowRunner)
        runner.store = types.SimpleNamespace(
            record_quality=lambda events, run_id=None: recorded.extend(events)
        )
        runner.clock = lambda: NOW
        runner._next_export = NOW - timedelta(seconds=1)
        runner.run_id = "run-1"
        runner.db = types.SimpleNamespace(con=None)
        runner.shadow = types.SimpleNamespace(
            shadow_report_path=Path("/tmp/wq/report.json"),
            shadow_export_seconds=300.0,
            shadow_export_rows=10,
            shadow_export_max_seconds=5.0,
            shadow_export_max_parts=5,
        )
        runner._export_decisions()
        return [event.kind for event in recorded]

    stalled = _run(["stopped after 0 part(s) by the 5s budget"], caught_up=False)
    assert stalled == ["decision_export_no_progress"], stalled

    # The positive control: being caught up is normal and must stay quiet.
    assert _run([], caught_up=True) == []

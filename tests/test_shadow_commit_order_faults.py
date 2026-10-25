"""WQN-21: Shadow commit ordering, under injected faults.

The invariant under test is one sentence from ``ShadowStore.commit_batch``:
effects and offsets land in **one** transaction. Two ways to break it, two
ways to prove it holds:

1. A failure *inside* the batch (after some effects ran) must roll back the
   effects **and** the offsets. Otherwise a restart would skip the batch's
   lines forever - loss, not retry.
2. A crash *after* COMMIT but before the tailer adopts the offsets must be a
   replay, not a duplication. The next cycle re-reads from the un-adopted
   offset; every table is keyed by natural ids, so the re-commit converges.

The runner-level "commit failed entirely" case (explode before BEGIN) is
already covered by ``test_shadow_checkpoint_advance.py``; these two fill the
remaining windows around the transaction boundary.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from shadow_helpers import (
    NOW,
    TARGET_DATE,
    FakeRunsClient,
    archive_line,
    book_payload,
    gamma_event,
    write_lines,
)
from weatherquant.config import Settings
from weatherquant.shadow import runner as runner_module
from weatherquant.shadow.config import ShadowSettings, load_thresholds
from weatherquant.shadow.runner import RunnerConfig, ShadowRunner

REPO = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO / "configs" / "shadow-thresholds.json"
VENUE_TS_MS = int((NOW - timedelta(seconds=4)).timestamp() * 1000)

_RUNNERS: list[ShadowRunner] = []


@pytest.fixture(autouse=True)
def _close_runners():
    yield
    for runner in _RUNNERS:
        runner.close(status="stopped", reason="teardown")
    _RUNNERS.clear()


@pytest.fixture(autouse=True)
def _no_live_forecast_fetches(monkeypatch):
    """Never let a test reach Open-Meteo (see test_shadow_checkpoint_advance)."""
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)


def _archive(root, *, rest_lines: int = 4) -> Path:
    raw = root / "raw"
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    for i in range(rest_lines):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    2 + i,
                    "rest",
                    book_payload(token_id="tok-0-yes", venue_ts_ms=VENUE_TS_MS + i * 1000),
                    received=NOW + timedelta(seconds=i),
                )
            ],
        )
    return raw


def _build(tmp_path, raw) -> ShadowRunner:
    models = tmp_path / "models"
    if not models.exists():
        shutil.copytree(REPO / "models", models)
    shadow = ShadowSettings(
        shadow_db_path=tmp_path / "shadow" / "shadow.duckdb",
        shadow_models_dir=models,
        shadow_health_dir=tmp_path / "health",
        shadow_report_path=tmp_path / "reports" / "report.json",
        shadow_thresholds_path=str(THRESHOLDS_PATH),
        collector_raw_dir=raw,
        shadow_model_names="ecmwf_raw,calibrated",
        shadow_primary_model="calibrated",
        shadow_tick_seconds=0.01,
        shadow_forecast_refresh_seconds=0.01,
        shadow_report_seconds=0.01,
        shadow_metrics_seconds=0.01,
        shadow_run_lookback_hours=24.0,
    )
    return ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )


def _counts(runner: ShadowRunner) -> dict[str, int]:
    con = runner.db.con
    return {
        table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("market_catalog", "token_books", "ingest_checkpoints", "data_quality_events")
    }


def test_a_failure_inside_the_batch_rolls_back_effects_and_offsets_together(
    tmp_path, monkeypatch
) -> None:
    """The checkpoints die with the effects that produced them, or a restart
    would resume past lines that were never stored."""
    raw = _archive(tmp_path, rest_lines=4)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()

    # ``start`` itself commits one informational quality event outside any
    # cycle; what must not change is the *batch's* footprint.
    baseline = _counts(runner)
    assert baseline["market_catalog"] == 0 and baseline["token_books"] == 0

    real_save = runner.store.save_checkpoints

    def explode_after_books(checkpoints):
        raise OSError("injected DuckDB failure after the effects ran")

    # The batch's order is markets -> books -> checkpoints; failing at the
    # checkpoints means the earlier effects already executed *inside* the
    # transaction and only the rollback can undo them.
    monkeypatch.setattr(runner.store, "save_checkpoints", explode_after_books)
    with pytest.raises(OSError, match="after the effects ran"):
        runner.cycle()

    assert _counts(runner) == baseline, "effects survived a transaction that was supposed to roll back"
    assert runner.tailer.checkpoints == {}, "memory adopted offsets across a failed commit"
    assert runner.state.markets == {} and runner.state.books == {}

    # The same lines are still ahead of the tailer: the retry consumes them
    # once, and nothing is duplicated by having tried before.
    monkeypatch.setattr(runner.store, "save_checkpoints", real_save)
    stats = runner.cycle()
    assert stats.lines_read > 0, "the rolled-back batch's lines were skipped on retry"
    counts = _counts(runner)
    # One event per bracket market; one row per token (all four books share
    # tok-0-yes, and the table is keyed by token).
    assert counts["market_catalog"] == 4, counts
    assert counts["token_books"] == 1, counts
    assert counts["ingest_checkpoints"] == 2
    assert runner.cycle().lines_read == 0, "the recovered batch was consumed twice"


def test_a_crash_after_commit_but_before_adopt_replays_without_duplicating(
    tmp_path, monkeypatch
) -> None:
    """The database is ahead of memory: the safe direction, and it must stay
    safe. Every table is keyed by natural ids precisely so this replay
    converges instead of doubling."""
    raw = _archive(tmp_path, rest_lines=4)
    runner = _build(tmp_path, raw)
    _RUNNERS.append(runner)
    runner.start()

    real_adopt = runner.tailer.adopt

    def crash_before_adopt(checkpoints, file_dates):
        raise RuntimeError("injected crash between COMMIT and adopt")

    monkeypatch.setattr(runner.tailer, "adopt", crash_before_adopt)
    with pytest.raises(RuntimeError, match="between COMMIT and adopt"):
        runner.cycle()

    # The transaction DID commit - the database is ahead of the tailer.
    counts_after_crash = _counts(runner)
    assert counts_after_crash["token_books"] == 1, counts_after_crash
    assert counts_after_crash["market_catalog"] == 4, counts_after_crash
    assert counts_after_crash["ingest_checkpoints"] == 2
    assert runner.tailer.checkpoints == {}, "memory must still be behind the database"

    # Restart-equivalent: adopt works again, the next cycle replays the batch
    # from the un-adopted offset and converges on the same facts.
    monkeypatch.setattr(runner.tailer, "adopt", real_adopt)
    runner.cycle()

    counts = _counts(runner)
    assert counts["token_books"] == 1, f"replay duplicated books: {counts}"
    assert counts["market_catalog"] == 4, f"replay duplicated markets: {counts}"
    offsets = {
        row[0]: row[1]
        for row in runner.db.con.execute(
            "SELECT file_path, offset_bytes FROM ingest_checkpoints"
        ).fetchall()
    }
    for path_key, offset in offsets.items():
        size = Path(path_key).stat().st_size
        assert offset == size, f"{path_key}: checkpoint {offset} != archive size {size}"
    # And the replayed batch is not consumed a third time.
    assert runner.cycle().lines_read == 0

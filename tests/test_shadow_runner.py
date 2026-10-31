"""End-to-end: archive in, decisions out, restart does not duplicate.

This is the integration half of the offline test list. It runs the real
loop - tailer, state, forecast cache, predictor, gates, store, report - against
a synthetic collector archive and a fake upstream, then does it all again from
a cold start to prove the second pass is a no-op rather than a second copy.

What it deliberately does *not* do is place an order. There is no code path
here that could, which is why nothing in this file has to assert that it did
not.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
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
from weatherquant.shadow.report import build_report, format_report
from weatherquant.shadow.runner import RunnerConfig, ShadowRunner

THRESHOLDS_PATH = Path("configs/shadow-thresholds.json")


def _fit_models(models_dir: Path) -> None:
    """Fit the two models this test runs, from synthetic pairs.

    Ten dates per lead bucket keeps every bucket above the 5-sample minimum,
    so the refusals under test are about *data*, never about an accident of
    how this fixture was written.
    """
    pairs = []
    for i in range(10):
        for lead in (12.0, 36.0, 60.0, 100.0, 150.0):
            pairs.append(
                {
                    "target_date": date(2026, 9, 1) + timedelta(days=i),
                    "run_time_utc": datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=i, hours=lead - 24),
                    "lead_hours": lead,
                    "predicted_max_f": 32.0 + (i % 3),
                    # Residuals must *vary*: a constant residual gives a zero
                    # spread, and a zero spread is a degenerate fit the
                    # probability layer refuses by design.
                    "observed_max_f": 32.5 + (i % 3) + (0.4 if i % 2 else -0.4),
                }
            )

    # Fit directly rather than through fit_models: the research-DB read path
    # has its own tests, and here the pairs must be exactly this data so the
    # decision assertions depend on the market, not on the fixture's provenance.
    from weatherquant.models.artifacts import fit_artifact, save_artifact, save_manifest

    artifacts = []
    for name in ("ecmwf_raw", "calibrated"):
        artifact = fit_artifact(
            model_name=name,
            model_version="vtest",
            pairs=pairs,
            training_cutoff_utc=datetime(2026, 9, 10, tzinfo=UTC),
            apply_bias=name == "calibrated",
        )
        save_artifact(artifact, models_dir)
        artifacts.append(artifact)
    save_manifest(models_dir, {a.model_name: a.model_version for a in artifacts})


# The venue clock a few seconds behind the decision instant, in epoch ms -
# the unit the CLOB /book payload carries.
VENUE_TS_MS = int((NOW - timedelta(seconds=4)).timestamp() * 1000)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    for token in ("tok-0-yes", "tok-1-yes", "tok-2-yes", "tok-3-yes"):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [archive_line(2, "rest", book_payload(token_id=token, venue_ts_ms=VENUE_TS_MS))],
        )
    return raw


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    path = tmp_path / "models"
    path.mkdir(parents=True, exist_ok=True)
    _fit_models(path)
    return path


def _settings(tmp_path: Path, archive: Path, models_dir: Path) -> ShadowSettings:
    return ShadowSettings(
        shadow_db_path=tmp_path / "shadow" / "shadow.duckdb",
        shadow_models_dir=models_dir,
        shadow_health_dir=tmp_path / "health",
        # Kept inside tmp_path: a report written to the repo's own data/ would
        # let one test read another's snapshot, which is exactly the coupling
        # the snapshot exists to avoid at runtime.
        shadow_report_path=tmp_path / "reports" / "report.json",
        # Every cycle republishes, so a test (and CI) sees a snapshot that
        # reflects the work just done rather than the state before it.
        shadow_report_seconds=0.01,
        shadow_thresholds_path=str(THRESHOLDS_PATH),
        collector_raw_dir=archive,
        shadow_model_names="ecmwf_raw,calibrated",
        shadow_primary_model="calibrated",
        shadow_tick_seconds=0.01,
        shadow_forecast_refresh_seconds=0.01,
        shadow_metrics_seconds=0.01,
        shadow_collector_silence_seconds=900.0,
        # Narrow enough that the first cycle exhausts the window, so a second
        # cycle that still calls upstream would be re-asking for runs it holds.
        shadow_run_lookback_hours=24.0,
    )


@pytest.fixture
def make_runner(tmp_path, archive, models_dir, monkeypatch):
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)

    def _make(**overrides) -> ShadowRunner:
        shadow = _settings(tmp_path, archive, models_dir)
        if overrides:
            shadow = shadow.model_copy(update=overrides)
        config = RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        )
        runner = ShadowRunner(config, clock=lambda: NOW + timedelta(seconds=1))
        _RUNNERS.append(runner)
        return runner

    return _make


# Every runner built by a test is closed on teardown, so tests can read the
# database they just wrote while the fixture owns the lifecycle.
_RUNNERS: list[ShadowRunner] = []


@pytest.fixture(autouse=True)
def _close_runners():
    yield
    for runner in _RUNNERS:
        runner.close(status="stopped", reason="teardown")
    _RUNNERS.clear()


def _run_one_cycle(make_runner) -> tuple[ShadowRunner, object]:
    runner = make_runner()
    runner.start()
    stats = runner.cycle()
    return runner, stats


# --- the happy path --------------------------------------------------------


def test_one_cycle_ingests_the_archive_and_records_decisions(make_runner) -> None:
    runner, stats = _run_one_cycle(make_runner)

    assert stats.lines_read == 5  # one gamma + four books
    assert stats.markets == 4
    assert stats.books == 4
    assert stats.decisions == 8  # four markets, two directions each
    assert stats.predictions >= 1

    db = runner.db
    counts = db.table_counts()
    assert counts["market_catalog"] == 4
    assert counts["token_books"] == 4
    assert counts["ingest_checkpoints"] == 2  # gamma + rest
    assert counts["shadow_decisions"] == 8
    assert counts["model_predictions"] >= 1
    assert counts["model_versions"] == 2
    assert counts["rejection_reasons"] >= 0


def test_every_decision_carries_a_state_from_the_specified_set(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    rows = runner.db.con.execute("SELECT state, primary_reason FROM shadow_decisions").fetchall()
    assert rows
    allowed_states = {"ACCEPT", "REJECT", "NO_TRADE", "DATA_UNAVAILABLE"}
    allowed_reasons = {
        "NO_FORECAST",
        "FORECAST_NOT_YET_AVAILABLE",
        "NO_VALID_MODEL",
        "MODEL_STALE",
        "NO_ORDERBOOK",
        "STALE_ORDERBOOK",
        "INCOMPLETE_ORDERBOOK",
        "INSUFFICIENT_LIQUIDITY",
        "UNKNOWN_FEE",
        "NEGATIVE_NET_EDGE",
        "EDGE_BELOW_THRESHOLD",
        "TIME_MISMATCH",
        "MARKET_CLOSED",
        "COLLECTOR_DISCONNECTED",
        "DATA_QUALITY_FAILURE",
        "EVALUATION_ERROR",
    }
    for state, reason in rows:
        assert state in allowed_states
        if state in {"ACCEPT", "CANDIDATE"}:
            assert reason is None
        else:
            assert reason in allowed_reasons


def test_a_decision_records_the_books_it_looked_at(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    row = runner.db.con.execute(
        "SELECT book_id, venue_ts_utc, collector_received_at_utc, shadow_received_at_utc, "
        "snapshot_age_ms, model_run_id, proposed_shares, estimated_fees_usd, net_edge "
        "FROM shadow_decisions WHERE state = 'ACCEPT' LIMIT 1"
    ).fetchone()
    if row is None:
        # No ACCEPT is a perfectly good outcome; assert on whatever is there.
        row = runner.db.con.execute(
            "SELECT book_id, venue_ts_utc, collector_received_at_utc, shadow_received_at_utc, "
            "snapshot_age_ms, model_run_id, proposed_shares, estimated_fees_usd, net_edge "
            "FROM shadow_decisions LIMIT 1"
        ).fetchone()
    assert row[0] is not None  # book_id
    assert row[1] is not None  # venue timestamp
    assert row[2] is not None  # collector received
    assert row[3] is not None  # shadow received
    assert row[5] is not None  # model_run_id


def test_the_raw_archive_is_never_written_to(make_runner, archive) -> None:
    before = {p: (p.stat().st_size, p.read_bytes()) for p in sorted(archive.rglob("*.jsonl"))}
    _run_one_cycle(make_runner)
    after = {p: (p.stat().st_size, p.read_bytes()) for p in sorted(archive.rglob("*.jsonl"))}
    assert before == after


def test_the_shadow_database_is_its_own_file(tmp_path, make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    assert runner.db.path.name == "shadow.duckdb"
    assert runner.db.path.exists()
    assert runner.db.path.parent.name == "shadow"


# --- restart ---------------------------------------------------------------


def test_a_restart_replays_nothing_and_duplicates_nothing(make_runner) -> None:
    first = make_runner()
    first.start()
    first.cycle()
    first.close(status="stopped", reason="first")  # releases the lease

    second = make_runner()
    second.start()
    stats = second.cycle()

    # Nothing new to read...
    assert stats.lines_read == 0
    assert stats.forecasts_fetched == 0  # ...and no upstream re-asked

    # ...and the *ingested* state was not written twice. Decisions are a
    # different matter: they are per-cycle observations, and a second cycle
    # legitimately produces a second set - what it must not do is silently
    # reuse the first cycle's rows as if they described this one.
    counts = second.db.table_counts()
    assert counts["market_catalog"] == 4
    assert counts["token_books"] == 4
    assert counts["shadow_decisions"] == 16  # 8 per cycle x 2 cycles

    decisions = second.db.con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT cycle_id) FROM shadow_decisions"
    ).fetchone()
    assert decisions == (16, 2)
    second.close(status="stopped", reason="second")


def test_new_lines_after_a_restart_are_picked_up(make_runner, archive) -> None:
    first, _stats = _run_one_cycle(make_runner)
    first.close(status="stopped", reason="first")

    # The collector publishes a new market after we restarted.
    event = gamma_event(event_id="evt-2", labels=["50-51°F"])
    write_lines(archive, "gamma", TARGET_DATE, [archive_line(9, "gamma", event)])

    runner = make_runner()
    runner.start()
    stats = runner.cycle()
    assert stats.lines_read == 1
    assert runner.db.table_counts()["market_catalog"] == 5
    runner.close(status="stopped", reason="third")


def test_an_interrupted_evaluation_is_visible_as_a_stuck_candidate(make_runner) -> None:
    """A CANDIDATE row after a restart is how an interrupted cycle shows up."""
    runner, _stats = _run_one_cycle(make_runner)
    runner.db.execute(
        "INSERT INTO shadow_decisions (decision_id, run_id, cycle_id, market_id, station, "
        "direction, state, decision_time_utc, thresholds_version, market_closed, created_at_utc) "
        "VALUES ('stuck', 'r', 'c', 'm', 'KLGA', 'buy_yes', 'CANDIDATE', ?, 'v1', FALSE, ?)",
        [NOW, NOW],
    )
    report = build_report(runner.db.con, now=NOW + timedelta(minutes=1))
    assert report["decisions"]["interrupted"] == 1
    assert any("CANDIDATE" in w for w in report["warnings"])


# --- single instance -------------------------------------------------------


def test_a_second_runner_cannot_open_the_same_database(make_runner) -> None:
    first = make_runner()
    first.start()
    from weatherquant.data.lock import LockError

    second = make_runner()
    with pytest.raises(LockError):
        second.start()
    first.close()


# --- health ----------------------------------------------------------------


def test_a_closed_market_in_the_window_records_no_trade(make_runner, archive) -> None:
    """Closed-but-recent markets are evaluated, so NO_TRADE is observable.

    Filtering to tradeable-only would leave NO_TRADE permanently zero in
    production - the state would exist in the spec and in unit tests but never
    in the data, which is exactly how a lifecycle gate rots unnoticed.
    """
    event = gamma_event(event_id="evt-closed", tradeable=False)
    write_lines(archive, "gamma", TARGET_DATE, [archive_line(3, "gamma", event)])

    runner, stats = _run_one_cycle(make_runner)
    assert stats.decisions > 0

    rows = runner.db.con.execute(
        "SELECT d.state, d.primary_reason, COUNT(*) FROM shadow_decisions d "
        "WHERE d.market_id LIKE 'evt-closed-m%' GROUP BY 1, 2"
    ).fetchall()
    assert rows, "the closed event produced no decisions"
    assert all(state == "NO_TRADE" for state, _reason, _n in rows), rows
    assert all(reason == "MARKET_CLOSED" for _state, reason, _n in rows), rows
    # Nothing was priced: there was nothing to price.
    priced = runner.db.con.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE market_id LIKE 'evt-closed-m%' "
        "AND net_edge IS NOT NULL"
    ).fetchone()[0]
    assert priced == 0


def test_no_input_can_arrive_after_the_decision_that_used_it(
    tmp_path, archive, models_dir, monkeypatch
) -> None:
    """Regression: the decision instant must postdate every input the cycle gathered.

    Found by running against a real collector archive. The cycle stamped each
    decision with the time it *started*, but the books and the forecasts were
    gathered afterwards - so every input looked like it had arrived after the
    decision that used it, and TIME_MISMATCH refused all 66 decisions on
    perfectly good data. A clock that advances on every read makes that
    impossible to reintroduce unnoticed: with a frozen clock every timestamp
    compares equal and the bug is invisible.
    """
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)

    tick = {"n": 0}

    def advancing_clock():
        tick["n"] += 1
        return NOW + timedelta(seconds=tick["n"])

    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=advancing_clock,
    )
    _RUNNERS.append(runner)
    runner.start()
    stats = runner.cycle()

    assert stats.decisions > 0
    offending = runner.db.con.execute(
        "SELECT detail FROM rejection_reasons WHERE reason = 'TIME_MISMATCH'"
    ).fetchall()
    assert offending == [], f"inputs stamped after the decision: {[r[0] for r in offending]}"


def test_the_health_file_is_written_so_the_probe_never_opens_the_database(tmp_path, make_runner) -> None:
    from weatherquant.shadow.health import evaluate_shadow_health

    runner = make_runner()
    runner.start()
    runner.cycle()

    path = runner.shadow.shadow_health_dir / "heartbeat.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"]
    assert payload["updated_at_utc"]
    assert payload["last_decision_at_utc"]
    assert payload["thresholds_version"] == load_thresholds(THRESHOLDS_PATH).version

    verdict = evaluate_shadow_health(path, now=NOW + timedelta(seconds=5))
    assert verdict.exit_code == 0
    assert verdict.status in {"healthy", "degraded"}


def test_starting_alone_publishes_a_heartbeat(tmp_path, archive, models_dir, monkeypatch) -> None:
    """Liveness must not wait on the first cycle's network I/O.

    Found in CI: the first cycle fetches forecasts before writing anything, so
    the probe read "not started" for as long as the upstream took to answer -
    a liveness signal reporting a third party's latency. `start()` writes it,
    and so does the top of every cycle.
    """
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    _RUNNERS.append(runner)
    runner.start()  # no cycle yet

    path = shadow.shadow_health_dir / "heartbeat.json"
    assert path.exists(), "start() did not publish a heartbeat"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["run_id"] == runner.run_id


def test_once_returns_exit_code_4_when_the_lease_is_held(tmp_path, archive, models_dir) -> None:
    """The single-instance refusal has to survive `--once`.

    `--once` wraps start() in a broad `except Exception`, which turned the
    documented refusal (exit 4) into a generic failure (exit 1) - so the very
    command an operator and CI use to probe for the lock reported a crash
    instead of a correct refusal.
    """
    from weatherquant.data.lock import SingleInstanceLock
    from weatherquant.shadow.cli import main

    shadow = _settings(tmp_path, archive, models_dir)
    lock_path = shadow.shadow_db_path.parent / ".shadow.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Held by a live pid with a fresh heartbeat: not reclaimable.
    holder = SingleInstanceLock(lock_path, run_id="someone-else", hostname="other-host")
    holder.acquire()
    try:
        code = main(
            [
                "--db", str(shadow.shadow_db_path),
                "--raw", str(shadow.collector_raw_dir),
                "--models", str(shadow.shadow_models_dir),
                "run", "--once",
            ]
        )
        assert code == 4, f"expected the single-instance refusal code, got {code}"
    finally:
        holder.release()


def test_the_engine_publishes_a_report_snapshot(tmp_path, archive, models_dir, monkeypatch) -> None:
    """A snapshot exists before the first cycle finishes, from the engine's own
    connection - which is the only side that can open the file at all.

    The clock advances: publication is throttled against it, so a frozen clock
    would leave the snapshot describing the state *before* any work was done
    and the assertion below would be measuring the throttle, not the engine.
    """
    import itertools

    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    counter = itertools.count(start=1)
    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=next(counter)),
    )
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    path = shadow.shadow_report_path
    assert path.exists(), "the engine did not publish a report snapshot"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source"] == "shadow-main"
    assert payload["schema_version"] == 1
    assert payload["generated_at_utc"]
    assert payload["expires_at_utc"]
    assert payload["decisions"]["evaluated"] > 0, "the snapshot predates the cycle it should cover"
    assert "stale_after_seconds" in payload


def test_a_failing_report_never_stops_the_engine(tmp_path, archive, models_dir, monkeypatch) -> None:
    """Reporting is not the mission. A report that raises is logged and dropped."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    monkeypatch.setattr(runner_module, "publish_report_snapshot", _boom)

    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    _RUNNERS.append(runner)
    runner.start()
    stats = runner.cycle()  # must not raise

    assert stats.decisions > 0, "the engine stopped evaluating when the report failed"
    assert not path_exists(shadow.shadow_report_path)
    row = runner.db.con.execute(
        "SELECT kind, severity FROM data_quality_events WHERE kind = 'report_publish_failed'"
    ).fetchall()
    assert row and row[0][1] == "warning", "the failure must leave a trace"


def test_the_engine_publishes_the_detail_snapshots_the_dashboard_reads(
    tmp_path, archive, models_dir, monkeypatch
) -> None:
    """The three row-level snapshots, produced by the engine's own connection.

    This is the half the dashboard contract could not test from its side: the
    producer proved it could build the files, and the API proved it could read
    them, but only the runner can open ``shadow.duckdb`` - so if the runner
    never calls the producer, both of those stay green over a dashboard that
    permanently says ``unavailable``. Same lesson as the checkpoint defect:
    drive the runner, not the helper.
    """
    import itertools

    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    counter = itertools.count(start=1)
    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=next(counter)),
    )
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    directory = shadow.shadow_report_path.parent
    payloads = {}
    for name in ("recent-decisions.json", "market-snapshot.json", "forecast-snapshot.json"):
        path = directory / name
        assert path.exists(), f"the engine did not publish {name}"
        payloads[name] = json.loads(path.read_text(encoding="utf-8"))

    for name, payload in payloads.items():
        assert payload["schema_version"] == 1, name
        assert payload["generated_at_utc"], name
        assert payload["source_service"] == "weatherquant-shadow", name
        assert payload["stale_after_seconds"] > 0, name
        assert payload["source_version"], f"{name} carries no version to trace it to"

    decisions = payloads["recent-decisions.json"]["data"]["decisions"]
    assert decisions, "the detail snapshot exists but is empty after a real cycle"
    assert payloads["recent-decisions.json"]["data"]["window"] <= 500
    # LIVE and REPLAY must be distinguishable per row, never by inference.
    assert all(row["mode"] in {"LIVE", "REPLAY"} for row in decisions)
    stamps = [row["decided_at_utc"] for row in decisions]
    assert stamps == sorted(stamps, reverse=True), "rows must be newest first"

    markets = payloads["market-snapshot.json"]["data"]["markets"]
    assert markets, "no market reached the model-vs-market snapshot"
    assert payloads["forecast-snapshot.json"]["data"]["distributions"] is not None


def test_a_failing_detail_publisher_never_stops_the_engine(
    tmp_path, archive, models_dir, monkeypatch
) -> None:
    """Detail rows are not the mission either - and must not cost us the report.

    The two publications are deliberately separate try blocks: a malformed
    decision row should cost the dashboard its detail view, not its aggregates.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    monkeypatch.setattr(runner_module, "publish_detail_snapshots", _boom)

    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    _RUNNERS.append(runner)
    runner.start()
    stats = runner.cycle()  # must not raise

    assert stats.decisions > 0, "the engine stopped evaluating when a detail snapshot failed"
    assert path_exists(shadow.shadow_report_path), "a detail failure also cost us the report"
    assert not path_exists(shadow.shadow_report_path.parent / "market-snapshot.json")
    row = runner.db.con.execute(
        "SELECT kind, severity FROM data_quality_events WHERE kind = 'detail_publish_failed'"
    ).fetchall()
    assert row and row[0][1] == "warning", "the failure must leave a trace"


def path_exists(path) -> bool:
    from pathlib import Path as _P

    return _P(path).exists()


def test_a_missing_heartbeat_is_starting_not_unhealthy() -> None:
    from weatherquant.shadow.health import evaluate_shadow_health

    verdict = evaluate_shadow_health(Path("does/not/exist.json"))
    assert verdict.status == "starting"
    assert verdict.exit_code == 0


def test_a_stale_heartbeat_is_unhealthy(tmp_path) -> None:
    from weatherquant.shadow.health import evaluate_shadow_health, write_heartbeat

    path = tmp_path / "hb.json"
    write_heartbeat(path, {"status": "running", "updated_at_utc": (NOW - timedelta(hours=2)).isoformat()})
    verdict = evaluate_shadow_health(path, now=NOW)
    assert verdict.status == "unhealthy"
    assert verdict.exit_code == 1


# --- report ----------------------------------------------------------------


def test_the_report_answers_every_question_the_spec_lists(tmp_path, make_runner) -> None:
    from weatherquant.shadow.health import write_heartbeat

    runner, _stats = _run_one_cycle(make_runner)
    write_heartbeat(
        runner.shadow.shadow_health_dir / "heartbeat.json",
        {"status": "running", "updated_at_utc": NOW.isoformat(), "run_id": runner.run_id},
    )
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))

    required = {
        "run", "liveness", "decisions", "theoretical_edge",
        "prediction_quality", "liquidity", "coverage", "data_quality",
    }
    assert required <= set(report)

    liveness = report["liveness"]
    for key in (
        "collector_last_seen_utc",
        "latest_orderbook_venue_age_seconds",
        "latest_forecast_run_age_seconds",
    ):
        assert key in liveness

    decisions = report["decisions"]
    for key in (
        "evaluated", "candidates_generated", "accepted", "rejected",
        "no_trade", "data_unavailable", "primary_reasons", "p95_decision_latency_ms",
    ):
        assert key in decisions

    assert "data_gaps" in report["data_quality"]
    assert "errors" in report["data_quality"]
    assert "forecast_coverage" in report["coverage"]
    assert "orderbook_coverage" in report["coverage"]


def test_the_report_never_invents_a_realised_return(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))

    assert report["equity_curve"] is None
    assert "no realised return" in report["equity_curve_note"]
    assert "under the model's own probabilities" in report["theoretical_edge"]["note"]
    assert "not a realised return" in report["theoretical_edge"]["note"]


def test_the_report_separates_quality_from_edge_from_liquidity_from_opportunity(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))

    assert report["prediction_quality"]["scored"] is False
    assert "settlement" in report["prediction_quality"]["note"]
    assert report["liquidity"]["note"].startswith("depth actually present")
    assert report["decisions"]["accepted"] == sum(
        1
        for (state,) in runner.db.con.execute("SELECT state FROM shadow_decisions").fetchall()
        if state == "ACCEPT"
    )


def test_the_report_renders_as_readable_text_without_crashing(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))
    text = format_report(report)
    assert "[decisions]" in text
    assert "NOT realised profit" in text
    assert "equity curve" in text.lower()


# --- CLI -------------------------------------------------------------------


def test_the_cli_init_creates_the_database_and_reports_the_models(tmp_path, capsys) -> None:
    from weatherquant.shadow.cli import main

    db_path = tmp_path / "cli-shadow.duckdb"
    code = main(["--db", str(db_path), "init"])
    out = capsys.readouterr().out
    assert code in (0, 2)  # 2 when no manifest; the DB work must still have happened
    assert db_path.exists()
    assert "shadow database" in out


def test_the_cli_report_on_a_missing_database_exits_two(tmp_path, capsys) -> None:
    from weatherquant.shadow.cli import main

    code = main(["--db", str(tmp_path / "nope.duckdb"), "report", "--json"])
    assert code == 2


def test_the_cli_report_reads_back_what_a_run_wrote(tmp_path, make_runner, capsys) -> None:
    from weatherquant.shadow.cli import main

    runner, _stats = _run_one_cycle(make_runner)
    code = main(["--db", str(runner.db.path), "report", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["decisions"]["evaluated"] == 8


# --- LIVE vs REPLAY (runner level) ----------------------------------------

# An archive whose lines all carry a received_at three days before the clock.
OLD = NOW - timedelta(days=3)
VENUE_TS_OLD_MS = int((OLD - timedelta(seconds=4)).timestamp() * 1000)


def _historical_archive(root: Path) -> Path:
    """The same shape as `archive`, but written three days before `now`."""
    raw = root / "raw-old"
    write_lines(raw, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event(), received=OLD)])
    for token in ("tok-0-yes", "tok-1-yes", "tok-2-yes", "tok-3-yes"):
        write_lines(
            raw,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    2, "rest", book_payload(token_id=token, venue_ts_ms=VENUE_TS_OLD_MS),
                    received=OLD,
                )
            ],
        )
    return raw


def test_replay_decisions_are_stamped_with_the_archive_instant(
    tmp_path, models_dir, monkeypatch
) -> None:
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    raw = _historical_archive(tmp_path)

    shadow = _settings(tmp_path, raw, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW,
    )
    _RUNNERS.append(runner)
    runner.start()
    stats = runner.cycle()

    assert runner._mode is not None and runner._mode.value == "REPLAY"
    assert stats.decisions > 0

    # The decision instant is the archive's own newest event, not the clock.
    row = runner.db.con.execute(
        "SELECT MAX(decision_time_utc), MIN(mode), COUNT(DISTINCT mode_reason) "
        "FROM shadow_decisions"
    ).fetchone()
    assert row[0] == OLD, "a replay decision must be stamped with the archive instant"
    assert row[1] == "REPLAY"
    assert row[2] >= 1
    reason = runner.db.con.execute(
        "SELECT mode_reason FROM shadow_decisions LIMIT 1"
    ).fetchone()[0]
    assert reason and reason.startswith("auto:")

    # No live-feed complaint: nobody was expected to be writing.
    reasons = {
        r[0] for r in runner.db.con.execute(
            "SELECT reason FROM rejection_reasons WHERE is_primary"
        ).fetchall()
    }
    assert "COLLECTOR_DISCONNECTED" not in reasons


def test_configured_live_stays_live_even_against_a_stale_archive(
    tmp_path, models_dir, monkeypatch
) -> None:
    """The operator can insist on live - and then silence is reported as silence.

    `auto` would call this REPLAY; `live` says the feed should be current and
    is not, which is the finding an operator actually needs.
    """
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    raw = _historical_archive(tmp_path)

    shadow = _settings(tmp_path, raw, models_dir).model_copy(update={"shadow_mode": "live"})
    runner = ShadowRunner(
        RunnerConfig(settings=Settings(), shadow=shadow, thresholds=load_thresholds(THRESHOLDS_PATH)),
        clock=lambda: NOW,
    )
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    assert runner._mode.value == "LIVE"
    row = runner.db.con.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE mode = 'LIVE' AND state = 'DATA_UNAVAILABLE' "
        "AND primary_reason = 'COLLECTOR_DISCONNECTED'"
    ).fetchone()[0]
    assert row > 0, "a live-mode run against a three-day-old archive must report the silence"
    # ...and none of them is an ACCEPT.
    assert runner.db.con.execute(
        "SELECT COUNT(*) FROM shadow_decisions WHERE state = 'ACCEPT'"
    ).fetchone()[0] == 0


def test_the_mode_is_decided_once_and_does_not_flip_mid_run(
    tmp_path, archive, models_dir, monkeypatch
) -> None:
    """A collector that dies after startup must surface as an outage, not a replay."""
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    shadow = _settings(tmp_path, archive, models_dir)
    runner = ShadowRunner(
        RunnerConfig(settings=Settings(), shadow=shadow, thresholds=load_thresholds(THRESHOLDS_PATH)),
        clock=lambda: NOW,
    )
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()
    assert runner._mode.value == "LIVE"

    # Simulate the feed going quiet for an hour. Mode must not move.
    runner.clock = lambda: NOW + timedelta(hours=1)
    runner.cycle()
    assert runner._mode.value == "LIVE", "mode must not flip to REPLAY when the feed stops"


def test_the_report_separates_live_accepts_from_replay_ones(make_runner) -> None:
    runner, _stats = _run_one_cycle(make_runner)
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))
    dec = report["decisions"]

    assert "by_mode" in dec
    assert "live_accepted" in dec and "replay_accepted" in dec
    # Every row written by the runner is explicitly LIVE here (fresh archive),
    # so nothing may be counted as replay.
    assert dec["replay_accepted"] == 0
    assert dec["live_accepted"] == sum(
        1 for (state, mode) in runner.db.con.execute(
            "SELECT state, mode FROM shadow_decisions"
        ).fetchall()
        if state == "ACCEPT" and mode == "LIVE"
    )
    assert not any("REPLAY" in w for w in report["warnings"])


def test_a_dead_feed_never_produces_a_live_accept(tmp_path, models_dir, monkeypatch) -> None:
    """The deployment safety property, end to end.

    Auto mode against an archive nobody is writing resolves to REPLAY - so the
    engine may still reason about history, and the report must show that zero
    of it counts as a live signal. The failure mode this guards is a service
    whose feed died and whose log quietly kept saying ACCEPT.
    """
    monkeypatch.setattr(runner_module, "SingleRunsClient", FakeRunsClient)
    raw = _historical_archive(tmp_path)

    shadow = _settings(tmp_path, raw, models_dir)
    runner = ShadowRunner(
        RunnerConfig(
            settings=Settings(),
            shadow=shadow,
            thresholds=load_thresholds(THRESHOLDS_PATH),
        ),
        clock=lambda: NOW,
    )
    _RUNNERS.append(runner)
    runner.start()
    runner.cycle()

    assert runner._mode.value == "REPLAY"
    report = build_report(runner.db.con, now=NOW + timedelta(seconds=10))
    dec = report["decisions"]

    assert dec["live_accepted"] == 0, "a dead feed produced a LIVE accept"
    assert dec["by_mode"].get("LIVE", {}).get("accepted", 0) == 0
    # Whatever it did conclude about history is labelled and warned about.
    assert any("REPLAY, not LIVE" in w for w in report["warnings"])


def test_live_export_keeps_decision_time_depth_and_fees_after_catalog_changes(make_runner, archive, tmp_path):
    import hashlib

    from weatherquant.paper.books import BookEvidence
    from weatherquant.paper.events import parse_signal_event
    from weatherquant.shadow.export import DecisionExporter, read_export

    runner, _ = _run_one_cycle(make_runner)
    con = runner.db.con
    expected = {}
    for raw in (archive / "rest" / f"{TARGET_DATE}.jsonl").read_bytes().splitlines():
        if raw:
            payload = json.loads(raw)["payload"]
            expected[payload["asset_id"]] = (hashlib.sha256(raw).hexdigest(), payload)
    old_fees = dict(con.execute("SELECT market_id, fee_rate FROM market_catalog").fetchall())
    assert all(rate is not None and rate != 0.99 for rate in old_fees.values())
    assert con.execute("SELECT COUNT(*) FROM decision_book_depth").fetchone() == (4,)
    con.execute("UPDATE market_catalog SET fee_rate=0.99")
    con.execute("UPDATE token_books SET bids_json='[]', asks_json='[]'")
    root = tmp_path / "audit-export"
    exporter = DecisionExporter(con, root)
    assert exporter.run_batch().rows_exported == 8
    rows = list(read_export(root))
    assert {row["direction"] for row in rows} == {"buy_yes", "sell_yes"}
    assert len({row["decision_id"] for row in rows}) == len(rows)
    for row in rows:
        event = parse_signal_event(row)
        evidence = BookEvidence.from_signal(event)
        assert evidence.book_id == event.book_id
        source_digest, payload = expected[event.token_id]
        assert evidence.source_event_id == source_digest
        assert row["fee_rate"] == old_fees[event.market_id]
        assert [(lv.price_usd_per_share, lv.size_shares) for lv in evidence.book.bids] == sorted(
            ((float(lv["price"]), float(lv["size"])) for lv in payload["bids"]), reverse=True)
        assert [(lv.price_usd_per_share, lv.size_shares) for lv in evidence.book.asks] == sorted(
            (float(lv["price"]), float(lv["size"])) for lv in payload["asks"])

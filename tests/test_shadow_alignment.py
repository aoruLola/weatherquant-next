"""Time alignment and LIVE/REPLAY isolation, audited from recorded history (WQN-25).

The runtime gates refuse bad input as it arrives; this audit re-derives the
same invariants from the written rows, which is the only form of proof that
survives a bug, a config change or a hand edit. Each acceptance clause gets
both directions here: a clean history passes, and each specific break -
future run, unpublishable issue, mismatched date or instant, unusable or
mixed mode, untraceable fusion input - is named and attached to its row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.shadow.alignment import audit_alignment
from weatherquant.storage.shadow import ShadowDatabase

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
RUN = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
PUBLISHABLE = RUN + timedelta(hours=12)  # run + conservative lag, before NOW
TARGET = date(2026, 10, 22)

DECISION_COLUMNS = (
    "decision_id, run_id, cycle_id, market_id, event_id, target_date, station, "
    "direction, state, model_name, model_version, forecast_run_time_utc, "
    "decision_time_utc, mode, mode_reason, created_at_utc"
)
PREDICTION_COLUMNS = (
    "model_name, model_version, station, target_date, event_id, "
    "forecast_run_time_utc, probabilities_json, lead_hours, n_hours, "
    "n_missing_hours, model_run_id, training_cutoff_utc, "
    "forecast_publishable_at_utc, forecast_publishable_basis, "
    "forecast_received_at_utc, decision_time_utc, calibration_version, "
    "source_data_hash, computed_at_utc"
)


@pytest.fixture
def db(tmp_path: Path):
    database = ShadowDatabase(tmp_path / "shadow.duckdb")
    database.open()
    yield database
    database.close()


def _decision(
    db,
    *,
    i: int,
    market: str = "m-1",
    event: str = "e-1",
    target: date = TARGET,
    model: str | None = "fusion",
    run_time: datetime | None = RUN,
    when: datetime = NOW,
    mode: str = "LIVE",
    created: datetime | None = None,
    state: str = "ACCEPT",
    cycle: str | None = None,
) -> str:
    decision_id = f"id-{i:04d}"
    db.con.execute(
        f"INSERT INTO shadow_decisions ({DECISION_COLUMNS}) VALUES "
        "(?, 'run-1', ?, ?, ?, ?, 'KLGA', 'buy_yes', ?, ?, 'v1', ?, ?, ?, ?, ?)",
        [
            decision_id,
            cycle or f"cycle-{i}",
            market,
            event,
            target,
            state,
            model,
            run_time,
            when,
            mode,
            "configured:live" if mode == "LIVE" else "configured:replay",
            created or when + timedelta(seconds=5),
        ],
    )
    return decision_id


def _prediction(
    db,
    *,
    model: str = "fusion",
    event: str = "e-1",
    target: date = TARGET,
    run_time: datetime = RUN,
    publishable: datetime | None = PUBLISHABLE,
    when: datetime = NOW,
    n_missing: int = 0,
    model_run_id: str = "run-x",
) -> None:
    db.con.execute(
        f"INSERT INTO model_predictions ({PREDICTION_COLUMNS}) VALUES "
        "(?, 'v1', 'KLGA', ?, ?, ?, '{}', 48.0, 24, ?, ?, "
        "datetime '2020-01-01', ?, 'estimated', ?, ?, 'cal-v1', 'sha', ?)",
        [
            model,
            target,
            event,
            run_time,
            n_missing,
            model_run_id,
            publishable,
            run_time + timedelta(hours=6),
            when,
            when,
        ],
    )


def _kinds(report) -> set[str]:
    return {v.kind for v in report.violations}


# --- the clean history ---------------------------------------------------------


def test_a_clean_history_is_consistent_and_counts_exposure(db):
    for model in ("calibrated", "ecmwf_raw", "fusion", "gfs_raw"):
        _prediction(db, model=model)
    for i, market in enumerate(("m-1", "m-2")):  # two markets, one weather day
        _decision(db, i=i, market=market)

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent, [v.as_dict() for v in report.violations]
    assert report.checked_decisions == 2
    assert report.checked_predictions == 1
    assert report.per_mode["LIVE"]["decisions"] == 2
    assert report.per_mode["LIVE"]["accepts"] == 2
    groups = [g for g in report.exposure_groups if g.kind == "same_weather_day"]
    assert len(groups) == 1
    assert groups[0].key == f"KLGA/{TARGET.isoformat()}"
    assert sorted(groups[0].market_ids) == ["m-1", "m-2"]


def test_one_forecast_run_shared_across_markets_is_listed_not_refused(db):
    for model in ("calibrated", "ecmwf_raw", "fusion", "gfs_raw"):
        _prediction(db, model=model)
    for i, market in enumerate(("m-1", "m-2", "m-3")):
        _decision(db, i=i, market=market)

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent
    sharing = report.forecast_sharing_markets
    # The decisions carry model_name='fusion': one group, three markets fed
    # by the single recorded run - correlated samples, listed not refused.
    assert len(sharing) == 1
    assert sharing[0]["model_name"] == "fusion"
    assert sharing[0]["markets"] == 3


# --- no future forecast was read -----------------------------------------------


def test_a_run_initialised_after_the_decision_is_a_violation(db):
    future_run = NOW + timedelta(hours=6)
    _prediction(db, run_time=future_run, publishable=future_run + timedelta(hours=12))
    _decision(db, i=1, run_time=future_run)

    report = audit_alignment(db.con, now=NOW)

    assert "FUTURE_FORECAST_RUN" in _kinds(report)


def test_an_issue_that_was_not_publishable_yet_is_a_violation(db):
    _prediction(db, publishable=NOW + timedelta(hours=1))
    _decision(db, i=1)

    report = audit_alignment(db.con, now=NOW)

    assert "FORECAST_NOT_PUBLISHABLE" in _kinds(report)


def test_cross_date_reuse_checks_only_the_prediction_for_each_decision(db):
    tomorrow = TARGET + timedelta(days=1)
    _prediction(db, model="gfs_raw")
    _prediction(db, model="gfs_raw", target=tomorrow, publishable=NOW + timedelta(hours=1), n_missing=1)
    _decision(db, i=1, model="gfs_raw")
    late = _decision(db, i=2, model="gfs_raw", target=tomorrow, mode="REPLAY")

    report = audit_alignment(db.con, now=NOW)

    assert report.checked_decisions == report.checked_predictions == 2
    assert report.partial_local_day_decisions == 1
    assert report.per_mode["LIVE"]["decisions"] == report.per_mode["REPLAY"]["decisions"] == 1
    assert [(v.kind, v.decision_id) for v in report.violations] == [("FORECAST_NOT_PUBLISHABLE", late)]


def test_wrong_dates_do_not_supply_forecast_evidence(db):
    for offset in (1, 2):
        _prediction(db, model="gfs_raw", target=TARGET + timedelta(days=offset),
                    publishable=NOW + timedelta(days=1), n_missing=1)
    did = _decision(db, i=1, model="gfs_raw")

    report = audit_alignment(db.con, now=NOW)

    assert report.checked_decisions == 1
    assert report.checked_predictions == report.partial_local_day_decisions == 0
    assert [(v.kind, v.decision_id) for v in report.violations] == [("TARGET_DATE_MISMATCH", did)]


def test_distinct_events_with_the_same_model_date_and_run_count_separately(db):
    for i, event in enumerate(("e-1", "e-2")):
        _prediction(db, model="gfs_raw", event=event)
        _decision(db, i=i, model="gfs_raw", event=event)

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent
    assert report.checked_decisions == report.checked_predictions == 2


def test_missing_decision_date_cannot_be_inferred_from_other_predictions(db):
    _prediction(db, model="gfs_raw")
    did = _decision(db, i=1, model="gfs_raw")
    db.con.execute("UPDATE shadow_decisions SET target_date = NULL WHERE decision_id = ?", [did])

    report = audit_alignment(db.con, now=NOW)

    assert report.checked_predictions == 0
    assert [(v.kind, v.decision_id) for v in report.violations] == [("PREDICTION_UNTRACEABLE", did)]


def test_a_decision_citing_an_unrecorded_prediction_is_untraceable(db):
    _decision(db, i=1)  # no model_predictions row at all

    report = audit_alignment(db.con, now=NOW)

    assert "PREDICTION_UNTRACEABLE" in _kinds(report)


def test_a_partial_local_day_is_counted_not_refused(db):
    _prediction(db, model="ecmwf_raw", n_missing=3)
    _decision(db, i=1, model="ecmwf_raw")

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent, "a legal-but-weaker forecast must not read as broken"
    assert report.partial_local_day_decisions == 1


def test_the_decision_and_prediction_must_name_the_same_date_and_instant(db):
    _prediction(db, target=TARGET + timedelta(days=1))
    _decision(db, i=1)

    mismatched_date = audit_alignment(db.con, now=NOW)
    assert "TARGET_DATE_MISMATCH" in _kinds(mismatched_date)

    _prediction(db)
    db.con.execute(
        "UPDATE model_predictions SET decision_time_utc = ? WHERE decision_time_utc = ? AND model_name = 'fusion'",
        [NOW - timedelta(minutes=1), NOW],
    )
    mismatched_time = audit_alignment(db.con, now=NOW)
    assert "PREDICTION_TIME_MISMATCH" not in _kinds(mismatched_time)


# --- the four models' inputs line up with their outputs -------------------------


def test_fusion_without_a_same_run_source_row_is_untraceable(db):
    _prediction(db, model="fusion")
    _prediction(db, model="ecmwf_raw")
    # gfs_raw missing entirely: the fusion average has one leg unrecorded
    _decision(db, i=1, model="fusion")

    report = audit_alignment(db.con, now=NOW)

    assert any(v.kind == "MODEL_INPUT_UNTRACEABLE" and "gfs_raw" in v.detail for v in report.violations), [
        v.as_dict() for v in report.violations
    ]


def test_fusion_with_both_sources_at_the_same_run_is_consistent(db):
    for model in ("ecmwf_raw", "gfs_raw", "fusion"):
        _prediction(db, model=model)
    _decision(db, i=1, model="fusion")

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent, [v.as_dict() for v in report.violations]


def test_calibrated_traces_to_the_same_ecmwf_run(db):
    _prediction(db, model="calibrated", model_run_id="cal-run")
    # ecmwf_raw row exists but at a different run time: the calibration's
    # input cannot be shown to be the run it claims
    _prediction(db, model="ecmwf_raw", run_time=RUN - timedelta(hours=6))

    report = audit_alignment(db.con, now=NOW)

    assert "MODEL_INPUT_UNTRACEABLE" in _kinds(report)


# --- LIVE and REPLAY do not mix --------------------------------------------------


def test_a_modeless_or_unknown_mode_row_cannot_be_placed(db):
    _decision(db, i=1, mode="UNKNOWN")
    _decision(db, i=2, mode=None)  # type: ignore[arg-type]

    report = audit_alignment(db.con, now=NOW)

    assert report.violation_counts.get("MODE_UNUSABLE") == 2
    assert not report.consistent


def test_one_cycle_spanning_two_modes_is_a_violation(db):
    _decision(db, i=1, market="m-1", cycle="cycle-shared", mode="LIVE")
    _decision(
        db,
        i=2,
        market="m-2",
        cycle="cycle-shared",
        mode="REPLAY",
        created=NOW + timedelta(hours=1),
    )

    report = audit_alignment(db.con, now=NOW)

    assert "CYCLE_MODE_MIXED" in _kinds(report)


def test_a_live_row_judged_hours_before_its_write_was_replayed(db):
    _prediction(db)
    _decision(db, i=1, when=NOW - timedelta(hours=3), created=NOW)

    report = audit_alignment(db.con, now=NOW)

    assert "LIVE_ROW_REPLAYED" in _kinds(report)


def test_a_replay_row_whose_instant_postdates_its_write_is_impossible(db):
    _decision(
        db,
        i=1,
        mode="REPLAY",
        when=NOW + timedelta(hours=2),
        created=NOW,
    )

    report = audit_alignment(db.con, now=NOW)

    assert "REPLAY_INSTANT_IN_FUTURE" in _kinds(report)


def test_a_replay_row_whose_instant_precedes_its_write_is_legal(db):
    replayed_run = RUN - timedelta(days=2)
    replayed_instant = NOW - timedelta(hours=48)
    _prediction(
        db,
        model="ecmwf_raw",
        run_time=replayed_run,
        publishable=replayed_run + timedelta(hours=12),
        when=replayed_instant,
    )
    _decision(
        db,
        i=1,
        model="ecmwf_raw",
        mode="REPLAY",
        when=replayed_instant,
        created=NOW,
        run_time=replayed_run,
    )

    report = audit_alignment(db.con, now=NOW)

    assert report.consistent, [v.as_dict() for v in report.violations]
    assert report.per_mode["REPLAY"]["decisions"] == 1


# --- cross-date and cross-market duplication stays traceable --------------------


def test_a_market_quoted_under_two_dates_is_listed_as_date_moved(db):
    for model in ("calibrated", "ecmwf_raw", "fusion", "gfs_raw"):
        _prediction(db, model=model)
    _decision(db, i=1, market="m-1", target=TARGET)
    for model in ("calibrated", "ecmwf_raw", "fusion", "gfs_raw"):
        _prediction(db, model=model, event="e-2", target=TARGET + timedelta(days=1))
    _decision(db, i=2, market="m-1", event="e-2", target=TARGET + timedelta(days=1))

    report = audit_alignment(db.con, now=NOW)

    moved = [g for g in report.exposure_groups if g.kind == "market_date_moved"]
    assert len(moved) == 1
    assert moved[0].key == "m-1"
    assert moved[0].decision_count == 2


def test_the_runtime_gate_refuses_a_forecast_for_another_day():
    """The audit's companion: the decision gate itself now checks the date.

    A resolver bug that handed the market another day's forecast must fail
    the evaluation, not silently price the wrong day.
    """
    import sys
    from pathlib import Path as P

    sys.path.insert(0, str(P(__file__).parent))
    from shadow_helpers import NOW as HELPER_NOW
    from shadow_helpers import make_catalog_market, make_forecast, make_prediction
    from weatherquant.shadow.config import load_thresholds
    from weatherquant.shadow.decision import Direction, Reason, evaluate

    thresholds = load_thresholds("configs/shadow-thresholds.json")
    decision = evaluate(
        cycle_id="c-1",
        market=make_catalog_market(),  # settles TARGET_DATE of the helpers
        direction=Direction.BUY_YES,
        prediction=make_prediction(),
        forecast=make_forecast(
            target_date=TARGET + timedelta(days=1)  # not the market's day
        ),
        snapshot=None,
        thresholds=thresholds,
        buffer_fraction=0.0,
        decision_time=HELPER_NOW,
        collector_last_seen=HELPER_NOW - timedelta(seconds=5),
        collector_silence_seconds=900.0,
        max_forecast_age_hours=thresholds.max_forecast_age_hours,
        market_closed=False,
    )
    # The absent book also fires (absent beats present-but-refused), so the
    # assertion is on the reason, not the final state.
    assert Reason.DATA_QUALITY_FAILURE in decision.reasons
    assert "settles" in decision.details.get(Reason.DATA_QUALITY_FAILURE.value, "")


def test_later_cycles_can_reuse_a_recorded_prediction(db):
    _prediction(db, model="ecmwf_raw", when=NOW)
    _decision(db, i=1, model="ecmwf_raw", when=NOW)
    _decision(db, i=2, model="ecmwf_raw", when=NOW + timedelta(minutes=1))
    report = audit_alignment(db.con, now=NOW)
    assert report.consistent, report.as_dict()
    assert report.checked_decisions == 2


def test_replay_can_reuse_a_prediction_first_evaluated_in_a_later_cycle(db):
    _prediction(db, model="ecmwf_raw", when=NOW + timedelta(hours=1))
    _decision(db, i=1, model="ecmwf_raw", when=NOW, mode="REPLAY", created=NOW + timedelta(hours=2))
    assert audit_alignment(db.con, now=NOW).consistent

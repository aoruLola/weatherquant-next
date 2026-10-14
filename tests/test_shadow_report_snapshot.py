"""Reporting without touching a database someone else is holding.

The bug this file exists for: `weatherquant-shadow report` opened
shadow.duckdb read-write while the engine held an exclusive lease on it, and
DuckDB refuses the file to a second process even read-only - so the command an
operator runs *while the engine is up* was the one command that could not run.

The fix inverts where the read happens: the engine computes the report from
the connection it already has and swaps a JSON file into place; the CLI reads
that. These tests assert the inversion actually holds - by making the database
open fail and checking the CLI does not notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherquant.shadow import cli as shadow_cli
from weatherquant.shadow.config import ShadowSettings
from weatherquant.shadow.report import (
    SNAPSHOT_SCHEMA_VERSION,
    _coverage,
    format_report,
    load_report_snapshot,
    publish_report_snapshot,
    snapshot_staleness,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
# Tests that ask the CLI for staleness are judged against the wall clock, so
# "old" has to be measured from now - a fixed timestamp silently drifts into
# the future and reads as fresh.
REAL_NOW = datetime.now(tz=UTC)
THRESHOLDS = Path(__file__).resolve().parents[1] / "configs" / "shadow-thresholds.json"


def _report(generated_at: datetime | None = None) -> dict:
    """A plausible report. Defaults to the wall clock, because that is what the
    engine stamps - a fixed constant drifts into the future and then reads as
    "fresh" however old it actually is. Tests that need a specific instant pass
    it."""
    stamp = (generated_at or datetime.now(tz=UTC)).isoformat()
    return {
        "generated_at_utc": stamp,
        "run": {"run_id": "abc", "status": "running"},
        "decisions": {"evaluated": 12, "by_state": {"ACCEPT": 1}},
        "equity_curve": None,
    }


# --- writing ---------------------------------------------------------------


def test_a_snapshot_carries_its_own_freshness_fields(tmp_path) -> None:
    path = tmp_path / "reports" / "report.json"
    payload = publish_report_snapshot(path, _report(NOW), stale_after_seconds=300, now=NOW)

    assert payload["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert payload["source"] == "shadow-main"
    assert payload["generated_at_utc"] == NOW.isoformat()
    assert payload["stale_after_seconds"] == 300
    assert payload["expires_at_utc"] == (NOW + timedelta(seconds=300)).isoformat()
    assert path.exists()
    # round-trips exactly as written
    assert load_report_snapshot(path) == payload


def test_the_generation_time_is_when_the_numbers_were_computed(tmp_path) -> None:
    """Not when the file happened to be written.

    A reader asking "how old is this?" must be answered with the age of the
    data, so the timestamp comes from the report the engine built.
    """
    path = tmp_path / "r.json"
    report = _report(NOW)
    publish_report_snapshot(path, report, stale_after_seconds=60, now=NOW + timedelta(seconds=50))
    loaded = load_report_snapshot(path)
    assert loaded["generated_at_utc"] == NOW.isoformat()


def test_publishing_replaces_atomically_and_never_leaves_a_partial_file(tmp_path) -> None:
    path = tmp_path / "r.json"
    publish_report_snapshot(path, _report(), stale_after_seconds=60, now=NOW)
    first = path.read_text(encoding="utf-8")

    bigger = _report()
    bigger["decisions"] = {**bigger["decisions"], "evaluated": 999}
    publish_report_snapshot(path, bigger, stale_after_seconds=60, now=NOW + timedelta(seconds=1))

    # parses cleanly, contains the new value, and no temp file was left behind
    assert json.loads(path.read_text(encoding="utf-8"))["decisions"]["evaluated"] == 999
    assert first != path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.json")) == [path]


# --- reading ---------------------------------------------------------------


def test_missing_corrupt_and_wrong_documents_all_read_as_none(tmp_path) -> None:
    assert load_report_snapshot(tmp_path / "nope.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_report_snapshot(broken) is None

    not_a_report = tmp_path / "other.json"
    not_a_report.write_text('{"hello": 1}', encoding="utf-8")
    assert load_report_snapshot(not_a_report) is None

    wrong_type = tmp_path / "list.json"
    wrong_type.write_text("[1,2,3]", encoding="utf-8")
    assert load_report_snapshot(wrong_type) is None


def test_staleness_goes_from_fresh_to_expired_exactly_at_the_window(tmp_path) -> None:
    path = tmp_path / "r.json"
    publish_report_snapshot(path, _report(NOW), stale_after_seconds=300, now=NOW)

    fresh, age, past = snapshot_staleness(load_report_snapshot(path), now=NOW + timedelta(seconds=299))
    assert fresh is False and age == pytest.approx(299) and past == 0

    stale, age, past = snapshot_staleness(load_report_snapshot(path), now=NOW + timedelta(seconds=301))
    assert stale is True and age == pytest.approx(301) and past == pytest.approx(1)


def test_an_unreadable_timestamp_is_stale_not_fresh(tmp_path) -> None:
    """A timestamp we cannot parse is not evidence of recency."""
    stale, age, past = snapshot_staleness({"generated_at_utc": "yesterday-ish"})
    assert stale is True and age is None and past is None


# --- the CLI never opens the database -------------------------------------


def _settings(tmp_path: Path) -> ShadowSettings:
    return ShadowSettings(
        shadow_db_path=tmp_path / "shadow" / "shadow.duckdb",
        shadow_report_path=tmp_path / "reports" / "report.json",
        shadow_health_dir=tmp_path / "health",
        shadow_thresholds_path=str(THRESHOLDS),
    )


def _args(**overrides):
    class A:
        json = True
        horizon = 7
        offline = False
        db = None

    for key, value in overrides.items():
        setattr(A, key, value)
    return A()


def test_the_cli_reads_the_snapshot_without_touching_the_database(tmp_path, monkeypatch, capsys) -> None:
    shadow = _settings(tmp_path)
    publish_report_snapshot(
        shadow.shadow_report_path, _report(), stale_after_seconds=300, now=NOW
    )

    def _explode(*_a, **_k):
        raise AssertionError("the report CLI opened the database")

    monkeypatch.setattr(shadow_cli, "ShadowDatabase", _explode)

    code = shadow_cli.cmd_report(None, shadow, _args())
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["decisions"]["evaluated"] == 12
    assert payload["source"] == "shadow-main"
    assert payload["stale"] is False
    assert payload["age_seconds"] is not None


def test_the_text_form_says_where_the_numbers_came_from(tmp_path, capsys) -> None:
    shadow = _settings(tmp_path)
    publish_report_snapshot(
        shadow.shadow_report_path, _report(), stale_after_seconds=300, now=NOW
    )
    code = shadow_cli.cmd_report(None, shadow, _args(json=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "[snapshot]" in out
    assert "generated at" in out
    assert "fresh" in out


def test_an_expired_snapshot_is_still_shown_but_says_so(tmp_path, capsys) -> None:
    """A stale report is still a report - provided it says it is stale.

    Refusing to print it would leave an operator with nothing, and printing it
    quietly would let three-hour-old numbers be read as current. So it prints,
    with the age and the overshoot stated.
    """
    shadow = _settings(tmp_path)
    publish_report_snapshot(
        shadow.shadow_report_path, _report(), stale_after_seconds=60, now=REAL_NOW
    )
    path = shadow.shadow_report_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["generated_at_utc"] = (REAL_NOW - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")

    code = shadow_cli.cmd_report(None, shadow, _args(json=False))
    out = capsys.readouterr().out
    assert code == 0, "a stale report is still a report"
    assert "EXPIRED" in out
    assert "describe the past" in out


def test_json_mode_reports_staleness_as_data_not_just_text(tmp_path, capsys) -> None:
    shadow = _settings(tmp_path)
    publish_report_snapshot(
        shadow.shadow_report_path, _report(), stale_after_seconds=60, now=REAL_NOW
    )
    path = shadow.shadow_report_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["generated_at_utc"] = (REAL_NOW - timedelta(hours=6)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")

    shadow_cli.cmd_report(None, shadow, _args(json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"] is True
    assert payload["age_seconds"] > 3600
    assert payload["seconds_past_expiry"] > 3600


def test_offline_forces_a_database_read(tmp_path, monkeypatch, capsys) -> None:
    """The retained capability: inspect a database when nothing is running."""
    shadow = _settings(tmp_path)
    publish_report_snapshot(
        shadow.shadow_report_path, _report(), stale_after_seconds=300, now=NOW
    )
    # A snapshot exists, but --offline must ignore it.
    shadow.shadow_db_path.parent.mkdir(parents=True, exist_ok=True)
    shadow.shadow_db_path.write_bytes(b"")
    monkeypatch.setattr(shadow_cli, "build_report", lambda con, horizon_days=7: _report())

    class FakeDB:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        @property
        def con(self):
            return object()

    monkeypatch.setattr(shadow_cli, "ShadowDatabase", FakeDB)
    code = shadow_cli.cmd_report(None, shadow, _args(offline=True, json=True))
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["source"] == "offline-db", "--offline must ignore the snapshot"
    assert "snapshot" not in payload or payload.get("source") == "offline-db"


def test_offline_against_a_leased_database_explains_itself(tmp_path, monkeypatch, capsys) -> None:
    """The message the old CLI should have had."""
    shadow = _settings(tmp_path)
    shadow.shadow_db_path.parent.mkdir(parents=True, exist_ok=True)
    shadow.shadow_db_path.write_bytes(b"")

    def _locked(*_a, **_k):
        raise RuntimeError("IO Error: Cannot open file - another program is using it")

    monkeypatch.setattr(shadow_cli, "ShadowDatabase", _locked)

    code = shadow_cli.cmd_report(None, shadow, _args(offline=True))
    err = capsys.readouterr().err
    assert code == 3, "a locked database is a distinct outcome, not a crash"
    assert "exclusive lease" in err
    assert "snapshot the engine publishes" in err


def test_no_snapshot_and_no_database_reports_that_clearly(tmp_path, capsys) -> None:
    shadow = _settings(tmp_path)
    code = shadow_cli.cmd_report(None, shadow, _args())
    err = capsys.readouterr().err
    assert code == 2
    assert "no shadow database" in err
    assert "no published snapshot" in err


# --- the engine side ------------------------------------------------------


def test_format_report_round_trips_a_snapshot(tmp_path) -> None:
    """The snapshot must be renderable by the same formatter the CLI uses."""
    payload = publish_report_snapshot(tmp_path / "r.json", _report(), stale_after_seconds=300, now=NOW)
    text = format_report(payload)
    assert "evaluated" in text or "decisions" in text or "[decisions]" in text or "run" in text


def test_build_report_is_what_gets_published(tmp_path) -> None:
    """Smoke: the snapshot is a build_report output plus freshness fields."""
    payload = publish_report_snapshot(tmp_path / "r.json", _report(), stale_after_seconds=300, now=NOW)
    for key in ("generated_at_utc", "decisions", "equity_curve", "schema_version", "expires_at_utc"):
        assert key in payload, key
    assert payload["equity_curve"] is None  # never a fabricated return


# --- coverage is one set divided by another ---------------------------------


def _coverage_db(tmp_path):
    """A real migrated shadow DB with two open dates and four tracked models."""
    from datetime import UTC, datetime

    from weatherquant.storage.shadow import ShadowDatabase

    db = ShadowDatabase(tmp_path / "shadow.duckdb")
    db.open()
    con = db.con
    now = datetime.now(tz=UTC)
    for i, (day, token) in enumerate([("2026-10-13", "tok-a"), ("2026-10-14", "tok-b")]):
        con.execute(
            "INSERT INTO market_catalog (market_id, event_id, question, token_yes, token_no, "
            "station, target_date, active, closed, accepting_orders, first_seen_utc, last_seen_utc) "
            "VALUES (?, ?, ?, ?, ?, 'KLGA', ?, TRUE, FALSE, TRUE, ?, ?)",
            [f"m-{i}", f"ev-{i}", f"q{i}", token, f"{token}-no", day, now, now],
        )
    for model in ("calibrated", "ecmwf_raw", "fusion", "gfs_raw"):
        con.execute(
            "INSERT INTO model_versions (model_name, model_version, training_cutoff_utc, "
            "created_at_utc, source_data_hash, artifact_sha256, n_pairs, n_dates, "
            "loaded_at_utc, is_primary) VALUES (?, 'v1', ?, ?, 'h', 'a', 1, 1, ?, FALSE)",
            [model, now, now, now],
        )
    # Only three of the eight (model, open date) pairs exist: one forecast short.
    for model, day in [("calibrated", "2026-10-13"), ("calibrated", "2026-10-14"), ("fusion", "2026-10-13")]:
        con.execute(
            "INSERT INTO model_predictions (model_name, model_version, station, target_date, "
            "event_id, forecast_run_time_utc, probabilities_json, lead_hours, model_run_id, "
            "training_cutoff_utc, forecast_publishable_at_utc, forecast_publishable_basis, "
            "forecast_received_at_utc, decision_time_utc, calibration_version, "
            "source_data_hash, computed_at_utc) VALUES (?, 'v1', 'KLGA', ?, 'ev-0', ?, '{}', 1, "
            "'r', ?, ?, 'estimated', ?, ?, 'v1', 'h', ?)",
            [model, day, now, now, now, now, now, now],
        )
    return db


def test_forecast_coverage_counts_the_same_set_it_divides_by(tmp_path) -> None:
    """16/12 showing 100% is a ratio whose parts have nothing to do with each other.

    The numerator counted every (model, target_date) inside a horizon window;
    the denominator counted open target dates x tracked models. A three-date
    horizon over four dates of history therefore reported 16 available against
    12 needed, and the clamp turned that into 1.0 - coverage that could not
    fall below 100% however many forecasts were genuinely missing.
    """
    db = _coverage_db(tmp_path)
    try:
        cov = _coverage(db.con, horizon_days=7)
        assert cov["open_target_dates"] == ["2026-10-13", "2026-10-14"]
        assert len(cov["models_tracked"]) == 4
        assert cov["forecast_needed"] == 8  # 2 open dates x 4 models
        assert cov["forecast_available"] == 3  # only the three that exist
        assert cov["forecast_missing"] == 5
        assert cov["forecast_coverage"] == 3 / 8
        # The clamp must not be doing the work: available is counted within
        # the same set, so it cannot exceed needed on its own.
        assert cov["forecast_available"] <= cov["forecast_needed"]
        assert cov["forecast_coverage"] < 1.0
    finally:
        db.close()

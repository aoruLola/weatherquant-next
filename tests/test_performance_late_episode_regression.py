import json
from datetime import UTC, datetime, timedelta

import pytest

from weatherquant.performance.episodes import EndReason, build_episodes, episode_from_open
from weatherquant.performance.state import PerformanceStateStore
from weatherquant.performance.streaming import ingest_export


def _write_part(root, index, rows):
    parts = root / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    (parts / f"part-{index:09d}.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def _rows():
    start = datetime(2026, 10, 26, tzinfo=UTC)
    return [
        dict(
            decision_id=f"d-{seq}",
            export_seq=seq,
            market_id="m",
            target_date="2026-10-26",
            model_name="fusion",
            direction="buy_yes",
            mode="REPLAY",
            state=state,
            net_edge=0.1,
            decision_time_utc=(start + timedelta(minutes=minute)).isoformat(),
        )
        for seq, minute, state in [(1, 0, "ACCEPT"), (2, 2, "ACCEPT"), (3, 1, "REJECT")]
    ]


def _assert_chronological(store, rows):
    expected, _ = build_episodes(rows)
    actual = [
        episode_from_open(item.open_ep, end_reason=item.end_reason or EndReason.END_OF_DATA).as_dict()
        for item in store.iter_episodes()
    ]
    assert actual == [item.as_dict() for item in expected]
    assert len(actual) == 2


def test_late_reject_rebuilds_the_same_episodes_as_business_time_order(tmp_path):
    export = tmp_path / "export"
    rows = _rows()
    _write_part(export, 1, rows[:2])
    state_db = tmp_path / "state.duckdb"
    with PerformanceStateStore(state_db) as store:
        ingest_export(store, export)
    _write_part(export, 2, rows[2:])
    with PerformanceStateStore(state_db) as store:
        ingest_export(store, export)
        _assert_chronological(store, rows)
        assert store.load_state().rows_consumed == 3
        ingest_export(store, export)
        _assert_chronological(store, rows)


def test_rebuild_failure_keeps_a_durable_retry_marker(tmp_path, monkeypatch):
    from weatherquant.performance import streaming

    export = tmp_path / "export"
    rows = _rows()
    _write_part(export, 1, rows)
    state_db = tmp_path / "state.duckdb"
    real = streaming._ensure_chronological_episodes

    def crash(store):
        raise RuntimeError("crash after ingest commit")

    with PerformanceStateStore(state_db) as store:
        monkeypatch.setattr(streaming, "_ensure_chronological_episodes", crash)
        with pytest.raises(RuntimeError, match="crash after ingest"):
            ingest_export(store, export)
    monkeypatch.setattr(streaming, "_ensure_chronological_episodes", real)
    with PerformanceStateStore(state_db) as store:
        assert store.load_state().rows_consumed == 3
        ingest_export(store, export)
        _assert_chronological(store, rows)
        assert store.fetchone("SELECT required FROM performance_episode_rebuild")[0] is False


def test_existing_state_is_repaired_after_the_chronological_migration(tmp_path):
    export = tmp_path / "export"
    rows = _rows()
    _write_part(export, 1, rows)
    db = tmp_path / "state.duckdb"
    with PerformanceStateStore(db) as store:
        ingest_export(store, export)
        store.execute("DELETE FROM performance_episodes WHERE is_open")
        store.execute("UPDATE performance_ingest SET algorithm_version='performance-streaming-1'")
        store.execute("DROP TABLE performance_episode_rebuild")
        store.execute("DELETE FROM schema_migrations WHERE version='0004'")
    with PerformanceStateStore(db) as store:
        assert store.fetchone("SELECT required FROM performance_episode_rebuild")[0]
        ingest_export(store, export)
        _assert_chronological(store, rows)
        assert store.load_state().algorithm_version == "performance-streaming-2"
        assert store.load_state().rows_consumed == 3

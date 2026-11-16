import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def soak(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import soak_memory

    return soak_memory


@pytest.fixture
def reports(tmp_path, monkeypatch, soak):
    hb = {"status": "stopped", "stats": {"books_rows_inserted": 1, "price_points_inserted": 1}}
    monkeypatch.setattr(soak, "read_heartbeat", lambda path: hb)
    monkeypatch.setattr(
        "weatherquant.data.heartbeat.evaluate_health",
        lambda path: SimpleNamespace(status=SimpleNamespace(value="stopped"), exit_code=0),
    )
    samples = [soak.Sample(i, 100, 110, 0, 0, "healthy", 1, 1) for i in range(20)]
    collection = soak.PhaseReport("collection", duration_s=20, samples=samples)
    backup = soak.PhaseReport("backup", samples=[soak.Sample(1, 100, 110, 0, 0, "backup", 1, 1)])
    gates = {"collection_seconds": 20, "max_rss_mb": 3072, "max_slope_mb_per_min": 8, "max_psi_full": 2}
    return collection, backup, {"verified": True, "bytes": 1024, "hwm_mb": 110}, tmp_path, gates


def test_clean_collection_and_verified_backup_pass(soak, reports):
    assert soak._report(*reports) == 0


@pytest.mark.parametrize("rss, hwm, psi", [(8000, 8000, 0), (100, 8000, 0), (100, 100, 90)])
def test_backup_resource_limit_cannot_pass_with_a_clean_collection(soak, reports, rss, hwm, psi):
    _, backup, summary, _, _ = reports
    backup.samples[0] = soak.Sample(0, rss, hwm, 0, psi, "backup", 1, 1)
    summary["hwm_mb"] = hwm
    assert soak._report(*reports) == 1


def test_early_exit_and_inadequate_sampling_cannot_pass(soak, reports, capsys):
    collection = reports[0]
    collection.events.append("collector exit code -9")
    collection.duration_s = 3
    del collection.samples[3:]
    assert soak._report(*reports) == 1
    out = capsys.readouterr().out
    assert "requested observation window" in out
    assert "RSS slope is unmeasured" in out
    assert "collector exit code -9" in out


def test_between_sample_collection_peak(soak, reports):
    reports[0].samples[-1].hwm_mb = 4000
    assert soak._report(*reports) == 1


def test_missing_market_data_fails(soak, reports, monkeypatch):
    monkeypatch.setattr(soak, "read_heartbeat", lambda path: {"status": "stopped", "stats": {"books_rows_inserted": 1}})
    assert soak._report(*reports) == 1


def test_psi_requires_both_some_and_full(soak, tmp_path, monkeypatch):
    path = tmp_path / "pressure"
    monkeypatch.setattr(soak, "Path", lambda name: path)
    with pytest.raises(FileNotFoundError):
        soak.read_psi()
    path.write_text("some avg10=0.05 avg60=2\n")
    with pytest.raises(ValueError, match="some/full"):
        soak.read_psi()
    with path.open("a") as stream:
        stream.write("full avg10=3.14 avg60=2\n")
    assert soak.read_psi() == (0.05, 3.14)


def test_existing_data_is_preserved(soak, tmp_path, monkeypatch):
    sentinel = tmp_path / "weatherquant.duckdb"
    sentinel.write_bytes(b"existing")
    monkeypatch.setattr(soak, "read_psi", lambda: (0, 0))
    monkeypatch.setattr(sys, "argv", ["soak", "--data", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        soak.main()
    assert exc.value.code == 2
    assert sentinel.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "minutes, limit, swap, error",
    [
        ("719", "6442450944", "0", "720 minutes"),
        ("720", "max", "0", "memory.max"),
        ("720", "6442450944", "max", "memory.swap.max"),
    ],
)
def test_acceptance_requires_full_window_and_container_budget(
    soak, tmp_path, monkeypatch, capsys, minutes, limit, swap, error
):
    from weatherquant.storage import db

    monkeypatch.setattr(db, "DEFAULT_MEMORY_LIMIT", "3GB")
    monkeypatch.setattr(db, "DEFAULT_THREADS", "4")
    monkeypatch.setattr(soak, "CGROUP_ROOT", tmp_path)
    (tmp_path / "memory.max").write_text(limit)
    (tmp_path / "memory.swap.max").write_text(swap)
    monkeypatch.setattr(sys, "argv", ["soak", "--data", str(tmp_path / "new"), "--acceptance", "--minutes", minutes])
    with pytest.raises(SystemExit) as exc:
        soak.main()
    assert exc.value.code == 2
    assert error in capsys.readouterr().err
    assert not (tmp_path / "new").exists()


def test_samples_pressure_during_backup(soak, tmp_path, monkeypatch):
    from weatherquant.data.writer import CollectorWriter

    started = threading.Event()
    sampled = threading.Event()
    backup = CollectorWriter.backup

    def measured_backup(writer, destination):
        started.set()
        assert sampled.wait(5)
        return backup(writer, destination)

    def kernel_psi():
        if started.is_set():
            sampled.set()
            return 4, 3
        return 0, 0

    monkeypatch.setattr(CollectorWriter, "backup", measured_backup)
    monkeypatch.setattr(soak, "read_psi", kernel_psi)
    monkeypatch.setattr(soak, "read_rss_mb", lambda pid: (100, 110))
    report, summary = soak.run_backup_cycle(tmp_path / "weatherquant.duckdb", tmp_path, 1, 2)
    assert summary["verified"]
    assert report.peak_psi_full() == 3


# ---------------------------------------------------------------------------
# End to end through main(): the exit code and the report agree, and the
# message names the gate that was crossed. The collection/backup phases are
# injected, so no real collection runs and no memory is stressed.
# ---------------------------------------------------------------------------


def _run_cli(soak, tmp_path, monkeypatch, *, collection, backup, summary, extra=()):
    data = tmp_path / "soak"

    def fake_collection(data_dir, minutes, interval):
        (data_dir / "weatherquant.duckdb").write_bytes(b"")
        return collection

    def fake_backup_cycle(db_path, data_dir, batches, rows, *, flush_rows=500):
        return backup, summary

    monkeypatch.setattr(soak, "run_collection", fake_collection)
    monkeypatch.setattr(soak, "run_backup_cycle", fake_backup_cycle)
    monkeypatch.setattr(soak, "read_psi", lambda: (0.0, 0.0))
    monkeypatch.setattr(
        soak,
        "read_heartbeat",
        lambda path: {"status": "stopped", "stats": {"books_rows_inserted": 1, "price_points_inserted": 1}},
    )
    monkeypatch.setattr(
        "weatherquant.data.heartbeat.evaluate_health",
        lambda path: SimpleNamespace(status=SimpleNamespace(value="stopped"), exit_code=0),
    )
    monkeypatch.setattr(sys, "argv", ["soak", "--data", str(data), "--minutes", "1", *extra])
    return soak.main(), data


def _clean_reports(soak, *, rss=100.0, hwm=100.0):
    samples = [
        soak.Sample(at=i * 3.0, rss_mb=rss, hwm_mb=hwm, psi_some=0.0, psi_full=0.0, status="healthy", books=1, prices=1)
        for i in range(20)
    ]
    collection = soak.PhaseReport("collection", duration_s=60.0, samples=samples)
    backup = soak.PhaseReport("backup", samples=[soak.Sample(0, rss, hwm, 0.0, 0.0, "backup", 1, 1)])
    return collection, backup


def test_cli_passes_when_the_peak_equals_the_gate(soak, tmp_path, monkeypatch, capsys):
    collection, backup = _clean_reports(soak, rss=100.0, hwm=100.0)
    summary = {"verified": True, "bytes": 1024, "hwm_mb": 100.0}
    code, _ = _run_cli(
        soak,
        tmp_path,
        monkeypatch,
        collection=collection,
        backup=backup,
        summary=summary,
        extra=["--max-rss-mb", "100"],
    )
    assert code == 0
    assert "RESULT: PASSED" in capsys.readouterr().out


def test_cli_fails_above_the_rss_gate_and_names_it(soak, tmp_path, monkeypatch, capsys):
    collection, backup = _clean_reports(soak, rss=101.0, hwm=101.0)
    summary = {"verified": True, "bytes": 1024, "hwm_mb": 101.0}
    code, _ = _run_cli(
        soak,
        tmp_path,
        monkeypatch,
        collection=collection,
        backup=backup,
        summary=summary,
        extra=["--max-rss-mb", "100"],
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "RESULT: FAILED" in out
    assert "peak RSS 101 MiB above the gate 100.0 MiB" in out


def test_cli_fails_when_the_memory_samples_are_empty(soak, tmp_path, monkeypatch, capsys):
    collection = soak.PhaseReport("collection", duration_s=60.0, samples=[])
    backup = soak.PhaseReport("backup", samples=[])
    code, _ = _run_cli(
        soak,
        tmp_path,
        monkeypatch,
        collection=collection,
        backup=backup,
        summary={"verified": True, "bytes": 1024, "hwm_mb": 0.0},
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "fewer than ten second-half samples" in out
    assert "missing process RSS measurement" in out


def test_cli_fails_when_the_backup_metrics_are_missing(soak, tmp_path, monkeypatch, capsys):
    collection, backup = _clean_reports(soak)
    backup.samples = []
    code, _ = _run_cli(soak, tmp_path, monkeypatch, collection=collection, backup=backup, summary={})
    out = capsys.readouterr().out
    assert code == 1
    assert "missing process RSS measurement" in out
    assert "backup did not verify" in out


def test_cli_fails_when_a_verified_backup_wrote_no_bytes(soak, tmp_path, monkeypatch, capsys):
    collection, backup = _clean_reports(soak)
    code, _ = _run_cli(
        soak,
        tmp_path,
        monkeypatch,
        collection=collection,
        backup=backup,
        summary={"verified": True, "bytes": 0, "hwm_mb": 100.0},
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "wrote zero bytes" in out


def test_cli_refuses_the_production_data_directory(soak, tmp_path, monkeypatch, capsys):
    production = tmp_path / "data"
    monkeypatch.setattr(soak, "PRODUCTION_DATA_DIR", production)
    monkeypatch.delenv("WQ_DB_PATH", raising=False)
    monkeypatch.setattr(soak, "read_psi", lambda: (0.0, 0.0))
    monkeypatch.setattr(sys, "argv", ["soak", "--data", str(production / "soak")])
    with pytest.raises(SystemExit) as exc:
        soak.main()
    assert exc.value.code == 2
    assert "production data directory" in capsys.readouterr().err
    assert not production.exists()

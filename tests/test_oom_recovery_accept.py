import os
import subprocess
import sys
from pathlib import Path

import pytest

from weatherquant.storage import connect


@pytest.fixture
def oom(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import oom_recovery_accept

    return oom_recovery_accept


def test_real_wal_replay_and_one_batch_continuation(oom, tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DUCKDB_MEMORY_LIMIT", "128MB")
    monkeypatch.setenv("WQ_DUCKDB_TEMP_DIR", str(tmp_path / "spill"))
    script = """
import os, sys
from pathlib import Path
from scripts.oom_recovery_accept import run_child
fsync = os.fsync
def crash_after_journal(fd):
    fsync(fd)
    os._exit(77)
os.fsync = crash_after_journal
run_child(Path(sys.argv[1]), 16, 5, resume_once=True)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=oom.PROJECT_ROOT, timeout=45)
    assert result.returncode == 77
    db = tmp_path / "weatherquant.duckdb"
    wal = Path(str(db) + ".wal")
    assert wal.stat().st_size > 0
    assert oom._db_state(db, 5) == (1, 5, [1])
    assert not wal.exists() or wal.stat().st_size == 0
    assert oom.run_child(tmp_path, 16, 5, resume_once=True) == 0
    assert oom._db_state(db, 5) == (2, 10, [1, 2])

    with connect(db) as con:
        con.execute("UPDATE oom_batches SET seq=1 WHERE batch_id=1 AND seq=2")
    with pytest.raises(ValueError, match="duplicated or torn"):
        oom._db_state(db, 5)


def test_a_kill_mid_transaction_rolls_back_and_continues(oom, tmp_path, monkeypatch):
    """An OOM kill inside a transaction must not leave half a batch behind."""
    monkeypatch.setenv("WQ_DUCKDB_MEMORY_LIMIT", "128MB")
    monkeypatch.setenv("WQ_DUCKDB_TEMP_DIR", str(tmp_path / "spill"))
    script = """
import os, sys
from pathlib import Path
from weatherquant.storage import connect
from scripts.oom_recovery_accept import run_child
data = Path(sys.argv[1])
assert run_child(data, 16, 5, resume_once=True) == 0
con = connect(data / "weatherquant.duckdb")
con.execute("BEGIN TRANSACTION")
con.executemany(
    "INSERT INTO oom_batches (batch_id, seq, payload) VALUES (?, ?, ?)",
    [(2, i, "torn") for i in range(5)],
)
os._exit(88)  # exactly what SIGKILL does: no commit, no rollback, no shutdown
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=oom.PROJECT_ROOT, timeout=45)
    assert result.returncode == 88
    db = tmp_path / "weatherquant.duckdb"
    assert oom._db_state(db, 5) == (1, 5, [1])

    assert oom.run_child(tmp_path, 16, 5, resume_once=True) == 0
    assert oom._db_state(db, 5) == (2, 10, [1, 2])
    with connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM oom_batches WHERE batch_id = 2").fetchone()[0] == 5


def test_parent_preserves_existing_data(oom, tmp_path, monkeypatch, capsys):
    db = tmp_path / "weatherquant.duckdb"
    db.write_bytes(b"existing")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(sys, "argv", ["oom", "--data", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        oom.main()
    assert exc.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert db.read_bytes() == b"existing"


def test_parent_refuses_the_production_data_directory(oom, tmp_path, monkeypatch, capsys):
    production = tmp_path / "data"
    monkeypatch.setattr(oom, "PRODUCTION_DATA_DIR", production)
    monkeypatch.delenv("WQ_DB_PATH", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(sys, "argv", ["oom", "--data", str(production / "drill")])
    with pytest.raises(SystemExit) as exc:
        oom.main()
    assert exc.value.code == 2
    assert "production data directory" in capsys.readouterr().err
    assert not production.exists()


def test_a_private_directory_is_accepted(oom, tmp_path, monkeypatch):
    monkeypatch.delenv("WQ_DB_PATH", raising=False)
    assert oom._isolation_refusal(tmp_path / "drill") is None


@pytest.mark.parametrize("resume", [False, True])
def test_child_joins_its_parents_group_before_writing(oom, tmp_path, monkeypatch, resume):
    group = tmp_path / f"wq-oom-accept-{os.getppid()}"
    group.mkdir()
    monkeypatch.setattr(oom, "CGROUP_ROOT", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["oom", "--child", "--data", str(tmp_path), *(["--resume-once"] if resume else [])]
    )

    def writer(data_dir, row_bytes, rows_per_batch, *, resume_once):
        assert (group / "cgroup.procs").read_text() == str(os.getpid())
        assert resume_once is resume
        return 0

    monkeypatch.setattr(oom, "run_child", writer)
    assert oom.main() == 0

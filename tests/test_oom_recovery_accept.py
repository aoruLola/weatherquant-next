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


def test_parent_preserves_existing_data(oom, tmp_path, monkeypatch):
    db = tmp_path / "weatherquant.duckdb"
    db.write_bytes(b"existing")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(sys, "argv", ["oom", "--data", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        oom.main()
    assert exc.value.code == 2
    assert db.read_bytes() == b"existing"


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

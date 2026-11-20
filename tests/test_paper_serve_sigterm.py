"""``docker stop`` sends SIGTERM to PID 1; ``serve`` must shut down cleanly.

Without a handler the engine either dies mid-flight (as a child process) or,
as PID 1 in the container, ignores the signal until Docker SIGKILLs it (exit
137). Either way the lease and the run row are left open. This drives the real
CLI in a subprocess and requires a clean exit with the lease released.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = "from weatherquant.paper.cli import main; raise SystemExit(main())"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_stops_serve_cleanly_and_releases_the_lease(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "WQ_PAPER_ENABLED": "true",
        "WQ_PAPER_DB_PATH": str(tmp_path / "paper" / "paper.duckdb"),
        "WQ_PAPER_HEALTH_DIR": str(tmp_path / "health"),
        "WQ_PAPER_EVENTS_DIR": str(tmp_path / "events"),
        "WQ_PAPER_RAW_DIR": str(tmp_path / "raw"),
        "WQ_PAPER_RISK_RULES_PATH": str(REPO_ROOT / "configs" / "paper-risk.json"),
        "WQ_PAPER_FEE_SCHEDULE_PATH": str(REPO_ROOT / "configs" / "fee-schedule.json"),
        "WQ_PAPER_API_HOST": "127.0.0.1",
        "WQ_PAPER_API_PORT": str(_free_port()),
        "WQ_PAPER_TICK_SECONDS": "0.2",
    }
    (tmp_path / "events").mkdir()
    (tmp_path / "raw").mkdir()
    init = subprocess.run([sys.executable, "-c", CLI, "init"], env=env, capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stderr

    log_path = tmp_path / "serve.log"
    port = int(env["WQ_PAPER_API_PORT"])
    with log_path.open("w") as log_file:
        proc = subprocess.Popen([sys.executable, "-c", CLI, "serve"], env=env, stdout=log_file, stderr=subprocess.STDOUT)
    lock = tmp_path / "paper" / ".paper.lock"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            assert proc.poll() is None, f"serve exited early: {log_path.read_text()}"
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("serve never started listening")
        time.sleep(0.5)  # let at least one tick heartbeat the lease
        assert lock.exists()

        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert code == 0, f"serve exited {code} on SIGTERM"
    assert not lock.exists(), "the lease must be released on a clean stop"

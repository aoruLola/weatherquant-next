"""WQN-48: offline serve acceptance and Paper container deployment invariants."""

from __future__ import annotations

import importlib.util
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from paper_book_helpers import TOKEN, book_payload, write_archive, ws_line
from paper_helpers import export_row
from weatherquant.data.books import parse_book_payload
from weatherquant.paper.cli import main
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile.paper"
COMPOSE = ROOT / "docker" / "compose.paper.unraid.yaml"
HEALTHCHECK = ROOT / "docker" / "healthcheck_paper.py"


def _load_healthcheck():
    spec = importlib.util.spec_from_file_location("paper_container_healthcheck", HEALTHCHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _yaml_mapping(path: Path, heading: str) -> dict[str, str]:
    """Read a scalar-only mapping block without adding a YAML dependency."""
    result: dict[str, str] = {}
    active = False
    parent_indent = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if stripped == f"{heading}:":
            active = True
            parent_indent = indent
            continue
        if not active:
            continue
        if indent <= parent_indent:
            break
        if indent == parent_indent + 2 and ":" in stripped and not stripped.startswith("- "):
            key, value = stripped.split(":", 1)
            result[key] = value.strip().split(" #", 1)[0]
    return result


def _yaml_sequence(path: Path, heading: str) -> list[str]:
    """Read a scalar-only YAML sequence, including its read-only suffixes."""
    result: list[str] = []
    active = False
    parent_indent = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if stripped == f"{heading}:":
            active = True
            parent_indent = indent
            continue
        if not active:
            continue
        if indent <= parent_indent:
            break
        if stripped.startswith("- "):
            result.append(stripped[2:].strip().strip("\"'"))
    return result


def test_dockerfile_paper_contract() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "groupadd --gid 1000" in text
    assert "useradd --uid 1000" in text
    assert "USER wq" in text
    assert "configs/paper-risk.json" in text
    assert "configs/fee-schedule.json" in text
    assert "WQ_PAPER_FEE_SCHEDULE_PATH=/app/configs/fee-schedule.json" in text
    assert "docker/healthcheck_paper.py" in text
    assert 'ENTRYPOINT ["weatherquant-paper"]' in text
    assert 'CMD ["serve"]' in text
    assert "WQ_PAPER_HEALTH_DIR=/data/health" in text
    assert "HEALTHCHECK" in text


def test_compose_pins_sha_and_derives_version_metadata() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    environment = _yaml_mapping(COMPOSE, "environment")
    assert "ghcr.io/aorulola/weatherquant-next-paper:sha-${PAPER_SHA:?" in text
    assert ":paper-stable" not in text
    assert ":latest" not in text
    assert environment["GIT_SHA"].startswith("${PAPER_SHA:?")
    assert environment["IMAGE_TAG"].startswith("${PAPER_SHA:?")
    assert environment["WQ_PAPER_ENABLED"] == "${WQ_PAPER_ENABLED:-false}"


def test_compose_mounts_only_published_inputs_read_only() -> None:
    volumes = _yaml_sequence(COMPOSE, "volumes")
    assert "/mnt/user/appdata/weatherquant-paper:/data" in volumes
    assert "/mnt/user/appdata/weatherquant-shadow/reports/decisions-export:/shadow-decisions:ro" in volumes
    assert "/mnt/user/appdata/weatherquant-next/raw:/collector-raw:ro" in volumes
    assert len(volumes) == 3
    for volume in volumes:
        source = volume.split(":", 1)[0]
        if source.startswith(("/mnt/user/appdata/weatherquant-next", "/mnt/user/appdata/weatherquant-shadow")):
            assert volume.endswith(":ro"), f"upstream input is writable: {volume}"
        assert not source.endswith((".duckdb", ".duckdb.wal"))


def test_compose_resources_api_and_shutdown_contract() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    environment = _yaml_mapping(COMPOSE, "environment")
    ports = _yaml_sequence(COMPOSE, "ports")
    assert "mem_limit:" in text
    assert "cpus:" in text
    assert "stop_grace_period: 60s" in text
    assert "restart: unless-stopped" in text
    assert "driver: json-file" in text
    assert 'max-size: "20m"' in text
    assert 'max-file: "5"' in text
    assert ports == ["${PAPER_API_LAN_IP:?set PAPER_API_LAN_IP to the NAS LAN address}:8790:8790"]
    assert environment["WQ_PAPER_API_HOST"] == "0.0.0.0"


def test_release_and_development_workflows_are_paper_only() -> None:
    release = (ROOT / ".github/workflows/paper-release.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/paper-ci.yml").read_text(encoding="utf-8")
    ci_without_self_check = "\n".join(line for line in ci.splitlines() if "grep -nE" not in line)
    assert 'tags: ["paper-v*"]' in release
    assert "Dockerfile.paper" in release
    assert "${{ github.repository }}-paper" in release
    assert "paper-stable" in release
    assert "type=sha,format=long,prefix=sha-" in release
    assert "weatherquant-next-shadow:" not in release
    assert "weatherquant-next-dashboard:" not in release
    assert "ci_image_gate.sh paper" in ci
    assert "Dockerfile.paper" in ci
    assert "push: true" not in ci_without_self_check
    assert "packages: write" not in ci_without_self_check
    assert "docker/login-action" not in ci_without_self_check


def test_native_heartbeat_is_preferred_and_staleness_is_unhealthy(tmp_path, monkeypatch) -> None:
    health_dir = tmp_path / "health"
    db_path = tmp_path / "paper" / "paper.duckdb"
    health_dir.mkdir()
    db_path.parent.mkdir()
    monkeypatch.setenv("WQ_PAPER_HEALTH_DIR", str(health_dir))
    monkeypatch.setenv("WQ_PAPER_DB_PATH", str(db_path))
    probe = _load_healthcheck()
    now = datetime.now(tz=UTC)
    native = health_dir / "heartbeat.json"
    lock = db_path.parent / ".paper.lock"
    native.write_text(json.dumps({"updated_at_utc": now.isoformat()}), encoding="utf-8")
    lock.write_text(json.dumps({"heartbeat_at_utc": (now - timedelta(hours=1)).isoformat()}), encoding="utf-8")
    assert probe.main() == 0

    native.write_text(json.dumps({"updated_at_utc": (now - timedelta(seconds=121)).isoformat()}), encoding="utf-8")
    lock.write_text(json.dumps({"heartbeat_at_utc": now.isoformat()}), encoding="utf-8")
    assert probe.main() == 1, "a fresh lease must not hide a stale native heartbeat"


def test_offline_serve_consumes_fills_and_reports_fresh_health(tmp_path, monkeypatch, capsys) -> None:
    """Run the real init + serve path once, with no Docker or network input."""
    db_path = tmp_path / "data" / "paper" / "paper.duckdb"
    events_dir = tmp_path / "decisions-export"
    raw_dir = tmp_path / "raw"
    health_dir = tmp_path / "health"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    monkeypatch.setenv("WQ_PAPER_DB_PATH", str(db_path))
    monkeypatch.setenv("WQ_PAPER_EVENTS_DIR", str(events_dir))
    monkeypatch.setenv("WQ_PAPER_RAW_DIR", str(raw_dir))
    monkeypatch.setenv("WQ_PAPER_HEALTH_DIR", str(health_dir))
    monkeypatch.setenv("WQ_PAPER_RISK_RULES_PATH", str(ROOT / "configs" / "paper-risk.json"))
    monkeypatch.setenv("WQ_PAPER_FEE_SCHEDULE_PATH", str(ROOT / "configs" / "fee-schedule.json"))
    monkeypatch.setenv("WQ_PAPER_API_HOST", "127.0.0.1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setenv("WQ_PAPER_API_PORT", str(sock.getsockname()[1]))

    assert main(["init"]) == 0
    capsys.readouterr()

    decision_time = datetime.now(tz=UTC) - timedelta(seconds=30)
    venue_time = decision_time - timedelta(seconds=10)
    received_time = decision_time - timedelta(seconds=5)
    book = book_payload(
        bids=[(0.52, 500.0)],
        asks=[(0.55, 60.0), (0.57, 200.0)],
        timestamp=venue_time.isoformat(),
    )
    book_id = parse_book_payload(
        book,
        token_id=TOKEN,
        market_id="mkt-1",
        fetched_at_utc=received_time,
        run_id="fixture",
        source="ws",
    ).content_hash
    write_archive(
        raw_dir,
        source="ws",
        day=decision_time.date().isoformat(),
        lines=[ws_line(book, received=received_time)],
    )
    row = export_row(
        token_id=TOKEN,
        decision_time_utc=decision_time.isoformat(),
        created_at_utc=decision_time.isoformat(),
        venue_ts_utc=venue_time.isoformat(),
        collector_received_at_utc=received_time.isoformat(),
        shadow_received_at_utc=received_time.isoformat(),
        book_id=book_id,
        proposed_shares=10.0,
        estimated_fill_shares=10.0,
    )
    (events_dir / "parts").mkdir(parents=True)
    (events_dir / "parts" / "part-000000001.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    probe = _load_healthcheck()
    health_results: list[int] = []

    def one_serve_tick(server) -> None:
        try:
            server.service_actions()
            # Run while the live serve lease exists, exactly as Docker's
            # separate health process would; release happens after this returns.
            health_results.append(probe.main())
        finally:
            server.server_close()

    monkeypatch.setattr("weatherquant.paper.cli.serve_forever", one_serve_tick)
    assert main(["serve"]) == 0
    assert health_results == [0]
    assert "healthy:" in capsys.readouterr().out

    with PaperDatabase(db_path) as db:
        store = PaperStore(db)
        assert store.account()["initial_capital_usdc"] == 100.0
        assert store.signal_counts() == {"FILLED": 1}
        fills = db.execute("SELECT decision_id, filled_shares, complete FROM paper_fills").fetchall()
        assert len(fills) == 1
        assert fills[0][0] == row["decision_id"]
        assert fills[0][1] == 10.0
        assert fills[0][2] is True

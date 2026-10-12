"""Invariants over the dashboard's Dockerfile, compose file and workflows.

Same rationale as ``tests/test_workflows.py``: these are properties that only
exist where a Dockerfile, a shell script and a workflow meet, and where a
silent regression means either an image that overwrites someone else's tag or
an acceptance list that drifted between CI and release.

Scanned as raw text rather than parsed YAML - the invariant is about the
command line as written, and it keeps the suite free of a YAML dependency.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "dashboard-ci.yml"
RELEASE = REPO_ROOT / ".github" / "workflows" / "dashboard-release.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile.dashboard"
COMPOSE = REPO_ROOT / "docker" / "compose.dashboard.unraid.yaml"
ACCEPT = REPO_ROOT / "scripts" / "dashboard_docker_accept.sh"
HEALTHCHECK = REPO_ROOT / "docker" / "healthcheck_dashboard.py"


def test_dashboard_workflows_exist() -> None:
    for path in (CI, RELEASE, DOCKERFILE, COMPOSE, ACCEPT, HEALTHCHECK):
        assert path.exists(), f"{path.relative_to(REPO_ROOT)} is required by the delivery spec"


# ---------------------------------------------------------------------------
# tag isolation: the dashboard must not be able to move anyone else's tags
# ---------------------------------------------------------------------------


def test_ci_never_publishes() -> None:
    text = CI.read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # The guard job quotes the very patterns it forbids; those lines are
        # the check, not a violation (same carve-out the guard itself uses).
        if "grep -nE" in line:
            continue
        for forbidden in (
            "uses: docker/login-action",
            "push: true",
            "packages: write",
            "imagetools create",
        ):
            if forbidden in line:
                offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, "dashboard-ci.yml contains publish capability:\n" + "\n".join(offenders)


def test_release_publishes_only_the_dashboard_image() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    assert "packages: write" in text
    # The image repository carries the -dashboard suffix; the collector and
    # shadow live in different repositories and cannot be reached from here.
    assert "github.repository }}-dashboard" in text
    # The only `stable` this workflow may ever move is its own namespaced one.
    assert '-t "$REGISTRY/$IMAGE:dashboard-stable"' in text
    assert '$IMAGE:stable"' not in text, "would move the collector's stable tag"
    assert ":shadow-stable" not in text, "would touch the shadow's tag namespace"
    # The stable move is gated to real tag pushes, never to a manual dispatch.
    assert "if: github.ref_type == 'tag'" in text


def test_release_runs_every_gate_before_publishing() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    verify_at = text.index("jobs:")
    publish_at = text.index("  publish:")
    assert verify_at < publish_at
    publish_block = text[publish_at:]
    assert "needs: verify" in publish_block
    for gate in ("pytest", "npm test", "playwright test", "dashboard_docker_accept.sh"):
        assert gate in text, f"release never runs {gate}"


def test_release_and_ci_share_one_acceptance_script() -> None:
    """The anti-drift property: no second copy of the check list."""
    for workflow in (CI, RELEASE):
        text = workflow.read_text(encoding="utf-8")
        assert "scripts/dashboard_docker_accept.sh" in text, (
            f"{workflow.name} does not use the shared acceptance script"
        )
        assert "docker run" not in text, (
            f"{workflow.name} starts containers inline; acceptance belongs in the shared script"
        )


# ---------------------------------------------------------------------------
# the shared acceptance script
# ---------------------------------------------------------------------------


def test_acceptance_script_has_the_required_checks() -> None:
    text = ACCEPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    # non-root
    assert "id -u" in text
    # health via the probe
    assert "healthcheck.py" in text
    # read-only mounts really read-only
    assert ":ro" in text and "touch /sources" in text
    # no database is visible to the container at all
    assert ".duckdb" in text
    # restart recovery and graceful stop
    assert "docker restart" in text
    assert "docker stop -t" in text
    # the fixture stamp must be asserted, not assumed
    assert 'dataset"] == "fixture"' in text or '["dataset"] == "fixture"' in text


def test_acceptance_script_is_valid_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - Windows without Git Bash
        pytest.skip("bash not available on this host")
    result = subprocess.run(
        [bash, "-n", str(ACCEPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_acceptance_docker_heredocs_forward_stdin() -> None:
    """`docker exec` silently drops stdin unless `-i` is given.

    The first CI run shipped exactly that bug: `docker exec ... python -`
    with a heredoc, the container's python read EOF, exited 0, and every
    assertion inside it - envelope shape, fixture stamp, database invisibility
    - never executed while the job stayed green. Any inline python fed by a
    heredoc through docker exec must request stdin explicitly.
    """
    lines = ACCEPT.read_text(encoding="utf-8").splitlines()
    heredoc_lines = [line for line in lines if "<<'PY'" in line]
    assert heredoc_lines, "the acceptance script no longer runs inline assertions?"
    for line in heredoc_lines:
        if "docker exec" in line:
            assert "docker exec -i" in line, (
                "docker exec heredoc without -i drops stdin; the assertions "
                f"would silently not run: {line.strip()}"
            )
    # ... and none may regress back to a bare `docker exec <c> python -`.
    offenders = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(lines, start=1)
        if not line.strip().startswith("#")
        and re.search(r"docker exec(?! -i)\s+\S+.*\bpython -", line)
    ]
    assert not offenders, "docker exec feeding `python -` without -i:\n" + "\n".join(offenders)


def test_acceptance_proves_inline_assertions_ran() -> None:
    """Exit code 0 is not evidence that a heredoc executed.

    Each docker-inline block must print a marker AND the script must grep for
    it afterwards, so an empty run fails loudly instead of passing.
    """
    text = ACCEPT.read_text(encoding="utf-8")
    docker_heredocs = [
        line for line in text.splitlines() if "<<'PY'" in line and "docker exec" in line
    ]
    assert len(docker_heredocs) >= 2, "expected the API and database-visibility blocks"
    # proof-of-execution greps for every docker-inline block
    assert 'grep -q "API OK"' in text, "API envelope block leaves no proof it ran"
    assert 'grep -q "no .duckdb anywhere"' in text, (
        "database-visibility block leaves no proof it ran"
    )
    assert "echo \"$api_out\"" in text and "echo \"$db_out\"" in text, (
        "the blocks' output must reach the CI log for review"
    )


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


def test_dockerfile_serves_only_the_dashboard() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "EXPOSE 8787" in text
    assert "USER wq" in text, "the container must run as a non-root user"
    assert "HEALTHCHECK" in text
    assert 'CMD ["weatherquant-dashboard"]' in text
    # single container serves API + built frontend
    assert "WQ_STATIC_DIR=/app/static" in text
    # nothing that would let it touch Docker or a database
    lower = text.lower()
    assert "docker.sock" not in lower
    assert "docker-cli" not in lower and "docker.io" not in lower
    # the dashboard dependency group is the only FastAPI source; the collector
    # and shadow images must not be able to pick it up by accident
    assert "--group dashboard" in text
    assert "--no-dev" in text


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def test_compose_mounts_are_read_only_subdirectories() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    mounts = re.findall(r"- ([^\s:]+):(/[^\s:]+):(ro|rw)", text)
    assert mounts, "no bind mounts found"
    for source, target, mode in mounts:
        assert mode == "ro", f"{source}:{target} is not read-only"
        # only the JSON subdirectories - never a database file or its directory
        assert source.endswith(("/health", "/raw", "/reports")), source
        assert not source.endswith(".duckdb")
    # the whole data directories must not be mounted
    assert "/mnt/user/appdata/weatherquant-next:" not in text
    assert "/mnt/user/appdata/weatherquant-shadow:" not in text


def test_compose_has_no_privileged_surface() -> None:
    text = COMPOSE.read_text(encoding="utf-8").lower()
    assert "docker.sock" not in text
    assert "privileged" not in text
    assert "network_mode: host" not in text


def test_compose_publishes_only_8787_and_restarts() -> None:
    # Comments may show alternative port lines as examples; judge the config.
    active_lines = [
        line.split("#")[0]
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(active_lines)
    # port mappings are the "- \"host:container\"" list items (user: "1000:1000"
    # must not count as a port).
    ports = re.findall(r'^\s*-\s+"(\d+:\d+)"', text, flags=re.MULTILINE)
    assert ports == ["8787:8787"], ports
    assert "restart: unless-stopped" in text
    assert "weatherquant-dashboard" in text
    # The default image is a pinned SHA, required explicitly - never a
    # floating tag that a pull could silently re-point.
    assert "sha-${DASHBOARD_SHA" in text, "production must default to a pinned SHA"
    assert ":latest" not in text
    assert ":dashboard-stable" not in text, "stable floats; production pins a SHA"


def test_production_config_never_enables_fixture() -> None:
    """Fixture data is for local runs and CI only - never the NAS stack."""
    active_lines = [
        line.split("#")[0]
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(active_lines)
    assert "WQ_DATASET: live" in text
    assert "fixture" not in text.lower()

    # and the code default is live, so an unset variable cannot flip it
    from weatherquant.dashboard.config import DashboardSettings

    for key in ("WQ_DATASET", "WQ_COLLECTOR_DATA_DIR", "WQ_SHADOW_DATA_DIR"):
        os.environ.pop(key, None)
    assert DashboardSettings().dataset == "live"


# ---------------------------------------------------------------------------
# healthcheck probe
# ---------------------------------------------------------------------------


def test_healthcheck_probes_only_itself_and_never_imports_the_app() -> None:
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "127.0.0.1:8787/healthz" in text
    # the probe must not import weatherquant (a broken package should still
    # produce a readable HTTP answer, and the probe has nothing to say about
    # upstreams anyway)
    assert "weatherquant" not in text
    assert "duckdb" not in text


def test_healthcheck_accepts_200_and_reports_sources() -> None:
    """Run the probe against a live stub: 200 passes, 500 fails."""
    import http.server
    import importlib.util
    import threading

    responses: dict[str, tuple[int, bytes]] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = responses.get(self.path, (404, b"{}"))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import json
        import os

        # The probe reads its URL at import time, so the environment has to be
        # in place BEFORE the module loads - otherwise this test would quietly
        # assert against the default port.
        os.environ["WQ_HEALTHCHECK_URL"] = f"http://127.0.0.1:{port}/healthz"
        spec = importlib.util.spec_from_file_location("wq_dashboard_healthcheck", HEALTHCHECK)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            responses["/healthz"] = (
                200,
                json.dumps(
                    {
                        "schema_version": 1,
                        "dataset": "live",
                        "freshness": {"state": "fresh"},
                        "data": {"sources": {"collector_heartbeat": "fresh"}},
                    }
                ).encode(),
            )
            assert module.main() == 0

            responses["/healthz"] = (503, b'{"detail":"stopping"}')
            assert module.main() == 1

            responses["/healthz"] = (200, b"not json")
            assert module.main() == 1
        finally:
            os.environ.pop("WQ_HEALTHCHECK_URL", None)
    finally:
        server.shutdown()
        server.server_close()

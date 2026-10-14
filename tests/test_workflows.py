"""Invariants over the Dockerfile and the CI workflows.

These exist because the first ``release.yml`` run failed with:

    weatherquant-collect: error: unrecognized arguments: python -c ...
    Process completed with exit code 2

The image sets ``ENTRYPOINT ["weatherquant-collect"]``, so ``docker run ... <image>
python -c "..."`` hands ``python -c ...`` to the *collector* as its own argv
instead of running Python. No unit test can see that: it only exists where a
Dockerfile and a shell command meet.

The workflows are scanned as raw text rather than parsed as YAML on purpose.
The invariant is about the command line as written, joining ``\\``-continuations
is exactly what the shell does, and it keeps the test free of a YAML
dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
DOCKERFILE = REPO_ROOT / "Dockerfile"

# A docker run that is meant to run something *other* than the image's
# ENTRYPOINT has to say so explicitly.
NON_ENTRYPOINT_PROGRAMS = re.compile(r"\bpython\b")

# Both matter: `docker run` starts a container that can collide with the
# collector's lock, and `docker stop` is what has to happen before a
# verification container can safely reuse the volume.
_WANTED = ("docker run", "docker stop")


def _wanted(line: str) -> bool:
    return any(word in line for word in _WANTED)


def _docker_commands(path: Path) -> list[str]:
    """``docker run`` / ``docker stop`` commands, in file order, as the shell sees them."""
    commands: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if pending:
            pending += " " + stripped.rstrip("\\").strip()
            if raw.rstrip().endswith("\\"):
                continue
            if _wanted(pending):
                commands.append(" ".join(pending.split()))
            pending = ""
            continue
        if stripped.startswith("#"):
            continue
        if raw.rstrip().endswith("\\") and _wanted(stripped):
            pending = stripped.rstrip("\\").strip()
            continue
        if _wanted(stripped):
            commands.append(" ".join(stripped.split()))
    return commands


def test_workflows_exist() -> None:
    assert len(WORKFLOWS) >= 2, "ci.yml and release.yml are expected"
    assert DOCKERFILE.exists()


def test_dockerfile_entrypoint_is_the_collector() -> None:
    """The premise of every other assertion in this file."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["weatherquant-collect"]' in text, (
        "the Dockerfile entrypoint changed; update this file to match, because a "
        "different entrypoint changes which docker run commands need --entrypoint"
    )


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_python_commands_override_the_entrypoint(workflow: Path) -> None:
    """``docker run <image> python ...`` must pass ``--entrypoint python``.

    Without it the string is handed to weatherquant-collect as argv, argparse
    rejects it, and the container exits 2 - which is exactly how the v0.1.0
    release failed.
    """
    offenders = [
        command
        for command in _docker_commands(workflow)
        if NON_ENTRYPOINT_PROGRAMS.search(command) and "--entrypoint" not in command
    ]
    message = (
        f"{workflow.name}: these docker run commands invoke python but keep the "
        "image ENTRYPOINT, so python becomes the collector's argv:\n  "
        + "\n  ".join(offenders)
    )
    assert not offenders, message


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_data_checks_run_after_the_collector_stops(workflow: Path) -> None:
    """A second container against the same volume would hit the single-instance lock.

    The release runs read-only verification queries; if any of them run while
    the collector still holds the lease, the job fails with exit 4 for a reason
    unrelated to what it was checking.
    """
    commands = _docker_commands(workflow)
    if not any("SELECT COUNT(*)" in command for command in commands):
        pytest.skip("workflow performs no database verification")

    stop_index = next(
        (i for i, c in enumerate(commands) if c.strip().startswith("docker stop")), None
    )
    query_index = next((i for i, c in enumerate(commands) if "SELECT COUNT(*)" in c), None)
    assert stop_index is not None, f"{workflow.name}: no `docker stop` found"
    assert query_index is not None
    assert stop_index < query_index, (
        f"{workflow.name}: a database query runs before `docker stop`; the "
        "collector would still hold the single-instance lock"
    )


def test_healthcheck_honours_the_disk_threshold_overrides() -> None:
    """CI runners ship nearly full; NAS thresholds would flag them as degraded."""
    module = _load_healthcheck({"WQ_DISK_WARN_PERCENT": "10", "WQ_DISK_CRITICAL_PERCENT": "3"})
    assert module.DISK_WARN_PERCENT == 10.0
    assert module.DISK_CRITICAL_PERCENT == 3.0


def test_healthcheck_defaults_stay_conservative_for_the_nas() -> None:
    """Overridable must not mean relaxed: the default is what the NAS gets."""
    module = _load_healthcheck({})
    assert module.DISK_WARN_PERCENT == 15.0
    assert module.DISK_CRITICAL_PERCENT == 5.0


def _load_healthcheck(env: dict[str, str]):
    import importlib.util
    import os

    keys = ("WQ_DISK_WARN_PERCENT", "WQ_DISK_CRITICAL_PERCENT")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location(
            f"wq_healthcheck_{abs(hash(tuple(sorted(env.items()))))}",
            REPO_ROOT / "docker" / "healthcheck.py",
        )
        assert spec and spec.loader, "healthcheck.py could not be loaded"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module


def test_disk_overrides_reach_the_healthcheck_from_the_environment() -> None:
    """The workflow sets them, so the probe must actually read them."""
    source = (REPO_ROOT / "docker" / "healthcheck.py").read_text(encoding="utf-8")
    assert "WQ_DISK_WARN_PERCENT" in source
    assert "WQ_DISK_CRITICAL_PERCENT" in source
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        if "docker run -d" not in text:
            continue
        assert "WQ_DISK_WARN_PERCENT" in text, (
            f"{workflow.name} starts a container without declaring its disk "
            "expectations, so a runner's free space is judged by the NAS threshold"
        )


# --- release tags must match the version the image will report ---------------


RELEASE_TAG_GUARDS = (
    "shadow-release.yml",
    "dashboard-release.yml",
    "mcp-release.yml",
)


def test_every_release_workflow_guards_its_own_tag() -> None:
    """A release whose tag disagrees with pyproject ships an unidentifiable image.

    ``app_version`` comes from package metadata, i.e. from this file. The
    drift already happened once - shadow-v0.1.0, shadow-v0.1.1 and v0.1.2 all
    shipped ``version = "0.1.1"``, so three different builds reported the same
    string and "which shadow is live" could only be answered from the registry.

    The guard lives in the workflow because that is the only place a tag
    exists. This test exists because a guard that can be deleted quietly is
    the same as no guard.
    """
    for name in RELEASE_TAG_GUARDS:
        workflow = REPO_ROOT / ".github" / "workflows" / name
        assert workflow.exists(), f"{name} is missing"
        text = workflow.read_text(encoding="utf-8")
        assert "Release tag must match pyproject version" in text, (
            f"{name} publishes an image without checking that its tag matches "
            "pyproject.version, so the image cannot identify itself"
        )
        # The check has to actually fail, not just exist as a step name.
        assert "exit 1" in text
        assert "pyproject.toml" in text


def test_pyproject_version_is_a_single_declared_string() -> None:
    """The guard parses this shape; if it changes shape the guard stops working."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    versions = re.findall(r'^version = "([^"]+)"$', text, flags=re.MULTILINE)
    assert len(versions) == 1, f"expected exactly one declared version, got {versions}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions[0]), versions[0]


# --- production must run a pinned image, not a floating pointer -------------


SHADOW_COMPOSE = REPO_ROOT / "docker" / "compose.shadow.unraid.yaml"


def _active_lines(path: Path) -> str:
    """Config as written, with comments stripped - judge what runs, not what is said."""
    return "\n".join(
        line.split("#")[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


def test_shadow_stack_pins_an_immutable_sha() -> None:
    """The shadow must not track `shadow-stable`.

    The release workflow moves that pointer the instant a shadow release
    passes its gates, so a stack that follows it adopts the newest build on
    the next `docker compose pull` or one click of Unraid's "Check for
    updates" - with nobody having chosen that version. Production pins a
    SHA instead: the running build is a property of the file, and rolling
    back is a one-line edit.

    The dashboard has had this assertion since P2-1 (see test_dashboard_ci);
    this is the same invariant for the engine that owns the database.
    """
    text = _active_lines(SHADOW_COMPOSE)
    assert "sha-${SHADOW_SHA:?" in text, "production must run a pinned SHA"
    assert ":shadow-stable" not in text, "`shadow-stable` floats; production pins a SHA"
    assert ":latest" not in text


def test_shadow_version_metadata_comes_from_the_pinned_sha() -> None:
    """GIT_SHA / IMAGE_TAG must be stated, not guessed.

    The engine records both in `runs` and publishes them on its heartbeat -
    the only place an operator can read "which build is live" without
    registry access. Defaulting them to `unknown` / `shadow-stable` while the
    container runs a pinned SHA puts a mutable pointer in the field that is
    supposed to identify an immutable build. Both now read SHADOW_SHA, the
    same value the image tag is built from, so they cannot disagree.
    """
    text = _active_lines(SHADOW_COMPOSE)
    assert "GIT_SHA: ${SHADOW_SHA:?" in text, "GIT_SHA must come from the pinned sha"
    assert "IMAGE_TAG: ${SHADOW_SHA:?" in text, "IMAGE_TAG must come from the pinned sha"
    assert "GIT_SHA:-unknown" not in text
    assert "IMAGE_TAG:-shadow-stable" not in text


def test_dashboard_version_metadata_comes_from_the_pinned_sha() -> None:
    """Same invariant on the other stack, which already pinned its image.

    The dashboard has pinned `sha-${DASHBOARD_SHA}` since P2-1 but still
    defaulted GIT_SHA / IMAGE_TAG, so it could report `dashboard-stable`
    while running a specific commit.
    """
    dashboard = REPO_ROOT / "docker" / "compose.dashboard.unraid.yaml"
    text = _active_lines(dashboard)
    assert "sha-${DASHBOARD_SHA:?" in text
    assert "GIT_SHA: ${DASHBOARD_SHA:?" in text
    assert "IMAGE_TAG: ${DASHBOARD_SHA:?" in text
    assert "GIT_SHA:-unknown" not in text
    assert "IMAGE_TAG:-dashboard-stable" not in text


def test_shadow_stack_still_refuses_the_properties_it_needs() -> None:
    """The pin is a change to two lines, not a licence to reopen the others."""
    text = _active_lines(SHADOW_COMPOSE)
    assert "ports:" not in text, "the shadow serves nothing and must publish no port"
    assert "/collector-raw:ro" in text or "/collector-raw\":ro" in text.replace("'", '"')
    assert "stop_grace_period: 60s" in text
    assert "user: \"1000:1000\"" in text


# --- the MCP container must stay the least privileged one --------------------

MCP_COMPOSE = REPO_ROOT / "docker" / "compose.mcp.unraid.yaml"
MCP_DOCKERFILE = REPO_ROOT / "Dockerfile.mcp"


def test_the_mcp_container_has_no_data_mount_at_all() -> None:
    """The gateway reads an HTTP API; a volume would be a second way in.

    Read-only is not enough here. The security property is that a
    compromised gateway has no database to reach, and a `:ro` mount still
    puts the file inside the container where a parser bug can find it.
    """
    text = _active_lines(MCP_COMPOSE)
    assert "volumes:" not in text, "the MCP gateway must mount nothing"
    assert "docker.sock" not in text, "a gateway that can schedule containers is not a gateway"
    assert "portainer" not in text.lower()


def test_the_mcp_container_binds_loopback_unless_a_token_is_supplied() -> None:
    """An unauthenticated gateway on a LAN address is an audience nobody chose.

    The port line is loopback by default, and the process itself refuses a
    non-loopback bind without WQ_MCP_TOKEN - both halves are asserted so
    neither can be relaxed alone.
    """
    text = _active_lines(MCP_COMPOSE)
    assert '- "127.0.0.1:8788:8788"' in text
    assert "WQ_MCP_TOKEN" in text
    assert "WQ_MCP_ALLOWED_ORIGINS" in text


def test_the_mcp_image_is_pinned_and_installs_only_its_own_group() -> None:
    text = _active_lines(MCP_COMPOSE)
    assert "sha-${MCP_SHA:?" in text, "production must run a pinned SHA"
    assert ":latest" not in text

    dockerfile = MCP_DOCKERFILE.read_text(encoding="utf-8")
    assert "--group mcp" in dockerfile
    assert "--group dashboard" not in dockerfile, (
        "the dashboard stack's dependencies have no business in the gateway image"
    )
    assert "USER wq" in dockerfile
    assert "read_only: true" in text


def test_the_mcp_container_has_no_order_tool_imported() -> None:
    """No path in this package may reach execution or a wallet."""
    root = REPO_ROOT / "src" / "weatherquant" / "mcp"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("build_fill_plan", "private_key", "wallet", "sign_order", "place_order"):
            if forbidden in source:
                offenders.append(f"{path.name}: {forbidden}")
    assert not offenders, offenders

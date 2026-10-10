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

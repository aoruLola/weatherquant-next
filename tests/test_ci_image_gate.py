"""Exercise the development workflows' image gate script against actual Git diffs.

The gate decides whether a PR should build + container-accept a Docker image.
It is shell and Git, not Python, so these tests drive it via subprocess
against a deliberately small Git fixture rather than by parsing the script.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "ci_image_gate.sh"
MODULES = {"collector", "dashboard", "shadow"}


def run_gate(repo: Path, event: str, module: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required to exercise the CI script")
    return subprocess.run(
        [bash, GATE.as_posix(), module],
        cwd=repo,
        env={**os.environ, "GITHUB_EVENT_NAME": event},
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        # Docs-only: nothing triggers an image build
        ("docs/operations.md", set()),
        # Shared core: every module builds
        ("src/weatherquant/config.py", MODULES),
        # Gate script itself: every module must re-evaluate
        ("scripts/ci_image_gate.sh", MODULES),
        # Collector-only paths
        ("docker/healthcheck.py", {"collector"}),
        ("Dockerfile", {"collector"}),
        # Shadow-only paths
        ("models/manifest.json", {"shadow"}),
        ("Dockerfile.shadow.dockerignore", {"shadow"}),
        ("configs/shadow-thresholds.json", {"shadow"}),
        # Dashboard-only paths
        ("dashboard/tests/helpers.py", {"dashboard"}),
        ("scripts/dashboard_docker_accept.sh", {"dashboard"}),
        (".github/workflows/dashboard-ci.yml", {"dashboard"}),
        ("Dockerfile.dashboard", {"dashboard"}),
    ],
)
def test_pr_images(tmp_path: Path, changed: str, expected: set[str]) -> None:
    """A PR merge commit diffs against its exact base (HEAD^1)."""

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-b", "base")
    git("commit", "--allow-empty", "-m", "base")
    git("switch", "-c", "feature")
    path = tmp_path / changed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("change", encoding="utf-8")
    git("add", "--", changed)
    git("commit", "-m", "change image input")
    # A second empty commit so the merge is a proper two-parent object.
    git("commit", "--allow-empty", "-m", "later commit")
    git("switch", "base")
    git("merge", "--no-ff", "feature", "-m", "PR merge")

    for module in MODULES:
        result = run_gate(tmp_path, "pull_request", module)
        result.check_returncode()
        want = "build_image=true" if module in expected else "build_image=false"
        assert result.stdout.strip() == want, (
            f"{module} got {result.stdout.strip()!r}, wanted {want} "
            f"(stderr={result.stderr.strip()!r})"
        )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_full_build_needs_no_pr_history(tmp_path: Path, event: str) -> None:
    """Non-PR events always return build_image=true with no Git history needed.

    Push-to-main and manual dispatch skip the selective diff entirely - they
    run the full matrix regardless of which files changed, and they do so
    even on a shallow clone where HEAD^1 does not exist.
    """
    for module in MODULES:
        result = run_gate(tmp_path, event, module)
        result.check_returncode()
        assert result.stdout.strip() == "build_image=true"


def test_missing_pr_history_fails_the_gate(tmp_path: Path) -> None:
    """A PR without a merge-commit history should abort, not silently skip.

    If the synthetic merge commit's first parent is missing, git diff against
    it fails - the gate must not catch that and return build_image=false
    (which would silently skip a needed build on a misconfigured checkout).
    """
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = run_gate(tmp_path, "pull_request", "collector")
    assert result.returncode != 0
    # Must NOT have printed a decision - the failure must bubble up.
    assert "build_image=false" not in result.stdout
    assert "build_image=true" not in result.stdout


def test_unknown_module_is_a_usage_error(tmp_path: Path) -> None:
    result = run_gate(tmp_path, "pull_request", "unknown-module")
    assert result.returncode == 2
    assert "build_image" not in result.stdout


def test_gate_script_is_valid_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available on this host")
    result = subprocess.run(
        [bash, "-n", str(GATE)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

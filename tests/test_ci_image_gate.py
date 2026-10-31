"""Exercise the development workflows' image selection against actual Git diffs."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "ci_image_gate.sh"
MODULES = {"collector", "dashboard", "shadow"}


def run_gate(repo, event, module):
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
        ("docs/operations.md", set()),
        ("src/weatherquant/shared.py", MODULES),
        ("scripts/ci_image_gate.sh", MODULES),
        ("docker/healthcheck.py", {"collector"}),
        ("models/manifest.json", {"shadow"}),
        ("Dockerfile.shadow.dockerignore", {"shadow"}),
        ("dashboard/tests/helpers.py", {"dashboard"}),
        ("scripts/dashboard_docker_accept.sh", {"dashboard"}),
        (".github/workflows/dashboard-ci.yml", {"dashboard"}),
    ],
)
def test_pr_images(tmp_path, changed, expected):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
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
    git("commit", "--allow-empty", "-m", "later commit")
    git("switch", "base")
    git("merge", "--no-ff", "feature", "-m", "PR merge")

    for module in MODULES:
        result = run_gate(tmp_path, "pull_request", module)
        result.check_returncode()
        assert result.stdout.strip() == ("build_image=true" if module in expected else "build_image=false")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_full_build_needs_no_pr_history(tmp_path, event):
    for module in MODULES:
        result = run_gate(tmp_path, event, module)
        result.check_returncode()
        assert result.stdout.strip() == "build_image=true"


def test_missing_pr_history_fails_the_gate(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    result = run_gate(tmp_path, "pull_request", "collector")
    assert result.returncode != 0
    assert "build_image=false" not in result.stdout

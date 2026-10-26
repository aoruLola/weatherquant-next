"""The release runbook cannot drift from the tests and workflow it cites.

A runbook that names a test the suite no longer has, or a workflow step that
was renamed, walks an operator through a door that isn't there. These checks
are deliberately mechanical: every ``test_*`` name the runbook references must
exist in this repository, and the workflow it walks must still contain the
gates the runbook claims it has.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "mcp-release-runbook.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mcp-release.yml"
TESTS_ROOT = REPO_ROOT / "tests"


def test_every_test_the_runbook_cites_exists() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    cited = set(re.findall(r"tests/test_\w+\.py::(test_\w+)", text))
    assert cited, "the runbook should cite concrete tests, not vibes"
    available = {
        path.name: re.findall(r"def (test_\w+)", path.read_text(encoding="utf-8"))
        for path in TESTS_ROOT.glob("test_*.py")
    }
    missing = [
        name
        for name in sorted(cited)
        if not any(name in names for names in available.values())
    ]
    assert not missing, f"runbook cites tests that do not exist: {missing}"


def test_the_runbook_matches_the_release_workflow() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert WORKFLOW.exists()
    assert "mcp-release.yml" in text, "the runbook must name the workflow it walks"
    for gate in (
        "refuses to start open",        # the container-level non-loopback gate
        "Config.Volumes",               # the no-data-mount gate
        "environment: release",         # the publish approval gate
        "mcp-stable",                   # the mutable pointer this workflow owns
    ):
        assert gate in workflow, f"workflow is missing the gate the runbook promises: {gate}"
    for gate in ("verify", "publish"):
        assert f"  {gate}:" in workflow, f"runbook documents a job that is gone: {gate}"

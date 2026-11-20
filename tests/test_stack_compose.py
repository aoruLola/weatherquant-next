"""Every production stack must run an immutable sha tag chosen in its .env.

The rule is deliberately generic: any ``docker/compose*.unraid.yaml`` file is
covered, so a new stack (the paper engine, for example) inherits the check the
moment its compose file lands. A mutable ``stable``/``latest`` tag in
production is a rollback hazard: the next pull adopts a build nobody chose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = sorted((REPO_ROOT / "docker").glob("compose*.unraid.yaml"))
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"

# `image: ghcr.io/<owner>/<repo>:sha-${VAR:?...}` - the required-variable form,
# not `${VAR:-default}`, so an unset .env refuses to start instead of falling
# back to a floating tag.
PINNED_IMAGE = re.compile(r"^\s*image:\s*ghcr\.io/[\w./-]+:sha-\$\{([A-Za-z_][A-Za-z0-9_]*):\?", re.MULTILINE)


def _active(path: Path) -> str:
    """Config as written, comments stripped - judge what runs, not what is said."""
    return "\n".join(
        line.split("#")[0] for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("#")
    )


def test_the_expected_stacks_are_covered() -> None:
    names = {path.name for path in COMPOSE_FILES}
    assert {
        "compose.unraid.yaml",
        "compose.shadow.unraid.yaml",
        "compose.dashboard.unraid.yaml",
        "compose.mcp.unraid.yaml",
    } <= names, names


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_every_stack_pins_an_immutable_sha(compose: Path) -> None:
    text = _active(compose)
    match = PINNED_IMAGE.search(text)
    assert match, f"{compose.name}: image must be ghcr.io/...:sha-${{VAR:?}}"
    variable = match.group(1)
    assert f"GIT_SHA: ${{{variable}:?" in text, (
        f"{compose.name}: GIT_SHA must come from {variable}, the value the image is pinned to"
    )
    assert f"IMAGE_TAG: ${{{variable}:?" in text, (
        f"{compose.name}: IMAGE_TAG must come from {variable}, the value the image is pinned to"
    )


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_no_stack_tracks_a_mutable_tag(compose: Path) -> None:
    text = _active(compose)
    assert ":stable" not in text, f"{compose.name}: `stable` floats; production pins a SHA"
    assert ":latest" not in text, f"{compose.name}: `latest` floats; production pins a SHA"


def test_collector_pins_collector_sha() -> None:
    text = _active(REPO_ROOT / "docker" / "compose.unraid.yaml")
    assert "sha-${COLLECTOR_SHA:?" in text
    assert "GIT_SHA: ${COLLECTOR_SHA:?" in text
    assert "IMAGE_TAG: ${COLLECTOR_SHA:?" in text
    # The mutable forms this replaced must not come back.
    assert "GIT_SHA:-unknown" not in text
    assert "IMAGE_TAG:-stable" not in text
    assert "weatherquant-next:stable" not in text


def test_the_collector_release_publishes_the_sha_tag_it_pins() -> None:
    """`COLLECTOR_SHA` is only usable if the release publishes sha-<full sha>."""
    text = RELEASE.read_text(encoding="utf-8")
    assert "sha-${{ steps.meta.outputs.sha }}" in text, (
        "release.yml must publish the immutable sha-<full sha> tag the stack pins"
    )
    assert "imagetools create" in text and ":stable" in text, (
        "the stable pointer must still be moved from the SHA tag, not the other way round"
    )

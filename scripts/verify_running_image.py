#!/usr/bin/env python
"""Assert that a running container really is the build it claims to be.

WQN-4 asked for proof that the deployed image is the checkout's worker. The
last mile is the container itself: it can be running a cached image, an image
pulled under the wrong tag, or one whose ``GIT_SHA`` was set by hand. This
tool reads ``docker inspect`` output and checks three things against a git
sha you supply:

* the image tag is exactly ``sha-<git sha>`` - not ``stable``, not a digest,
  not a different commit's tag;
* the container's ``GIT_SHA`` environment variable is that sha;
* its ``IMAGE_TAG`` environment variable is that sha (or ``sha-<that sha>``,
  for images that recorded the tag form).

The core is a pure function over the parsed JSON, so the whole check runs in
unit tests without Docker. The script only adds argument parsing and a
non-zero exit on mismatch.

Usage::

    docker inspect weatherquant-collector > /tmp/inspect.json
    uv run python scripts/verify_running_image.py --inspect /tmp/inspect.json \\
        --sha 5410c75d584771b4477de07f4b1d7b83b764bfe1

    docker inspect weatherquant-dashboard | uv run python scripts/verify_running_image.py \\
        --inspect - --sha "$DASHBOARD_SHA"

Exit codes: 0 everything matches, 1 a mismatch, 2 the input could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ImageCheck:
    """The verdict, with each fact kept for the report."""

    ok: bool
    expected_tag: str
    image: str | None = None
    actual_tag: str | None = None
    git_sha: str | None = None
    image_tag: str | None = None
    running: bool | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "expected_tag": self.expected_tag,
            "image": self.image,
            "actual_tag": self.actual_tag,
            "git_sha": self.git_sha,
            "image_tag": self.image_tag,
            "running": self.running,
            "problems": list(self.problems),
        }


def container_document(payload: object) -> dict:
    """``docker inspect <container>`` returns a one-element list; accept both."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    raise ValueError("expected one container document (a dict or a one-element list)")


def image_tag(image: str | None) -> str | None:
    """The tag from ``repo:tag``; ``None`` for a digest reference or no tag."""
    if not image or "@" in image:
        return None
    _head, separator, tail = image.rpartition(":")
    if separator and "/" not in tail:
        return tail
    return None


def _environment(document: dict) -> dict[str, str]:
    config = document.get("Config")
    config = config if isinstance(config, dict) else {}
    values: dict[str, str] = {}
    for item in config.get("Env") or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def check_container(payload: object, git_sha: str) -> ImageCheck:
    """The whole check, as a pure function over inspect JSON."""
    sha = git_sha.strip()
    if not sha:
        raise ValueError("a git sha is required")
    document = container_document(payload)
    config = document.get("Config")
    config = config if isinstance(config, dict) else {}
    image = config.get("Image") or document.get("Image") or None
    actual_tag = image_tag(image)
    expected_tag = f"sha-{sha}"
    environment = _environment(document)
    env_git_sha = environment.get("GIT_SHA")
    env_image_tag = environment.get("IMAGE_TAG")
    state = document.get("State")
    running = state.get("Running") if isinstance(state, dict) else None

    problems: list[str] = []
    if not image:
        problems.append("the container document has no image reference")
    elif actual_tag != expected_tag:
        problems.append(f"image tag is {actual_tag or '(none)'!r}, expected {expected_tag!r}")
    if env_git_sha != sha:
        problems.append(f"GIT_SHA is {env_git_sha or '(unset)'!r}, expected {sha!r}")
    if env_image_tag not in (sha, expected_tag):
        problems.append(f"IMAGE_TAG is {env_image_tag or '(unset)'!r}, expected {sha!r} or {expected_tag!r}")
    if running is False:
        problems.append("the container is not running")

    return ImageCheck(
        ok=not problems,
        expected_tag=expected_tag,
        image=image,
        actual_tag=actual_tag,
        git_sha=env_git_sha,
        image_tag=env_image_tag,
        running=running,
        problems=tuple(problems),
    )


def _read(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inspect", required=True, metavar="PATH", help="docker inspect JSON file, or - for stdin")
    parser.add_argument("--sha", required=True, help="the git commit sha the container is supposed to be running")
    parser.add_argument("--json", action="store_true", help="print the verdict as JSON")
    args = parser.parse_args(argv)

    try:
        verdict = check_container(_read(args.inspect), args.sha)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot verify: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(verdict.as_dict(), indent=2))
    else:
        print(f"image:      {verdict.image or '(none)'}")
        print(f"expected:   {verdict.expected_tag}")
        print(f"GIT_SHA:    {verdict.git_sha or '(unset)'}")
        print(f"IMAGE_TAG:  {verdict.image_tag or '(unset)'}")
        if verdict.running is not None:
            print(f"running:    {verdict.running}")
        for problem in verdict.problems:
            print(f"MISMATCH:   {problem}")
        print("RESULT: MATCH" if verdict.ok else "RESULT: MISMATCH")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

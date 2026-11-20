#!/usr/bin/env python
"""Snapshot the pinned build state of the Unraid compose stacks.

Why this exists
---------------
Every production stack now runs an immutable ``sha-<commit>`` image, chosen by
a variable in that stack's ``.env``. That makes a rollback a one-line edit -
provided we recorded what the line said *before* the change. This tool records
it, and only it:

* a byte-for-byte copy of each stack's compose file and ``.env``;
* the variables those compose files read to build the image reference, and
  their values (the pinned SHAs);
* the image digests, when a ``docker inspect`` document is supplied;
* the pinned Performance generation id, when ``--performance`` points at the
  published directory (see ``scripts/pin_performance_generation.py``).

It is deliberately **read-only on data directories**. It never opens a
DuckDB file, never copies a WAL file, never copies or moves an archive. A
snapshot that touched the data would be a second thing to reason about during
an incident, and a byte-copy of a live DuckDB is torn anyway.

Usage::

    uv run python scripts/stack_snapshot.py --out /mnt/user/backups/wq-snapshots \\
        --stack collector=/mnt/user/appdata/compose-manager/weatherquant-next \\
        --stack shadow=/mnt/user/appdata/compose-manager/weatherquant-shadow \\
        --stack dashboard=/mnt/user/appdata/compose-manager/weatherquant-dashboard \\
        --inspect collector-inspect.json \\
        --performance /mnt/user/appdata/weatherquant-next/performance

The snapshot is a new directory named for the UTC instant it was taken; it
never overwrites an earlier one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

TOOL_VERSION = "stack-snapshot-1"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
COMPOSE_CANDIDATES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

_IMAGE_LINE = re.compile(r"^\s*image:\s*(.+?)\s*$")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


@dataclass(frozen=True, slots=True)
class Stack:
    """One stack directory: the name it is known by, and the two files."""

    name: str
    directory: Path
    compose: Path
    env: Path


def parse_env(text: str) -> dict[str, str]:
    """``KEY=VALUE`` pairs, enough for the pinning variables.

    Deliberately not a full dotenv parser: the snapshot only needs the values
    compose would substitute, and a parser that quietly expanded ``$OTHER``
    would report a value the stack never used.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if match is None:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def compose_image(text: str) -> str | None:
    """The first active ``image:`` value in a compose file."""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        match = _IMAGE_LINE.match(line)
        if match is None:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def resolve_image(image: str, env: dict[str, str]) -> tuple[str, dict[str, str], list[str]]:
    """Substitute ``${VAR...}`` from the stack's ``.env``.

    Returns the resolved reference, the pinning variables actually present and
    the variables the image needs but the ``.env`` does not define. The last
    list is a finding, not an exception: taking the snapshot while a variable
    is missing is still useful, and the caller reports it.
    """
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        value = env.get(name)
        if not value:
            missing.append(name)
            return match.group(0)
        return value

    resolved = _VARIABLE.sub(substitute, image)
    pinned = {name: env[name] for name in _VARIABLE.findall(image) if env.get(name)}
    return resolved, pinned, missing


def image_repository(image: str) -> str:
    """``repo`` from ``repo:tag`` or ``repo@digest``."""
    if "@" in image:
        return image.split("@", 1)[0]
    head, separator, tail = image.rpartition(":")
    if separator and "/" not in tail and " " not in tail:
        return head
    return image


def find_compose(directory: Path) -> Path:
    for name in COMPOSE_CANDIDATES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    matches = sorted(
        path for pattern in ("compose*.yaml", "compose*.yml") for path in directory.glob(pattern) if path.is_file()
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no compose file in {directory}")
    raise FileNotFoundError(f"several compose files in {directory}: {[p.name for p in matches]}")


def parse_stack_spec(spec: str) -> Stack:
    name, separator, raw_dir = spec.partition("=")
    if not separator or not name.strip() or not raw_dir.strip():
        raise ValueError(f"expected NAME=DIR, got {spec!r}")
    directory = Path(raw_dir).expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"stack directory does not exist: {directory}")
    env = directory / ".env"
    if not env.is_file():
        raise FileNotFoundError(f"stack .env does not exist: {env}")
    return Stack(name=name.strip(), directory=directory, compose=find_compose(directory), env=env)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_documents(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _container_facts(document: dict) -> dict:
    """The fields that answer 'which build is this container running'.

    Accepts both ``docker inspect <container>`` and ``docker image inspect
    <ref>`` documents. Only the image document carries ``RepoDigests`` - the
    content digest a rollback can pin to; a container document carries the
    local image id in its top-level ``Image`` field instead. Both are recorded.
    """
    config = document.get("Config")
    config = config if isinstance(config, dict) else {}
    if "RepoTags" in document:
        tags = [tag for tag in document.get("RepoTags") or [] if isinstance(tag, str)]
        image = tags[0] if tags else ""
        image_id = document.get("Id")
        kind = "image"
    else:
        image = config.get("Image") or ""
        top = document.get("Image")
        image_id = top if isinstance(top, str) and top.startswith("sha256:") else None
        if not image and isinstance(top, str) and not top.startswith("sha256:"):
            image = top
        kind = "container"
    environment: dict[str, str] = {}
    for item in config.get("Env") or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            environment[key] = value
    return {
        "container": str(document.get("Name") or "").lstrip("/") or None,
        "image": image,
        "kind": kind,
        "image_id": image_id,
        "repo_digests": [item for item in document.get("RepoDigests") or [] if isinstance(item, str)],
        "git_sha": environment.get("GIT_SHA"),
        "image_tag": environment.get("IMAGE_TAG"),
    }


def performance_generation(root: Path | None) -> dict | None:
    """The pinned generation, read from ``current.json`` - never from a DB."""
    if root is None:
        return None
    pointer = root / "current.json"
    record: dict = {"root": str(root), "generation_id": None, "current_json": str(pointer)}
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return record
    if isinstance(payload, dict) and payload.get("generation_id"):
        record["generation_id"] = str(payload["generation_id"])
    return record


def build_snapshot(
    stacks: list[Stack],
    *,
    inspect_documents: list[dict],
    performance: dict | None,
    created_at: datetime,
) -> dict:
    """The snapshot's metadata: what was pinned, in which files, with what digests."""
    entries: dict[str, dict] = {}
    repositories: dict[str, str] = {}
    for stack in stacks:
        compose_text = stack.compose.read_text(encoding="utf-8")
        env_text = stack.env.read_text(encoding="utf-8")
        image = compose_image(compose_text)
        resolved, pinned, missing = resolve_image(image or "", parse_env(env_text))
        entry = {
            "directory": str(stack.directory),
            "compose_file": stack.compose.name,
            "env_file": stack.env.name,
            "image": resolved or None,
            "pinned": pinned,
            "missing_variables": missing,
            "compose_sha256": _sha256(stack.compose),
            "env_sha256": _sha256(stack.env),
            "containers": [],
        }
        entries[stack.name] = entry
        if resolved:
            repositories[image_repository(resolved)] = stack.name

    unmatched: list[dict] = []
    for document in inspect_documents:
        facts = _container_facts(document)
        stack_name = repositories.get(image_repository(facts["image"]))
        if stack_name is None:
            unmatched.append(facts)
        else:
            entries[stack_name]["containers"].append(facts)

    return {
        "tool_version": TOOL_VERSION,
        "created_at_utc": created_at.isoformat(),
        "stacks": entries,
        "unmatched_containers": unmatched,
        "performance": performance,
    }


def write_snapshot(
    out_root: Path,
    stacks: list[Stack],
    *,
    inspect_documents: list[dict],
    performance: dict | None,
    created_at: datetime | None = None,
) -> Path:
    """Create a new timestamped snapshot directory and return it."""
    created_at = created_at or datetime.now(tz=UTC)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = created_at.strftime(STAMP_FORMAT)
    destination = out_root / stamp
    counter = 1
    while destination.exists():
        destination = out_root / f"{stamp}-{counter}"
        counter += 1
    destination.mkdir()

    for stack in stacks:
        target = destination / stack.name
        target.mkdir()
        shutil.copyfile(stack.compose, target / "compose.yaml")
        shutil.copyfile(stack.env, target / ".env")

    payload = build_snapshot(
        stacks,
        inspect_documents=inspect_documents,
        performance=performance,
        created_at=created_at,
    )
    (destination / "snapshot.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return destination


def _load_inspect(paths: list[Path]) -> list[dict]:
    documents: list[dict] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        documents.extend(_inspect_documents(payload))
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True, help="directory the timestamped snapshot goes in")
    parser.add_argument(
        "--stack",
        action="append",
        required=True,
        metavar="NAME=DIR",
        help="a compose stack directory containing a compose file and .env; repeat per stack",
    )
    parser.add_argument("--inspect", type=Path, action="append", default=[], help="docker inspect JSON (repeatable)")
    parser.add_argument("--performance", type=Path, help="published performance directory (reads current.json only)")
    parser.add_argument("--now", help="ISO-8601 UTC instant for the snapshot name (default: now)")
    args = parser.parse_args(argv)

    try:
        stacks = [parse_stack_spec(spec) for spec in args.stack]
        names = [stack.name for stack in stacks]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate stack names: {names}")
        documents = _load_inspect(args.inspect)
        created_at = datetime.fromisoformat(args.now) if args.now else datetime.now(tz=UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        performance = performance_generation(args.performance)
        destination = write_snapshot(
            args.out,
            stacks,
            inspect_documents=documents,
            performance=performance,
            created_at=created_at,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"snapshot failed: {exc}", file=sys.stderr)
        return 2

    print(f"snapshot written: {destination}")
    for name, entry in build_snapshot(
        stacks,
        inspect_documents=documents,
        performance=performance,
        created_at=created_at,
    )["stacks"].items():
        pinned = ", ".join(f"{key}={value}" for key, value in entry["pinned"].items()) or "(none resolved)"
        print(f"  {name:<10} {entry['image']}  ({pinned})")
        for container in entry["containers"]:
            digests = ", ".join(container["repo_digests"]) or (
                f"image id {container['image_id']} (pass `docker image inspect <ref>` for RepoDigests)"
                if container.get("image_id")
                else "(no RepoDigests)"
            )
            print(f"             container {container['container']}: {digests}")
        if entry["missing_variables"]:
            print(f"             MISSING in .env: {', '.join(entry['missing_variables'])}")
    if performance is not None:
        print(f"  performance generation: {performance['generation_id'] or '(none pinned)'}")
    print("data directories were not read, copied or modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

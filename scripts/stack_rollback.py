#!/usr/bin/env python
"""Restore the pinned SHAs recorded in a stack snapshot.

Why this exists
---------------
``scripts/stack_snapshot.py`` records the compose files, the ``.env`` files and
the pinned variables before a change. This is the other half: one command that
puts the ``.env`` files back, so "roll back" is a single step rather than
remembering which SHA ran last week.

What it does, precisely:

* dry run by default - prints the exact ``.env`` diff for every stack and
  writes nothing;
* ``--apply`` rewrites **only the ``.env`` files**, atomically, from the
  snapshot's byte-for-byte copies. Compose files, databases, WAL files and
  archives are never touched;
* when the snapshot recorded a Performance generation, prints the generation
  to re-pin and the exact command to run. The Dashboard image and the
  generation must be rolled back together: a Dashboard that predates the
  partitioned detail cannot read a generation that publishes it that way.

Usage::

    uv run python scripts/stack_rollback.py --snapshot /backups/wq-snapshots/20261120T120000Z
    uv run python scripts/stack_rollback.py --snapshot DIR --apply
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SNAPSHOT_FILE = "snapshot.json"


_REQUIRED_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?[^}]*\}")
_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _defined(env: bytes) -> set[str]:
    """Keys the ``.env`` gives a non-empty value (what ``${VAR:?}`` accepts)."""
    names: set[str] = set()
    for line in env.decode("utf-8", errors="replace").splitlines():
        if line.strip().startswith("#"):
            continue
        match = _ENV_KEY.match(line)
        if match and match.group(2).strip().strip("\"'"):
            names.add(match.group(1))
    return names


@dataclass(frozen=True, slots=True)
class StackPlan:
    """One stack's restore, checked and diffed before anything is written."""

    name: str
    env_path: Path
    snapshot_env: Path
    current: bytes
    target: bytes
    # Variables the stack's *current* compose file requires (``${VAR:?}``)
    # that the snapshot's .env does not define. Restoring it as-is would leave
    # compose refusing to start - typically a snapshot taken before the stack
    # was pinned. Empty when the restore is consistent.
    unresolved: tuple[str, ...] = ()
    compose_changed: bool = False

    @property
    def changed(self) -> bool:
        return self.current != self.target

    def diff(self) -> str:
        before = self.current.decode("utf-8", errors="replace").splitlines(keepends=True)
        after = self.target.decode("utf-8", errors="replace").splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"{self.env_path} (current)",
                tofile=f"{self.snapshot_env} (snapshot)",
            )
        )


def load_snapshot(snapshot_dir: Path) -> dict:
    payload = json.loads((snapshot_dir / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("stacks"), dict):
        raise ValueError(f"{snapshot_dir / SNAPSHOT_FILE} has no stacks mapping")
    return payload


def plan_rollback(snapshot_dir: Path, snapshot: dict) -> list[StackPlan]:
    """Every stack's current and snapshot ``.env``, without writing anything."""
    plans: list[StackPlan] = []
    for name, entry in snapshot["stacks"].items():
        directory = Path(entry["directory"]).expanduser()
        env_path = directory / entry.get("env_file", ".env")
        snapshot_env = snapshot_dir / name / ".env"
        if not directory.is_dir():
            raise FileNotFoundError(f"{name}: stack directory no longer exists: {directory}")
        if not env_path.is_file():
            raise FileNotFoundError(f"{name}: .env not found: {env_path}")
        if not snapshot_env.is_file():
            raise FileNotFoundError(f"{name}: snapshot has no .env: {snapshot_env}")
        target = snapshot_env.read_bytes()
        compose_path = directory / entry.get("compose_file", "compose.yaml")
        required: list[str] = []
        compose_changed = False
        if compose_path.is_file():
            compose_bytes = compose_path.read_bytes()
            text = compose_bytes.decode("utf-8", errors="replace")
            active = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            required = sorted(set(_REQUIRED_VARIABLE.findall(active)))
            recorded = entry.get("compose_sha256")
            compose_changed = bool(recorded) and hashlib.sha256(compose_bytes).hexdigest() != recorded
        defined = _defined(target)
        plans.append(
            StackPlan(
                name=name,
                env_path=env_path,
                snapshot_env=snapshot_env,
                current=env_path.read_bytes(),
                target=target,
                unresolved=tuple(v for v in required if v not in defined),
                compose_changed=compose_changed,
            )
        )
    return plans


def _atomic_write(path: Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _print_performance(snapshot: dict, stack_names: set[str]) -> None:
    performance = snapshot.get("performance")
    if not isinstance(performance, dict) or not performance.get("generation_id"):
        return
    generation = performance["generation_id"]
    root = performance.get("root", "<performance dir>")
    print("\npaired performance generation")
    print(f"  generation pinned at snapshot: {generation}")
    print("  re-pin it in the same window as the Dashboard rollback:")
    print(
        "    uv run python scripts/pin_performance_generation.py "
        f"--performance {root} --generation {generation} --apply"
    )
    if "dashboard" in stack_names:
        print(
            "  the Dashboard image and the generation are a pair: pin_performance_generation.py "
            "reports the generation's format, and a Dashboard built for the other format "
            "will answer 500 rather than rolled back."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", type=Path, required=True, help="a directory written by stack_snapshot.py")
    parser.add_argument("--apply", action="store_true", help="rewrite the .env files (default: dry run)")
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="apply even when the restored .env lacks a variable the current compose file requires",
    )
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot(args.snapshot)
        plans = plan_rollback(args.snapshot, snapshot)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"rollback failed: {exc}", file=sys.stderr)
        return 2

    print(f"snapshot: {args.snapshot}")
    print(f"taken at: {snapshot.get('created_at_utc', '(unknown)')}")
    for plan in plans:
        print(f"\nstack {plan.name}")
        print(f"  .env: {plan.env_path}")
        if plan.compose_changed:
            print(
                f"  NOTE: the compose file changed since the snapshot; its snapshot copy is "
                f"{args.snapshot / plan.name / 'compose.yaml'}"
            )
        if plan.unresolved:
            print(
                f"  BLOCKER: the restored .env does not define {', '.join(plan.unresolved)}, which "
                "the current compose file requires - compose would refuse to start. Add the "
                "variable(s) to the snapshot's .env copy (or restore the snapshot's compose file "
                "by hand), or pass --allow-unresolved."
            )
        if not plan.changed:
            print("  unchanged - the pinned SHAs already match the snapshot")
            continue
        print(plan.diff(), end="" if plan.diff().endswith("\n") else "\n")

    _print_performance(snapshot, {plan.name for plan in plans})

    changed = [plan for plan in plans if plan.changed]
    if not changed:
        print("\nnothing to restore.")
        return 0
    if not args.apply:
        print("\ndry run: nothing was written. Re-run with --apply to restore the .env files.")
        return 0
    blocked = [plan.name for plan in changed if plan.unresolved]
    if blocked and not args.allow_unresolved:
        print(
            f"\nnot applied: {', '.join(blocked)} would be left unable to start (see BLOCKER above). "
            "Nothing was written.",
            file=sys.stderr,
        )
        return 1

    for plan in changed:
        _atomic_write(plan.env_path, plan.target)
        print(f"\nrestored: {plan.env_path}")
    print(
        "\nonly the .env files were rewritten. Compose files, databases, WAL files and "
        "archives were not touched; recreate the containers to adopt the pinned SHAs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

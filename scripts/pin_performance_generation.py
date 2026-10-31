#!/usr/bin/env python
"""List, inspect and repin the published performance generation.

Why this exists
---------------
The rollback path for a bad performance publish is *not* "delete the
directory". The snapshots are the only record of what was published, the
research state store is the only record of what was consumed, and an operator
mid-incident should not be choosing which of those to destroy. What actually
went wrong with a bad publish is one thing: the pointer names a generation that
the running Dashboard should not be reading. So the tool does one thing - it
moves the pointer, after checking that the generation it is moving to is
complete and readable - and it leaves everything else on disk.

It also reports the *format family* of each generation, because the Dashboard
and the snapshots have to be rolled back together: a Dashboard built before the
detail was published as parts cannot read a snapshot that publishes it that
way, and moving only one of them produces a 500 rather than a rollback.

Usage::

    uv run python scripts/pin_performance_generation.py --performance DIR --list
    uv run python scripts/pin_performance_generation.py --performance DIR \
        --generation 20261015T120000Z-abcdef012345          # dry run
    uv run python scripts/pin_performance_generation.py --performance DIR \
        --generation 20261015T120000Z-abcdef012345 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from weatherquant.performance.generation_lock import GenerationBusy, generation_lock  # noqa: E402

SNAPSHOT_FILES = (
    "summary.json",
    "edge.json",
    "win-rate.json",
    "opportunities.json",
    "settlements.json",
    "models.json",
)

#: What a Dashboard built before this round can and cannot read. The detail
#: changed from an inline array to a partition descriptor, and the summary
#: stopped carrying one capital result per episode.
BOUNDED = "bounded-detail"
LEGACY = "legacy-detail"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GenerationInfo:
    """One published generation, and what a reader can expect from it."""

    generation_id: str
    directory: Path
    published_at_utc: str | None
    dataset: str | None
    algorithm_version: str | None
    format: str
    files_present: tuple[str, ...]
    missing_files: tuple[str, ...]
    detail_parts: int
    problems: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_files and not self.problems

    def as_dict(self) -> dict:
        return {
            "generation_id": self.generation_id,
            "published_at_utc": self.published_at_utc,
            "dataset": self.dataset,
            "algorithm_version": self.algorithm_version,
            "format": self.format,
            "complete": self.complete,
            "missing_files": list(self.missing_files),
            "detail_parts": self.detail_parts,
            "problems": list(self.problems),
        }


def _load(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def inspect(directory: Path) -> GenerationInfo:
    manifest = _load(directory / "manifest.json") or {}
    present = tuple(name for name in SNAPSHOT_FILES if (directory / name).is_file())
    missing = tuple(name for name in SNAPSHOT_FILES if name not in present)
    problems = []
    if manifest.get("generation_id") != directory.name:
        problems.append("missing manifest or generation ID mismatch")
    files = manifest.get("files")
    for name in present:
        entry = files.get(name) if isinstance(files, dict) else None
        if not isinstance(entry, dict) or entry.get("bytes") != (directory / name).stat().st_size:
            problems.append(f"{name} does not match manifest size")

    opportunities = (_load(directory / "opportunities.json") or {}).get("data")
    summary = (_load(directory / "summary.json") or {}).get("data")
    episodes = opportunities.get("episodes") if isinstance(opportunities, dict) else None
    capital = summary.get("reference_capital") if isinstance(summary, dict) else None
    family = UNKNOWN
    parts = 0
    if isinstance(episodes, list) and isinstance(capital, dict) and isinstance(capital.get("results"), list):
        family = LEGACY
    elif (isinstance(episodes, dict) and episodes.get("partitioned") is True
          and isinstance(capital, dict) and isinstance(capital.get("results_sample"), list)
          and "results" not in capital):
        family = BOUNDED
        partitions = episodes.get("partitions")
        if episodes.get("directory") != "opportunities" or not isinstance(partitions, list):
            problems.append("invalid opportunities partition descriptor")
        else:
            parts = len(partitions)
            for entry in partitions:
                name = entry.get("file") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name or Path(name).name != name:
                    problems.append("invalid opportunities part name")
                    continue
                path = directory / "opportunities" / name
                if not path.is_file() or path.stat().st_size != entry.get("bytes"):
                    problems.append(f"missing or truncated opportunities/{name}")
    if family == UNKNOWN:
        problems.append("unrecognized snapshot format")
    return GenerationInfo(
        generation_id=directory.name,
        directory=directory,
        published_at_utc=manifest.get("published_at_utc") or manifest.get("generated_at_utc"),
        dataset=manifest.get("dataset"),
        algorithm_version=manifest.get("algorithm_version"),
        format=family,
        files_present=present,
        missing_files=missing,
        detail_parts=parts,
        problems=tuple(problems),
    )


def current_pointer(root: Path) -> str | None:
    pointer = _load(root / "current.json")
    return str(pointer.get("generation_id")) if pointer else None


def list_generations(root: Path) -> list[GenerationInfo]:
    generations = root / "generations"
    if not generations.is_dir():
        return []
    return [
        inspect(path)
        for path in sorted(generations.iterdir(), key=lambda p: p.name)
        if path.is_dir()
    ]


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write the pointer so a reader sees the old value or the new one."""
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    Path(temporary).replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--performance", required=True, help="the published performance directory")
    parser.add_argument("--generation", help="the generation to make current")
    parser.add_argument("--list", action="store_true", help="list the published generations")
    parser.add_argument("--apply", action="store_true", help="actually move the pointer (default: dry run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.performance)
    if not root.exists():
        print(f"no performance directory at {root}", file=sys.stderr)
        return 2

    try:
        with generation_lock(root) if args.apply and args.generation and not args.list else nullcontext():
            return _pin_or_list(root, args)
    except GenerationBusy as exc:
        print(exc, file=sys.stderr)
        return 75


def _pin_or_list(root: Path, args: argparse.Namespace) -> int:
    generations = list_generations(root)
    pointer = current_pointer(root)

    if args.list or not args.generation:
        report = {
            "performance": str(root),
            "current": pointer,
            "generations": [info.as_dict() | {"current": info.generation_id == pointer} for info in generations],
        }
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        print(f"performance: {root}")
        print(f"current:     {pointer or '(none - flat layout)'}")
        for info in generations:
            mark = "*" if info.generation_id == pointer else " "
            state = "complete" if info.complete else ", ".join((*info.missing_files, *info.problems))
            print(
                f" {mark} {info.generation_id}  {info.format:14s} {state:10s} "
                f"parts={info.detail_parts} dataset={info.dataset} "
                f"published={info.published_at_utc} algorithm={info.algorithm_version}"
            )
        if not generations:
            print("  (no generations published)")
        return 0

    target = next((info for info in generations if info.generation_id == args.generation), None)
    if target is None:
        print(f"no generation {args.generation!r} under {root / 'generations'}", file=sys.stderr)
        return 2
    if not target.complete:
        print(
            f"refusing: generation {target.generation_id} is incomplete "
            f"({', '.join((*target.missing_files, *target.problems))})",
            file=sys.stderr,
        )
        return 2
    if target.generation_id == pointer and not args.apply:
        print(f"already current: {target.generation_id}")

    print(f"performance:   {root}")
    print(f"format:        {target.format}")
    print(f"pointer:       {pointer or '(none - flat layout)'}  ->  {target.generation_id}")
    print(
        "roll back the Dashboard image in the same window unless its format already "
        f"matches {target.format}."
    )
    if not args.apply:
        print("dry run: nothing was written. Re-run with --apply to move the pointer.")
        return 0

    previous = pointer
    _atomic_write_json(root / "current.json", {"generation_id": target.generation_id})
    print(f"pointer moved: {previous or '(none)'} -> {target.generation_id}")
    print(
        "note for the change record: the generation you left behind is untouched and still "
        "on disk, and so is the research state store."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

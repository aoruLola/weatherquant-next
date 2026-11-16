#!/usr/bin/env python
"""Prune old performance generations to bound disk usage.

Strategy:
- Keep the current generation (named in current.json).
- Keep the most recent N complete generations (default 14).
- Keep the last generation from each month, going back up to M months (default 6).
- **Keep at least one complete, verified Legacy-format generation** (for paired
  Dashboard rollback to sha-e6698dfd or earlier).
- Incomplete generations (missing core files or malformed) are prioritized for
  deletion, but only after all other retention rules are satisfied.
- Delete everything else.

Why this exists
---------------
The streaming builder publishes a new generation on every run, even when the
content is unchanged. At a 5-minute cadence, that is 288 generations per day,
roughly 2.2 GB/day. The CLI has no built-in retention logic, so without this
script the generations directory grows without bound.

Usage::

    # Dry run (list what would be deleted)
    uv run python scripts/prune_performance_generations.py --performance DIR

    # Actually delete
    uv run python scripts/prune_performance_generations.py --performance DIR --apply

    # Keep more recent generations
    uv run python scripts/prune_performance_generations.py --performance DIR \
        --keep-recent 28

    # Keep monthly snapshots further back
    uv run python scripts/prune_performance_generations.py --performance DIR \
        --keep-monthly 12
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pin_performance_generation import LEGACY, inspect  # noqa: E402

from weatherquant.performance.generation_lock import GenerationBusy, generation_lock  # noqa: E402


@dataclass(frozen=True, slots=True)
class Generation:
    """One published generation directory."""

    generation_id: str
    directory: Path
    published_at_utc: datetime
    size_bytes: int
    format_family: str
    complete: bool
    detail_parts: int

    @classmethod
    def from_directory(cls, directory: Path) -> Generation | None:
        """Parse one generation directory. Returns None if it cannot be parsed."""
        generation_id = directory.name
        # generation_id format: YYYYMMDDTHHMMSSz-<hash>
        if not generation_id or len(generation_id) < 16:
            return None
        try:
            timestamp_part = generation_id.split("-")[0]
            published_at_utc = datetime.strptime(
                timestamp_part, "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=UTC)
        except (ValueError, IndexError):
            return None

        info = inspect(directory)

        # Measure directory size
        size_bytes = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())

        return cls(
            generation_id=generation_id,
            directory=directory,
            published_at_utc=published_at_utc,
            size_bytes=size_bytes,
            format_family=info.format,
            complete=info.complete,
            detail_parts=info.detail_parts,
        )


def list_generations(performance_dir: Path) -> list[Generation]:
    """Enumerate all published generations, newest first."""
    generations_dir = performance_dir / "generations"
    if not generations_dir.is_dir():
        return []

    parsed = []
    for child in generations_dir.iterdir():
        if not child.is_dir():
            continue
        gen = Generation.from_directory(child)
        if gen is not None:
            parsed.append(gen)

    # Sort newest first
    parsed.sort(key=lambda g: g.published_at_utc, reverse=True)
    return parsed


def load_current_generation(performance_dir: Path) -> str | None:
    """Read the current generation id from current.json."""
    pointer = performance_dir / "current.json"
    if not pointer.is_file():
        return None
    try:
        data = json.loads(pointer.read_text())
        return data.get("generation_id")
    except Exception:
        return None


def compute_retention(
    generations: list[Generation],
    *,
    current_generation_id: str | None,
    keep_recent: int,
    keep_monthly: int,
) -> tuple[list[Generation], list[Generation]]:
    """Decide which generations to keep and which to delete.

    Returns (to_keep, to_delete).
    """
    to_keep: set[str] = set()

    # Rule 1: Always keep current
    if current_generation_id:
        to_keep.add(current_generation_id)

    # Rule 2: Keep the most recent N complete generations
    complete_gens = [g for g in generations if g.complete]
    for gen in complete_gens[:keep_recent]:
        to_keep.add(gen.generation_id)

    # Rule 3: Keep the last generation from each month, going back M months
    # Group by (year, month)
    by_month: dict[tuple[int, int], list[Generation]] = defaultdict(list)
    for gen in generations:
        if gen.complete:
            key = (gen.published_at_utc.year, gen.published_at_utc.month)
            by_month[key].append(gen)

    # Sort months newest-first, take the first keep_monthly, and from each bucket
    # keep the LAST generation (earliest in that month)
    month_keys = sorted(by_month.keys(), reverse=True)[:keep_monthly]
    for key in month_keys:
        bucket = by_month[key]
        # Sort oldest-first within the bucket, take the last one
        bucket_sorted = sorted(bucket, key=lambda g: g.published_at_utc)
        if bucket_sorted:
            to_keep.add(bucket_sorted[-1].generation_id)

    # Rule 4: Keep at least one complete Legacy-format generation
    legacy_gens = [g for g in generations if g.complete and g.format_family == LEGACY]
    if legacy_gens:
        # Keep the most recent one
        to_keep.add(legacy_gens[0].generation_id)

    # Everything not in to_keep is to_delete
    keep_list = [g for g in generations if g.generation_id in to_keep]
    to_delete = [g for g in generations if g.generation_id not in to_keep]

    return keep_list, to_delete


def format_size(size_bytes: int) -> str:
    """Human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune old performance generations to bound disk usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--performance",
        type=Path,
        required=True,
        help="Path to the performance directory (contains current.json and generations/)",
    )
    parser.add_argument(
        "--keep-recent",
        type=int,
        default=14,
        help="Number of most recent complete generations to keep (default 14)",
    )
    parser.add_argument(
        "--keep-monthly",
        type=int,
        default=6,
        help="Number of months to keep the last generation from (default 6)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the generations (default is dry-run)",
    )
    args = parser.parse_args()

    performance_dir: Path = args.performance
    if not performance_dir.is_dir():
        print(f"ERROR: {performance_dir} is not a directory", file=sys.stderr)
        return 1

    try:
        with generation_lock(performance_dir) if args.apply else nullcontext():
            return _prune(performance_dir, args)
    except GenerationBusy as exc:
        print(exc, file=sys.stderr)
        return 75


def _prune(performance_dir: Path, args: argparse.Namespace) -> int:
    current_generation_id = load_current_generation(performance_dir)
    if current_generation_id:
        print(f"Current generation: {current_generation_id}")
    else:
        print("WARNING: No current.json found or it is unreadable")

    generations = list_generations(performance_dir)
    if not generations:
        print("No generations found.")
        return 0

    print(f"Total generations: {len(generations)}")
    complete_count = sum(1 for g in generations if g.complete)
    legacy_count = sum(
        1 for g in generations if g.complete and g.format_family == LEGACY
    )
    print(f"  Complete: {complete_count}")
    print(f"  Legacy-format: {legacy_count}")

    to_keep, to_delete = compute_retention(
        generations,
        current_generation_id=current_generation_id,
        keep_recent=args.keep_recent,
        keep_monthly=args.keep_monthly,
    )

    total_size = sum(g.size_bytes for g in generations)
    delete_size = sum(g.size_bytes for g in to_delete)
    keep_size = total_size - delete_size

    print("\nRetention policy:")
    print(f"  Keep recent: {args.keep_recent} (complete only)")
    print(f"  Keep monthly: {args.keep_monthly}")
    print("  Keep at least 1 Legacy-format generation")
    print(f"\nWill keep: {len(to_keep)} generations ({format_size(keep_size)})")
    print(f"Will delete: {len(to_delete)} generations ({format_size(delete_size)})")

    if not to_delete:
        print("\nNothing to delete.")
        return 0

    print("\n--- Generations to DELETE ---")
    for gen in sorted(to_delete, key=lambda g: g.published_at_utc):
        complete_mark = "OK" if gen.complete else "INCOMPLETE"
        format_mark = f"[{gen.format_family}]" if gen.complete else ""
        print(
            f"  {complete_mark:11s} {gen.generation_id}  "
            f"{gen.published_at_utc.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{format_size(gen.size_bytes):8s} {format_mark}"
        )

    print("\n--- Generations to KEEP ---")
    # Show which rule(s) kept each one
    keep_dict = {g.generation_id: g for g in to_keep}
    reasons: dict[str, list[str]] = {}
    for gid in keep_dict:
        reasons[gid] = []

    if current_generation_id and current_generation_id in keep_dict:
        reasons[current_generation_id].append("CURRENT")

    complete_gens = [g for g in generations if g.complete]
    for gen in complete_gens[: args.keep_recent]:
        if gen.generation_id in reasons:
            reasons[gen.generation_id].append("RECENT")

    by_month: dict[tuple[int, int], list[Generation]] = defaultdict(list)
    for gen in generations:
        if gen.complete and gen.generation_id in keep_dict:
            key = (gen.published_at_utc.year, gen.published_at_utc.month)
            by_month[key].append(gen)
    month_keys = sorted(by_month.keys(), reverse=True)[: args.keep_monthly]
    for key in month_keys:
        bucket = by_month[key]
        bucket_sorted = sorted(bucket, key=lambda g: g.published_at_utc)
        if bucket_sorted and bucket_sorted[-1].generation_id in reasons:
            reasons[bucket_sorted[-1].generation_id].append("MONTHLY")

    legacy_gens = [g for g in generations if g.complete and g.format_family == LEGACY]
    if legacy_gens and legacy_gens[0].generation_id in reasons:
        reasons[legacy_gens[0].generation_id].append("LEGACY-ROLLBACK")

    for gen in sorted(to_keep, key=lambda g: g.published_at_utc, reverse=True):
        reason_str = ", ".join(reasons.get(gen.generation_id, []))
        complete_mark = "OK" if gen.complete else "INCOMPLETE"
        format_mark = f"[{gen.format_family}]" if gen.complete else ""
        print(
            f"  {complete_mark:11s} {gen.generation_id}  "
            f"{gen.published_at_utc.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{format_size(gen.size_bytes):8s} {format_mark:17s} ({reason_str})"
        )

    if not legacy_count:
        print("No complete legacy-detail rollback generation is available.", file=sys.stderr)
        if args.apply:
            return 1

    if not args.apply:
        print("\n[DRY RUN] Pass --apply to actually delete.")
        return 0

    print("\n--- Deleting ---")
    for gen in to_delete:
        try:
            shutil.rmtree(gen.directory)
            print(f"  Deleted: {gen.generation_id}")
        except Exception as exc:
            print(f"  FAILED to delete {gen.generation_id}: {exc}", file=sys.stderr)

    print(f"\nDone. Freed {format_size(delete_size)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

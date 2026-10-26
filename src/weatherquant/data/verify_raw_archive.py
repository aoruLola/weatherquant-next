"""Verify the raw JSONL archive's checksum trail (WQN-21).

Recomputes every partition's chained digest from the raw bytes and compares
it with the ``.sha256`` sidecar the collector published, plus the structural
checks (terminated final line, strictly increasing ``archive_seq``).

Run it on the NAS against the mounted data directory, or on a copied archive
elsewhere - it opens everything read-only::

    python -m weatherquant.data.verify_raw_archive /mnt/user/appdata/weatherquant-next/raw

Exit codes: 0 every partition verified; 1 at least one did not; 2 the
directory could not be read at all.

This lives in the package (rather than only in ``scripts/``) so the published
collector image can run it against a read-only mount without any extra files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from weatherquant.data.archive import verify_archive_tree

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_raw_archive",
        description="Recompute the raw JSONL archive's checksum trail and compare with sidecars.",
    )
    parser.add_argument("root", type=Path, help="the collector's raw/ directory")
    args = parser.parse_args(argv)

    results = verify_archive_tree(args.root)
    if not results:
        print(f"no JSONL partitions under {args.root}", file=sys.stderr)
        return 2

    bad = 0
    for record in results:
        path = record["path"]
        if record["ok"]:
            print(f"ok    {path}  ({record.get('lines', 0)} lines)")
            continue
        bad += 1
        print(f"FAIL  {path}")
        for problem in record["problems"]:
            print(f"      - {problem}")

    if bad:
        print(f"\n{bad} of {len(results)} partition(s) failed verification", file=sys.stderr)
        return 1
    print(f"\nall {len(results)} partition(s) verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

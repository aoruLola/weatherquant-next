"""Report what migrating the legacy database would do. Reads only.

Usage:
    uv run python scripts/legacy_plan.py [path-to-legacy.duckdb]

The legacy file is opened read-only; this script writes zero bytes to it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weatherquant.config import Settings
from weatherquant.legacy import LegacySource, plan_import


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Settings().legacy_db_path
    if not path.exists():
        print(f"legacy database not found: {path}", file=sys.stderr)
        return 2

    with LegacySource(path) as src:
        plan = plan_import(src)

    print(plan.summary())
    print()
    print("pending-verification reasons:")
    found = False
    for table in plan.migrating_tables:
        if not table.pending:
            continue
        found = True
        print(f"  {table.legacy_table} ({table.pending}/{table.rows}):")
        for reason, n in table.reasons:
            print(f"    {n:>7}  {reason}")
    if not found:
        print("  (none - every migratable row passed all rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

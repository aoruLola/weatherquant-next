#!/usr/bin/env python3
"""Repo-side wrapper: verify the raw JSONL archive's checksum trail (WQN-21).

The implementation lives in the package so the published image can run it
(``python -m weatherquant.data.verify_raw_archive``); this wrapper only
exists for ``uv run python scripts/verify_raw_archive.py`` convenience in a
checkout. Arguments and exit codes are identical.
"""

from __future__ import annotations

from weatherquant.data.verify_raw_archive import main

if __name__ == "__main__":
    raise SystemExit(main())

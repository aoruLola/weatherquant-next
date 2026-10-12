"""Atomic publication of files for readers that cannot open the database.

The shadow's DuckDB file is held by one process for its whole life, and
DuckDB blocks any *other* process from opening it at all - read-only
included. So everything an outside reader needs has to arrive some other way,
and the pattern is always the same: write a small JSON file and swap it into
place.

One implementation, two callers (the heartbeat and the report snapshot),
because two copies of a fifteen-line routine is exactly how the CI checks
drifted apart within a single session.

The file is left world-readable. ``mkstemp`` creates the temporary file 0600
and ``os.replace`` carries that mode onto the target, so without the explicit
chmod only the writer's own uid could ever read it - and the entire purpose
of these files is that something else reads them: a container probe, an
operator, a CI job. Nothing in either payload is secret.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_json"]


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    """Write ``payload`` as JSON so a reader never sees a partial file.

    A reader may open this at any instant: a half-written document would fail
    to parse and be indistinguishable from corruption. Writing to a temporary
    file in the same directory and renaming is atomic on POSIX, and the
    directory is the same one so the rename cannot cross a filesystem.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, sort_keys=True, default=str)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        with contextlib.suppress(OSError):
            os.chmod(target, 0o644)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target

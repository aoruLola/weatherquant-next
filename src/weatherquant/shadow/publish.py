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
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "PartitionedArray",
    "StreamedArray",
    "atomic_write_json",
    "atomic_write_json_streaming",
    "contains_streamed_array",
]


class StreamedArray:
    """A list that is written as it is produced, instead of being held.

    The published performance snapshots contain two arrays that grow with the
    whole history: every ACCEPT edge (``by_edge_band``) and every episode
    (``opportunities.episodes``). Building either as a Python list is exactly
    the memory wall the streaming engine exists to remove, so a payload may
    carry one of these instead and the writer pulls items from the store while
    it writes.

    The object is a marker, not a sequence: it deliberately does not implement
    ``__len__`` or ``__getitem__``, so nothing can quietly materialise it.
    ``__iter__`` is provided only so tests can read one back.
    """

    __slots__ = ("_factory", "label")

    def __init__(self, factory: Callable[[], Iterable[Any]], *, label: str | None = None) -> None:
        self._factory = factory
        self.label = label

    def __iter__(self):
        return iter(self._factory())


class PartitionedArray:
    """A collection published as a directory of bounded files, not as one array.

    A JSON array cannot be read by page: a reader that wants rows 4001-4050 of
    ``opportunities.episodes`` has to parse all 240,000 of them first, which is
    how a 163 MB snapshot became a 163 MB dashboard response. Publishing the
    same rows as fixed-size parts turns "read a page" into "read a part", and
    each part records its first and last sort key so a reader can find the part
    a cursor points at without opening any of them.

    The parts are written into the generation directory before the pointer
    moves, so the batch is still atomic: a reader either sees the whole
    generation or the previous one, and never a half-written directory.
    """

    __slots__ = ("_factory", "collection", "directory", "key_of", "label", "partition_size")

    def __init__(
        self,
        factory: Callable[[], Iterable[Any]],
        *,
        directory: str,
        collection: str,
        partition_size: int = 1000,
        key_of: Callable[[Any], list[Any]] | None = None,
        label: str | None = None,
    ) -> None:
        if partition_size <= 0:
            raise ValueError(f"partition_size must be > 0, got {partition_size!r}")
        self._factory = factory
        self.directory = directory
        self.collection = collection
        self.partition_size = partition_size
        self.key_of = key_of
        self.label = label or collection

    def __iter__(self):
        return iter(self._factory())


def contains_streamed_array(value: Any) -> bool:
    """True if this payload would be written by the streaming path."""
    if isinstance(value, (StreamedArray, PartitionedArray)):
        return True
    if isinstance(value, dict):
        return any(contains_streamed_array(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_streamed_array(item) for item in value)
    return False


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


def atomic_write_json_streaming(
    path: str | Path,
    payload: dict[str, Any],
    *,
    root: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """``atomic_write_json``, but able to stream arrays it cannot hold.

    Byte-for-byte identical to :func:`atomic_write_json` for a payload with no
    :class:`StreamedArray` or :class:`PartitionedArray` in it - same key
    ordering, same separators, same ``default=str`` - so switching a payload
    between the two writers cannot change what a reader sees. A test asserts
    that identity directly.

    ``root`` is the directory a :class:`PartitionedArray` writes its parts
    into. It is required only when the payload carries one, and it is the
    generation directory, so the parts land inside the batch that names them.

    Memory is bounded by one item at a time (plus DuckDB's own fetch buffer),
    which is the whole point: the array never exists anywhere.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _write_value(
                handle,
                payload,
                root=Path(root) if root is not None else None,
                progress=progress,
            )
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


def _write_partitioned_array(
    handle, value: PartitionedArray, *, root: Path, progress: Callable[[str], None] | None
) -> None:
    """Write the parts, then the descriptor that points at them.

    The descriptor replaces the array in place, so a reader that used to get
    ``data.episodes`` as a list now gets an object describing where the rows
    are - including each part's first and last sort key, which is what makes a
    keyset cursor resolvable without reading every part.
    """
    directory = root / value.directory
    directory.mkdir(parents=True, exist_ok=True)
    partitions: list[dict[str, Any]] = []
    total = 0
    buffer: list[Any] = []
    index = 0

    def flush() -> None:
        nonlocal buffer, index, total
        if not buffer:
            return
        name = f"part-{index:05d}.json"
        path = directory / name
        atomic_write_json(path, buffer)
        entry: dict[str, Any] = {
            "file": name,
            "items": len(buffer),
            "bytes": path.stat().st_size,
        }
        if value.key_of is not None:
            entry["first_key"] = value.key_of(buffer[0])
            entry["last_key"] = value.key_of(buffer[-1])
        partitions.append(entry)
        total += len(buffer)
        buffer = []
        index += 1

    for item in value:
        buffer.append(item)
        if len(buffer) >= value.partition_size:
            flush()
    flush()

    handle.write(json.dumps({
        "collection": value.collection,
        "partitioned": True,
        "directory": value.directory,
        "partition_size": value.partition_size,
        "total": total,
        "partitions": partitions,
    }, sort_keys=True, default=str))
    if progress is not None:
        progress(f"{value.label}: {total} item(s) in {len(partitions)} part(s)")


def _write_value(
    handle,
    value: Any,
    *,
    root: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    if isinstance(value, PartitionedArray):
        if root is None:
            raise ValueError(
                f"{value.label} is published as parts, which needs the generation "
                "directory to write them into"
            )
        _write_partitioned_array(handle, value, root=root, progress=progress)
        return
    if isinstance(value, StreamedArray):
        handle.write("[")
        first = True
        for count, item in enumerate(value, start=1):
            if first:
                first = False
            else:
                handle.write(", ")
            handle.write(json.dumps(item, default=str))
            if progress is not None and count % 100_000 == 0:
                progress(f"{value.label or 'array'}: {count} items")
        handle.write("]")
        return
    if isinstance(value, dict):
        handle.write("{")
        first = True
        for key in sorted(value, key=str):
            if first:
                first = False
            else:
                handle.write(", ")
            handle.write(json.dumps(str(key), default=str))
            handle.write(": ")
            _write_value(handle, value[key], root=root, progress=progress)
        handle.write("}")
        return
    if isinstance(value, (list, tuple)):
        handle.write("[")
        for index, item in enumerate(value):
            if index:
                handle.write(", ")
            _write_value(handle, item, root=root, progress=progress)
        handle.write("]")
        return
    handle.write(json.dumps(value, default=str))


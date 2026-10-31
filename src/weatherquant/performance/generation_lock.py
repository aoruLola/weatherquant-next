"""Serialize mutations of one published performance tree."""

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class GenerationBusy(RuntimeError):
    """Another publish or maintenance command is changing this tree."""


@contextmanager
def generation_lock(root: Path) -> Iterator[None]:
    # Keep one stable file. Closing the descriptor releases the OS lock,
    # including after a killed worker. Unlinking it would allow a second lock.
    with (root / ".generation.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            raise GenerationBusy(f"generation tree busy: {root}") from exc
        yield

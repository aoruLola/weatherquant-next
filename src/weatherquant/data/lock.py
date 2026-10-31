"""Single-instance protection for the collector.

DuckDB allows exactly one writer process. Two collectors sharing a data
directory would fight for the file lock and one would die with an opaque IO
error, so exclusivity is claimed *before* the database is opened.

Implementation notes
--------------------
* The lock is an ``O_CREAT | O_EXCL`` file containing the owner pid, run id and
  acquisition time. Creating it is atomic on every platform this targets, which
  is what makes the check race-free.
* The lock is **leased**: if the owning pid is gone, or its heartbeat is stale,
  the lock is considered abandoned and may be taken over. Without this, an
  OOM-killed container would leave a lock that blocks every future run.
* Ownership is verified by pid liveness *and* run id, never by pid alone: pid
  reuse is common inside containers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["LockError", "LockInfo", "SingleInstanceLock"]


class LockError(RuntimeError):
    """The data directory is already owned by another live collector."""


@dataclass(frozen=True, slots=True)
class LockInfo:
    run_id: str
    pid: int
    hostname: str
    acquired_at_utc: datetime
    heartbeat_at_utc: datetime

    @classmethod
    def from_json(cls, payload: str) -> LockInfo:
        data = json.loads(payload)
        return cls(
            run_id=str(data["run_id"]),
            pid=int(data["pid"]),
            hostname=str(data.get("hostname", "")),
            acquired_at_utc=datetime.fromisoformat(data["acquired_at_utc"]),
            heartbeat_at_utc=datetime.fromisoformat(data["heartbeat_at_utc"]),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "pid": self.pid,
                "hostname": self.hostname,
                "acquired_at_utc": self.acquired_at_utc.isoformat(),
                "heartbeat_at_utc": self.heartbeat_at_utc.isoformat(),
            },
            sort_keys=True,
        )


def _pid_alive(pid: int) -> bool:
    """True only if ``pid`` names a process that is genuinely running.

    Windows needs a different mechanism, and the difference matters: there
    ``os.kill(pid, 0)`` *succeeds* for a process that has already exited but
    whose object is still open, and raises ``OSError(EINVAL)`` for a pid that
    never existed - and both of those look like "alive" to the POSIX-oriented
    fallback below. The consequence was that an abandoned lock was never
    reclaimed and every subsequent start exited 4.

    A Windows supervisor can still hold a handle to an exited process.
    The handle is signaled on exit, even while its PID can still be opened.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import _winapi

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as exc:
            # ERROR_INVALID_PARAMETER means that the PID no longer exists.
            return exc.winerror != 87
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to someone else.
        return True
    except OSError:
        # Unknown state: never steal a lock we cannot prove is dead.
        return True
    return True


class SingleInstanceLock:
    """A leased, crash-tolerant exclusive lock over a data directory.

    Usage::

        with SingleInstanceLock(path, run_id=..., stale_after=120.0) as lock:
            ...            # lock.heartbeat() periodically
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        hostname: str,
        stale_after_seconds: float = 120.0,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.hostname = hostname
        self.stale_after_seconds = stale_after_seconds
        self._held = False
        self._acquired_at = datetime.now(tz=UTC)

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Three attempts: a stale takeover can lose a race with another starter,
        # so the create is retried rather than trusted to win first time.
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._try_take_over_stale():
                    continue
                holder = self._read_holder()
                detail = f"held by pid={holder.pid} run={holder.run_id}" if holder else "unreadable"
                raise LockError(
                    f"another collector holds {self.path} ({detail}); refusing to start a "
                    "second writer against the same DuckDB file"
                ) from None
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(self._current_info().to_json())
                self._held = True
                return
        raise LockError(f"could not acquire {self.path} after 3 attempts")

    def heartbeat(self) -> None:
        if not self._held:
            raise LockError("heartbeat() called without holding the lock")
        self.path.write_text(self._current_info().to_json(), encoding="utf-8")

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            holder = self._read_holder()
            if holder is not None and holder.run_id == self.run_id:
                self.path.unlink(missing_ok=True)
        except OSError:
            # A lock we cannot delete will be reclaimed by the stale check on
            # the next run once our pid is gone - which it now is.
            pass

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- internals ---------------------------------------------------------

    def _current_info(self) -> LockInfo:
        now = datetime.now(tz=UTC)
        return LockInfo(
            run_id=self.run_id,
            pid=os.getpid(),
            hostname=self.hostname,
            acquired_at_utc=self._acquired_at,
            heartbeat_at_utc=now,
        )

    def _read_holder(self) -> LockInfo | None:
        try:
            return LockInfo.from_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _try_take_over_stale(self) -> bool:
        """Remove an abandoned lock. Returns True if we cleared it."""
        holder = self._read_holder()
        if holder is None:
            # Unreadable or half-written: only reclaim if it is old enough that
            # a concurrent creator has certainly finished writing it.
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            if age < self.stale_after_seconds:
                return False
            self.path.unlink(missing_ok=True)
            return True

        if holder.run_id == self.run_id and _pid_alive(holder.pid):
            # Same process re-entering - not a conflict.
            return False

        heartbeat_age = (datetime.now(tz=UTC) - holder.heartbeat_at_utc).total_seconds()
        if _pid_alive(holder.pid) and heartbeat_age < self.stale_after_seconds:
            return False

        # Either the owner is dead, or it stopped beating. Both mean abandoned.
        self.path.unlink(missing_ok=True)
        return True

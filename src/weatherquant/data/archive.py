"""Append-only archive of raw upstream payloads.

Why files as well as DuckDB
---------------------------
The parsed rows in DuckDB are a *derived* view: they only keep the fields a
schema anticipated. The raw frame is what lets us re-derive them later when we
learn the payload had a field we ignored - and it is the only evidence of what
the exchange actually sent when a parsed row looks wrong.

Envelope
--------
Every line is one JSON object::

    {
      "archive_seq": 1234,            # monotonic within the file
      "source": "ws" | "rest" | "gamma",
      "received_at_utc": "2026-10-10T12:00:00.123+00:00",   # OUR clock
      "venue_ts_utc": null,            # THEIR clock, or null
      "venue_ts_origin": "missing",    # "venue" | "missing"
      "quality": "ok",                 # parse/shape verdict
      "run_id": "...",
      "payload": { ... verbatim ... }
    }

``received_at_utc`` and ``venue_ts_utc`` are different columns for a reason:
collapsing them would make latency measurements meaningless. When the upstream
message carries no event time, ``venue_ts_utc`` stays ``null`` and
``venue_ts_origin`` is ``"missing"`` - **a local timestamp is never written
into the venue field.**

Files are partitioned by source and UTC date: ``raw/<source>/YYYY-MM-DD.jsonl``.
Partitioning by date keeps files bounded and lets a restore or a parquet
export operate on one day at a time.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

__all__ = ["ArchiveRecord", "RawArchive"]


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    source: str
    payload: Any
    received_at_utc: datetime
    venue_ts_utc: datetime | None
    quality: str
    run_id: str

    @property
    def venue_ts_origin(self) -> str:
        return "venue" if self.venue_ts_utc is not None else "missing"


class RawArchive:
    """Buffered JSONL writer, partitioned by source and UTC date.

    Writes are buffered and flushed on size/count/time thresholds so that a
    per-frame ``fsync`` does not turn into the collector's bottleneck. The
    buffer is flushed on :meth:`close` and on demand, which the worker calls
    before checkpointing and before exiting.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str,
        flush_records: int = 200,
        flush_seconds: float = 5.0,
    ) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.flush_records = flush_records
        self.flush_seconds = flush_seconds
        self._buffer: list[str] = []
        self._seq = 0
        self._last_flush = datetime.now(tz=UTC)
        self._lock = threading.Lock()
        self._closed = False
        self._write_uncertain = False
        self.records_written = 0

    # -- writing -----------------------------------------------------------

    def append(self, record: ArchiveRecord) -> None:
        if record.run_id != self.run_id:
            raise ValueError(
                f"archive record carries run_id={record.run_id!r} but this archive "
                f"belongs to {self.run_id!r}; cross-run records would break provenance"
            )
        if record.venue_ts_utc is not None and record.venue_ts_utc.tzinfo is None:
            raise ValueError("venue_ts_utc must be timezone-aware")

        with self._lock:
            if self._closed or self._write_uncertain:
                raise RuntimeError("archive is closed or its last write could not be rolled back")
            self._seq += 1
            line = json.dumps(
                {
                    "archive_seq": self._seq,
                    "source": record.source,
                    "received_at_utc": record.received_at_utc.isoformat(),
                    "venue_ts_utc": record.venue_ts_utc.isoformat() if record.venue_ts_utc else None,
                    "venue_ts_origin": record.venue_ts_origin,
                    "quality": record.quality,
                    "run_id": record.run_id,
                    "payload": record.payload,
                },
                separators=(",", ":"),
                default=str,
            )
            self._buffer.append(line)
            due = (
                len(self._buffer) >= self.flush_records
                or (datetime.now(tz=UTC) - self._last_flush).total_seconds() >= self.flush_seconds
            )
            if due:
                self._flush_locked()

    def flush(self) -> int:
        """Write and fsync complete JSONL records. Returns durable lines."""
        with self._lock:
            return self._flush_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            self._closed = True

    # -- internals ---------------------------------------------------------

    def _flush_locked(self) -> int:
        if self._write_uncertain:
            raise RuntimeError("archive write outcome uncertain; refusing a duplicate-prone retry")
        if not self._buffer:
            self._last_flush = datetime.now(tz=UTC)
            return 0

        # Group without discarding any pending records. A partition is removed
        # only after its complete bytes have been fsynced. Other partitions
        # remain retryable if a later one fails.
        pending: list[tuple[tuple[str, date], str]] = []
        by_file: dict[tuple[str, date], list[str]] = {}
        for line in self._buffer:
            obj = json.loads(line)
            day = datetime.fromisoformat(obj["received_at_utc"]).astimezone(UTC).date()
            key = (obj["source"], day)
            pending.append((key, line))
            by_file.setdefault(key, []).append(line)

        durable = 0
        for key, lines in by_file.items():
            directory = self.root / key[0]
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{key[1].isoformat()}.jsonl"
            payload = ("\n".join(lines) + "\n").encode("utf-8")
            # O_APPEND protects the line boundary even after a short write.
            # The collector lock guarantees this process is the sole writer.
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                original_size = os.fstat(fd).st_size
                if original_size:
                    # lseek + read rather than `os.pread`: pread is
                    # POSIX-only and does not exist on Windows, where
                    # this module is exercised too. O_APPEND pins every
                    # *write* to the end on both platforms, so moving the
                    # descriptor for a one-byte read cannot move where the
                    # payload lands.
                    os.lseek(fd, original_size - 1, os.SEEK_SET)
                    last_byte = os.read(fd, 1)
                else:
                    last_byte = b"\n"
                if original_size and last_byte != b"\n":
                    # A torn last line from a previous process must not be
                    # buried in the JSONL stream. Preserve it for inspection;
                    # refuse until repaired rather than silently discard it.
                    raise RuntimeError(f"archive has an unterminated line: {path}")
                try:
                    offset = 0
                    while offset < len(payload):
                        count = os.write(fd, payload[offset:])
                        if count <= 0:
                            raise OSError("archive write made no progress")
                        offset += count
                    # os.write completion alone is not a durability promise.
                    os.fsync(fd)
                except OSError:
                    try:
                        # Remove this attempt's partial bytes before retry.
                        # Completed historical records remain untouched.
                        os.ftruncate(fd, original_size)
                        os.fsync(fd)
                    except OSError:
                        # A rollback that cannot be verified means retrying
                        # could duplicate a complete line. Fail closed.
                        self._write_uncertain = True
                    raise
            finally:
                os.close(fd)

            durable += len(lines)
            self.records_written += len(lines)
            pending = [(partition, line) for partition, line in pending if partition != key]
            self._buffer = [line for _, line in pending]

        self._last_flush = datetime.now(tz=UTC)
        return durable

    def __enter__(self) -> RawArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def extract_venue_ts(payload: Any) -> datetime | None:
    """Pull a venue event timestamp out of a payload, or return ``None``.

    **Never** falls back to the local clock. If the upstream message carries no
    recognisable event time, the caller stores ``None`` and marks
    ``venue_ts_origin='missing'``.

    Accepts millisecond, second and microsecond epoch values because different
    Polymarket endpoints disagree on the unit; the magnitude decides which.
    """
    if isinstance(payload, dict):
        for key in ("timestamp", "ts", "time", "last_trade_time", "event_time"):
            if key in payload and payload[key] is not None:
                value = payload[key]
                parsed = _epoch_to_datetime(value)
                if parsed is not None:
                    return parsed
    return None


def _epoch_to_datetime(value: Any) -> datetime | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None
    if numeric <= 0:
        return None
    if numeric > 1e17:  # nanoseconds
        numeric /= 1e9
    elif numeric > 1e14:  # microseconds
        numeric /= 1e6
    elif numeric > 1e11:  # milliseconds
        numeric /= 1e3
    elif numeric > 1e9:
        pass  # seconds
    else:
        # Too small to be a plausible epoch - refuse rather than guess.
        return None
    try:
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None

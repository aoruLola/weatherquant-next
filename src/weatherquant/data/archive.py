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
        self._open: dict[tuple[str, date], Any] = {}
        self._closed = False
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
        with self._lock:
            self._buffer.append(line)
            due = (
                len(self._buffer) >= self.flush_records
                or (datetime.now(tz=UTC) - self._last_flush).total_seconds() >= self.flush_seconds
            )
            if due:
                self._flush_locked()

    def flush(self) -> int:
        """Force buffered lines to disk. Returns lines written."""
        with self._lock:
            return self._flush_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
            for fh in self._open.values():
                fh.close()
            self._open.clear()
            self._closed = True

    # -- internals ---------------------------------------------------------

    def _flush_locked(self) -> int:
        if not self._buffer:
            self._last_flush = datetime.now(tz=UTC)
            return 0
        by_file: dict[tuple[str, date], list[str]] = {}
        # Re-derive the partition from the buffered line so a single buffer can
        # span a UTC midnight without needing a second buffer.
        for line in self._buffer:
            obj = json.loads(line)
            day = datetime.fromisoformat(obj["received_at_utc"]).astimezone(UTC).date()
            by_file.setdefault((obj["source"], day), []).append(line)
        self._buffer.clear()

        written = 0
        for key, lines in by_file.items():
            fh = self._handle(*key)
            fh.write("\n".join(lines) + "\n")
            written += len(lines)
        self.records_written += written
        self._last_flush = datetime.now(tz=UTC)
        return written

    def _handle(self, source: str, day: date):
        key = (source, day)
        fh = self._open.get(key)
        if fh is not None:
            return fh
        directory = self.root / source
        directory.mkdir(parents=True, exist_ok=True)
        # Held open deliberately across appends and released in close(); one
        # handle per (source, date) partition keeps a day's writes sequential.
        fh = open(directory / f"{day.isoformat()}.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        self._open[key] = fh
        return fh

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

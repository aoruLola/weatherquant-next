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

Integrity trail (WQN-21)
------------------------
Two kinds of file sit next to the data and describe what the writer
durable-wrote:

* ``<file>.sha256`` - a sidecar with a **chained digest**: the raw bytes of
  each line are folded into the previous digest, so the chain is
  reproducible from the file's bytes alone (hashed per *line*, never per
  flush batch - batch boundaries exist only in memory and would not survive
  a restart). It records the byte and line counts the digest covers; bytes
  beyond that count are written but not yet checksummed.
* ``recovery.jsonl`` - one line per *recovery event*: a torn final line found
  on restart, a sidecar that no longer matches the file and had to be
  rebuilt, or an ``archive_seq`` baseline picked up from a previous run.
  These are the events an operator needs when reconstructing what happened;
  none of them may silently pass.

``archive_seq`` is monotonic *within a file*, including across process
restarts: a restarted archive reads the last durable line of each partition
and continues from its sequence number, so a restart never mints an id the
previous run already used. :func:`verify_archive_tree` recomputes every
chain from the raw bytes and reports any disagreement with the sidecars.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

__all__ = ["ArchiveRecord", "RawArchive", "verify_archive_tree"]

log = logging.getLogger(__name__)

# The digest of an empty file - the root of every chain.
EMPTY_CHAIN = hashlib.sha256(b"").hexdigest()

# How far back to read when picking up an archive_seq baseline from an
# existing partition file. 64 KiB covers a day's tail at any realistic line
# length; the read happens once per partition per process.
_TAIL_READ_BYTES = 65536


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
        # Integrity trail state, loaded lazily per partition on first contact:
        # {"chain": hex, "bytes": covered byte count, "lines": covered lines}.
        self._chains: dict[tuple[str, date], dict[str, Any]] = {}
        # Partitions whose files have had their archive_seq baseline read.
        self._seen_partitions: set[tuple[str, date]] = set()

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
            # Before minting sequence numbers into this partition, continue
            # from whatever the previous run durable-wrote there. Without the
            # baseline a restart would re-mint seq 1, 2, 3... into a file that
            # already carries them - and "monotonic within the file" would be
            # a claim the file contradicts.
            key = (record.source, record.received_at_utc.astimezone(UTC).date())
            if key not in self._seen_partitions:
                self._seen_partitions.add(key)
                self._continue_seq_from_file_locked(key)
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

    def _partition_path(self, key: tuple[str, date]) -> Path:
        return self.root / key[0] / f"{key[1].isoformat()}.jsonl"

    def _continue_seq_from_file_locked(self, key: tuple[str, date]) -> None:
        """Pick up the sequence baseline a previous run left in this partition."""
        path = self._partition_path(key)
        try:
            last_seq = _last_seq_in_file(path)
        except OSError:
            last_seq = 0
        if last_seq > self._seq:
            self._seq = last_seq
            self._log_recovery(
                "seq_continued_from_file",
                path=str(path),
                last_durable_seq=last_seq,
                run_id=self.run_id,
            )

    def _log_recovery(self, event: str, **fields: Any) -> None:
        """Append one recovery event to ``<root>/recovery.jsonl``.

        Best-effort by design: an unreadable recovery log must never stop the
        archive from writing (or refusing to write) data. The fsync is not
        optional - a recovery note that itself vanishes on crash is exactly
        the evidence an operator will not have.
        """
        entry = {
            "ts_utc": datetime.now(tz=UTC).isoformat(),
            "event": event,
            "run_id": self.run_id,
            **fields,
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / "recovery.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                data = (json.dumps(entry, separators=(",", ":"), default=str) + "\n").encode()
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise OSError("recovery log write made no progress")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            log.debug("could not append recovery log entry %s", event, exc_info=True)

    def _chain_state_locked(self, key: tuple[str, date], path: Path) -> dict[str, Any]:
        """The partition's checksum state, loaded on first contact.

        A sidecar that matches the file's size is trusted (detecting a *content*
        mismatch is :func:`verify_archive_tree`'s job - it costs a full rehash
        and must not sit on the write path). A missing or short sidecar means
        the file outgrew its checksums - a crash, or a run that predates this
        trail - and the chain is rebuilt from the bytes that are actually
        there, with a recovery event naming the discontinuity.
        """
        state = self._chains.get(key)
        if state is not None:
            return state
        size = path.stat().st_size if path.exists() else 0
        sidecar = _read_sidecar(path)
        if sidecar is not None and size == sidecar.get("bytes"):
            state = dict(sidecar)
        elif size:
            chain, covered_bytes, lines = _scan_file_trail(path)
            state = {"chain": chain, "bytes": covered_bytes, "lines": lines}
            self._log_recovery(
                "checksum_rebuilt",
                path=str(path),
                file_bytes=size,
                trail_bytes=covered_bytes,
                sidecar_bytes=None if sidecar is None else sidecar.get("bytes"),
                reason="sidecar missing or does not match file size",
            )
        else:
            state = {"chain": EMPTY_CHAIN, "bytes": 0, "lines": 0}
        self._chains[key] = state
        return state

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
            # Load the checksum state *before* this batch's bytes exist: the
            # rebuild path hashes what is on disk, and after the write that
            # would already include this batch - folding the batch on top
            # would count it twice.
            chain_state = self._chain_state_locked(key, path)
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
                    self._log_recovery(
                        "unterminated_line_detected",
                        path=str(path),
                        file_bytes=original_size,
                        detail="appending would bury a torn final line; repair required",
                    )
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
                        self._log_recovery(
                            "write_uncertain",
                            path=str(path),
                            detail="rollback after write failure could not be verified",
                        )
                    raise
            finally:
                os.close(fd)

            # Durability reached: extend the checksum trail to cover exactly
            # the bytes just written, then publish the sidecar atomically. A
            # crash between the two leaves a sidecar that covers fewer bytes
            # than the file has - visible, honest, and rebuilt (with a
            # recovery event) by the next run.
            chain = chain_state["chain"]
            for line in lines:
                chain = _chain_digest(chain, (line + "\n").encode("utf-8"))
            chain_state["chain"] = chain
            chain_state["bytes"] += len(payload)
            chain_state["lines"] += len(lines)
            _write_sidecar(path, chain_state)

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


# ---------------------------------------------------------------------------
# Integrity trail helpers (WQN-21)
# ---------------------------------------------------------------------------


def _chain_digest(previous_hex: str, payload: bytes) -> str:
    """Fold one line's bytes into the running chained digest.

    ``sha256(previous_digest_bytes || line_bytes)``. The chain starts from the
    digest of the empty file, so an empty archive has a well-known value and
    every step is reproducible from the file alone - the property that makes
    :func:`verify_archive_tree` possible without knowing flush boundaries.
    """
    return hashlib.sha256(bytes.fromhex(previous_hex) + payload).hexdigest()


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".sha256")


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    """The partition's checksum sidecar, normalised to the in-memory shape.

    On disk the digest is named ``chain_sha256`` (self-describing for an
    operator who finds the file); in memory it is ``chain``. Mapping here -
    rather than letting two spellings leak through every caller - keeps the
    loaded state exactly the shape a flush updates.
    """
    try:
        raw = _sidecar_path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "chain_sha256" not in data or "bytes" not in data:
        return None
    return {
        "chain": data["chain_sha256"],
        "bytes": data["bytes"],
        "lines": data.get("lines", 0),
    }


def _write_sidecar(path: Path, state: dict[str, Any]) -> None:
    """Publish the sidecar atomically: tmp + fsync + rename + dir fsync.

    A reader (or a crashed writer) must never see a half-written sidecar next
    to a complete file: the sidecar lagging the file is recoverable, a torn
    sidecar is not interpretable at all.
    """
    payload = json.dumps(
        {
            "chain_sha256": state["chain"],
            "bytes": state["bytes"],
            "lines": state["lines"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    tmp = _sidecar_path(path).with_suffix(".sha256.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        data = payload.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("sidecar write made no progress")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, _sidecar_path(path))
    if hasattr(os, "O_DIRECTORY"):
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _iter_line_bytes(path: Path):
    """Yield each complete line of a JSONL file, newline included."""
    with path.open("rb") as fh:
        yield from fh


def _scan_file_trail(path: Path) -> tuple[str, int, int]:
    """``(chained digest, covered bytes, covered lines)`` of a file as it stands.

    Coverage stops at the first line without a trailing newline: bytes after a
    torn line were never fsynced as part of a complete record, so they stay
    *outside* the trail - the file has them, the trail does not, and that
    discrepancy is exactly what verification should report.
    """
    chain = EMPTY_CHAIN
    covered = 0
    lines = 0
    for line in _iter_line_bytes(path):
        if not line.endswith(b"\n"):
            break
        chain = _chain_digest(chain, line)
        covered += len(line)
        lines += 1
    return chain, covered, lines


def _last_seq_in_file(path: Path) -> int:
    """The archive_seq of the last *complete* line, or 0 when unreadable.

    A torn trailing fragment (a crash mid-write) is dropped before looking at
    the last line: it was never durable, so its absence must not hide the
    baseline the surviving lines do provide.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if not size:
        return 0
    with path.open("rb") as fh:
        fh.seek(max(0, size - _TAIL_READ_BYTES))
        chunk = fh.read()
    if chunk and not chunk.endswith(b"\n"):
        # Discard the unterminated tail fragment.
        cut = chunk.rfind(b"\n")
        chunk = chunk[: cut + 1] if cut >= 0 else b""
    complete = [ln for ln in chunk.split(b"\n") if ln.strip()]
    if not complete:
        return 0
    try:
        obj = json.loads(complete[-1])
        return int(obj.get("archive_seq") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def verify_archive_tree(root: str | Path) -> list[dict[str, Any]]:
    """Recompute every partition's chained digest and compare with its sidecar.

    Returns one record per ``*.jsonl`` partition. A partition is ``ok`` only
    when the sidecar exists, its byte and line counts match the file, and the
    digest recomputed from the raw bytes equals the sidecar's chain. Extra
    bytes beyond the sidecar's count are reported separately: they are
    written-but-unchecksummed (a crash window), which is a different - and
    smaller - claim than corruption.
    """
    results: list[dict[str, Any]] = []
    root_path = Path(root)
    if not root_path.exists():
        return [{"path": str(root_path), "ok": False, "problems": ["archive root does not exist"]}]
    for path in sorted(root_path.rglob("*.jsonl")):
        if path.name == "recovery.jsonl":
            continue
        problems: list[str] = []
        chain = EMPTY_CHAIN
        lines = 0
        seq: int | None = None
        seq_monotonic = True
        torn_tail = False
        for raw_line in _iter_line_bytes(path):
            if not raw_line.endswith(b"\n"):
                torn_tail = True
                break
            chain = _chain_digest(chain, raw_line)
            lines += 1
            try:
                obj = json.loads(raw_line)
                current = int(obj.get("archive_seq") or 0)
            except (json.JSONDecodeError, ValueError, TypeError):
                problems.append(f"line {lines} is not parseable JSON with an archive_seq")
                current = None
            if current is not None:
                if seq is not None and current <= seq:
                    seq_monotonic = False
                seq = current
        if torn_tail:
            problems.append("file ends with a torn (newline-less) line")
        if not seq_monotonic:
            problems.append("archive_seq is not strictly increasing within the file")

        sidecar = _read_sidecar(path)
        if sidecar is None:
            problems.append("no readable .sha256 sidecar")
        else:
            if sidecar.get("chain") != chain:
                problems.append(
                    f"chain mismatch: sidecar {str(sidecar.get('chain'))[:16]}... "
                    f"vs recomputed {chain[:16]}..."
                )
            if sidecar.get("lines") != lines:
                problems.append(f"line count mismatch: sidecar {sidecar.get('lines')} vs {lines}")
            file_bytes = path.stat().st_size
            if sidecar.get("bytes") != file_bytes:
                kind = (
                    "bytes beyond checksum trail (crash window)"
                    if file_bytes > int(sidecar.get("bytes", 0))
                    else "file is shorter than the checksum trail claims"
                )
                problems.append(
                    f"byte count mismatch: sidecar {sidecar.get('bytes')} vs file "
                    f"{file_bytes} - {kind}"
                )
        results.append(
            {
                "path": str(path),
                "ok": not problems,
                "lines": lines,
                "problems": problems,
            }
        )
    return results


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

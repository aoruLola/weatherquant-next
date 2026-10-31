"""Read-only tail of the collector's raw JSONL archive.

Contract
--------
The archive is mounted read-only and is treated as an *append-only stream of
complete lines*:

* A line is consumed only once a trailing newline has been seen, so a
  half-written line is never decoded. The offset only ever advances past
  ``\\n``.
* The byte offset per file is committed **in the same transaction** as the
  effects of the lines it covers. A crash therefore replays a batch or skips
  nothing, rather than the two disagreeing.
* Truncation, disappearance and malformed-but-complete lines are each recorded
  as a data-quality event. None are skipped silently, because "the file looked
  fine" is exactly what a silent skip assumes.

Ordering is by ``(source, date)``, the archive's own partition key. Across
files the order is well-defined but arbitrary - the archive carries no
cross-file sequence - so nothing downstream may depend on it.

Clocks
------
``received_at_utc`` on the envelope is when the *collector* saw the event;
``shadow_received_at_utc`` is when this process read it. The difference is the
read delay the specification asks to measure, and the two are never merged:
collapsing them would make a slow shadow look like a fast collector.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from weatherquant.shadow.events import (
    BookObservation,
    CatalogMarket,
    Envelope,
    EventKind,
    classify,
    market_of_token_table,
    parse_envelope,
    parse_gamma,
    parse_ws_frame,
    rest_book,
)

__all__ = [
    "ArchiveTailer",
    "Checkpoint",
    "QualityEvent",
    "ReadStats",
    "TailResult",
]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    file_path: str
    source: str
    offset_bytes: int
    lines_processed: int
    last_run_id: str | None
    last_archive_seq: int | None
    last_received_at_utc: datetime | None
    file_identity: str
    updated_at_utc: datetime


@dataclass(frozen=True, slots=True)
class QualityEvent:
    """Something the archive did that must not pass unrecorded."""

    kind: str
    severity: str  # info | warning | error
    detail: str
    source: str | None = None
    path: str | None = None
    offset_bytes: int | None = None
    ts_utc: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass
class ReadStats:
    lines_read: int = 0
    lines_malformed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    books: int = 0
    books_incomplete: int = 0
    markets: int = 0
    deltas_ignored: int = 0
    unattached_books: int = 0
    shadow_lag_ms_max: float = 0.0
    collector_received_max: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "lines_read": self.lines_read,
            "lines_malformed": self.lines_malformed,
            "by_kind": dict(self.by_kind),
            "books": self.books,
            "books_incomplete": self.books_incomplete,
            "markets": self.markets,
            "deltas_ignored": self.deltas_ignored,
            "unattached_books": self.unattached_books,
            "shadow_lag_ms_max": round(self.shadow_lag_ms_max, 3),
            "collector_received_max": self.collector_received_max.isoformat()
            if self.collector_received_max
            else None,
        }


@dataclass
class TailResult:
    stats: ReadStats
    quality: list[QualityEvent]
    market_updates: list[list[CatalogMarket]]
    book_updates: list[BookObservation]
    notes: list[tuple[EventKind, str]]
    checkpoints: dict[str, Checkpoint]
    # Per-source "newest archive date seen", returned rather than applied.
    # `poll` must not move *any* of this tailer's own state: the caller adopts
    # both maps together, and only once the transaction that carries them has
    # committed. See `ArchiveTailer.adopt`.
    file_dates: dict[str, date]


class ArchiveTailer:
    """Advance per-file offsets over a read-only archive.

    The tailer holds *no* domain state beyond the token->market map it needs
    to attach a book to its market. It decodes lines and reports them;
    whatever the caller does with them is committed together with the
    checkpoints returned alongside.
    """

    def __init__(
        self,
        raw_dir: str | Path,
        *,
        station: str,
        clock: Callable[[], datetime] | None = None,
        max_bytes_per_poll: int = 8 * 1024 * 1024,
    ) -> None:
        self.raw_dir = Path(raw_dir)
        self.station = station
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.max_bytes_per_poll = max_bytes_per_poll
        self.checkpoints: dict[str, Checkpoint] = {}
        self.last_file_date: dict[str, date] = {}
        self.market_of_token_map: dict[str, str] = {}
        # Changes learned during a poll are provisional until Runner commits.
        self._poll_market_of_token_map: dict[str, str] = {}

    # -- lifecycle ----------------------------------------------------------

    def load_checkpoints(self, rows: Iterable) -> None:
        for row in rows:
            (
                file_path,
                source,
                offset_bytes,
                lines_processed,
                last_run_id,
                last_archive_seq,
                last_received_at_utc,
                file_identity,
                updated_at_utc,
            ) = row
            self.checkpoints[file_path] = Checkpoint(
                file_path=file_path,
                source=source,
                offset_bytes=int(offset_bytes),
                lines_processed=int(lines_processed),
                last_run_id=last_run_id,
                last_archive_seq=int(last_archive_seq) if last_archive_seq is not None else None,
                last_received_at_utc=last_received_at_utc,
                file_identity=file_identity,
                updated_at_utc=updated_at_utc,
            )
            parsed = _parse_date(Path(file_path).name)
            if parsed is not None:
                current = self.last_file_date.get(source)
                if current is None or parsed > current:
                    self.last_file_date[source] = parsed

    def set_market_of_token(self, table: dict[str, str]) -> None:
        self.market_of_token_map = dict(table)

    @property
    def market_of_token(self) -> dict[str, str]:
        return dict(self.market_of_token_map)

    # -- reading ------------------------------------------------------------

    def poll(self) -> TailResult:
        # Within-batch metadata must resolve later books without advancing
        # the committed token map if DuckDB rolls back.
        self._poll_market_of_token_map = dict(self.market_of_token_map)
        stats = ReadStats()
        quality: list[QualityEvent] = []
        market_updates: list[list[CatalogMarket]] = []
        book_updates: list[BookObservation] = []
        notes: list[tuple[EventKind, str]] = []
        advanced: dict[str, Checkpoint] = {}
        advanced_dates: dict[str, date] = {}

        for path, source in self._files():
            key = str(path)
            existing = self.checkpoints.get(key)

            if not path.exists():
                if existing is not None:
                    quality.append(
                        QualityEvent(
                            kind="archive_file_missing",
                            severity="error",
                            detail=f"checkpointed archive file no longer exists: {path}",
                            source=source,
                            path=key,
                            offset_bytes=existing.offset_bytes,
                            ts_utc=self.clock(),
                        )
                    )
                continue

            offset = existing.offset_bytes if existing else 0
            was_truncated = False
            if existing is not None and _is_truncated(path, offset):
                was_truncated = True
                quality.append(
                    QualityEvent(
                        kind="archive_file_truncated",
                        severity="error",
                        detail=(
                            f"{path} shrank from offset {offset} to {path.stat().st_size} "
                            "bytes; the tail restarts at byte 0 and the lost range is "
                            "recorded rather than assumed continuous"
                        ),
                        source=source,
                        path=key,
                        offset_bytes=offset,
                        ts_utc=self.clock(),
                    )
                )
                offset = 0

            read = self._read_complete_lines(path, offset)
            if read is None:
                quality.append(
                    QualityEvent(
                        kind="archive_file_unreadable",
                        severity="error",
                        detail=f"could not read {path}; left at offset {offset}",
                        source=source,
                        path=key,
                        offset_bytes=offset,
                        ts_utc=self.clock(),
                    )
                )
                continue
            data, offset_after, stalled = read
            if stalled:
                quality.append(
                    QualityEvent(
                        kind="archive_line_oversized",
                        severity="error",
                        detail=(
                            f"{path} has a single unterminated line of at least "
                            f"{self.max_bytes_per_poll} bytes from offset {offset}; refusing "
                            "to buffer it unbounded, the tail is parked at this offset"
                        ),
                        source=source,
                        path=key,
                        offset_bytes=offset,
                        ts_utc=self.clock(),
                    )
                )
                # Persist the park. Without an entry the same stall would be
                # rediscovered from scratch every poll, and a file that never
                # grows would leave no evidence we had stopped trying.
                advanced[key] = _repositioned(existing, key, source, offset, path, self.clock())
                continue

            lines_here = data.count(b"\n")
            if lines_here:
                last_envelope = self._consume(
                    data,
                    path=path,
                    source=source,
                    stats=stats,
                    quality=quality,
                    market_updates=market_updates,
                    book_updates=book_updates,
                    notes=notes,
                )
                previous = existing
                advanced[key] = _checkpoint_for(
                    key,
                    source,
                    offset_after,
                    (previous.lines_processed if previous else 0) + lines_here,
                    last_envelope,
                    path,
                    self.clock(),
                )
                parsed_date = _parse_date(path.name)
                if parsed_date is not None:
                    self._note_gap(source, parsed_date, quality, advanced_dates)
            elif existing is None or was_truncated:
                # Two cases share this: a brand-new file that needs an entry
                # (or it would be re-scanned as new on every poll), and a
                # truncated one whose offset must be *persisted* at 0 - or the
                # shrink would be re-reported every cycle while the tail kept
                # re-reading from the stale offset.
                advanced[key] = _repositioned(existing, key, source, 0, path, self.clock())

        return TailResult(
            stats=stats,
            quality=quality,
            market_updates=market_updates,
            book_updates=book_updates,
            notes=notes,
            checkpoints=advanced,
            file_dates=advanced_dates,
        )

    def adopt(self, checkpoints: dict[str, Checkpoint], file_dates: dict[str, date] | None = None) -> None:
        """Take ownership of offsets the caller has just committed.

        This is the only place the tailer's own state moves, and it is called
        *after* the DuckDB transaction that wrote the same offsets has
        committed. Updating inside `poll` would advance memory for effects the
        database never saw: a rolled-back batch would then be skipped on the
        next cycle, and the lines it covered would be lost silently.

        Commit first, adopt second. A failure between them re-reads a batch,
        which is recoverable; adopting before the commit loses one, which is
        not.
        """
        self.checkpoints.update(checkpoints)
        if file_dates:
            self.last_file_date.update(file_dates)

    # -- internals ----------------------------------------------------------

    def _files(self) -> list[tuple[Path, str]]:
        """Archive files, plus any we hold a checkpoint for that has since vanished.

        The second half matters: a file we are mid-way through and that then
        disappears would otherwise simply stop being enumerated, and the fact
        that we lost it would never surface as anything at all.
        """
        found: dict[str, tuple[Path, str]] = {}
        if self.raw_dir.exists():
            for directory in sorted(p for p in self.raw_dir.iterdir() if p.is_dir()):
                for path in sorted(directory.glob("*.jsonl")):
                    if _parse_date(path.name) is None:
                        continue
                    found[str(path)] = (path, directory.name)

        prefix = str(self.raw_dir)
        for key, checkpoint in self.checkpoints.items():
            if key in found:
                continue
            if key.startswith(prefix):
                found[key] = (Path(key), checkpoint.source)
        return sorted(found.values(), key=lambda item: str(item[0]))

    def _read_complete_lines(self, path: Path, offset: int) -> tuple[bytes, int, bool] | None:
        """``(complete_bytes, next_offset, stalled)``; ``None`` on an I/O error."""
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read(self.max_bytes_per_poll)
        except OSError:
            return None
        if not data:
            return b"", offset, False
        cut = data.rfind(b"\n")
        if cut == -1:
            if len(data) >= self.max_bytes_per_poll:
                # Longer than the cap with no terminator: parking beats either
                # losing it or growing without bound.
                return b"", offset, True
            # Tail of an in-flight line: leave it for the next poll.
            return b"", offset, False
        return data[: cut + 1], offset + cut + 1, False

    def _consume(
        self,
        data: bytes,
        *,
        path: Path,
        source: str,
        stats: ReadStats,
        quality: list[QualityEvent],
        market_updates: list[list[CatalogMarket]],
        book_updates: list[BookObservation],
        notes: list[tuple[EventKind, str]],
    ) -> Envelope | None:
        last: Envelope | None = None
        for raw in data.split(b"\n"):
            if not raw:
                continue
            line = raw.removesuffix(b"\r").decode("utf-8", errors="replace")
            stats.lines_read += 1

            try:
                envelope = parse_envelope(line)
            except ValueError as exc:
                stats.lines_malformed += 1
                quality.append(
                    QualityEvent(
                        kind="archive_line_malformed",
                        severity="error",
                        detail=f"{path}: {exc}",
                        source=source,
                        path=str(path),
                        ts_utc=self.clock(),
                    )
                )
                continue

            last = envelope
            kind = classify(envelope.source, envelope.payload)
            stats.by_kind[kind.value] = stats.by_kind.get(kind.value, 0) + 1

            lag_ms = (self.clock() - envelope.received_at_utc).total_seconds() * 1000.0
            stats.shadow_lag_ms_max = max(stats.shadow_lag_ms_max, max(lag_ms, 0.0))
            if stats.collector_received_max is None or envelope.received_at_utc > stats.collector_received_max:
                stats.collector_received_max = envelope.received_at_utc

            try:
                self._dispatch(
                    envelope,
                    kind,
                    stats=stats,
                    quality=quality,
                    market_updates=market_updates,
                    book_updates=book_updates,
                    notes=notes,
                )
            except ValueError as exc:
                stats.lines_malformed += 1
                quality.append(
                    QualityEvent(
                        kind="archive_payload_unusable",
                        severity="error",
                        detail=f"{path} seq={envelope.archive_seq}: {exc}",
                        source=source,
                        path=str(path),
                        ts_utc=self.clock(),
                    )
                )
        # ``None`` means every line in the chunk was malformed. The offsets
        # still advance - the bytes were consumed and recorded as errors - and
        # the checkpoint keeps the previous run/sequence provenance.
        return last

    def _dispatch(
        self,
        envelope: Envelope,
        kind: EventKind,
        *,
        stats: ReadStats,
        quality: list[QualityEvent],
        market_updates: list[list[CatalogMarket]],
        book_updates: list[BookObservation],
        notes: list[tuple[EventKind, str]],
    ) -> None:
        if kind is EventKind.MARKET_METADATA:
            markets, problems = parse_gamma(
                envelope.payload, default_station=self.station, run_id=envelope.run_id
            )
            for problem in problems:
                quality.append(
                    QualityEvent(
                        kind="gamma_parse_problem",
                        severity="warning",
                        detail=problem,
                        source=envelope.source,
                        ts_utc=self.clock(),
                    )
                )
            if markets:
                stats.markets += len(markets)
                market_updates.append(markets)
                self._poll_market_of_token_map.update(market_of_token_table(markets))
            return

        if kind is EventKind.BOOK_SNAPSHOT:
            observations, unattached = self._snapshots(envelope)
            stats.books += len(observations)
            stats.unattached_books += len(unattached)
            for detail in unattached:
                notes.append((kind, detail))
            for observation in observations:
                if not observation.complete:
                    stats.books_incomplete += 1
                book_updates.append(replace(observation, source_event_id=envelope.raw_sha256))
            if not observations and not unattached:
                raise ValueError("book frame produced no snapshot and no explanation")
            return

        if kind is EventKind.BOOK_DELTA:
            # Counted and never reconstructed: one level of an unknown book is
            # not a book. See the module docstring.
            stats.deltas_ignored += 1
            notes.append(
                (
                    kind,
                    "price_change delta seen; carries one level only, so it is "
                    "recorded and not priced",
                )
            )
            return

        notes.append((kind, f"{kind.value} recorded; carries no executable depth"))

    def _snapshots(self, envelope: Envelope) -> tuple[list[BookObservation], list[str]]:
        payload = envelope.payload
        shadow_now = self.clock()
        if envelope.source == "rest":
            token_id = str(payload.get("asset_id") or "")
            if not token_id:
                raise ValueError("book payload has no asset_id")
            market_id = self._poll_market_of_token_map.get(token_id, "")
            if not market_id:
                return [], [f"rest book for token {token_id} arrived before any market metadata"]
            return [
                rest_book(
                    payload,
                    token_id=token_id,
                    market_id=market_id,
                    collector_received_at_utc=envelope.received_at_utc,
                    shadow_received_at_utc=shadow_now,
                    run_id=envelope.run_id,
                )
            ], []

        snapshots, ws_notes = parse_ws_frame(
            payload,
            market_of_token=self._poll_market_of_token_map,
            collector_received_at_utc=envelope.received_at_utc,
            shadow_received_at_utc=shadow_now,
            run_id=envelope.run_id,
        )
        return snapshots, [detail for _kind, detail in ws_notes]

    def _note_gap(
        self, source: str, day: date, quality: list[QualityEvent], pending: dict[str, date]
    ) -> None:
        """Record whole missing days in a source's archive, never a silent jump.

        Reads the last *committed* date (or one already advanced by this
        poll) and writes to ``pending`` - never to ``self.last_file_date``.
        If the caller's transaction rolls back, ``pending`` is discarded and
        the next poll re-derives the same gap instead of having lost it.
        """
        previous = pending.get(source, self.last_file_date.get(source))
        if previous is not None and (day - previous).days > 1:
            n_missing = (day - previous).days - 1
            first = previous + timedelta(days=1)
            last = day - timedelta(days=1)
            quality.append(
                QualityEvent(
                    kind="archive_gap",
                    severity="error",
                    detail=(
                        f"no {source} archive file for {n_missing} day(s) "
                        f"({first} .. {last}); the collector was not writing during this span"
                    ),
                    source=source,
                    ts_utc=self.clock(),
                )
            )
        if previous is None or day > previous:
            pending[source] = day


def _is_truncated(path: Path, offset: int) -> bool:
    try:
        return path.stat().st_size < offset
    except OSError:
        return True


def _identity(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _checkpoint_for(
    key: str,
    source: str,
    offset: int,
    lines: int,
    envelope: Envelope | None,
    path: Path,
    now: datetime,
) -> Checkpoint:
    return Checkpoint(
        file_path=key,
        source=source,
        offset_bytes=offset,
        lines_processed=lines,
        last_run_id=envelope.run_id if envelope else None,
        last_archive_seq=envelope.archive_seq if envelope else None,
        last_received_at_utc=envelope.received_at_utc if envelope else None,
        file_identity=_identity(path),
        updated_at_utc=now,
    )


def _parse_date(name: str) -> date | None:
    if not name.endswith(".jsonl"):
        return None
    try:
        return date.fromisoformat(name[: -len(".jsonl")])
    except ValueError:
        return None


def _repositioned(
    existing: Checkpoint | None,
    key: str,
    source: str,
    offset: int,
    path: Path,
    now: datetime,
) -> Checkpoint:
    """A checkpoint at ``offset`` that keeps whatever provenance we already had."""
    if existing is None:
        return _checkpoint_for(key, source, offset, 0, None, path, now)
    return Checkpoint(
        file_path=key,
        source=source,
        offset_bytes=offset,
        lines_processed=existing.lines_processed,
        last_run_id=existing.last_run_id,
        last_archive_seq=existing.last_archive_seq,
        last_received_at_utc=existing.last_received_at_utc,
        file_identity=_identity(path),
        updated_at_utc=now,
    )

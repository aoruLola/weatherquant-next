"""The collector worker: process lifecycle, scheduling and health publication.

Responsibilities, in the order they matter:

1. **Exactly one writer.** The single-instance lock is claimed before the
   database is opened, so two containers sharing a data directory cannot fight
   over DuckDB. Exit code 4 means "someone else owns this data directory" and
   is deliberately distinct from a crash.
2. **Nothing is lost across restarts.** Any gap a previous run left open is
   closed with a reason, and this run opens its own gap that it closes on exit -
   an interruption that leaves no trace would silently overstate coverage.
3. **Graceful shutdown.** SIGTERM stops intake, flushes the batch, checkpoints,
   records the run's exit, publishes a final heartbeat and releases the lock, in
   that order. A container that exits without doing this leaves an open gap and
   an uncheckpointed WAL.
4. **Health without touching the database.** The heartbeat file is rewritten
   every tick even when there is nothing to collect; the Docker probe reads only
   that file, so it never contends for the DuckDB lock.

Scheduling is deliberately simple - fixed intervals on one thread, with the
WebSocket on a producer thread feeding a queue. There is no scheduler library
and no async plumbing: a collector that runs unattended for months needs its
loop readable at 3am, not clever.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import signal
import socket
import threading
import time
import uuid
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from weatherquant import __version__
from weatherquant.config import Settings
from weatherquant.data.archive import ArchiveRecord, RawArchive, extract_venue_ts
from weatherquant.data.books import CLOB_BASE, parse_book_payload
from weatherquant.data.discovery import GammaDiscovery
from weatherquant.data.heartbeat import (
    UPSTREAM_STALL_SOFT_SECONDS,
    CollectionStats,
    HealthStatus,
    Heartbeat,
    HeartbeatWriter,
)
from weatherquant.data.http import ResilientHttp
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.data.ratelimit import RateLimitRegistry
from weatherquant.data.writer import CollectorWriter

log = logging.getLogger(__name__)

__all__ = ["CollectorWorker", "ExitCode", "worker_main"]


class ExitCode:
    """Process exit codes. Distinct values so a supervisor can act on them."""

    OK = 0
    FATAL = 1
    BAD_CONFIG = 2
    LOCK_HELD = 4
    INTERRUPTED = 130


class _Shutdown:
    """Thread-safe shutdown flag set from signal handlers."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def set(self, reason: str) -> None:
        self.reason = reason
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


class CollectorWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        run_id: str | None = None,
        git_sha: str | None = None,
        image_tag: str | None = None,
        # Tunables are constructor args so a soak test can compress them.
        tick_seconds: float = 1.0,
        heartbeat_seconds: float = 10.0,
        book_poll_seconds: float = 5.0,
        discovery_seconds: float = 900.0,
        integrity_seconds: float = 300.0,
        parquet_daily: bool = True,
        backup_daily: bool = True,
        stream_enabled: bool = True,
        once: bool = False,
    ) -> None:
        self.settings = settings
        self.run_id = run_id or uuid.uuid4().hex
        self.git_sha = git_sha
        self.image_tag = image_tag
        self.hostname = socket.gethostname()
        self.tick_seconds = tick_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.book_poll_seconds = book_poll_seconds
        self.discovery_seconds = discovery_seconds
        self.integrity_seconds = integrity_seconds
        self.parquet_daily = parquet_daily
        self.backup_daily = backup_daily
        self.stream_enabled = stream_enabled
        self.once = once

        self.shutdown = _Shutdown()
        self.stats = CollectionStats()
        self.started_at = datetime.now(tz=UTC)

        self._writer: CollectorWriter | None = None
        self._archive: RawArchive | None = None
        self._http: ResilientHttp | None = None

        # Heartbeat pacing uses its own monotonic clock: comparing
        # time.monotonic() against a wall-clock timestamp mixes two unrelated
        # time bases and would fire every tick.
        self._last_heartbeat_mono = 0.0
        self._last_discovery_mono = -1e18
        self._last_book_poll_mono = -1e18
        self._last_integrity_mono = -1e18
        self._last_parquet_day: date | None = None
        self._last_backup_day: date | None = None

        self._last_rest_mid: dict[str, tuple[datetime, float]] = {}
        self._last_mismatch_note = datetime.min.replace(tzinfo=UTC)
        self._last_data_change = datetime.now(tz=UTC)
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._fatal: str | None = None
        self._last_defect: str | None = None
        self._rest_consecutive_failures = 0
        # Cached transport counters so the *final* heartbeat (published after
        # http is closed) still reports what the run achieved rather than zero.
        self._last_contact: datetime | None = None
        self._last_rest_ok = 0
        self._last_rest_failed = 0
        self._last_retries = 0
        self._last_rate_waits = 0
        self._last_timeouts = 0
        self._archive_records = 0
        self._books: dict[str, str] = {}  # token_id -> market_id
        # Tokens the venue says have no orderbook (archived markets). Re-asking
        # every cycle would turn a correct 404 into permanent request noise.
        self._no_book: set[str] = set()
        self._ws_queue: queue.Queue = queue.Queue(maxsize=20_000)
        self._ws_connected = False
        self._ws_last_message: datetime | None = None
        self._ws_endpoint = ""

        # --- polling fairness and cost -----------------------------------
        # Where the last sweep stopped. Without this the budget always cuts
        # the tail of the same list, and the same handful of tokens are
        # deferred forever while the aggregate looks healthy.
        self._poll_cursor = 0
        self._poll_budget_exceeded = 0
        self._poll_deferred = 0
        self._last_poll_budget_note = -1e18
        # Recent request latencies (request-start -> response received), bounded
        # so the heartbeat cannot grow with the run.
        self._rest_latencies: deque[float] = deque(maxlen=200)
        self._last_sweep_latency_ms: dict[str, Any] | None = None
        # When each token was last seen. Answers "which token is being starved"
        # without re-reading the archive.
        self._last_polled_at: dict[str, datetime] = {}
        # High-water mark of the stream queue, sampled by the consumer.
        self._ws_queue_high_water = 0
        self._frames_dropped = 0
        self._stream: Any = None
        self._status = HealthStatus.STARTING


    @staticmethod
    def _data_dir_writable(directory: Path) -> bool:
        """True if we can create files under ``directory``.

        Checked explicitly rather than left to fail mid-startup, because the
        failure mode is a bind mount owned by another uid - a deployment
        mistake, not a code bug, and the message has to say so.
        """
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-probe"
            probe.touch()
            probe.unlink()
        except OSError:
            return False
        return True

    def _publish_safe(self, heartbeat_path: Path) -> None:
        """Best-effort heartbeat so the probe reports *why* startup failed."""
        try:
            publisher = HeartbeatWriter(heartbeat_path)
        except OSError:
            return
        try:
            self._publish(publisher, None)
        except Exception:
            log.debug("could not publish startup failure heartbeat", exc_info=True)

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        settings = self.settings
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_path = settings.db_path.parent / "health" / "heartbeat.json"

        # Pre-flight: the data directory is a bind mount, so the image's own
        # `chown` is masked by whatever the host directory is owned by. Failing
        # here with an actionable message beats a PermissionError traceback deep
        # inside pathlib when the collector starts on a fresh NAS.
        if not self._data_dir_writable(settings.db_path.parent):
            uid, gid = os.getuid(), os.getgid()
            message = (
                f"data directory {settings.db_path.parent} is not writable by uid {uid}. "
                f"A bind mount hides whatever chown the image did, so the host directory "
                f"has to match this uid: `chown -R {uid}:{gid} <host data dir>`, or set "
                f"`user:` in the compose file to the uid that owns it. "
                f"See docs/unraid-collector-deployment.md section 1."
            )
            log.error(message)
            self._status = HealthStatus.FATAL
            self._fatal = message
            self._publish_safe(heartbeat_path)
            return ExitCode.BAD_CONFIG

        publisher = HeartbeatWriter(heartbeat_path)

        lock = SingleInstanceLock(
            settings.db_path.parent / ".collector.lock",
            run_id=self.run_id,
            hostname=self.hostname,
            stale_after_seconds=max(60.0, self.heartbeat_seconds * 4),
        )
        try:
            lock.acquire()
        except LockError as exc:
            log.error("%s", exc)
            # Publish a heartbeat so the probe reports a clear reason rather
            # than "file missing" while another instance runs.
            self._status = HealthStatus.FATAL
            self._fatal = str(exc)
            # Published with no http client: the point is that a refused second
            # instance still tells the probe *why*, rather than leaving a stale
            # heartbeat (or none) behind.
            self._publish(publisher, None)
            return ExitCode.LOCK_HELD

        archive: RawArchive | None = None
        http: ResilientHttp | None = None
        stream = None
        status = HealthStatus.FATAL
        reason = "unknown"

        try:
            self._writer = CollectorWriter(
                settings.db_path,
                run_id=self.run_id,
                flush_rows=500,
                flush_seconds=5.0,
                checkpoint_seconds=300.0,
            )
            self._writer.open()
            # Before this run registers itself: anything still 'running' was
            # abandoned by a process that died without closing (SIGKILL, OOM,
            # watchdog exit - the container can stay Up through all of them).
            # Failing those rows now is what makes "which runs ended
            # abnormally" answerable by query (WQN-5).
            abandoned = self._writer.mark_abandoned_runs_failed()
            if abandoned:
                self._event(
                    "warning",
                    "restart",
                    f"marked {len(abandoned)} abandoned run(s) as failed",
                    {"run_ids": abandoned[:20]},
                )
            self._writer.start_run(
                app_version=__version__,
                git_sha=self.git_sha,
                image_tag=self.image_tag,
                hostname=self.hostname,
                pid=os.getpid(),
            )
            # A previous run that was killed leaves its gap open. Close it now,
            # with the reason, so the hole in the data is dated rather than
            # merely implied.
            reopened = self._writer.close_open_gaps(detail="previous run ended uncleanly")
            if reopened:
                self._event("warning", "restart", f"closed {reopened} open gap(s) from a prior run")

            self._archive = archive = RawArchive(settings.raw_dir, run_id=self.run_id)
            self._http = http = ResilientHttp(
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
                limits=RateLimitRegistry(),
                user_agent=f"weatherquant-next/{__version__}",
            )
            discovery = GammaDiscovery(http, default_station=settings.station)

            if self.stream_enabled:
                from weatherquant.data.stream import MarketStream

                stream = MarketStream(
                    queue=self._ws_queue,
                    stats=self.stats,
                    shutdown=self.shutdown,
                    state_callback=self.note_stream_state,
                    # Assets come from discovery, which runs after the stream
                    # starts; the callable is polled each loop so the
                    # subscription tracks newly discovered tokens.
                    asset_ids=lambda: list(self._books),
                )
                stream.start()

            self._install_signal_handlers()
            self._status = HealthStatus.HEALTHY
            self._publish(publisher, http)

            # This run's own gap: everything between now and a clean exit is
            # uncollected time and must be visible as such.
            self._writer.record_gap(
                reason="start_after_restart",
                started_at_utc=self.started_at,
                detail=f"run={self.run_id} pid={os.getpid()} host={self.hostname}",
            )

            self._loop(http, discovery, stream, publisher, archive)
            status = HealthStatus.STOPPED
            reason = self.shutdown.reason or "shutdown requested"
        except Exception as exc:
            log.exception("collector fatal error")
            self._fatal = f"{type(exc).__name__}: {exc}"
            reason = self._fatal
            status = HealthStatus.FATAL
            if self._writer is not None:
                try:
                    self._event("critical", "fatal", reason)
                    self._writer.flush(force=True)
                except Exception:
                    log.exception("could not record fatal event")
        finally:
            # Each shutdown step is isolated: a stream that fails to stop must
            # not prevent the writer from flushing, checkpointing and recording
            # the run's exit. Without this, one subsystem's failure silently
            # converts a clean shutdown into a torn one - the run stays marked
            # `running` and its collection gap stays open forever.
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    log.exception("stream did not stop cleanly")
            if archive is not None:
                # Finish the raw archive before marking the collector run
                # stopped. A failed fsync must produce a fatal heartbeat,
                # not a clean exit with an incomplete raw ledger.
                try:
                    archive.close()
                except Exception as exc:
                    log.exception("raw archive did not close durably")
                    self._fatal = f"archive_close_failed: {type(exc).__name__}: {exc}"
                    reason = self._fatal
                    status = HealthStatus.FATAL
                finally:
                    self._archive_records = archive.records_written
            if self._writer is not None:
                # Close this run's own gap before the writer goes away. A run
                # that opened a gap at start must close it at exit whatever the
                # exit reason, or every *clean* shutdown would leave a phantom
                # "collection gap" that later research would read as an outage.
                try:
                    self._writer.close_open_gaps(detail=f"run ended: {reason}")
                except Exception:
                    log.exception("could not close this run's collection gap")
                try:
                    self._writer.close(
                        status="failed" if status is HealthStatus.FATAL else "stopped",
                        reason=reason,
                    )
                except Exception:
                    log.exception("error closing writer")
            if http is not None:
                http.close()
            self._status = (
                HealthStatus.FATAL if status is HealthStatus.FATAL else HealthStatus.STOPPED
            )
            try:
                self._publish(publisher, None)
            except Exception:
                log.exception("could not publish final heartbeat")
            lock.release()

        if status is HealthStatus.FATAL:
            return ExitCode.FATAL
        if self.shutdown.reason.startswith("signal"):
            return ExitCode.INTERRUPTED
        return ExitCode.OK

    # -- main loop ---------------------------------------------------------

    def _loop(self, http, discovery, stream, publisher, archive) -> None:
        # Held so the drain and the heartbeat can read the producer's drop
        # counter: it lives on the stream thread, and the only safe place to
        # sample it from is here, on the consumer.
        self._stream = stream
        cycles = 0
        while not self.shutdown.requested():
            now = time.monotonic()
            cycles += 1

            # Heartbeat before the poll, not after: polling 66 tokens with every
            # request retrying against a dead network can take far longer than
            # the probe's staleness limit, and a collector that stops ticking
            # looks identical to a dead one. Tick first, then work.
            if now - self._last_heartbeat_mono >= self.heartbeat_seconds:
                self._publish(publisher, http)

            # Drain the stream BEFORE the poll as well as after it. The producer
            # runs on its own thread and this one blocks inside REST, so the
            # queue only grows while a sweep is in flight; draining on both
            # sides bounds the backlog to one sweep instead of a sweep plus a
            # whole tick. Rest does not stop WS - it just delays it by the
            # sweep, which is exactly what the budget exists to keep short.
            self._drain_stream(archive)

            if now - self._last_book_poll_mono >= self.book_poll_seconds:
                self._last_book_poll_mono = now
                self._poll_books(http)
                # After the sweep, on a *fresh* clock. The original second
                # check compared `now` - captured before all of this - against
                # a timestamp the first branch had just written, so it could
                # never be true and the post-work safety net was dead code.
                if time.monotonic() - self._last_heartbeat_mono >= self.heartbeat_seconds:
                    self._publish(publisher, http)

            if now - self._last_discovery_mono >= self.discovery_seconds:
                self._last_discovery_mono = now
                self._run_discovery(discovery, archive)
            if now - self._last_integrity_mono >= self.integrity_seconds:
                self._last_integrity_mono = now
                self._run_integrity()

            self._drain_stream(archive)
            if self._writer is not None and self._writer.pending_rows:
                self._writer.flush_if_due()
                self._writer.checkpoint_if_due()
            if time.monotonic() - self._last_heartbeat_mono >= self.heartbeat_seconds:
                self._publish(publisher, http)
            self._maybe_daily_jobs()

            if self.once and cycles >= 3:
                self.shutdown.set("once: single cycle complete")
                break

            self.shutdown.wait(self.tick_seconds)

    # -- tasks -------------------------------------------------------------

    def _run_discovery(self, discovery: GammaDiscovery, archive: RawArchive | None) -> None:
        if self._writer is None or archive is None:
            return
        try:
            events = 0
            markets = 0
            problems: list[str] = []
            for found in discovery.discover():
                if self.shutdown.requested():
                    break
                archive.append(
                    ArchiveRecord(
                        source="gamma",
                        payload=found.raw,
                        received_at_utc=found.fetched_at_utc,
                        venue_ts_utc=extract_venue_ts(found.raw),
                        quality="ok" if not found.problems else "issues",
                        run_id=self.run_id,
                    )
                )
                self._writer.stage_event(_with_run(found.event_row, self.run_id))
                for row in found.market_rows:
                    self._writer.stage_market(_with_run(row, self.run_id))
                events += 1
                markets += len(found.market_rows)
                problems.extend(found.problems)
                for token in _tokens_of(found.market_rows):
                    self._books[token[0]] = token[1]
            self._writer.flush_if_due()
            self.stats.events_upserted += events
            self.stats.markets_upserted += markets
            if events:
                self._last_data_change = datetime.now(tz=UTC)
            if problems:
                self.stats.integrity_failures += len(problems)
                self._last_defect = problems[0][:200]
                self._event(
                    "warning",
                    "discovery_problems",
                    f"{len(problems)} problem(s) across {events} event(s)",
                    {"first": problems[:10], "tokens": len(self._books)},
                )
            log.info(
                "discovery: %d event(s), %d market(s), %d token(s) tracked, %d problem(s)",
                events,
                markets,
                len(self._books),
                len(problems),
            )
        except Exception as exc:
            self._rest_consecutive_failures += 1
            self._note_error(f"discovery: {exc}")
            log.warning("discovery failed: %s", exc)

    # One poll cycle must not outlive the probe's staleness limit: with every
    # request retrying against an unreachable network, 66 tokens could otherwise
    # take tens of minutes inside a single blocking loop.
    POLL_BUDGET_SECONDS = 45.0
    # Never hand a token less than this, even with a second left on the budget.
    # A 100 ms timeout against a slow-but-alive endpoint would turn ordinary
    # latency into a failed request; a bounded overshoot of a couple of seconds
    # is the cheaper mistake.
    POLL_TIMEOUT_FLOOR_SECONDS = 2.0
    # Budget exhaustion is normal under load and must not spam the log: the
    # first occurrence and then one every five minutes, with the running total
    # in both the log line and the collector_events row.
    POLL_BUDGET_NOTE_INTERVAL_SECONDS = 300.0

    def _book_request_budget(self, deadline: float) -> tuple[float, int]:
        """``(timeout_seconds, retries)`` for one fetch inside the sweep.

        The defaults (20 s, 4 attempts) are tuned for a one-off call. Inside a
        loop that also owns the heartbeat they are not safe: five attempts
        against an unreachable host is ~110 s of wall time, which is two and a
        half times the whole budget for a *single token*.

        So the remaining budget is divided honestly: the timeout never exceeds
        what is left, and a second attempt is allowed only when the pair
        provably fits inside it. When it does not, the sweep's own cadence -
        another pass in ``book_poll_seconds`` - is the retry, which costs the
        loop nothing.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0.0, 0
        timeout = max(
            self.POLL_TIMEOUT_FLOOR_SECONDS,
            min(self.settings.http_timeout_seconds, remaining),
        )
        retries = 1 if remaining >= 2 * timeout + 1.0 else 0
        return timeout, retries

    def _rotated_books(self) -> list[tuple[str, str]]:
        """Start each sweep where the last one stopped.

        ``self._books`` is a dict, so a budget cut always drops the *tail* -
        the same tokens, in the same order, every cycle. That is starvation
        with a perfectly healthy-looking aggregate: most tokens refresh on
        time while a handful never do. Rotating the start position by one each
        sweep spreads the truncation across the whole set instead.
        """
        items = list(self._books.items())
        if not items:
            return items
        start = self._poll_cursor % len(items)
        # Advance by one, not to the end: successive sweeps then step through
        # the list, and every token eventually sits at the front when the
        # budget is still open.
        self._poll_cursor = (start + 1) % len(items)
        return items[start:] + items[:start]

    def _poll_books(self, http: ResilientHttp | None) -> None:
        if self._writer is None or self._archive is None or http is None or not self._books:
            return
        ok = failed = 0
        cycle_started = time.monotonic()
        deadline = cycle_started + self.POLL_BUDGET_SECONDS
        attempted = 0
        latencies: list[float] = []

        for token_id, market_id in self._rotated_books():
            if token_id in self._no_book:
                continue
            if self.shutdown.requested():
                break
            if time.monotonic() >= deadline:
                # Defer the remainder to the next tick rather than blocking the
                # heartbeat. Nothing is lost: the rotated start means the next
                # sweep picks up here instead of starting over from the head.
                eligible = sum(1 for t, _ in self._books.items() if t not in self._no_book)
                deferred = max(0, eligible - attempted)
                self._poll_budget_exceeded += 1
                self._poll_deferred += deferred
                self._note_poll_budget(time.monotonic() - cycle_started, deferred)
                break

            timeout_seconds, retries = self._book_request_budget(deadline)
            # Marked before the request: this is when we *asked*. The value
            # that goes into the archive and the row is taken after the
            # response, and the difference is the latency this sweep records.
            request_started = datetime.now(tz=UTC)
            try:
                # Fetch the raw payload first: the archive stores it verbatim,
                # and only then is it parsed into a row.
                payload = http.get_json(
                    f"{CLOB_BASE}/book",
                    params={"token_id": token_id},
                    limiter="clob_book",
                    allow_not_found=True,
                    timeout_seconds=timeout_seconds,
                    retries=retries,
                )
                received_at = datetime.now(tz=UTC)
                attempted += 1
                if payload is None:
                    # "No orderbook exists for this token" - a definitive answer
                    # about an archived market, not an upstream failure. Remember
                    # it so the collector stops re-asking every cycle.
                    self._no_book.add(token_id)
                    self.stats.rest_requests_ok += 1
                    self._rest_consecutive_failures = 0
                    log.debug("no orderbook for token %s; skipping it from now on", token_id[:12])
                    continue
                snapshot = parse_book_payload(
                    payload,
                    token_id=token_id,
                    market_id=market_id,
                    fetched_at_utc=received_at,
                    run_id=self.run_id,
                )
            except Exception as exc:
                failed += 1
                attempted += 1
                self.stats.rest_requests_failed += 1
                self._note_error(f"book {token_id[:12]}: {exc}")
                log.debug("book fetch failed for %s: %s", token_id[:12], exc)
                continue

            latency_ms = (received_at - request_started).total_seconds() * 1000.0
            latencies.append(latency_ms)
            self._rest_latencies.append(latency_ms)
            # Per-token so "which token has not been seen lately" is answerable
            # from the heartbeat rather than only from the archive.
            self._last_polled_at[token_id] = received_at

            self._archive.append(
                ArchiveRecord(
                    source="rest",
                    payload=payload,
                    received_at_utc=received_at,
                    venue_ts_utc=snapshot.venue_ts,
                    quality=snapshot.quality,
                    run_id=self.run_id,
                )
            )
            self._writer.stage_book(snapshot.row)
            if snapshot.quality != "ok":
                self._last_defect = snapshot.quality
                if _is_one_sided(snapshot.quality):
                    # An extreme bracket routinely has no bid or no ask: that is
                    # the market being empty, not the payload being corrupt.
                    self.stats.one_sided_books += 1
                else:
                    self.stats.integrity_failures += 1
            ok += 1
            self.stats.rest_requests_ok += 1
            self._rest_consecutive_failures = 0

            mid = _midpoint(snapshot.bids, snapshot.asks)
            if mid is not None:
                self._writer.stage_price(
                    (
                        market_id,
                        token_id,
                        received_at,
                        mid,
                        "rest",
                        received_at,
                        snapshot.venue_ts_origin,
                        self.run_id,
                    )
                )
                self.stats.price_points_inserted += 1
                self._last_data_change = received_at
                # The REST poll is the authoritative snapshot. Keeping its
                # midpoint gives every stream update something to be checked
                # against, which is what turns "two sources" into "two sources
                # that agree".
                self._last_rest_mid[token_id] = (received_at, mid)

        if failed:
            self._rest_consecutive_failures = max(self._rest_consecutive_failures, failed)
        if latencies:
            self._last_sweep_latency_ms = _latency_summary(latencies)
        self._writer.flush_if_due()
        log.debug("book poll: %d ok, %d failed", ok, failed)

    def _note_poll_budget(self, elapsed: float, deferred: int) -> None:
        """Record that the sweep ran out of budget, without a log storm.

        Budget exhaustion is a *normal* outcome of a full token set and is the
        thing the rotation exists to spread around, so it is a counter first
        and a log line second - the first, and then one every five minutes.
        """
        now = time.monotonic()
        if (
            now - self._last_poll_budget_note >= self.POLL_BUDGET_NOTE_INTERVAL_SECONDS
            or self._poll_budget_exceeded == 1
        ):
            self._last_poll_budget_note = now
            log.warning(
                "book poll budget exhausted after %.1fs; %d token(s) deferred "
                "(total %d sweep(s), %d deferral(s))",
                elapsed,
                deferred,
                self._poll_budget_exceeded,
                self._poll_deferred,
            )
            self._event(
                "warning",
                "poll_budget",
                f"budget exhausted after {elapsed:.1f}s; {deferred} token(s) deferred",
            )


    # How far apart the stream and the poll may be before we call it a
    # discrepancy. Half a cent in probability terms is far wider than normal
    # latency jitter and far narrower than a real book move.
    WS_REST_TOLERANCE = 0.005
    # Only compare observations taken close together. A midpoint from two
    # minutes ago is allowed to differ from a fresh frame simply because the
    # book moved - counting that as a mismatch would report normal trading as a
    # data-quality problem. Two poll cycles (5s each) is the window in which
    # "these two disagree" actually means something.
    WS_REST_MAX_AGE_SECONDS = 10.0
    WS_REST_NOTE_INTERVAL_SECONDS = 300.0

    def _check_ws_against_rest(self, row: tuple) -> None:
        """Compare a stream-derived midpoint against the latest REST midpoint.

        REST is treated as the snapshot of record. A disagreement wider than
        :attr:`WS_REST_TOLERANCE` means one of the two paths is wrong, and that
        is exactly the kind of thing that should surface as a recorded event
        rather than be averaged away. Findings are rate-limited so a persistently
        divergent pair cannot flood ``collector_events``.
        """
        token_id, stream_mid = row[1], row[3]
        known = self._last_rest_mid.get(token_id)
        if known is None:
            return
        rest_at, rest_mid = known
        age = (datetime.now(tz=UTC) - rest_at).total_seconds()
        if age > self.WS_REST_MAX_AGE_SECONDS:
            return
        gap = abs(stream_mid - rest_mid)
        if gap <= self.WS_REST_TOLERANCE:
            return
        self.stats.ws_rest_mismatches += 1
        now = datetime.now(tz=UTC)
        if (now - self._last_mismatch_note).total_seconds() < self.WS_REST_NOTE_INTERVAL_SECONDS:
            return
        self._last_mismatch_note = now
        self._event(
            "warning",
            "ws_rest_mismatch",
            f"stream mid {stream_mid:.4f} vs poll mid {rest_mid:.4f} "
            f"(gap {gap:.4f}, {age:.0f}s since poll)",
            {"token_id": token_id[:16], "stream_mid": stream_mid, "rest_mid": rest_mid, "age_s": age},
        )
        self._last_defect = f"ws_rest_mismatch:{token_id[:12]}"

    def _run_integrity(self) -> None:
        if self._writer is None:
            return
        try:
            report = self._writer.integrity_check()
            if not report.ok:
                self.stats.integrity_failures += len(report.problems)
                self._last_defect = report.problems[0][:200]
                self._event("error", "integrity", "; ".join(report.problems))
            usage = self._writer.disk_usage()
            if usage["disk_free_percent"] < 5.0:
                self._event(
                    "critical",
                    "disk",
                    f"disk critically low: {usage['disk_free_percent']:.1f}% free",
                )
            elif usage["disk_free_percent"] < 15.0:
                self._event(
                    "warning",
                    "disk",
                    f"disk low: {usage['disk_free_percent']:.1f}% free",
                )
        except Exception as exc:
            log.warning("integrity check failed: %s", exc)

    def _maybe_daily_jobs(self) -> None:
        """Parquet export and backup, once each per UTC day."""
        if self._writer is None:
            return
        today = datetime.now(tz=UTC).date()
        if self.parquet_daily and self._last_parquet_day != today:
            try:
                report = self._writer.export_parquet_day(
                    today - timedelta(days=1), self.settings.raw_dir.parent / "parquet"
                )
                if report.tables:
                    log.info("parquet %s: %s", report.day, report.rows)
                if report.errors:
                    self._event("warning", "parquet", "; ".join(report.errors))
                self._last_parquet_day = today
            except Exception as exc:
                log.warning("parquet export failed: %s", exc)
        if self.backup_daily and self._last_backup_day != today:
            try:
                report = self._writer.backup(self.settings.db_path.parent / "backups")
                if report.ok:
                    log.info(
                        "backup verified: %s (%d bytes, %d tables)",
                        report.destination.name,
                        report.bytes_written,
                        len(report.row_counts),
                    )
                    self._event(
                        "info", "backup", f"verified backup {report.destination.name}"
                    )
                else:
                    log.error("backup failed: %s", report.error)
                    self._event("error", "backup_failed", report.error or "unknown")
                self._last_backup_day = today
            except Exception as exc:
                # The exception path is the one the acceptance cares about:
                # a backups mount that vanished or filled up must leave an
                # audit trail, not just a log line on a rotating container.
                log.warning("backup failed: %s", exc)
                self._event("error", "backup_failed", f"{type(exc).__name__}: {exc}")
                self._last_backup_day = today

    def _drain_stream(self, archive: RawArchive | None) -> None:
        # Sampled *before* the writer guard: queue depth is a property of the
        # producer and this thread, and is worth reporting even when there is
        # nothing to write - a full queue with no writer is still a full queue.
        depth = self._ws_queue.qsize()
        if depth > self._ws_queue_high_water:
            self._ws_queue_high_water = depth
        if self._stream is not None:
            self._frames_dropped = self._stream.frames_dropped

        if self._writer is None or archive is None:
            return
        drained = 0
        while drained < 500:
            try:
                frame = self._ws_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            self._handle_stream_frame(frame, archive)

    def _handle_stream_frame(self, frame: Any, archive: RawArchive) -> None:
        received_at, payload = frame
        self.stats.ws_messages_received += 1
        self._last_data_change = received_at
        venue_ts = extract_venue_ts(payload)
        quality = "ok"
        try:
            rows = stream_price_rows(payload, received_at, self.run_id, self._books)
            for row in rows:
                self._writer.stage_price(row)  # type: ignore[union-attr]
                self.stats.price_points_inserted += 1
                self._check_ws_against_rest(row)
        except Exception as exc:
            quality = f"unparsed:{type(exc).__name__}"
            self.stats.integrity_failures += 1
            log.debug("could not parse stream frame: %s", exc)
        archive.append(
            ArchiveRecord(
                source="ws",
                payload=payload,
                received_at_utc=received_at,
                venue_ts_utc=venue_ts,
                quality=quality,
                run_id=self.run_id,
            )
        )
        if self._writer is not None:
            self._writer.flush_if_due()

    # -- helpers -----------------------------------------------------------

    def _event(self, level: str, kind: str, message: str, details: dict | None = None) -> None:
        if self._writer is None:
            return
        try:
            self._writer.log_event(level, kind, message, details)
        except Exception:
            log.debug("could not persist event", exc_info=True)

    def _telemetry(self) -> dict[str, Any]:
        """Rolling operational numbers published alongside the heartbeat.

        Every member answers a question the lifetime counters cannot: how slow
        REST is *right now*, whether the sweep is running out of budget, whether
        the stream queue is outgrowing the drain, and which token has gone
        longest unseen. Empty sub-mappings mean "not measured yet", which is a
        different claim from zero.

        The queue is sampled here as well as on the drain: this is the moment
        the number is *published*, and sampling only on the drain would report
        a depth that has already been emptied.
        """
        depth = self._ws_queue.qsize()
        if depth > self._ws_queue_high_water:
            self._ws_queue_high_water = depth

        return {
            "rest_latency_last_sweep_ms": self._last_sweep_latency_ms,
            "rest_latency_recent_ms": _latency_summary(list(self._rest_latencies)),
            "rest_timeouts": self._last_timeouts,
            "poll_budget": {
                "budget_seconds": self.POLL_BUDGET_SECONDS,
                "exceeded": self._poll_budget_exceeded,
                "deferred_tokens": self._poll_deferred,
            },
            "ws_queue": {
                "depth": depth,
                "high_water": self._ws_queue_high_water,
                "capacity": self._ws_queue.maxsize,
                "frames_dropped": self._frames_dropped,
            },
            "per_token_book_age_ms": self._per_token_book_age(),
        }

    def _per_token_book_age(self) -> dict[str, Any] | None:
        """How stale each token's newest book is, right now.

        Rotation is meant to make the *refresh gap* uniform; this shows whether
        the *age* actually converged. The oldest token is named, because a
        uniform p95 with one token an hour behind is still starvation.
        """
        if not self._last_polled_at:
            return None
        now = datetime.now(tz=UTC)
        ages = {
            token: (now - seen).total_seconds() * 1000.0
            for token, seen in self._last_polled_at.items()
        }
        values = list(ages.values())
        oldest = max(ages, key=ages.get)
        return {
            "tokens": len(values),
            "max_ms": round(max(values), 1),
            "p95_ms": round(_percentile(values, 95), 1),
            "median_ms": round(_percentile(values, 50), 1),
            "oldest_token": oldest[:24],
        }

    def _note_error(self, message: str) -> None:
        self._last_error = message[:500]
        self._last_error_at = datetime.now(tz=UTC)
        self.stats.errors_total += 1

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: Any) -> None:
            name = signal.Signals(signum).name
            log.info("received %s; shutting down gracefully", name)
            self.shutdown.set(f"signal {name}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            # ValueError when not on the main thread, OSError where unsupported.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    def note_stream_state(
        self, *, connected: bool, last_message: datetime | None, endpoint: str
    ) -> None:
        self._ws_connected = connected
        self._ws_last_message = last_message
        self._ws_endpoint = endpoint

    def _publish(
        self,
        publisher: HeartbeatWriter,
        http: ResilientHttp | None,
    ) -> None:
        """Publish the current heartbeat.

        ``self._status`` is the single source of truth for status: the caller
        sets it (FATAL on a crash, STOPPED on a clean exit) *before* calling, so
        this method must not override it. It previously forced STOPPED whenever
        it was the final write, which meant a crashed collector published a
        healthy-looking heartbeat and Docker never restarted it.
        """
        now_mono = time.monotonic()
        self._last_heartbeat_mono = now_mono
        wall = datetime.now(tz=UTC)

        writer_stats: dict[str, int] = {}
        disk = {"db_bytes": 0, "disk_free_bytes": 0, "disk_total_bytes": 0, "disk_free_percent": 100.0}
        raw_bytes = 0
        if self._writer is not None:
            try:
                writer_stats = dict(self._writer.stats)
                disk = self._writer.disk_usage()
                raw_bytes = self._writer.raw_tree_bytes(self.settings.raw_dir)
            except Exception:
                log.debug("could not read writer stats", exc_info=True)

        if http is not None:
            self._last_contact = http.last_contact_utc
            self._last_rest_ok = http.requests_ok
            self._last_rest_failed = http.requests_failed
            self._last_retries = http.retries_performed
            self._last_rate_waits = http.rate_limit_waits
            self._last_timeouts = getattr(http, "timeouts", 0)
        contact = self._last_contact
        rest_ok = self._last_rest_ok
        rest_failed = self._last_rest_failed
        retries = self._last_retries
        rate_waits = self._last_rate_waits

        # Reflect upstream liveness in the published status. The probe
        # recomputes this anyway, but a file that claims `healthy` while the
        # exchange has been silent for half an hour would mislead anyone reading
        # it directly.
        if self._status is HealthStatus.HEALTHY and contact is not None:
            silent = (wall - contact).total_seconds()
            if silent > UPSTREAM_STALL_SOFT_SECONDS:
                self._status = HealthStatus.DEGRADED
        elif self._status is HealthStatus.DEGRADED and contact is not None:
            if (wall - contact).total_seconds() <= UPSTREAM_STALL_SOFT_SECONDS:
                self._status = HealthStatus.HEALTHY

        heartbeat = Heartbeat(
            status=self._status,
            run_id=self.run_id,
            pid=os.getpid(),
            hostname=self.hostname,
            app_version=__version__,
            updated_at_utc=wall,
            started_at_utc=self.started_at,
            uptime_seconds=(wall - self.started_at).total_seconds(),
            last_upstream_contact_utc=contact,
            last_data_change_utc=self._last_data_change,
            ws_connected=self._ws_connected,
            ws_last_message_utc=self._ws_last_message,
            ws_endpoint=self._ws_endpoint,
            subscribed_tokens=len(self._books),
            rest_last_success_utc=contact,
            rest_consecutive_failures=self._rest_consecutive_failures,
            stats=CollectionStats(
                books_rows_inserted=writer_stats.get("books_inserted", 0),
                books_rows_deduplicated=writer_stats.get("books_deduplicated", 0),
                price_points_inserted=writer_stats.get("prices_inserted", 0),
                price_points_deduplicated=writer_stats.get("prices_deduplicated", 0),
                events_upserted=self.stats.events_upserted,
                markets_upserted=self.stats.markets_upserted,
                ws_messages_received=self.stats.ws_messages_received,
                ws_reconnects=self.stats.ws_reconnects,
                ws_subscribe_failures=self.stats.ws_subscribe_failures,
                rest_requests_ok=rest_ok,
                rest_requests_failed=rest_failed,
                retries=retries,
                rate_limit_waits=rate_waits,
                integrity_failures=self.stats.integrity_failures,
                one_sided_books=self.stats.one_sided_books,
                ws_rest_mismatches=self.stats.ws_rest_mismatches,
                archive_records=self._archive_records,
                dedupe_hits_total=writer_stats.get("books_deduplicated", 0)
                + writer_stats.get("prices_deduplicated", 0),
                errors_total=self.stats.errors_total,
            ),
            db_bytes=int(disk.get("db_bytes", 0)),
            raw_bytes=raw_bytes,
            disk_free_bytes=int(disk.get("disk_free_bytes", 0)),
            disk_total_bytes=int(disk.get("disk_total_bytes", 0)),
            disk_free_percent=float(disk.get("disk_free_percent", 100.0)),
            last_defect=self._last_defect,
            last_error_utc=self._last_error_at,
            last_error=self._last_error,
            fatal_error=self._fatal,
            telemetry=self._telemetry(),
        )
        try:
            publisher.write(heartbeat)
        except Exception:
            log.warning("heartbeat write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _with_run(row: tuple | None, run_id: str) -> tuple:
    if row is None:
        raise ValueError("cannot attach a run id to a missing row")
    values = list(row)
    values[-1] = run_id
    return tuple(values)


def _tokens_of(market_rows: list[tuple]) -> list[tuple[str, str]]:
    """``(token_id, market_id)`` pairs worth polling for depth.

    Only *currently tradeable* markets are included. Historical markets returned
    by keyword search are still stored as metadata - they are research data -
    but polling their books would ask the venue about markets that stopped
    having books long ago, and every one of those answers is a 404.

    Column layout matches the market row built by
    :func:`weatherquant.data.discovery.build_market_rows`; the indices are
    named here rather than repeated as magic numbers.
    """
    pairs: list[tuple[str, str]] = []
    for row in market_rows:
        market_id, token_yes, token_no = row[0], row[12], row[13]
        active, closed, accepting_orders = row[22], row[23], row[24]
        if not (active and accepting_orders and not closed):
            continue
        for token in (token_yes, token_no):
            if token:
                pairs.append((token, market_id))
    return pairs


def _percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile; ``None`` for an empty sample.

    Reported as ``None`` rather than 0: no samples is not the same as
    instantaneous, and a zero here would read as "requests are free".
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = (p / 100.0) * (n - 1)
    low = int(rank)
    high = min(low + 1, n - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _latency_summary(values: list[float]) -> dict[str, Any]:
    """Percentiles of one sweep's request latencies, for the heartbeat.

    Bounded by the caller's deque: this is a rolling view of recent cost, not
    a lifetime aggregate that only ever worsens.
    """
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(_percentile(values, 50), 1),
        "p95_ms": round(_percentile(values, 95), 1),
        "p99_ms": round(_percentile(values, 99), 1),
        "max_ms": round(max(values), 1),
    }


def _is_one_sided(quality: str) -> bool:
    """True when the only thing "wrong" is that one side of the book is empty."""
    return quality in {"NO_ASK_SIDE", "NO_BID_SIDE"}


def _midpoint(bids, asks) -> float | None:
    if not bids or not asks:
        return None
    return (bids[0].price_usd_per_share + asks[0].price_usd_per_share) / 2.0


def _iter_frames(payload: Any) -> list[dict]:
    """Normalise the venue's mixed framing into a list of event objects.

    Verified live: the initial book snapshot(s) arrive as a JSON **array** (one
    element per subscribed token) while ``price_change`` arrives as a **bare
    object**. Both carry ``event_type``. Accepting only one shape would silently
    drop half the stream.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _book_midpoint(event: dict) -> float | None:
    """Midpoint of a full ``book`` frame, from its own aggregated depth."""
    bids = event.get("bids") or []
    asks = event.get("asks") or []
    try:
        best_bid = max(float(x["price"]) for x in bids if x.get("price") is not None)
        best_ask = min(float(x["price"]) for x in asks if x.get("price") is not None)
    except (ValueError, TypeError):
        return None
    if not 0.0 < best_bid < 1.0 or not 0.0 < best_ask < 1.0:
        return None
    return (best_bid + best_ask) / 2.0


def stream_price_rows(
    payload: Any, received_at: datetime, run_id: str, books: dict[str, str]
) -> list[tuple]:
    """Extract price points from a stream frame.

    Only a *midpoint* is written to ``price_history``. The venue also sends
    individual level prices (``price_changes[].price``) and trade prices
    (``last_trade_price``); neither is a mid, and storing either under a mid
    column would corrupt every analysis that reads it. Where the frame carries
    ``best_bid``/``best_ask`` the midpoint is computed from those; otherwise the
    frame yields no row.

    The frame is archived verbatim regardless, so nothing is lost by declining
    to derive a row from it.
    """
    rows: list[tuple] = []
    for event in _iter_frames(payload):
        event_type = event.get("event_type")
        market_id: str | None = None
        token = ""
        mid: float | None = None

        if event_type == "book":
            token = str(event.get("asset_id") or "")
            market_id = books.get(token)
            mid = _book_midpoint(event)
        elif event_type == "price_change":
            for change in event.get("price_changes") or []:
                if not isinstance(change, dict):
                    continue
                token = str(change.get("asset_id") or "")
                market_id = books.get(token)
                if market_id is None:
                    continue
                best_bid = _to_float(change.get("best_bid"))
                best_ask = _to_float(change.get("best_ask"))
                if best_bid is None or best_ask is None:
                    continue
                if not (0.0 < best_bid < 1.0 and 0.0 < best_ask < 1.0):
                    continue
                rows.append(
                    (
                        market_id,
                        token,
                        received_at,
                        (best_bid + best_ask) / 2.0,
                        "ws",
                        received_at,
                        "missing" if _event_has_no_ts(event) else "venue",
                        run_id,
                    )
                )
            continue
        else:
            # last_trade_price / tick_size_change / unknown: archived raw,
            # never coerced into a price series.
            continue

        if market_id is None or mid is None or not 0.0 < mid < 1.0:
            continue
        rows.append(
            (
                market_id,
                token,
                received_at,
                mid,
                "ws",
                received_at,
                "missing" if _event_has_no_ts(event) else "venue",
                run_id,
            )
        )
    return rows


def _event_has_no_ts(event: dict) -> bool:
    return extract_venue_ts(event) is None


def worker_main(argv: list[str] | None = None) -> int:
    """Console entry point for the collector."""
    import argparse

    parser = argparse.ArgumentParser(prog="weatherquant-collect", description="Polymarket weather collector")
    parser.add_argument("--git-sha", default=os.environ.get("GIT_SHA"))
    parser.add_argument("--image-tag", default=os.environ.get("IMAGE_TAG"))
    parser.add_argument("--no-stream", action="store_true", help="run REST-only (diagnostics)")
    parser.add_argument("--discovery-seconds", type=float, default=900.0)
    parser.add_argument("--book-poll-seconds", type=float, default=5.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true", help="run one discovery+poll cycle then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("WQ_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = Settings()
    except Exception as exc:
        print(f"invalid configuration: {exc}", flush=True)
        return ExitCode.BAD_CONFIG

    worker = CollectorWorker(
        settings,
        git_sha=args.git_sha,
        image_tag=args.image_tag,
        stream_enabled=not args.no_stream,
        discovery_seconds=args.discovery_seconds,
        book_poll_seconds=args.book_poll_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        once=args.once,
    )
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(worker_main())

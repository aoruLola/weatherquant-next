"""Stability: the collector stays responsive, honest and fair under stress.

Each test is one of the acceptance points, stated as the failure it forbids:

* a REST sweep that outlives its budget starves the heartbeat;
* a sweep that always starts at the head starves the tail of the token list;
* a request start recorded as an arrival lies about latency;
* a full stream queue turns into one WARNING per frame and buries the log.

The existing soak script covers process-level restart and proxy outage over
minutes; these are the unit-level properties underneath those, so a regression
is caught here rather than in a thirty-minute run.
"""

from __future__ import annotations

import json
import queue
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from test_collector_worker import FakeHttp
from weatherquant.config import Settings
from weatherquant.data import worker as worker_module
from weatherquant.data.worker import CollectorWorker

BOOK_PRICE = "0.52"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        db_path=tmp_path / "wq.duckdb",
        raw_dir=tmp_path / "raw",
        station="KLGA",
    )


class TimingHttp(FakeHttp):
    """FakeHttp that records when each request *started* and can be slow.

    The whole point of this class is the clock: the archive's receipt stamp
    must be taken after the response, and the only way to prove that is to
    know when the call began.
    """

    def __init__(self, *, delay: float = 0.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delay = delay
        self.book_request_start: dict[str, datetime] = {}

    def get_json(self, url: str, *, params: Any = None, limiter: str | None = None,
                 allow_not_found: bool = False, **options: Any) -> Any:
        if "/book" in url and isinstance(params, dict) and params.get("token_id"):
            self.book_request_start[str(params["token_id"])] = datetime.now(tz=UTC)
        if self.delay:
            time.sleep(self.delay)
        return super().get_json(
            url, params=params, limiter=limiter, allow_not_found=allow_not_found, **options
        )


def _rest_lines(raw_dir: Path) -> list[dict]:
    out = []
    for path in sorted((raw_dir / "rest").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(line))
    return out


class _RecordingWriter:
    """Minimal stand-in: `_poll_books` stages into it and flushes.

    A real CollectorWriter would open DuckDB for each of these tests, none of
    which are about storage - they are about the sweep.
    """

    def __init__(self) -> None:
        self.books: list[tuple] = []
        self.prices: list[tuple] = []
        self.pending_rows = 0

    def stage_book(self, row: tuple) -> None:
        self.books.append(row)

    def stage_price(self, row: tuple) -> None:
        self.prices.append(row)

    def flush_if_due(self) -> dict:
        return {}

    def checkpoint_if_due(self) -> None:
        return None


class _RecordingArchive:
    """Same idea for the archive: collects records, writes nothing."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def append(self, record: Any) -> None:
        self.records.append(record)

    def flush(self) -> int:
        return 0

    def close(self) -> None:
        return None


def _worker_with_books(settings: Settings, tokens: int) -> CollectorWorker:
    """A worker wired for a sweep: synthetic tokens, no network, no stream."""
    worker = CollectorWorker(
        settings,
        run_id="stability",
        stream_enabled=False,
        once=True,
        heartbeat_seconds=0.05,
        book_poll_seconds=0.0,
        discovery_seconds=0.0,
        integrity_seconds=0.0,
        parquet_daily=False,
        backup_daily=False,
    )
    # `_poll_books` returns immediately without a writer and an archive; both
    # are installed here so the tests exercise the sweep, not the guard.
    worker._writer = _RecordingWriter()
    worker._archive = _RecordingArchive()
    worker._books = {f"token-{i:03d}": "market-1" for i in range(tokens)}
    return worker


# --- 1. an end-to-end budget that actually ends ---------------------------


def test_a_slow_sweep_stays_inside_its_end_to_end_budget(settings) -> None:
    """The budget has to bound the *sweep*, not just sit between two requests.

    Before, a single request could spend 5 x 20 s on retries - over twice the
    whole budget for one token - because the check only ran between tokens and
    the request itself was unbounded.
    """
    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp(delay=0.02)
    worker.POLL_BUDGET_SECONDS = 0.3

    started = time.monotonic()
    worker._poll_books(http)
    elapsed = time.monotonic() - started

    # Budget plus one request's worth of slack: the check happens before the
    # token, so the last request may overshoot, but only by its own duration.
    assert elapsed < 1.0, f"sweep took {elapsed:.2f}s against a 0.3s budget"
    assert http.calls, "the sweep should have attempted some tokens"


def test_the_budget_reaches_the_client_as_a_bounded_timeout(settings) -> None:
    """What the budget promises has to be what the transport is told."""
    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp()
    worker.POLL_BUDGET_SECONDS = 45.0

    worker._poll_books(http)

    book_calls = [
        opt
        for url, opt in zip(http.calls, http.request_options, strict=True)
        if "/book" in url
    ]
    assert book_calls, "no book calls were made"
    for options in book_calls:
        timeout = options.get("timeout_seconds")
        assert timeout is not None, "the sweep did not bound the request timeout"
        assert 0 < timeout <= worker.POLL_BUDGET_SECONDS
        assert options.get("retries", 0) <= 1, "a bounded budget must not allow full retries"


def test_a_tighter_budget_asks_for_fewer_attempts(settings) -> None:
    """When the pair would not fit, the sweep's own cadence is the retry."""
    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp()
    worker.POLL_BUDGET_SECONDS = 5.0

    worker._poll_books(http)

    book_calls = [opt for url, opt in zip(http.calls, http.request_options, strict=True) if "/book" in url]
    assert book_calls
    # 5 s left and a 20 s configured timeout means the timeout is clipped to
    # what remains, and two attempts of that could not fit - so retries = 0.
    for options in book_calls:
        assert options["timeout_seconds"] <= 5.0
        assert options["retries"] == 0


# --- 2. fair scheduling ---------------------------------------------------


def test_the_sweep_rotates_so_the_tail_is_not_always_the_same_tokens(settings) -> None:
    """Budget exhaustion must not permanently sacrifice the same tokens.

    Iterating a dict from the head and breaking on the budget defers the tail
    every single cycle: most tokens refresh on time while a handful never do,
    and the aggregate looks healthy. Measured on a real archive before this
    change: median gap 21 s, worst token's median 160 s - 7.7x.
    """
    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp(delay=0.02)
    # Only room for a handful of tokens per sweep. The budget must sit well
    # above this machine's monotonic clock granularity (~15 ms) - a sub-milli-
    # second budget is never *observed* to expire, and the test would then pass
    # without ever exercising a cut.
    worker.POLL_BUDGET_SECONDS = 0.1

    polled: set[str] = set()
    for _ in range(20):
        http.book_request_start.clear()
        worker._poll_books(http)
        polled.update(http.book_request_start)

    # Without rotation every sweep restarts at the head and only ever reaches
    # the first handful; with it, the start position steps forward each time.
    assert len(polled) >= 20, f"only {len(polled)}/40 tokens reached across 20 sweeps"
    assert worker._poll_cursor != 0
    assert worker._poll_budget_exceeded >= 1, "the budget never actually expired"


def test_budget_exhaustion_is_counted_and_logged_once_not_per_cycle(settings) -> None:
    """A normal outcome under load must not become a log storm."""
    import logging as _logging

    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp(delay=0.02)
    worker.POLL_BUDGET_SECONDS = 0.1

    records: list[str] = []

    class Capture(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = _logging.getLogger("weatherquant.data.worker")
    handler = Capture()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(_logging.WARNING)
    try:
        for _ in range(6):
            worker._poll_books(http)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert worker._poll_budget_exceeded == 6
    assert worker._poll_deferred > 0
    budget_lines = [m for m in records if "budget exhausted" in m]
    # Six sweeps, but only the first is logged: afterwards the 300 s interval
    # holds. The counts keep moving; the noise does not.
    assert len(budget_lines) == 1, budget_lines
    assert "deferred" in budget_lines[0]


# --- 3. truthful timestamps -----------------------------------------------


def test_the_archive_records_arrival_not_the_moment_the_request_was_sent(settings) -> None:
    """The single most load-bearing timestamp claim in the collector.

    `fetched_at` used to be taken *before* `get_json`, so the archive's
    `received_at_utc` - which the shadow engine treats as "when we had this"
    - predated the response, and could even predate the venue's own stamp.
    """
    http = TimingHttp(delay=0.05)
    # Bound to *this* instance: patching the class would make the worker build
    # its own, and the request starts would be recorded somewhere we cannot see.
    mp = pytest.MonkeyPatch()
    mp.setattr(worker_module, "ResilientHttp", lambda *args, **kwargs: http)
    try:
        worker = CollectorWorker(
            settings,
            run_id="ts-run",
            stream_enabled=False,
            once=True,
            heartbeat_seconds=0.05,
            book_poll_seconds=0.0,
            discovery_seconds=0.0,
            integrity_seconds=0.0,
            parquet_daily=False,
            backup_daily=False,
        )
        worker.run()
    finally:
        mp.undo()

    lines = _rest_lines(settings.raw_dir)
    assert lines, "the run produced no book archive lines"
    starts = http.book_request_start
    assert starts, "the fake never recorded a request start"

    # `once` runs three cycles, and the fake overwrites its per-token start, so
    # the recorded start is the *latest* one. Match it against the latest line
    # for that token - the earlier lines belong to earlier requests we no
    # longer hold a start for, and pairing them with this one would be wrong.
    latest: dict[str, dict] = {}
    for record in lines:
        token = str(record["payload"].get("asset_id"))
        latest[token] = record

    for token, record in latest.items():
        started = starts.get(token)
        received = datetime.fromisoformat(record["received_at_utc"])
        assert started is not None, f"no recorded start for {token}"
        # Strictly after: with a 50 ms delay, equality would mean the archive
        # was stamped with the request start rather than the arrival.
        assert received > started, f"received {received} does not postdate request {started}"
        assert (received - started).total_seconds() >= 0.04


def test_the_venue_timestamp_is_still_the_venues_own(settings) -> None:
    """Our clock must never be written into the venue field."""
    payload_with_ts = {
        "market": "0xabc",
        "asset_id": "token-venue",
        "timestamp": "1791732714680",
        "bids": [{"price": BOOK_PRICE, "size": "100"}],
        "asks": [{"price": "0.54", "size": "100"}],
    }
    expected = datetime.fromtimestamp(1791732714680 / 1000.0, tz=UTC)

    class VenueHttp(TimingHttp):
        def get_json(self, url: str, **kwargs: Any) -> Any:
            if "/book" in url:
                return dict(payload_with_ts)
            return super().get_json(url, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(worker_module, "ResilientHttp", VenueHttp)
    try:
        worker = CollectorWorker(
            settings,
            run_id="venue-run",
            stream_enabled=False,
            once=True,
            heartbeat_seconds=0.05,
            book_poll_seconds=0.0,
            discovery_seconds=0.0,
            integrity_seconds=0.0,
            parquet_daily=False,
            backup_daily=False,
        )
        worker.run()
    finally:
        mp.undo()

    lines = _rest_lines(settings.raw_dir)
    assert lines
    record = lines[0]
    assert record["venue_ts_utc"] == expected.isoformat()
    assert record["venue_ts_origin"] == "venue"
    # ...and the receipt is still ours, still later than the venue's.
    received = datetime.fromisoformat(record["received_at_utc"])
    assert received >= expected


# --- 4. stream resilience -------------------------------------------------


def test_dropped_frames_are_counted_and_logged_once_not_per_frame(caplog) -> None:
    """A full queue must not produce one WARNING per frame.

    Under a sustained backlog that is thousands of identical lines, which
    buries everything else the collector has to say.
    """
    import logging as _logging

    from weatherquant.data.stream import MarketStream

    stream = MarketStream(
        queue=queue.Queue(maxsize=1),
        stats=None,
        shutdown=None,
    )

    with caplog.at_level(_logging.WARNING, logger="weatherquant.data.stream"):
        for _ in range(1200):
            stream._note_drop()

    warnings = [r for r in caplog.records if "dropped" in r.getMessage()]
    assert stream.frames_dropped == 1200
    # First drop, then every 500th: 1, 500 and 1000 for 1200 drops - three
    # lines instead of twelve hundred.
    assert len(warnings) == 3, [r.getMessage() for r in warnings]
    assert "dropped 1 frame(s)" in warnings[0].getMessage()
    assert "1000 frame(s)" in warnings[-1].getMessage()


def test_queue_depth_and_high_water_reach_the_heartbeat(settings) -> None:
    """Backpressure must be visible, not only inferable from a drop count."""
    worker = _worker_with_books(settings, tokens=4)
    for _ in range(7):
        worker._ws_queue.put_nowait((datetime.now(tz=UTC), {"event_type": "book"}))

    # Read first: depth is sampled at publication time, so this sees the queue
    # as it stands, not as the drain left it.
    telemetry = worker._telemetry()
    ws = telemetry["ws_queue"]
    assert ws["capacity"] == 20_000
    assert ws["depth"] >= 7, "the queued frames should still be visible"
    assert ws["high_water"] >= ws["depth"]
    assert ws["high_water"] >= 7, "the peak must be remembered"
    assert ws["frames_dropped"] == 0

    # After the drain the depth falls, and the high-water keeps the record of
    # what it had been - that is the number that shows a past backlog.
    worker._drain_stream(worker._archive)
    after = worker._telemetry()["ws_queue"]
    assert after["depth"] <= ws["depth"]
    assert after["high_water"] >= 7


def test_budget_and_fairness_reach_the_heartbeat(settings) -> None:
    """The numbers an operator needs must be published, not just computed."""
    worker = _worker_with_books(settings, tokens=40)
    http = TimingHttp(delay=0.02)
    worker.POLL_BUDGET_SECONDS = 0.1
    worker._poll_books(http)

    telemetry = worker._telemetry()
    budget = telemetry["poll_budget"]
    assert budget["budget_seconds"] == 0.1
    assert budget["exceeded"] >= 1
    assert budget["deferred_tokens"] > 0

    ages = telemetry["per_token_book_age_ms"]
    assert ages is None or ages["tokens"] > 0


def test_rest_timeouts_are_counted_separately_from_transport_errors() -> None:
    """A timeout is "too slow", a refusal is "not there" - different problems."""
    import httpx

    from weatherquant.data.http import HttpStatusError, ResilientHttp

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    http = ResilientHttp(
        retries=1,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(HttpStatusError):
        http.get_json("https://example.invalid/book")

    assert http.timeouts >= 1, "a timeout must be counted as a timeout"
    assert http.requests_failed >= 1


# --- 5. no new gaps or duplicates -----------------------------------------


def test_a_slow_run_creates_no_gaps_and_no_duplicate_books(settings) -> None:
    """Acceptance: bounded work must not cost data."""
    from weatherquant.storage.db import connect

    mp = pytest.MonkeyPatch()
    mp.setattr(worker_module, "ResilientHttp", TimingHttp)
    try:
        worker = CollectorWorker(
            settings,
            run_id="integrity-run",
            stream_enabled=False,
            once=True,
            heartbeat_seconds=0.05,
            book_poll_seconds=0.0,
            discovery_seconds=0.0,
            integrity_seconds=0.0,
            parquet_daily=False,
            backup_daily=False,
        )
        worker.run()
    finally:
        mp.undo()

    con = connect(settings.db_path, read_only=True)
    try:
        books = con.execute("SELECT COUNT(*), COUNT(DISTINCT book_id) FROM order_books").fetchone()
        gaps = con.execute(
            "SELECT COUNT(*) FROM collection_gaps WHERE ended_at_utc IS NULL"
        ).fetchone()[0]
    finally:
        con.close()

    total, distinct = books
    assert total > 0, "the slow run produced no books"
    assert total == distinct, f"{total - distinct} duplicate book_id(s) written"
    assert gaps == 0, f"{gaps} gap(s) left open by a run that completed"


def test_the_heartbeat_still_reports_after_a_slow_sweep(settings) -> None:
    """Acceptance: REST blocking must not stop the heartbeat."""
    heartbeat_path = settings.db_path.parent / "health" / "heartbeat.json"

    mp = pytest.MonkeyPatch()
    mp.setattr(worker_module, "ResilientHttp", TimingHttp)
    try:
        worker = CollectorWorker(
            settings,
            run_id="heartbeat-run",
            stream_enabled=False,
            once=True,
            heartbeat_seconds=0.0,
            book_poll_seconds=0.0,
            discovery_seconds=0.0,
            integrity_seconds=0.0,
            parquet_daily=False,
            backup_daily=False,
        )
        worker.run()
    finally:
        mp.undo()

    assert heartbeat_path.exists(), "no heartbeat was published after a slow sweep"
    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["updated_at_utc"]
    assert payload["status"]
    assert "telemetry" in payload
    assert "poll_budget" in payload["telemetry"]

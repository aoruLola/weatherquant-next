"""The shadow engine's loop: ingest, predict, evaluate, persist, report.

Shape of one cycle
------------------
Four phases, in this order, and the order is load-bearing:

1. **Ingest.** Read new complete archive lines, apply them to the in-memory
   view, and commit *effects plus byte offsets in one transaction*. A crash
   anywhere afterwards replays nothing, because the offsets already moved
   together with what they describe.
2. **Forecast.** Network I/O, deliberately *outside* any transaction - a
   hanging upstream must never hold a database lock open.
3. **Evaluate.** Resolve forecasts, compute distributions, run the gates, and
   commit predictions and decisions.
4. **Health.** Rewrite the heartbeat file so the container probe can answer
   without opening DuckDB.

Everything expensive is keyed so it happens once:

* a distribution is memoised on ``(model, version, run, date, event)`` - the
  same run priced twice is the same bytes, so it is computed once;
* forecasts are fetched at most ``shadow_forecast_refresh_seconds`` apart, not
  on every tick, so a burst of archive lines cannot turn into a burst of
  upstream calls;
* the markets under evaluation are whatever is tradeable *now*, so a market
  that closes stops costing cycles the moment it closes.

Nothing here places an order. There is no wallet, no client order id, and no
code path that could reach one - by construction, not by convention.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from weatherquant.config import Settings
from weatherquant.data.http import ResilientHttp
from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.shadow.config import ShadowSettings, Thresholds
from weatherquant.shadow.decision import Decision, Direction, Mode, evaluate
from weatherquant.shadow.events import CatalogMarket
from weatherquant.shadow.forecast import ForecastCache
from weatherquant.shadow.health import disk_free_percent, write_heartbeat
from weatherquant.shadow.inference import PredictionResult, Predictor, prediction_rows
from weatherquant.shadow.ingest import ArchiveTailer, QualityEvent
from weatherquant.shadow.report import build_report, publish_report_snapshot
from weatherquant.shadow.state import ShadowState
from weatherquant.shadow.store import ShadowStore
from weatherquant.storage.shadow import ShadowDatabase
from weatherquant.weather.daily_max import local_day_bounds_utc
from weatherquant.weather.single_runs import SingleRunsClient

__all__ = ["RunnerConfig", "ShadowRunner"]

log = logging.getLogger("weatherquant.shadow")


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """The immutable part of a run, recorded so a run can be reproduced."""

    settings: Settings
    shadow: ShadowSettings
    thresholds: Thresholds

    def as_dict(self) -> dict:
        return {
            "station": self.settings.station,
            "market_timezone": self.settings.market_timezone,
            "weather_fee_rate": self.settings.weather_fee_rate,
            "execution_buffer_fraction": self.settings.execution_buffer_fraction,
            "primary_model": self.shadow.shadow_primary_model,
            "model_names": self.shadow.model_names,
            "collector_raw_dir": str(self.shadow.collector_raw_dir),
            "shadow_db_path": str(self.shadow.shadow_db_path),
            "tick_seconds": self.shadow.shadow_tick_seconds,
            "forecast_refresh_seconds": self.shadow.shadow_forecast_refresh_seconds,
            "collector_silence_seconds": self.shadow.shadow_collector_silence_seconds,
            "configured_mode": self.shadow.shadow_mode,
            "thresholds": self.thresholds.as_dict(),
        }


@dataclass
class CycleStats:
    cycle_id: str
    started_at: datetime
    lines_read: int = 0
    books: int = 0
    markets: int = 0
    quality_events: int = 0
    forecasts_fetched: int = 0
    predictions: int = 0
    decisions: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    primary_reasons: dict[str, int] = field(default_factory=dict)
    edges: list[float] = field(default_factory=list)
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "lines_read": self.lines_read,
            "books": self.books,
            "markets": self.markets,
            "quality_events": self.quality_events,
            "forecasts_fetched": self.forecasts_fetched,
            "predictions": self.predictions,
            "decisions": self.decisions,
            "by_state": dict(self.by_state),
            "primary_reasons": dict(self.primary_reasons),
            "duration_ms": self.duration_ms,
        }


class ShadowRunner:
    """Owns the database, the lock, and the loop. One instance per process."""

    #: Days either side of today that a market is worth watching, and the
    #: horizon the forecast cache holds runs for (Open-Meteo serves 240h).
    _horizon_days: int = 10

    def __init__(
        self,
        config: RunnerConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        http: ResilientHttp | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.settings = config.settings
        self.shadow = config.shadow
        self.thresholds = config.thresholds

        self.db = ShadowDatabase(self.shadow.shadow_db_path)
        self.store: ShadowStore | None = None
        self.state = ShadowState()
        self.tailer = ArchiveTailer(
            self.shadow.collector_raw_dir,
            station=self.settings.station,
            clock=self.clock,
        )
        self.predictor = Predictor(
            str(self.shadow.shadow_models_dir),
            model_names=self.shadow.model_names,
            clock=self.clock,
        )
        self.http = http
        self.forecast: ForecastCache | None = None

        self._lock: SingleInstanceLock | None = None
        self.run_id: str | None = None
        self._stop = False
        self._next_forecast_refresh = datetime.min.replace(tzinfo=UTC)
        self._next_metrics = datetime.min.replace(tzinfo=UTC)
        self._next_report = datetime.min.replace(tzinfo=UTC)
        self._forecast_backoff = 0.0
        self._last_decision_at: datetime | None = None
        self._last_cycle: CycleStats | None = None
        self._started_at: datetime | None = None
        self._cycles = 0
        self._fatal: str | None = None
        self._pending_stop_reason: str | None = None
        self._collector_last_seen: datetime | None = None
        self._phase_started_at: datetime | None = None
        self._closed = False
        #: Decided once, after the first ingest, and held for the whole run.
        self._mode: Mode | None = None
        self._mode_reason: str | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> str:
        """Acquire the single-instance lease, open the database, hydrate.

        The lock is claimed *before* the database is opened, exactly as the
        collector does: two writers on one file die with an opaque IO error,
        and a named refusal is a much better thing to log.
        """
        db_path = self.shadow.shadow_db_path
        lock_path = db_path.parent / ".shadow.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = SingleInstanceLock(
            lock_path,
            run_id=uuid.uuid4().hex,
            hostname=socket.gethostname(),
            stale_after_seconds=max(60.0, self.shadow.shadow_tick_seconds * 4),
        )
        self._lock.acquire()

        self.db.open()
        self.store = ShadowStore(self.db)

        self.state.load_catalog(self.store.load_catalog_rows())
        self.state.load_books(self.store.load_book_rows())
        self.tailer.load_checkpoints(self.store.load_checkpoints())
        self.tailer.set_market_of_token(self.state.market_of_token)
        # Hydrate the feed's last-seen time from the offsets. Without this a
        # restarted engine that reads nothing new on its first cycle would
        # report COLLECTOR_DISCONNECTED about a collector that is perfectly
        # healthy - it simply had nothing new to say.
        self._collector_last_seen = self.store.collector_last_seen()

        # Models are loaded before the run row, so a missing manifest fails the
        # start rather than producing a run that can never predict.
        loaded = self.predictor.load()

        self.http = self.http or ResilientHttp(
            timeout_seconds=self.settings.http_timeout_seconds,
            retries=self.settings.http_retries,
        )
        self.forecast = ForecastCache(
            SingleRunsClient(self.http, raw_dir=None),
            station=self.settings.station,
            target_dates=self._horizon_target_dates,
            clock=self.clock,
        )
        self.forecast.load_cached_rows(*self.store.load_forecast_rows())

        now = self.clock()
        self.run_id = self.db.start_run(
            "shadow",
            {
                **self.config.as_dict(),
                "loaded_models": loaded,
                "app_version": _app_version(),
                "git_sha": os.environ.get("GIT_SHA", "unknown"),
                "image_tag": os.environ.get("IMAGE_TAG", "dev"),
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
            },
        )
        self.store.save_model_versions(
            self.predictor.version_rows(loaded_at_utc=now, primary=self.shadow.shadow_primary_model)
        )
        self.store.record_quality(
            [
                QualityEvent(
                    kind="shadow_started",
                    severity="info",
                    detail=f"run {self.run_id} started with models {', '.join(loaded)}",
                    ts_utc=now,
                )
            ],
            run_id=self.run_id,
        )
        self._started_at = now
        self._persist_forecasts(now)
        log.info("shadow started run_id=%s models=%s", self.run_id, ", ".join(loaded))
        # Published before the first cycle, not after it. The first cycle does
        # network I/O, and a heartbeat that only appears once a forecast has
        # been fetched reports "not started" for as long as the upstream takes
        # - which is a liveness signal reading the latency of a third party.
        self._write_heartbeat(status="running")
        self._publish_report()
        return self.run_id

    def stop(self, reason: str = "requested") -> None:
        self._stop = True
        self._pending_stop_reason = reason

    def close(self, *, status: str = "stopped", reason: str | None = None) -> None:
        """Flush, record the run, release the lease. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        if self.store is not None and self.run_id is not None:
            try:
                self._write_heartbeat(status=status, reason=reason)
                self.db.finish_run(
                    self.run_id,
                    status=status,
                    stats=self._totals(),
                    reason=reason,
                )
            except Exception:
                log.exception("failed to record run finish")
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        self.db.close()

    # -- loop ---------------------------------------------------------------

    def run_forever(self) -> int:
        """Loop until SIGTERM/SIGINT or :meth:`stop`. Returns a shell exit code."""
        self._install_signal_handlers()
        exit_code = 0
        try:
            self.start()
            while not self._stop:
                try:
                    self.cycle()
                except LockError as exc:
                    log.error("lost the single-instance lease: %s", exc)
                    self._fatal = str(exc)
                    exit_code = 4
                    break
                except Exception as exc:
                    self._fatal = f"{type(exc).__name__}: {exc}"
                    log.exception("cycle failed; backing off")
                    self._write_heartbeat(status="running", fatal=self._fatal)
                    if not self._sleep(self.shadow.shadow_backoff_max_seconds):
                        break
                    continue
                if not self._sleep(self.shadow.shadow_tick_seconds):
                    break
        except LockError as exc:
            log.error("%s", exc)
            exit_code = 4
        finally:
            self.close(
                status="failed" if exit_code else "stopped",
                reason=self._fatal or getattr(self, "_pending_stop_reason", None),
            )
        return exit_code

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("received signal %s; finishing the current cycle", signum)
            self.stop(f"signal {signum}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):  # not the main thread, or unsupported
                signal.signal(sig, handler)

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns False if a stop was requested meanwhile."""
        deadline = self.clock() + timedelta(seconds=seconds)
        while not self._stop:
            remaining = (deadline - self.clock()).total_seconds()
            if remaining <= 0:
                return True
            try:
                time.sleep(min(remaining, 1.0))
            except KeyboardInterrupt:
                self.stop("keyboard interrupt")
                return False
        return False

    # -- one cycle ----------------------------------------------------------

    def cycle(self) -> CycleStats:
        assert self.store is not None and self.forecast is not None
        started = self.clock()
        stats = CycleStats(cycle_id=uuid.uuid4().hex[:16], started_at=started)
        # Rewritten at the TOP as well as the bottom. Phase 2 makes network
        # calls, so a cycle can be in flight for as long as an upstream takes
        # to answer; without this the probe would read "stale" about a process
        # that is perfectly alive and simply waiting.
        self._write_heartbeat(status="running")

        # Phase 1: ingest, atomically with its offsets.
        tail = self.tailer.poll()
        self.state.apply_markets(tail.market_updates)
        self.state.apply_books(tail.book_updates)
        self.tailer.set_market_of_token(self.state.market_of_token)
        flat_markets = [m for batch in tail.market_updates for m in batch]
        self.store.commit_batch(
            run_id=self.run_id or "",
            now=started,
            markets=flat_markets,
            books=tail.book_updates,
            checkpoints=tail.checkpoints,
            quality=tail.quality,
        )
        stats.lines_read = tail.stats.lines_read
        stats.books = len(tail.book_updates)
        stats.markets = len(flat_markets)
        stats.quality_events = len(tail.quality)
        if tail.stats.collector_received_max is not None and (
            self._collector_last_seen is None
            or tail.stats.collector_received_max > self._collector_last_seen
        ):
            self._collector_last_seen = tail.stats.collector_received_max

        # Mode is resolved once, after the first ingest: the archive's own age
        # is what tells a live feed from history, and a mode that could flip
        # mid-run would let a dead collector silently become a replay.
        mode, mode_reason = self._resolve_mode()

        # Phase 2: forecasts (network, no transaction held).
        #
        # Resolved against the *decision* instant, not the wall clock: in
        # replay that is the moment the archive represents, so only runs
        # publishable by then may be fetched for it.
        base = self._decision_instant()
        self._maybe_refresh_forecasts(stats, base)

        # Phase 3: predict and evaluate.
        #
        # The decision instant is read HERE, not at the top of the cycle. Every
        # input above was obtained between `started` and this line, so stamping
        # the decision with `started` would make each of them look like it
        # arrived *after* the decision that used it - and the clock gates would
        # then refuse every decision for a book that was in fact already in
        # hand. The instant a decision is made is the instant its inputs are
        # all available, which is this one.
        decision_time = base if mode is Mode.REPLAY else self.clock()
        late_quality: list[QualityEvent] = []
        self._evaluate(stats, decision_time, late_quality, mode, mode_reason)

        # Phase 4: metrics + health.
        now = self.clock()
        metrics = self._metrics_payload(stats, now) if now >= self._next_metrics else None
        if metrics is not None:
            self._next_metrics = now + timedelta(seconds=self.shadow.shadow_metrics_seconds)
        if late_quality:
            stats.quality_events += len(late_quality)
        if metrics is not None or late_quality:
            self.store.commit_batch(
                run_id=self.run_id or "",
                now=now,
                markets=[],
                books=[],
                checkpoints={},
                quality=late_quality,
                metrics=metrics,
            )

        self._publish_report()

        stats.duration_ms = int((self.clock() - started).total_seconds() * 1000)
        self._last_cycle = stats
        self._cycles += 1
        self._write_heartbeat(status="running")
        if self._lock is not None:
            self._lock.heartbeat()
        return stats

    # -- phases -------------------------------------------------------------

    def _maybe_refresh_forecasts(self, stats: CycleStats, now: datetime) -> None:
        assert self.forecast is not None
        if now < self._next_forecast_refresh:
            return
        try:
            result = self.forecast.refresh(
                now=now,
                lookback_hours=self.shadow.shadow_run_lookback_hours,
                max_new_per_model=self.shadow.shadow_max_new_runs_per_cycle,
            )
            stats.forecasts_fetched = result["fetched"]
            self._forecast_backoff = 0.0
        except Exception as exc:
            self._forecast_backoff = min(
                self._forecast_backoff * 2 or 30.0, self.shadow.shadow_backoff_max_seconds
            )
            log.warning("forecast refresh failed: %s; backing off %.0fs", exc, self._forecast_backoff)
            self.store.record_quality(
                [
                    QualityEvent(
                        kind="forecast_fetch_failed",
                        severity="warning",
                        detail=f"{type(exc).__name__}: {exc}",
                        ts_utc=now,
                    )
                ],
                run_id=self.run_id or "",
            )
            self._next_forecast_refresh = now + timedelta(seconds=self._forecast_backoff)
            return
        self._next_forecast_refresh = now + timedelta(
            seconds=self.shadow.shadow_forecast_refresh_seconds
        )
        self._persist_forecasts(now)

    def _persist_forecasts(self, now: datetime) -> None:
        assert self.forecast is not None and self.store is not None
        run_rows, daily_rows = self.forecast.rows_for_persistence()
        with self.db.transaction():
            self.store.save_forecasts(run_rows, daily_rows)

    def _publish_report(self) -> None:
        """Compute a report from the connection we already hold, then swap it in.

        The engine's DuckDB file is leased to this process for its whole life
        and DuckDB refuses any *other* process the file, read-only included -
        so an operator running `weatherquant-shadow report` against a live
        engine used to collide with it. Publishing a snapshot moves the read
        off the database entirely.

        Built here rather than by the CLI because this is the only side that
        can open the file, and throttled so a report is not recomputed on
        every tick.

        A failure here is logged and swallowed: reporting is not the mission,
        and a malformed row must not be able to stop the engine from running.
        """
        if self.store is None:
            return
        now = self.clock()
        if now < self._next_report:
            return
        # Advanced before the work: a report that raises must not be retried
        # on the very next tick.
        self._next_report = now + timedelta(seconds=self.shadow.shadow_report_seconds)
        try:
            report = build_report(self.db.con, now=now)
            publish_report_snapshot(
                self.shadow.shadow_report_path,
                report,
                stale_after_seconds=self.shadow.shadow_report_stale_seconds,
                now=now,
            )
        except Exception as exc:
            log.warning("could not publish the report snapshot: %s", exc)
            self.store.record_quality(
                [
                    QualityEvent(
                        kind="report_publish_failed",
                        severity="warning",
                        detail=f"{type(exc).__name__}: {exc}",
                        ts_utc=now,
                    )
                ],
                run_id=self.run_id or "",
            )

    def _decision_instant(self) -> datetime:
        """The instant decisions are judged against.

        LIVE: now, because a live feed's data is contemporaneous with the
        decision. REPLAY: the newest line in the archive - the moment this
        archive *represents*. Using `now` there would age every book by
        however long ago the archive was written, and using the archive's
        instant while tailing a live feed would freeze decisions in the past.
        """
        if self._mode is Mode.REPLAY:
            return self._collector_last_seen or self.clock()
        return self.clock()

    def _resolve_mode(self) -> tuple[Mode, str]:
        """Decide LIVE vs REPLAY once and hold it for the whole run.

        Returns ``(mode, reason)``; the reason is stored on every decision so
        "why is this row REPLAY?" is answerable from the row itself.

        `auto` looks at the age of the newest archive line against the
        silence budget: a feed being written now is LIVE, a directory of old
        files is REPLAY. Held for the run because a collector that dies
        *after* startup must surface as COLLECTOR_DISCONNECTED - flipping to
        replay at that moment would convert an outage into a stream of
        historical decisions that look current.
        """
        if self._mode is not None:
            # Both halves are cached, not just the mode: the reason is part of
            # what the decision rows record, and recomputing it against a
            # clock that has since moved would describe a different instant
            # than the one the mode was actually chosen from.
            return self._mode, self._mode_reason or "resolved_before_this_call"

        configured = self.shadow.shadow_mode
        if configured == "live":
            self._mode, reason = Mode.LIVE, "configured:live"
        elif configured == "replay":
            self._mode, reason = Mode.REPLAY, "configured:replay"
        elif self._collector_last_seen is None:
            # Nothing read yet: we cannot be replaying history we never saw,
            # and reporting a missing feed is more honest than inventing a
            # mode. The COLLECTOR_DISCONNECTED gate will say the rest.
            self._mode, reason = Mode.LIVE, "auto:no_archive_lines_read"
        else:
            age = (self.clock() - self._collector_last_seen).total_seconds()
            limit = self.shadow.shadow_collector_silence_seconds
            if age <= limit:
                self._mode, reason = Mode.LIVE, f"auto:archive_age_{age:.0f}s_le_{limit:.0f}s"
            else:
                self._mode, reason = Mode.REPLAY, f"auto:archive_age_{age:.0f}s_gt_{limit:.0f}s"
        self._mode_reason = reason
        log.info("shadow mode=%s reason=%s", self._mode.value, reason)
        return self._mode, reason

    def _evaluate(
        self,
        stats: CycleStats,
        now: datetime,
        late_quality: list[QualityEvent],
        mode: Mode,
        mode_reason: str,
    ) -> None:
        assert self.store is not None and self.forecast is not None
        watchable = self._evaluable_markets()
        if not watchable:
            return
        self._phase_started_at = self.clock()

        # Keyed on the prediction's natural key: two markets settling on the
        # same date share one distribution, and writing it twice would report
        # predictions that were never computed twice.
        predictions: dict[tuple, tuple] = {}
        decisions: list[Decision] = []
        seen_groups: dict[tuple, object] = {}

        for market in watchable:
            group = self._group_for(market, seen_groups)
            closed, closed_detail = self._closed_reason(market, now)
            forecast_by_model = {}
            for model_name in self.shadow.model_names:
                forecast = self.forecast.resolve(model_name, market.target_date, now)
                forecast_by_model[model_name] = forecast
                if group is None or forecast is None:
                    continue
                outcome = self.predictor.predict(
                    shadow_model=model_name,
                    forecast=forecast,
                    group=group,
                    decision_time=now,
                )
                if isinstance(outcome, PredictionResult):
                    row = prediction_rows(
                        result=outcome,
                        forecast=forecast,
                        group=group,
                        station=self.settings.station,
                        decision_time=now,
                        computed_at=self.clock(),
                    )
                    key = (row[0], row[1], row[2], row[3], row[4], row[5])
                    if key not in predictions:
                        predictions[key] = row
                        stats.predictions += 1
                else:
                    late_quality.append(
                        QualityEvent(
                            kind="prediction_refused",
                            severity="warning",
                            detail=f"{model_name} {market.event_id}: {outcome.reason} {outcome.detail}",
                            ts_utc=now,
                        )
                    )

            primary = self.shadow.shadow_primary_model
            snapshot = self.state.book_for(market.token_yes)
            prediction = None
            if group is not None and forecast_by_model.get(primary) is not None:
                prediction = self.predictor.predict(
                    shadow_model=primary,
                    forecast=forecast_by_model[primary],
                    group=group,
                    decision_time=now,
                )

            for direction in (Direction.BUY_YES, Direction.SELL_YES):
                decision = evaluate(
                    cycle_id=stats.cycle_id,
                    market=market,
                    direction=direction,
                    prediction=prediction,
                    forecast=forecast_by_model.get(primary),
                    snapshot=snapshot,
                    thresholds=self.thresholds,
                    buffer_fraction=self.settings.execution_buffer_fraction,
                    decision_time=now,
                    collector_last_seen=self._collector_last_seen,
                    collector_silence_seconds=self.shadow.shadow_collector_silence_seconds,
                    max_forecast_age_hours=self.thresholds.max_forecast_age_hours,
                    market_closed=closed,
                    closed_detail=closed_detail,
                    mode=mode,
                    mode_reason=mode_reason,
                )
                # Latency is measured with the *same* clock as everything
                # else. Mixing an injected clock with the wall clock would make
                # this number mean nothing the moment either one moved.
                latency_ms = max(
                    int((self.clock() - self._phase_started_at).total_seconds() * 1000), 0
                )
                decision = replace(decision, decision_latency_ms=latency_ms)
                decisions.append(decision)
                stats.by_state[decision.state.value] = stats.by_state.get(decision.state.value, 0) + 1
                if decision.primary_reason:
                    key = decision.primary_reason.value
                    stats.primary_reasons[key] = stats.primary_reasons.get(key, 0) + 1
                if decision.net_edge is not None:
                    stats.edges.append(decision.net_edge)

        stats.decisions = len(decisions)
        if decisions:
            self._last_decision_at = now
        if predictions or decisions:
            with self.db.transaction():
                self.store.save_predictions(list(predictions.values()))
                self.store.save_snapshots(decisions, now=now)
                self.store.save_decisions(decisions, run_id=self.run_id or "", now=now)

    def _group_for(self, market: CatalogMarket, cache: dict):
        if market.target_date is None:
            return None
        key = (market.event_id, market.target_date)
        if key in cache:
            return cache[key]
        groups = self.state.groups_for(market.target_date)
        chosen = next((g for g in groups if g.event_id == market.event_id), None)
        cache[key] = chosen
        return chosen

    def _closed_reason(self, market: CatalogMarket, now: datetime) -> tuple[bool, str]:
        if market.closed:
            return True, "the venue reports this market as closed"
        if not market.accepting_orders:
            return True, "the venue reports this market is not accepting orders"
        if not market.active:
            return True, "the venue reports this market as inactive"
        if market.target_date is None:
            return True, "market has no target date, so its trading window is unknown"
        _start, end = local_day_bounds_utc(market.target_date)
        if now >= end:
            return True, f"the target day {market.target_date} has already ended"
        return False, ""

    # -- reporting ----------------------------------------------------------

    def _today(self) -> date:
        return self.clock().astimezone().date()

    def _evaluable_markets(self) -> list[CatalogMarket]:
        """Markets whose target day is near enough to be worth watching.

        Closed markets inside the window are evaluated *too*. They produce
        ``NO_TRADE`` / ``MARKET_CLOSED``, which is a real observation about the
        feed and is the only way that state is reachable in production rather
        than only in a unit test. Filtering to tradeable-only would make the
        report's NO_TRADE count permanently zero and hide whether the lifecycle
        gate works at all.

        Outside the window is history: a market settling in July is not
        something this engine is watching, and emitting NO_TRADE for it every
        cycle would bury the ones that matter.
        """
        today = self._today()
        earliest, latest = today - timedelta(days=1), today + timedelta(days=self._horizon_days)
        return [
            m
            for m in self.state.markets.values()
            if m.target_date is not None and earliest <= m.target_date <= latest
        ]

    def _horizon_target_dates(self) -> list[date]:
        """Dates we should hold forecasts for: everything in the catalog that
        has not already finished, plus the next few days for markets that have
        not been discovered yet."""
        today = self._today()
        dates = {m.target_date for m in self.state.markets.values() if m.target_date}
        for offset in range(0, self._horizon_days + 1):
            dates.add(today + timedelta(days=offset))
        return sorted(d for d in dates if d >= today - timedelta(days=1))

    def _totals(self) -> dict:
        if self.store is None:
            return {}
        counts = self.db.table_counts()
        return {
            "tables": counts,
            "cycles": self._cycles,
            "started_at": self._started_at.isoformat() if self._started_at else None,
        }

    def _metrics_payload(self, stats: CycleStats, now: datetime) -> dict:
        """A machine-readable snapshot. Never a realised P&L: there is none."""
        edges = stats.edges
        return {
            "computed_at_utc": now.isoformat(),
            "cycle_id": stats.cycle_id,
            "cycles": self._cycles,
            "ingest": {
                "lines_read": stats.lines_read,
                "books": stats.books,
                "markets": stats.markets,
                "quality_events": stats.quality_events,
            },
            "forecasts": {"fetched": stats.forecasts_fetched},
            "mode": self._mode.value if self._mode else Mode.UNKNOWN.value,
            "decisions": {
                "evaluated": stats.decisions,
                "by_state": dict(stats.by_state),
                "primary_reasons": dict(stats.primary_reasons),
            },
            "edge": {
                "note": "theoretical expectation under the model, not realised profit",
                "count": len(edges),
                "mean": (sum(edges) / len(edges)) if edges else None,
                "max": max(edges) if edges else None,
                "min": min(edges) if edges else None,
                "positive": sum(1 for e in edges if e > 0),
            },
            "latency_ms": {"last_cycle": stats.duration_ms},
            "collector_last_seen_utc": getattr(self, "_collector_last_seen", None),
            "thresholds_version": self.thresholds.version,
            "equity_curve": None,
        }

    def _write_heartbeat(self, *, status: str, fatal: str | None = None, reason: str | None = None) -> None:
        path = self.shadow.shadow_health_dir / "heartbeat.json"
        now = self.clock()
        disk = disk_free_percent(path.parent)
        payload = {
            "status": status,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "updated_at_utc": now.isoformat(),
            "started_at_utc": self._started_at.isoformat() if self._started_at else None,
            "last_decision_at_utc": self._last_decision_at.isoformat()
            if self._last_decision_at
            else None,
            "collector_last_seen_utc": str(getattr(self, "_collector_last_seen", None) or ""),
            "cycles": self._cycles,
            "fatal_error": fatal or self._fatal,
            "stop_reason": reason,
            "thresholds_version": self.thresholds.version,
            "mode": self._mode.value if self._mode else Mode.UNKNOWN.value,
            "configured_mode": self.shadow.shadow_mode,
            "primary_model": self.shadow.shadow_primary_model,
            "disk": {"free_percent": disk},
            "last_cycle": self._last_cycle.as_dict() if self._last_cycle else None,
        }
        try:
            write_heartbeat(path, payload)
        except OSError as exc:
            log.warning("could not write heartbeat: %s", exc)


def _app_version() -> str:
    try:
        from importlib.metadata import version

        return version("weatherquant-next")
    except Exception:
        return "unknown"


def free_disk_percent(path: str | Path) -> float | None:
    return disk_free_percent(path)

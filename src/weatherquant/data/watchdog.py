"""A watchdog that ends the collector when its loop stops ticking.

Why a watchdog at all
---------------------
The heartbeat file answers *"is the loop alive?"* for anyone who reads it -
the Docker probe, an operator, a script. But reading it does nothing to a
process that is wedged inside one blocking call (a frozen FUSE mount, a
socket without a timeout, a lock that will never come back). Docker's
``restart: unless-stopped`` restarts a container only when its *process
exits*; an unhealthy-but-running container just sits there being unhealthy.

So the process needs one thread whose job is to notice the loop is silent and
get the process out of the way so the restart policy can do its work:

    loop stuck > loop_stuck_seconds (default 120s, the probe's staleness
    threshold) -> publish a final diagnostic heartbeat -> exit non-zero.

Why ``os._exit`` and not an exception
-------------------------------------
The loop is stuck; unwinding it would mean running ``finally`` blocks on
code that is itself the blockage (the writer's flush, the archive's fsync).
``os._exit`` skips all of that deliberately. Nothing is lost that the
recovery path does not already handle: DuckDB replays its WAL, the next run
closes the open collection gap with a reason, and the raw archive refuses to
bury a torn line.

What it must not do
-------------------
Fire during a *legitimate* long main-thread job. The loop declares those via
:meth:`allow` - "I am about to do something bounded but slow" - and a
declaration that overruns its own budget still fires. The watchdog is also
stopped before shutdown teardown begins, because teardown (backup join,
checkpoint) is deliberately allowed to take longer than a loop tick.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, breaks the import cycle
    from weatherquant.data.worker import CollectorWorker

__all__ = ["LoopWatchdog"]

log = logging.getLogger(__name__)

# How often to look at the clock. Not a tunable: it only sets how late the
# watchdog can notice a stuck loop, and 5s is noise next to a 120s threshold.
CHECK_INTERVAL_SECONDS = 5.0


class LoopWatchdog:
    """Watch one loop's ticks from an independent thread.

    Ownership: the loop thread calls :meth:`tick`/:meth:`allow`/:meth:`stop`;
    the watchdog thread only reads them (under the GIL, each is one
    attribute read). No lock is needed because the worst race is a fire
    decision one interval late or one interval early - both harmless against
    a threshold measured in minutes.
    """

    def __init__(
        self,
        worker: CollectorWorker,
        *,
        stuck_seconds: float,
        check_interval: float = CHECK_INTERVAL_SECONDS,
        on_stuck: Callable[[str], None] | None = None,
    ) -> None:
        self._worker = worker
        self.stuck_seconds = stuck_seconds
        self.check_interval = check_interval
        # Tests substitute a recorder; production takes the default, which
        # ends the process so the restart policy can act.
        self._on_stuck = on_stuck
        self._last_tick_mono = time.monotonic()
        self._pause_deadline_mono = 0.0
        self._stopped = threading.Event()
        self._fired = False
        self.thread: threading.Thread | None = None

    # -- loop side ----------------------------------------------------------

    def start(self) -> None:
        self._last_tick_mono = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="wq-loop-watchdog", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop watching. Called before shutdown teardown begins."""
        self._stopped.set()

    def tick(self) -> None:
        """The loop is alive and iterating."""
        self._last_tick_mono = time.monotonic()
        self._pause_deadline_mono = 0.0

    def allow(self, seconds: float) -> None:
        """Declare a bounded, slow main-thread job about to run.

        The watchdog holds fire until ``seconds`` have passed *without a
        tick*; a job that overruns its own declaration still triggers the
        exit. Declarations are not cumulative - the latest wins - and any
        tick clears them.
        """
        if seconds > 0.0:
            self._pause_deadline_mono = time.monotonic() + seconds

    # -- watchdog side ------------------------------------------------------

    @property
    def fired(self) -> bool:
        return self._fired

    def _run(self) -> None:
        while not self._stopped.wait(self.check_interval):
            now = time.monotonic()
            silence = now - self._last_tick_mono
            if now < self._pause_deadline_mono:
                continue
            if silence <= self.stuck_seconds:
                continue
            self._fire(silence)
            return

    def _fire(self, silence: float) -> None:
        self._fired = True
        message = (
            f"collection loop stuck for {silence:.0f}s "
            f"(limit {self.stuck_seconds:.0f}s); ending the process so the "
            "container restart policy can recover it"
        )
        log.error("%s", message)
        worker = self._worker
        # A final heartbeat that says *why*: without it the probe would only
        # ever see an increasingly stale file and the operator would have to
        # guess between "stuck", "hung in backup", and "dead".
        try:
            worker.publish_stuck_heartbeat(message)
        except Exception:
            log.debug("watchdog could not publish its heartbeat", exc_info=True)
        callback = self._on_stuck
        if callback is not None:
            callback(message)
            return
        # Not a graceful exit on purpose - see the module docstring. The
        # exit code is FATAL so a supervisor can tell "died badly, restart"
        # from "finished normally".
        from weatherquant.data.worker import ExitCode

        os._exit(ExitCode.FATAL)

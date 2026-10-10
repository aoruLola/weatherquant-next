"""Rate limiting for the Polymarket public APIs.

The venue enforces per-IP limits on a **10-second sliding window** and, when
exceeded, *delays* the request rather than rejecting it. Complying client-side
keeps us out of that queue and keeps the collector predictable.

Limits are taken from the official rate-limits table (see
``docs/unraid-collector-deployment.md`` §rate-limits). A single shared limiter
per endpoint family is used rather than per-call-site counters, because the
venue counts by IP: two subsystems each staying "within their own budget" can
still jointly exceed the real one.
"""

from __future__ import annotations

import threading
import time
from collections import deque

__all__ = ["RateLimitError", "RateLimitRegistry", "SlidingWindowLimiter"]


class RateLimitError(RuntimeError):
    """Raised when a request could not be admitted within the allowed wait."""

    def __init__(self, name: str, limit: int, waited_seconds: float) -> None:
        self.name = name
        self.limit = limit
        self.waited_seconds = waited_seconds
        super().__init__(
            f"rate limiter {name!r} could not admit a request within "
            f"{waited_seconds:.1f}s (limit {limit}/window)"
        )


class SlidingWindowLimiter:
    """A thread-safe sliding-window counter.

    Threads share one limiter because the limit is per IP, not per thread.
    """

    def __init__(self, name: str, max_requests: int, window_seconds: float = 10.0) -> None:
        if max_requests < 1:
            raise ValueError(f"{name}: max_requests must be >= 1, got {max_requests}")
        if window_seconds <= 0:
            raise ValueError(f"{name}: window_seconds must be > 0, got {window_seconds}")
        self.name = name
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        """Take a slot if one is free. Never blocks. Returns False if full."""
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            if len(self._stamps) >= self.max_requests:
                return False
            self._stamps.append(now)
            return True

    def acquire(self, *, timeout_seconds: float = 60.0) -> None:
        """Take a slot, sleeping if necessary.

        Raises :class:`RateLimitError` if no slot frees up within ``timeout``.
        Sleeping is correct here rather than failing: the venue itself queues
        over-limit requests, so a bounded wait is cheaper than a retry storm.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.try_acquire():
                return
            with self._lock:
                self._evict(time.monotonic())
                if self._stamps:
                    # Wait until the oldest in-window request falls out.
                    wait = self._stamps[0] + self.window_seconds - time.monotonic()
                else:
                    wait = 0.05
            wait = min(max(wait, 0.01), 1.0)
            if time.monotonic() + wait > deadline:
                raise RateLimitError(self.name, self.max_requests, timeout_seconds)
            time.sleep(wait)

    def retry_after_seconds(self) -> float:
        """How long until the next slot frees, given the current window."""
        with self._lock:
            self._evict(time.monotonic())
            if len(self._stamps) < self.max_requests:
                return 0.0
            return max(0.0, self._stamps[0] + self.window_seconds - time.monotonic())

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.popleft()

    @property
    def in_window(self) -> int:
        with self._lock:
            self._evict(time.monotonic())
            return len(self._stamps)


class RateLimitRegistry:
    """Named limiters for every endpoint family the collector touches.

    Values mirror the official per-10s limits so that changing one here is a
    deliberate, reviewable act rather than a magic number buried in a call.
    """

    def __init__(self) -> None:
        self._limiters = {
            "gamma_events": SlidingWindowLimiter("gamma_events", 500, 10.0),
            "gamma_search": SlidingWindowLimiter("gamma_search", 350, 10.0),
            "clob_book": SlidingWindowLimiter("clob_book", 1500, 10.0),
            "clob_books_batch": SlidingWindowLimiter("clob_books_batch", 500, 10.0),
            "data_prices_history": SlidingWindowLimiter("data_prices_history", 200, 10.0),
        }
        self._lock = threading.Lock()

    def get(self, name: str) -> SlidingWindowLimiter:
        with self._lock:
            try:
                return self._limiters[name]
            except KeyError:
                raise KeyError(
                    f"unknown rate limit family {name!r}; add it to RateLimitRegistry "
                    f"with its official limit. Known: {sorted(self._limiters)}"
                ) from None

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        return {
            name: {
                "limit": lim.max_requests,
                "window_seconds": lim.window_seconds,
                "in_window": lim.in_window,
            }
            for name, lim in sorted(self._limiters.items())
        }

"""Shared HTTP transport: retries, rate limiting, and contact bookkeeping.

One client for every upstream so that retry policy, rate limiting and the
"when did we last successfully talk to the exchange" clock live in exactly one
place. Two policies worth calling out:

**Retry-After is honoured, and 4xx are not retried.** A 429 means *slow down*,
so the limiter is told to wait rather than the request being hammered again. A
4xx means we sent something wrong; retrying it just repeats the mistake. 5xx,
timeouts and connection errors are transient and are retried with capped
exponential backoff.

**Every success advances a shared contact clock.** The health probe reads that
clock, so it must advance on *any* successful response - including one whose
body has not changed - or a quiet market would look like an outage.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from weatherquant.data.ratelimit import RateLimitRegistry, SlidingWindowLimiter

log = logging.getLogger(__name__)

__all__ = ["HttpStatusError", "ResilientHttp"]


class HttpStatusError(RuntimeError):
    """A request kept failing through every retry."""

    def __init__(self, url: str, attempts: int, detail: str) -> None:
        self.url = url
        self.attempts = attempts
        self.detail = detail
        super().__init__(f"{url} failed after {attempts} attempt(s): {detail}")


class ResilientHttp:
    """Rate-limited, retrying JSON transport."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retries: int = 4,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 30.0,
        limits: RateLimitRegistry | None = None,
        user_agent: str = "weatherquant-next/collector",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.retries = retries
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.limits = limits or RateLimitRegistry()
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        # Shared across REST and WebSocket paths: the health probe asks "have we
        # heard from the exchange at all recently?", not "did this one request
        # succeed?".
        self.last_contact_utc: datetime | None = None
        self.requests_ok = 0
        self.requests_failed = 0
        self.retries_performed = 0
        self.rate_limit_waits = 0
        self.timeouts = 0

    # -- public ------------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        limiter: str | None = None,
        allow_not_found: bool = False,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> Any:
        """GET a JSON body.

        ``allow_not_found=True`` makes a 404 return ``None`` instead of raising.
        Used for ``/book`` on tokens whose market has been archived: the venue
        answering "no orderbook exists for this token" is a *correct, complete*
        conversation, not a transport failure, and counting it as one would trip
        the health probe's consecutive-failure rule on a perfectly healthy
        collector.

        ``timeout_seconds`` and ``retries`` override the client defaults for
        **this call only**. The book poll passes both so that a single request
        cannot outlive the sweep's budget: the defaults are tuned for
        one-off calls, not for one call inside a 45-second loop that also owns
        the heartbeat. Omitting them keeps the default behaviour exactly as it
        was.
        """
        return self._request_json(
            "GET",
            url,
            params=params,
            limiter=limiter,
            allow_not_found=allow_not_found,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )

    def post_json(
        self,
        url: str,
        *,
        json_body: Any = None,
        limiter: str | None = None,
    ) -> Any:
        return self._request_json("POST", url, json=json_body, limiter=limiter)

    def touch(self) -> None:
        """Record that we successfully reached the upstream right now."""
        self.last_contact_utc = datetime.now(tz=UTC)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ResilientHttp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------

    def _limiter(self, name: str | None) -> SlidingWindowLimiter | None:
        if name is None:
            return None
        return self.limits.get(name)

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        limiter: str | None = None,
        allow_not_found: bool = False,
        timeout_seconds: float | None = None,
        retries: int | None = None,
    ) -> Any:
        bucket = self._limiter(limiter)
        last_error = "unknown"
        sleep_after = 0.0
        # Per-call overrides. `attempts` is the whole point: a caller with a
        # deadline asks for fewer of them, and the budget maths downstream
        # relies on that number being the one actually used.
        attempt_budget = self.retries if retries is None else max(0, int(retries))
        request_timeout = timeout_seconds

        for attempt in range(attempt_budget + 1):
            if attempt:
                # Jitter keeps a fleet of retrying workers from resynchronising
                # into a thundering herd against the same endpoint.
                delay = min(
                    self.backoff_base_seconds * (2 ** (attempt - 1)),
                    self.backoff_max_seconds,
                )
                delay *= 1.0 + random.uniform(0.0, 0.25)
                time.sleep(delay + sleep_after)
                sleep_after = 0.0
                self.retries_performed += 1

            if bucket is not None:
                try:
                    bucket.acquire(timeout_seconds=60.0)
                    if attempt:
                        self.rate_limit_waits += 1
                except Exception as exc:
                    log.warning("rate limiter %s could not admit: %s", bucket.name, exc)

            try:
                if method == "GET":
                    response = self._client.get(url, params=params, timeout=request_timeout)
                else:
                    response = self._client.post(url, json=json_body, timeout=request_timeout)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.requests_failed += 1
                if isinstance(exc, httpx.TimeoutException):
                    # Counted separately from transport failures: a timeout is
                    # "the far end was too slow", which is the number the
                    # budget exists to bound, and lumping it in with a refused
                    # connection would hide exactly that.
                    self.timeouts += 1
                continue

            if response.status_code == 429:
                self.requests_failed += 1
                retry_after = response.headers.get("Retry-After")
                sleep_after = _parse_retry_after(retry_after)
                if sleep_after > self.backoff_max_seconds:
                    last_error = "429 with an unreasonably long Retry-After"
                    break
                last_error = "429 Too Many Requests"
                continue

            if response.status_code >= 500:
                self.requests_failed += 1
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                continue

            if response.status_code == 404 and allow_not_found:
                # A definitive answer from the venue, not a failure.
                self.requests_ok += 1
                self.touch()
                return None

            if response.status_code >= 400:
                # Client error: retrying sends the same bad request again.
                self.requests_failed += 1
                raise HttpStatusError(url, attempt + 1, f"HTTP {response.status_code}: {response.text[:300]}")

            try:
                payload = response.json()
            except ValueError as exc:
                self.requests_failed += 1
                last_error = f"invalid JSON from {url}: {exc}"
                continue

            self.requests_ok += 1
            self.touch()
            return payload

        self.requests_failed += 1
        raise HttpStatusError(url, attempt_budget + 1, last_error)


def _parse_retry_after(value: str | None) -> float:
    """Seconds from a Retry-After header (delta-seconds form)."""
    if not value:
        return 1.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 1.0


def contact_watcher(http: ResilientHttp) -> Callable[[], datetime | None]:
    """Adapter so the worker can read the contact clock without importing httpx."""
    return lambda: http.last_contact_utc

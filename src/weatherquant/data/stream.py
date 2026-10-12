"""Polymarket market-channel WebSocket client.

Why this exists alongside REST polling
--------------------------------------
``/book`` gives a complete, authoritative depth snapshot but only at the moments
we ask. The stream gives sub-second updates between polls. Neither alone is
enough: REST alone misses the gaps, stream alone misses frames while
reconnecting. So REST is treated as the source of truth for *state* and the
stream as a source of *events*, with every frame archived verbatim either way.

Failure handling
----------------
The thread owns its own connection and reconnects forever with capped
exponential backoff. It never raises into the worker: a dead stream must not
stop REST collection. Reconnect count and last-message time are published
through the state callback so the heartbeat can show whether the stream is
actually alive.

Every inbound frame is queued to the worker *before* being parsed. If the
parser does not understand a frame shape, the frame is still archived - the
cost of an unknown message is a missing structured row, never missing raw data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_MARKET_WS_URL", "MarketStream"]

DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Backoff for reconnects. Capped so a venue outage does not leave us dark for
# hours after it recovers, with jitter so every client does not retry in unison.
_BACKOFF_START_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0

# The server is expected to send pings; we treat a quiet period this long as a
# dead connection and reconnect proactively rather than waiting for a TCP timeout.
_RECV_TIMEOUT_SECONDS = 60.0

# The venue documents a client-initiated text PING at this interval.
_PING_INTERVAL_SECONDS = 10.0

StateCallback = Callable[..., None]


class MarketStream(threading.Thread):
    """A background thread that keeps a market-channel subscription alive."""

    def __init__(
        self,
        *,
        queue: queue.Queue,
        stats: Any,
        shutdown: Any,
        state_callback: StateCallback | None = None,
        endpoint: str = DEFAULT_MARKET_WS_URL,
        asset_ids: Callable[[], list[str]] | None = None,
    ) -> None:
        super().__init__(name="market-stream", daemon=True)
        self.queue = queue
        self.stats = stats
        self.shutdown = shutdown
        self.state_callback = state_callback
        self.endpoint = endpoint
        self._asset_ids = asset_ids
        self._assets: list[str] = []
        # Drops happen on this (producer) thread and are read by the worker's
        # thread for the heartbeat, so the counter is guarded. A plain int
        # increment across threads is not something to rely on for a number
        # that will be published and compared.
        self._drop_lock = threading.Lock()
        self._frames_dropped = 0
        # Only the first drop and then every DROP_LOG_INTERVAL frames: one
        # WARNING per dropped frame turns a backlog into megabytes of identical
        # lines and buries everything else the collector has to say.
        self._drop_log_interval = 500
        # NOTE: not `_stop`. `Thread._stop` is an internal *method*; storing an
        # Event in an attribute with that name shadows it and makes
        # `Thread.join()` raise `TypeError: 'Event' object is not callable`,
        # which broke the collector's graceful shutdown outright.
        self._stop_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        super().start()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=10.0)

    def set_assets(self, asset_ids: list[str]) -> None:
        """Replace the subscribed token set (applies on the next (re)subscribe)."""
        self._assets = list(asset_ids)

    @property
    def assets(self) -> list[str]:
        if self._asset_ids is not None:
            return list(self._asset_ids())
        return list(self._assets)

    # -- thread body -------------------------------------------------------

    def run(self) -> None:
        assert self._loop is not None, "start() must create the event loop"
        try:
            self._loop.run_until_complete(self._supervise())
        finally:
            with _suppress_all():
                self._loop.close()
            self._report(connected=False, last_message=None)

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stop_event.is_set() and not self.shutdown.requested():
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                attempt += 1
                self.stats.ws_reconnects += 1
                log.warning("market stream disconnected (%s); reconnecting", exc)
                self._report(connected=False, last_message=None)

            if self._stop_event.is_set() or self.shutdown.requested():
                break
            delay = min(_BACKOFF_START_SECONDS * (2 ** min(attempt, 10)), _BACKOFF_MAX_SECONDS)
            delay *= 1.0 + (hash((time.time_ns(), attempt)) % 1000) / 2000.0  # deterministic jitter
            # Wait in small slices so SIGTERM is honoured promptly.
            waited = 0.0
            while waited < delay and not self._stop_event.is_set() and not self.shutdown.requested():
                await asyncio.sleep(0.1)
                waited += 0.1

    async def _session(self) -> None:
        import websockets

        try:
            async with websockets.connect(
                self.endpoint,
                open_timeout=_RECV_TIMEOUT_SECONDS,
                close_timeout=5.0,
                max_size=8 * 1024 * 1024,
                ping_interval=20.0,
                ping_timeout=_RECV_TIMEOUT_SECONDS,
            ) as ws:
                self._ws = ws
                assets = self.assets
                if not assets:
                    log.info("market stream idle: no tokens to subscribe to yet")
                # The subscription is re-sent on every (re)connect: the server
                # does not remember it across connections.
                await ws.send(json.dumps({"type": "market", "assets_ids": assets}))
                self._report(connected=True, last_message=None)
                log.info("market stream subscribed to %d token(s)", len(assets))

                # The venue's heartbeat is an application-level *text* frame
                # ("PING"/"PONG"), not a WebSocket protocol ping, and the
                # official docs require the client to send it every 10s.
                # Without it an idle connection can be dropped without warning.
                pinger = asyncio.create_task(self._ping_loop(ws))
                try:
                    await self._read_loop(ws, assets)
                finally:
                    pinger.cancel()
                    with _suppress_all():
                        await pinger
        finally:
            self._ws = None

    async def _read_loop(self, ws: Any, assets: list[str]) -> None:
        last_assets = list(assets)
        while not self._stop_event.is_set() and not self.shutdown.requested():
            current = self.assets
            if current != last_assets:
                await ws.send(json.dumps({"type": "market", "assets_ids": current}))
                last_assets = list(current)
                log.info("market stream resubscribed: %d token(s)", len(current))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_RECV_TIMEOUT_SECONDS)
            except TimeoutError:
                raise ConnectionError(f"no frame for {_RECV_TIMEOUT_SECONDS}s") from None
            received_at = datetime.now(tz=UTC)
            payload = _decode(raw)
            if payload is None:
                # PONG, an ack, or anything that is not JSON. It still proves
                # the connection is alive - recv() returned, so the timeout
                # above reset - but it is not market data and must never be
                # reported as a last-message time.
                continue
            self._report(connected=True, last_message=received_at)
            try:
                self.queue.put_nowait((received_at, payload))
            except queue.Full:
                # Dropping frames under backpressure is correct: the REST poll
                # still guarantees a depth snapshot, and blocking the read loop
                # would make things worse. What is *not* correct is logging
                # once per drop - that is the log storm this counter replaces.
                self._note_drop()

    def _note_drop(self) -> int:
        """Count a dropped frame; log the first and then every Nth.

        Split out from the recv loop so it can be tested directly: a full queue
        needs a 20 000-frame backlog to occur naturally, which is not something
        a unit test should have to construct. Returns the running total.
        """
        with self._drop_lock:
            self._frames_dropped += 1
            dropped = self._frames_dropped
        if dropped == 1 or dropped % self._drop_log_interval == 0:
            log.warning("stream queue full; dropped %d frame(s) so far", dropped)
        return dropped

    @property
    def frames_dropped(self) -> int:
        """Frames discarded because the queue was full. Read from any thread."""
        with self._drop_lock:
            return self._frames_dropped

    async def _ping_loop(self, ws: Any) -> None:
        """Send the protocol's application-level PING at the documented interval."""
        while True:
            await asyncio.sleep(_PING_INTERVAL_SECONDS)
            await ws.send("PING")

    def _report(self, *, connected: bool, last_message: datetime | None) -> None:
        if self.state_callback is None:
            return
        try:
            self.state_callback(
                connected=connected, last_message=last_message, endpoint=self.endpoint
            )
        except Exception:
            log.debug("state callback failed", exc_info=True)


def _decode(raw: Any) -> Any | None:
    """Parse one frame. ``None`` means "not JSON we can use"; it is not an error.

    Frames that are pings, acks, or anything unrecognised are dropped *after*
    being counted as messages, and the worker archives whatever the socket
    delivered - so an unexpected shape costs structure, not evidence.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


class _suppress_all:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return True

"""A short in-memory history of what *this process has actually observed*.

Trend sparklines are the easiest place for a monitoring UI to lie: a counter
that starts at 42 looks like a cliff, and a synthesised baseline looks like
history. This buffer only ever holds samples the running dashboard itself took
on a fixed interval, so a chart can honestly say "since the dashboard
started". Anything it has not seen renders as N/A rather than as a line
drawn from nothing.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

__all__ = ["COLLECTOR_COUNTER_KEYS", "SHADOW_VALUE_KEYS", "TrendBuffer"]

#: Counters we sample from the collector heartbeat (cumulative totals).
COLLECTOR_COUNTER_KEYS = (
    "rest_requests_ok",
    "rest_requests_failed",
    "ws_messages_received",
    "ws_reconnects",
    "errors_total",
    "rate_limit_waits",
    "integrity_failures",
    "one_sided_books",
    "retries",
)

#: Shadow values we sample from the heartbeat / report (instantaneous).
SHADOW_VALUE_KEYS = (
    "cycle_duration_ms",
    "cycle_decisions",
    "collector_silence_seconds",
    "report_age_seconds",
)

#: Collector telemetry samples (heartbeat.telemetry, Collector >= v0.1.2).
#: An empty dict in the heartbeat means "not measured yet", so these keys
#: simply never appear in a sample until the producer publishes them.
TELEMETRY_KEYS = (
    "ws_queue_depth",
    "ws_queue_high_water",
    "ws_frames_dropped",
    "rest_latency_ms",
)


class TrendBuffer:
    def __init__(self, *, max_samples: int = 360) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def append(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self._samples.append(sample)

    def series(self, key: str) -> list[dict[str, Any]]:
        """``[{t, v}]`` for one key, oldest first. Missing samples are omitted,
        never interpolated."""
        with self._lock:
            return [
                {"t": sample["t"], "v": sample[key]}
                for sample in self._samples
                if key in sample
            ]

    def series_keys(self) -> tuple[str, ...]:
        """Every key this buffer knows how to chart."""
        return COLLECTOR_COUNTER_KEYS + SHADOW_VALUE_KEYS + TELEMETRY_KEYS

    def window_seconds(self) -> float | None:
        with self._lock:
            if len(self._samples) < 2:
                return None
            first = self._samples[0].get("t")
            last = self._samples[-1].get("t")
        if not isinstance(first, datetime) or not isinstance(last, datetime):
            return None
        return round((last - first).total_seconds(), 1)

    @staticmethod
    def collect(
        *,
        now: datetime,
        collector_payload: dict[str, Any] | None,
        shadow_payload: dict[str, Any] | None,
        report_as_of: datetime | None,
        report_stale_after: float | None,
    ) -> dict[str, Any]:
        """Build one sample. Only genuinely present values are included."""
        sample: dict[str, Any] = {"t": now.isoformat()}
        stats = (collector_payload or {}).get("stats") or {}
        if isinstance(stats, dict):
            for key in COLLECTOR_COUNTER_KEYS:
                value = stats.get(key)
                if isinstance(value, (int, float)):
                    sample[key] = value
        telem = (collector_payload or {}).get("telemetry")
        if isinstance(telem, dict):
            queue = telem.get("ws_queue")
            if isinstance(queue, dict):
                for source_key, sample_key in (
                    ("depth", "ws_queue_depth"),
                    ("high_water", "ws_queue_high_water"),
                    ("frames_dropped", "ws_frames_dropped"),
                ):
                    value = queue.get(source_key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        sample[sample_key] = value
            latency = telem.get("rest_latency_last_sweep_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                sample["rest_latency_ms"] = float(latency)
        shadow = shadow_payload or {}
        cycle = shadow.get("last_cycle") if isinstance(shadow.get("last_cycle"), dict) else None
        if cycle and isinstance(cycle.get("duration_ms"), (int, float)):
            sample["cycle_duration_ms"] = cycle["duration_ms"]
        if cycle and isinstance(cycle.get("decisions"), (int, float)):
            sample["cycle_decisions"] = cycle["decisions"]
        silence = shadow.get("collector_last_seen_utc")
        if isinstance(silence, str) and silence:
            try:
                seen = datetime.fromisoformat(silence.replace("Z", "+00:00"))
                sample["collector_silence_seconds"] = round((now - seen).total_seconds(), 1)
            except ValueError:
                pass
        if report_as_of is not None and report_stale_after is not None:
            sample["report_age_seconds"] = round((now - report_as_of).total_seconds(), 1)
        return sample

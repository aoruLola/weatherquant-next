"""WQN-41 helpers: raw-archive fixtures with real L2 frames.

The lines mirror what the collector appends (``raw/<source>/<date>.jsonl``):
an envelope with source/received_at/quality plus the verbatim venue payload.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

T_DECISION = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)

TOKEN = "tok-yes-123"


def book_payload(
    *,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    token_id: str = TOKEN,
    timestamp: str | None = "2026-10-26T11:59:40Z",
) -> dict:
    payload = {
        "asset_id": token_id,
        "event_type": "book",
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return payload


def ws_line(payload, *, received: datetime, seq: int = 1) -> str:
    envelope = {
        "archive_seq": seq,
        "source": "ws",
        "received_at_utc": received.isoformat(),
        "venue_ts_utc": payload.get("timestamp"),
        "quality": "ok",
        "run_id": "run-1",
        "payload": payload,
    }
    return json.dumps(envelope)


def write_archive(
    raw_dir: Path,
    *,
    source: str,
    day: str,
    lines: list[str],
) -> Path:
    directory = raw_dir / source
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line)
            handle.write("\n")
    return path


def two_level_book() -> dict:
    """A book whose 100-share buy sweeps two ask levels."""
    return book_payload(
        bids=[(0.52, 500.0)],
        asks=[(0.55, 60.0), (0.57, 200.0)],
    )

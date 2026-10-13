"""Offline backfill: decisions and settlements in, six snapshots out.

Two rules govern every entry point here.

**The research process never opens the engine's database.** Shadow holds an
exclusive lease on its DuckDB for its whole life, so a second connection is
refused even read-only. Decisions arrive as an export file the engine
published; settlements arrive as a list of conditions to ask the platform
about. Both are inputs, never something this module goes and grabs.

**A run is labelled or it does not ship.** ``dataset`` is a required
argument, not a default. A backfill run against fixtures that silently
publishes as ``live`` is how a demo becomes a claim, and the API and MCP
gateway both surface the label to whoever reads the number.

Publication is atomic per file and all-or-nothing per run: every snapshot
is built in memory first, and only then written. A crash halfway leaves the
previous complete set in place rather than six files where three are new.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.performance.settlement import (
    SettlementStatus,
    fetch_resolution,
)
from weatherquant.performance.snapshot import (
    SNAPSHOT_FILES,
    build_performance,
    publish_performance,
)

__all__ = [
    "DATASETS",
    "backfill_settlements",
    "build_performance_snapshots",
    "load_decisions",
    "load_settlements",
]

#: The only two labels a snapshot may carry. There is no default.
DATASETS = ("live", "fixture")


def _require_dataset(dataset: str) -> str:
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {DATASETS}, got {dataset!r}")
    return dataset


def load_decisions(path: str | Path) -> list[dict[str, Any]]:
    """Read a decisions export.

    JSON and JSONL are read directly. Parquet is read only when pyarrow is
    installed - refusing rather than guessing at the format, because a
    silently misparsed export produces plausible-looking decisions.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"decisions export not found: {source}")

    suffix = source.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        with source.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    if suffix == ".json":
        with source.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            for key in ("decisions", "rows", "data"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            raise ValueError(
                "a JSON decisions export must be a list, or an object with a "
                "'decisions', 'rows' or 'data' list"
            )
        if isinstance(payload, list):
            return payload
        raise ValueError("unsupported JSON decisions export shape")

    if suffix in {".parquet", ".pq"}:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "reading a parquet export needs pyarrow; export to JSON or "
                "JSONL instead"
            ) from exc
        return pq.read_table(source).to_pylist()

    raise ValueError(f"unsupported decisions export format: {suffix!r}")


def load_settlements(path: str | Path) -> list[dict[str, Any]]:
    """Read a settlement index written by ``backfill_settlements``."""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"settlement index not found: {source}")
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("a settlement index must be a JSON list")
    return payload


def backfill_settlements(
    markets: Sequence[Mapping[str, Any]],
    *,
    get_json,
    dataset: str,
    verified: Iterable[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Ask the platform about each condition, and record what it said.

    ``get_json`` is injected so this stays testable and so the caller's
    rate limits and httpx User-Agent apply - the platform refuses Python's
    default agent, so the client must be httpx.

    ``verified`` is the set of ``(market_id, target_date)`` pairs that
    passed an observation cross-check. Everything else stays UNVERIFIABLE
    even when the platform has resolved it: a resolution the system has not
    confirmed is not one it may score.
    """
    _require_dataset(dataset)
    now = now or datetime.now(tz=UTC)
    verified_keys = {
        (str(v.get("market_id")), str(v.get("target_date")) if v.get("target_date") else None)
        for v in verified
    }

    rows: list[dict[str, Any]] = []
    for market in markets:
        market_id = str(market.get("market_id") or "")
        condition = str(market.get("condition_id") or "")
        target = market.get("target_date")
        target_date = str(target) if target else None
        if not market_id or not condition:
            continue

        status, record = fetch_resolution(get_json, condition_id=condition)
        resolved = record.price if status == SettlementStatus.RESOLVED else None
        key = (market_id, target_date)
        verification = "VERIFIED" if key in verified_keys else (
            "UNVERIFIED" if status == SettlementStatus.RESOLVED else "PENDING"
        )
        rows.append(
            {
                "market_id": market_id,
                "event_id": market.get("event_id"),
                "token_id": market.get("token_yes") or market.get("token_id"),
                "target_date": target_date,
                "station": market.get("station"),
                "settlement_status": status,
                "resolved_outcome": resolved,
                "resolution_source": "clob:/v2/resolutions",
                "resolved_at_utc": record.last_update_timestamp if record else None,
                "fetched_at_utc": now.isoformat(),
                "raw_source_reference": {
                    "condition_id": condition,
                    "transaction_hash": record.transaction_hash if record else None,
                    "log_index": record.log_index if record else None,
                },
                "verification_status": verification,
                "dataset": dataset,
            }
        )
    return rows


def build_performance_snapshots(
    *,
    decisions: Sequence[Mapping[str, Any]],
    settlements: Sequence[Any] = (),
    out_dir: str | Path,
    dataset: str,
    capital_usdc: float = 100.0,
    max_allocation_fraction: float = 0.10,
    gap_window_seconds: float = 900.0,
    book_for=None,
    now: datetime | None = None,
) -> list[Path]:
    """Build and publish all six snapshots, or none of them.

    Everything is computed before anything is written: a failure partway
    leaves the previous complete set on disk rather than a mixture of old
    and new that no reader can reconcile.
    """
    _require_dataset(dataset)
    if capital_usdc <= 0:
        raise ValueError(f"capital_usdc must be > 0, got {capital_usdc!r}")

    payloads = build_performance(
        decisions=list(decisions),
        settlements=list(settlements),
        now=now,
        dataset=dataset,
        capital_usdc=capital_usdc,
        max_allocation_fraction=max_allocation_fraction,
        gap_window_seconds=gap_window_seconds,
        book_for=book_for,
    )
    # Every file built first; only then does anything touch the directory.
    missing = [name for name in SNAPSHOT_FILES if name not in payloads]
    if missing:  # pragma: no cover - a builder bug, not a user error
        raise RuntimeError(f"builder did not produce {missing}")

    return publish_performance(payloads, out_dir)

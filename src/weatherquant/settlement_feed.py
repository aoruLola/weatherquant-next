"""Publish the paper engine's settlement feed from the platform's own answers.

Without a publisher the settlement channel (:mod:`weatherquant.paper.settlements`)
is empty in every deployment: positions never settle, occupied capital is
never released, and once the per-opportunity and per-market caps have used up
the 100 USDC account every later ACCEPT is refused for lack of funds - the
engine silently stops trading after its first day. This module closes that
loop, read-only.

It runs as its own process (``weatherquant-settlement-feed``) so the paper
engine keeps its structural no-network boundary: paper only ever *reads* the
feed file. The markets checked are the ones the collector archived (the only
markets Shadow can decide on, so the only ones Paper can hold) whose end date
has passed. Once the venue reports a market closed, two independent platform
reads are compared:

* ``data-api.polymarket.com/v2/resolutions`` - the on-chain (UMA) resolution,
  parsed by :mod:`weatherquant.performance.settlement`;
* ``gamma-api.polymarket.com/markets/<id>`` - the market's own final
  ``outcomePrices`` and ``umaResolutionStatus``.

Only when both say the same 0 or 1 for YES is the record published as
``RESOLVED`` with ``verification_status=VERIFIED`` - the one combination the
ledger pays out on. A disagreement is published as ``RESOLVED`` +
``UNCONFIRMED`` (stored for audit, moves nothing); a dispute is ``DISPUTED``
while it is open. ``was_disputed`` stays true on the resolution forever, so a
market whose dispute has since been resolved - data-api ``resolved`` with a
0/1 price *and* Gamma final with the same price - is published ``VERIFIED``
(with ``was_disputed`` kept in the record); otherwise its capital would stay
occupied for good.
A transport failure publishes nothing: an outage is not a settlement fact.

A new revision is appended only when ``(status, yes_payout,
verification_status)`` changes, so a stable answer is one line, and a later
change is a higher revision the ledger treats as a correction. This module
never opens any database: it reads the collector's raw gamma archive and
appends one JSONL file. It has no order, wallet or signing path; both calls
are GETs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.performance.settlement import (
    DATA_API_BASE,
    SettlementStatus,
    classify,
    parse_resolutions,
)

__all__ = [
    "GAMMA_API_BASE",
    "SettlementPublisher",
    "SyncResult",
    "archived_markets",
    "due_markets",
    "gamma_yes_payout",
    "main",
    "run_once",
]

log = logging.getLogger("weatherquant.settlement_feed")

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
SOURCE = "data-api.polymarket.com/v2/resolutions + gamma-api.polymarket.com/markets"

#: ``get_json(url, params=..., timeout=...)`` - raises on transport failure.
GetJson = Callable[..., Any]


def _as_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def gamma_yes_payout(market: dict[str, Any]) -> float | None:
    """YES's final payout per Gamma, or None while it is not final.

    Final means: the market is closed, UMA resolution is ``resolved``, the
    first outcome is ``Yes`` and the outcome prices are exactly 0/1.
    """
    if not market.get("closed"):
        return None
    if str(market.get("umaResolutionStatus") or "").lower() != "resolved":
        return None
    outcomes = _as_list(market.get("outcomes"))
    prices = _as_list(market.get("outcomePrices"))
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    if str(outcomes[0]).strip().lower() != "yes":
        return None
    try:
        values = [float(p) for p in prices]
    except (TypeError, ValueError):
        return None
    if sorted(values) != [0.0, 1.0]:
        return None
    return values[0]


@dataclass
class SyncResult:
    checked: int = 0
    published: int = 0
    open_markets: int = 0
    final: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "published": self.published,
            "open_markets": self.open_markets,
            "final": self.final,
            "errors": list(self.errors),
        }


class SettlementPublisher:
    """Append settlement records for held markets to a JSONL feed."""

    def __init__(
        self,
        feed_path: str | Path,
        get_json: GetJson,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.feed_path = Path(feed_path)
        self.get_json = get_json
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self.timeout_seconds = float(timeout_seconds)

    # -- feed state ---------------------------------------------------------

    def repair_torn_tail(self) -> bool:
        """Drop an unterminated last line left by a crash mid-append.

        This process is the feed's only writer and every record is written as
        one ``...\n`` line, so bytes after the last newline can only be an
        interrupted append. Left in place, the paper reader refuses the whole
        file (``MALFORMED_LINE``) and no settlement would ever apply again; the
        next append would also be glued onto it. The dropped record is simply
        re-published by the next pass.
        """
        if not self.feed_path.is_file():
            return False
        with self.feed_path.open("r+b") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size == 0:
                return False
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return False
            keep = 0
            position = size
            while position > 0:
                step = min(65536, position)
                position -= step
                handle.seek(position)
                chunk = handle.read(step)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    keep = position + index + 1
                    break
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())
        log.warning("settlement feed: dropped %d bytes of an interrupted append", size - keep)
        return True

    def pending_markets(self) -> list[str]:
        """Markets already published whose latest record is not final yet."""
        return sorted(m for m, row in self.latest_records().items() if not self._is_final(row))

    def latest_records(self) -> dict[str, dict[str, Any]]:
        """The highest-revision record already published, per market."""
        latest: dict[str, dict[str, Any]] = {}
        if not self.feed_path.is_file():
            return latest
        with self.feed_path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                    market_id = str(row["market_id"])
                    revision = int(row["revision"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if market_id not in latest or revision > int(latest[market_id]["revision"]):
                    latest[market_id] = row
        return latest

    @staticmethod
    def _is_final(row: dict[str, Any] | None) -> bool:
        return bool(
            row and row.get("status") == SettlementStatus.RESOLVED and row.get("verification_status") == "VERIFIED"
        )

    # -- one pass -----------------------------------------------------------

    def sync(self, market_ids: Iterable[str]) -> SyncResult:
        result = SyncResult()
        self.repair_torn_tail()
        latest = self.latest_records()
        for market_id in sorted({str(m) for m in market_ids if m}):
            if self._is_final(latest.get(market_id)):
                result.final += 1
                continue
            result.checked += 1
            try:
                record = self._observe(market_id)
            except Exception as exc:  # transport or payload: publish nothing
                result.errors.append(f"{market_id}: {type(exc).__name__}: {exc}")
                continue
            if record is None:
                result.open_markets += 1
                continue
            previous = latest.get(market_id)
            key = (record["status"], record.get("yes_payout"), record.get("verification_status"))
            if previous is not None and key == (
                previous.get("status"),
                previous.get("yes_payout"),
                previous.get("verification_status"),
            ):
                continue
            record["revision"] = int(previous["revision"]) + 1 if previous else 1
            self._append(record)
            latest[market_id] = record
            result.published += 1
        return result

    def _observe(self, market_id: str) -> dict[str, Any] | None:
        """One market's current settlement record, or None while it is open."""
        market = self.get_json(f"{GAMMA_API_BASE}/markets/{market_id}", timeout=self.timeout_seconds)
        if not isinstance(market, dict):
            raise ValueError("gamma market payload is not an object")
        if not market.get("closed"):
            return None
        condition_id = str(market.get("conditionId") or "")
        if not condition_id:
            raise ValueError("gamma market has no conditionId")
        payload = self.get_json(
            f"{DATA_API_BASE}/v2/resolutions",
            params={"condition": condition_id},
            timeout=self.timeout_seconds,
        )
        resolution = next(
            (r for r in parse_resolutions(payload) if str(r.condition_id or "").lower() == condition_id.lower()),
            None,
        )
        status = classify(resolution)
        gamma = gamma_yes_payout(market)
        if (
            status == SettlementStatus.DISPUTED
            and resolution is not None
            and resolution.status == "resolved"
            and resolution.price is not None
            and gamma == resolution.price
        ):
            # The dispute is over: both platform reads give the same final answer.
            status = SettlementStatus.RESOLVED
        now = self.clock()
        record: dict[str, Any] = {
            "market_id": market_id,
            "status": status,
            "condition_id": condition_id,
            "source": SOURCE,
            "retrieved_at_utc": now.isoformat(),
        }
        if resolution is not None:
            record["was_disputed"] = resolution.was_disputed
            record["question_id"] = resolution.question_id
            record["transaction_hash"] = resolution.transaction_hash
            ts = resolution.last_update_timestamp
            if ts and str(ts).isdigit():
                record["settled_at_utc"] = datetime.fromtimestamp(int(ts), tz=UTC).isoformat()
        if status == SettlementStatus.RESOLVED:
            assert resolution is not None and resolution.price is not None
            record["yes_payout"] = resolution.price
            record["verification_status"] = "VERIFIED" if gamma == resolution.price else "UNCONFIRMED"
            record["gamma_yes_payout"] = gamma
        return {k: v for k, v in record.items() if v is not None or k == "gamma_yes_payout"}

    def _append(self, record: dict[str, Any]) -> None:
        self.feed_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self.feed_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        log.info(
            "settlement feed: market %s revision %s %s payout=%s %s",
            record["market_id"],
            record["revision"],
            record["status"],
            record.get("yes_payout"),
            record.get("verification_status", ""),
        )


def archived_markets(raw_dir: str | Path, *, days: int = 7) -> dict[str, datetime | None]:
    """Market id -> end date for every market in the last ``days`` gamma files.

    Reads the collector's ``raw/gamma/YYYY-MM-DD.jsonl`` read-only. A line
    that does not parse is skipped: this is a work list, not evidence.
    """
    directory = Path(raw_dir) / "gamma"
    if not directory.is_dir():
        return {}
    files = sorted(directory.glob("*.jsonl"))[-max(int(days), 1) :]
    out: dict[str, datetime | None] = {}
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    payload = json.loads(line).get("payload")
                except (json.JSONDecodeError, AttributeError):
                    continue
                events = payload if isinstance(payload, list) else [payload]
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    for market in event.get("markets") or []:
                        if not isinstance(market, dict) or not market.get("id"):
                            continue
                        out[str(market["id"])] = _parse_ts(market.get("endDate") or event.get("endDate"))
    return out


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def due_markets(markets: dict[str, datetime | None], now: datetime) -> list[str]:
    """Markets whose end date has passed (or is unknown) - the ones worth asking about."""
    return sorted(m for m, end in markets.items() if end is None or end <= now)


def run_once(publisher: SettlementPublisher, raw_dir: str | Path, *, days: int = 7) -> SyncResult:
    # Markets already in the feed but not final stay on the work list after they
    # age out of the archive window (a dispute can outlast it).
    due = due_markets(archived_markets(raw_dir, days=days), publisher.clock())
    return publisher.sync([*due, *publisher.pending_markets()])


def main(argv: list[str] | None = None) -> int:
    import argparse

    from weatherquant.data.http import ResilientHttp

    parser = argparse.ArgumentParser(
        prog="weatherquant-settlement-feed",
        description="Publish the paper settlement feed from Polymarket's resolutions (read-only GETs).",
    )
    parser.add_argument(
        "--raw",
        default=os.environ.get("WQ_SETTLEMENT_RAW_DIR", "data/raw"),
        help="collector raw archive, read-only (env WQ_SETTLEMENT_RAW_DIR)",
    )
    parser.add_argument(
        "--feed",
        default=os.environ.get("WQ_SETTLEMENT_FEED_PATH", "data/settlements/feed.jsonl"),
        help="feed to append to (env WQ_SETTLEMENT_FEED_PATH); paper serve's default reads "
        "settlements/feed.jsonl beside its paper/ directory",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.environ.get("WQ_SETTLEMENT_POLL_SECONDS", "300")),
    )
    parser.add_argument("--days", type=int, default=7, help="gamma archive days to scan")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    http = ResilientHttp(user_agent="weatherquant-next/settlement-feed")

    def get_json(url: str, *, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        return http.get_json(url, params=params, timeout_seconds=timeout)

    publisher = SettlementPublisher(args.feed, get_json)
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("received signal %s; stopping settlement feed", signum)
        stop.set()

    import signal

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        started = time.monotonic()
        try:
            result = run_once(publisher, args.raw, days=args.days)
        except Exception as exc:  # one bad pass must not end the publisher
            log.warning("settlement feed pass failed: %s: %s", type(exc).__name__, exc)
        else:
            for error in result.errors:
                log.warning("settlement feed: %s", error)
            log.info("settlement feed pass: %s in %.1fs", result.as_dict(), time.monotonic() - started)
        if args.once or stop.wait(args.poll_seconds):
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

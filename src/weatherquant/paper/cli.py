"""The paper engine's command line: init, consume and status.

Deliberately small. ``init`` opens the account; ``consume`` reads one
bounded batch of published decision events; ``status`` reads the counters
back. The daemon loop and its heartbeat belong to the deployment ticket and
will grow this file rather than sprouting a second one.

The commands that *write* enforce the enablement gate: a paper engine that
initialises an account or consumes signals because someone typed a command
is an enablement decision nobody made. ``status`` stays available for a
disabled engine, because "what is on disk here?" must be answerable without
first approving anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import uuid
from datetime import UTC, datetime

from weatherquant.data.lock import LockError, SingleInstanceLock
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.config import PaperSettings
from weatherquant.paper.consumer import SignalConsumer
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.store import ACCOUNT_ID, PaperStore
from weatherquant.storage.paper import PaperDatabase

log = logging.getLogger("weatherquant.paper")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = PaperSettings()
        if args.command == "init":
            return _cmd_init(settings)
        if args.command == "status":
            return _cmd_status(settings)
        if args.command == "consume":
            return _cmd_consume(settings, batch_lines=args.batch_lines)
        if args.command == "fill":
            return _cmd_fill(settings)
    except ValueError as exc:
        # The enablement flag's parse error lands here: say it plainly and
        # exit non-zero rather than pretending the flag was absent.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"unknown command {args.command!r}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="weatherquant-paper", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="open the virtual account (requires WQ_PAPER_ENABLED=true)")
    sub.add_parser("status", help="print the account and the engine's counters")
    consume = sub.add_parser(
        "consume",
        help="read one bounded batch of published decision events (requires enablement)",
    )
    consume.add_argument(
        "--batch-lines",
        type=int,
        default=5000,
        help="max published lines to read in this pass (default 5000)",
    )
    sub.add_parser(
        "fill",
        help="price accepted signals against their point-in-time books (requires enablement)",
    )
    return parser


def _require_enabled(settings: PaperSettings) -> None:
    if not settings.enabled:
        raise ValueError(
            "the paper engine is disabled (WQ_PAPER_ENABLED is not true); "
            "production enablement is a separate approval - set WQ_PAPER_ENABLED=true "
            "explicitly to record it. There is no default that starts this engine."
        )


def _open_under_lock(settings: PaperSettings, purpose: str):
    """Acquire the single-instance lease and open the database.

    Returns ``(db, lock)``; the caller closes both, lock last.
    """
    lock_path = settings.db_path.parent / ".paper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock(
        lock_path,
        run_id=uuid.uuid4().hex,
        hostname=socket.gethostname(),
        stale_after_seconds=max(60.0, settings.tick_seconds * 4),
    )
    lock.acquire()
    try:
        db = PaperDatabase(settings.db_path)
        db.open()
    except Exception:
        lock.release()
        raise
    return db, lock


def _cmd_init(settings: PaperSettings) -> int:
    _require_enabled(settings)
    try:
        db, lock = _open_under_lock(settings, "paper-init")
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        store = PaperStore(db)
        created = store.ensure_account(
            initial_capital_usdc=settings.initial_capital_usdc,
            now=datetime.now(tz=UTC),
        )
        # The ledger opens with the account: the OPEN entry and the balances
        # row are the first two facts every later figure is derived from.
        ledger = Ledger(store)
        ledger.open_account(
            initial_capital_usdc=settings.initial_capital_usdc,
            now=datetime.now(tz=UTC),
        )
        run_id = db.start_run(
            "paper-init",
            {
                "initial_capital_usdc": settings.initial_capital_usdc,
                "events_dir": str(settings.events_dir),
            },
        )
        db.finish_run(run_id, status="stopped", stats={"account_created": created})
    finally:
        db.close()
        lock.release()
    state = "opened" if created else "already open"
    print(f"account {ACCOUNT_ID}: {state} with {settings.initial_capital_usdc} USDC")
    return 0


def _cmd_consume(settings: PaperSettings, *, batch_lines: int) -> int:
    _require_enabled(settings)
    try:
        db, lock = _open_under_lock(settings, "paper-consume")
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        store = PaperStore(db)
        if store.account() is None:
            print(
                f"error: account {ACCOUNT_ID} does not exist; run "
                "`weatherquant-paper init` first - signals are accepted into an account",
                file=sys.stderr,
            )
            return 3
        consumer = SignalConsumer(
            store,
            events_dir=settings.events_dir,
            max_signal_age_seconds=settings.max_signal_age_seconds,
            batch_lines=batch_lines,
        )
        result = consumer.consume_once()
        run_id = db.start_run("paper-consume", {"batch_lines": batch_lines})
        db.finish_run(run_id, status="stopped", stats=result.as_dict())
    finally:
        db.close()
        lock.release()
    print(json.dumps(result.as_dict(), sort_keys=True, indent=2, default=str))
    return 0


def _cmd_fill(settings: PaperSettings) -> int:
    _require_enabled(settings)
    try:
        db, lock = _open_under_lock(settings, "paper-fill")
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        store = PaperStore(db)
        if store.account() is None:
            print(
                f"error: account {ACCOUNT_ID} does not exist; run "
                "`weatherquant-paper init` first",
                file=sys.stderr,
            )
            return 3
        engine = FillEngine(
            store,
            book_source=ArchiveBookSource(settings.raw_dir, tail_bytes=settings.archive_tail_bytes),
            buffer_fraction=settings.execution_buffer_fraction,
            require_complete_fill=settings.require_complete_fill,
            max_book_age_seconds=settings.max_book_age_seconds,
        )
        result = engine.fill_pending()
        run_id = db.start_run("paper-fill", {})
        db.finish_run(run_id, status="stopped", stats=result.as_dict())
    finally:
        db.close()
        lock.release()
    print(json.dumps(result.as_dict(), sort_keys=True, indent=2, default=str))
    return 0


def _cmd_status(settings: PaperSettings) -> int:
    db_path = settings.db_path
    payload: dict[str, object] = {
        "enabled": settings.enabled,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
    }
    if db_path.exists():
        with PaperDatabase(db_path) as db:
            store = PaperStore(db)
            ledger = Ledger(store)
            payload["account"] = store.account()
            payload["balances"] = ledger.balances()
            payload["open_positions"] = [p.as_dict() for p in ledger.positions()]
            payload["signals"] = store.signal_counts()
            payload["refusals"] = store.refusal_counts()
            payload["quality_events"] = store.quality_counts()
            if ledger.balances() is not None:
                payload["ledger_consistent"] = ledger.verify()["consistent"]
    print(json.dumps(payload, sort_keys=True, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

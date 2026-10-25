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
from weatherquant.paper.api import ENDPOINTS
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.config import PaperSettings
from weatherquant.paper.consumer import SignalConsumer
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.risk import RiskGate, load_risk_rules
from weatherquant.paper.settlements import SettlementParseError, read_settlement_feed
from weatherquant.paper.store import ACCOUNT_ID, PaperStore
from weatherquant.paper.web import make_server, serve_forever
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
        if args.command == "settle":
            return _cmd_settle(settings, feed=args.feed)
        if args.command == "serve":
            return _cmd_serve(settings)
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
    settle = sub.add_parser(
        "settle",
        help="apply a published settlement feed: payouts, releases, corrections",
    )
    settle.add_argument("feed", help="path to the published settlement feed (JSONL)")
    sub.add_parser(
        "serve",
        help="serve the read-only API (GET /api/v1/paper/*) from this process",
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
            book_source=ArchiveBookSource(
                settings.raw_dir,
                tail_bytes=settings.archive_tail_bytes,
                timeout_seconds=settings.book_resolve_timeout_seconds,
            ),
            buffer_fraction=settings.execution_buffer_fraction,
            require_complete_fill=settings.require_complete_fill,
            max_book_age_seconds=settings.max_book_age_seconds,
            risk=RiskGate(
                Ledger(store),
                load_risk_rules(settings.risk_rules_path),
                initial_capital_usdc=settings.initial_capital_usdc,
            ),
        )
        result = engine.fill_pending()
        run_id = db.start_run("paper-fill", {})
        db.finish_run(run_id, status="stopped", stats=result.as_dict())
    finally:
        db.close()
        lock.release()
    print(json.dumps(result.as_dict(), sort_keys=True, indent=2, default=str))
    return 0


def _cmd_settle(settings: PaperSettings, *, feed: str) -> int:
    _require_enabled(settings)
    try:
        events = read_settlement_feed(feed)
    except (SettlementParseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        db, lock = _open_under_lock(settings, "paper-settle")
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        store = PaperStore(db)
        ledger = Ledger(store)
        if ledger.balances() is None:
            print(
                "error: the ledger is not open; run `weatherquant-paper init` first",
                file=sys.stderr,
            )
            return 3
        outcomes = []
        with db.transaction():
            for event in events:
                outcomes.append(ledger.apply_settlement(event, now=datetime.now(tz=UTC)))
        payload = {"outcomes": outcomes, "review": ledger.review_required()}
        run_id = db.start_run("paper-settle", {"feed": feed, "records": len(events)})
        db.finish_run(run_id, status="stopped", stats=payload)
    finally:
        db.close()
        lock.release()
    print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    return 0


def _cmd_serve(settings: PaperSettings) -> int:
    """Serve the read-only API from the engine's own process.

    The connection this command holds is the one the handlers read through;
    DuckDB's single-writer lease means a second process cannot open the
    file anyway, so serving from here is not just convenient - it is the
    only way a live reader can exist at all.
    """
    _require_enabled(settings)
    try:
        db, lock = _open_under_lock(settings, "paper-serve")
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        server = make_server(db.con, host=settings.api_host, port=settings.api_port)
        run_id = db.start_run(
            "paper-serve",
            {"api_host": settings.api_host, "api_port": settings.api_port},
        )
        db.finish_run(run_id, status="running", stats={"note": "run row closed on shutdown"})
    except OSError as exc:
        print(f"error: cannot bind {settings.api_host}:{settings.api_port}: {exc}", file=sys.stderr)
        db.close()
        lock.release()
        return 5
    print(
        f"paper read-only API on http://{settings.api_host}:{settings.api_port}"
        f"/api/v1/paper/{{{','.join(ENDPOINTS)}}} (GET only)"
    )
    try:
        serve_forever(server)
    finally:
        db.close()
        lock.release()
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

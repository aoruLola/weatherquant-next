"""The paper engine's command line: init and status.

Deliberately small. ``init`` opens the account; ``status`` reads it back.
The consume loop arrives with WQN-40 and grows this file rather than
sprouting a second one.

Both commands enforce the enablement gate for anything that *writes*: a
paper engine that initialises an account because someone typed a command is
an enablement decision nobody made. Reading status of a disabled engine is
harmless and stays available, because "what is on disk here?" must be
answerable without first approving anything.
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
from weatherquant.paper.config import PaperSettings
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
    return parser


def _require_enabled(settings: PaperSettings) -> None:
    if not settings.enabled:
        raise ValueError(
            "the paper engine is disabled (WQ_PAPER_ENABLED is not true); "
            "production enablement is a separate approval - set WQ_PAPER_ENABLED=true "
            "explicitly to record it. There is no default that starts this engine."
        )


def _cmd_init(settings: PaperSettings) -> int:
    _require_enabled(settings)
    lock_path = settings.db_path.parent / ".paper.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock(
        lock_path,
        run_id=uuid.uuid4().hex,
        hostname=socket.gethostname(),
        stale_after_seconds=max(60.0, settings.tick_seconds * 4),
    )
    try:
        lock.acquire()
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    try:
        with PaperDatabase(settings.db_path) as db:
            store = PaperStore(db)
            created = store.ensure_account(
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
        lock.release()
    state = "opened" if created else "already open"
    print(f"account {ACCOUNT_ID}: {state} with {settings.initial_capital_usdc} USDC")
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
            account = store.account()
            payload["account"] = account
            payload["signals"] = store.signal_counts()
            payload["refusals"] = store.refusal_counts()
    print(json.dumps(payload, sort_keys=True, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

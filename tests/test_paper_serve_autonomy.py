"""Once enabled, ``serve`` runs the whole simulated loop with no manual step.

It opens the account itself (no separate ``init`` that a fresh container
otherwise restarts on with exit 3) and applies the published settlement feed
from its default location, so resolved positions release their capital and
the account keeps trading after its first day.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from paper_fills_support import make_fill
from weatherquant.paper.cli import main
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture()
def paper_env(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "paper" / "paper.duckdb"
    for key, value in {
        "WQ_PAPER_ENABLED": "true",
        "WQ_PAPER_DB_PATH": str(db_path),
        "WQ_PAPER_EVENTS_DIR": str(tmp_path / "decisions-export"),
        "WQ_PAPER_RAW_DIR": str(tmp_path / "raw"),
        "WQ_PAPER_HEALTH_DIR": str(tmp_path / "health"),
        "WQ_PAPER_RISK_RULES_PATH": str(ROOT / "configs" / "paper-risk.json"),
        "WQ_PAPER_FEE_SCHEDULE_PATH": str(ROOT / "configs" / "fee-schedule.json"),
        "WQ_PAPER_API_HOST": "127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WQ_PAPER_AUTO_INIT", raising=False)
    monkeypatch.delenv("WQ_PAPER_SETTLEMENT_FEED_PATH", raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setenv("WQ_PAPER_API_PORT", str(sock.getsockname()[1]))
    monkeypatch.chdir(tmp_path)

    def one_tick(server) -> None:
        try:
            server.service_actions()
        finally:
            server.server_close()

    monkeypatch.setattr("weatherquant.paper.cli.serve_forever", one_tick)
    return db_path


def test_serve_opens_the_account_without_a_separate_init(paper_env):
    assert not paper_env.exists()
    assert main(["serve"]) == 0
    with PaperDatabase(paper_env) as db:
        store = PaperStore(db)
        assert store.account()["initial_capital_usdc"] == 100.0
        assert Ledger(store).balances()["available_usdc"] == pytest.approx(100.0)


def test_auto_init_can_be_turned_off(paper_env, monkeypatch):
    monkeypatch.setenv("WQ_PAPER_AUTO_INIT", "false")
    assert main(["serve"]) == 3


def test_auto_init_flag_is_strict(paper_env, monkeypatch):
    monkeypatch.setenv("WQ_PAPER_AUTO_INIT", "ture")
    assert main(["serve"]) == 2


def test_serve_still_requires_enablement(paper_env, monkeypatch):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "false")
    assert main(["serve"]) == 2
    assert not paper_env.exists()


def test_serve_applies_the_default_settlement_feed(paper_env):
    assert main(["init"]) == 0
    with PaperDatabase(paper_env) as db:
        ledger = Ledger(PaperStore(db))
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        assert ledger.balances()["occupied_usdc"] > 0
    feed = paper_env.parent.parent / "settlements" / "feed.jsonl"
    feed.parent.mkdir(parents=True)
    feed.write_text(
        json.dumps(
            {
                "market_id": "mkt-1",
                "revision": 1,
                "status": "RESOLVED",
                "yes_payout": 1.0,
                "verification_status": "VERIFIED",
                "settled_at_utc": NOW.isoformat(),
                "source": "test",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["serve"]) == 0
    with PaperDatabase(paper_env) as db:
        ledger = Ledger(PaperStore(db))
        balances = ledger.balances()
        assert balances["occupied_usdc"] == pytest.approx(0.0)
        assert balances["realized_pnl_usdc"] == pytest.approx(100.0 - 55.8 - 0.279)
        assert ledger.positions() == []


def test_a_malformed_feed_does_not_stop_serve(paper_env):
    feed = paper_env.parent.parent / "settlements" / "feed.jsonl"
    feed.parent.mkdir(parents=True)
    feed.write_text("not json\n", encoding="utf-8")
    assert main(["serve"]) == 0

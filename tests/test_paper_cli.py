"""WQN-39/40: the paper CLI's enablement gate, account lifecycle, and consume."""

from __future__ import annotations

import json

import pytest

from weatherquant.paper.cli import main
from weatherquant.paper.store import ACCOUNT_ID
from weatherquant.storage.paper import PaperDatabase


@pytest.fixture()
def paper_env(tmp_path, monkeypatch):
    db_path = tmp_path / "paper" / "paper.duckdb"
    monkeypatch.setenv("WQ_PAPER_DB_PATH", str(db_path))
    monkeypatch.setenv("WQ_PAPER_EVENTS_DIR", str(tmp_path / "decisions-export"))
    monkeypatch.chdir(tmp_path)  # no stray project .env can answer the gate
    return db_path


def test_init_refuses_while_disabled(paper_env, capsys):
    # No WQ_PAPER_ENABLED anywhere: the engine must not come into existence
    # by omission.
    assert main(["init"]) == 2
    out = capsys.readouterr().err
    assert "WQ_PAPER_ENABLED" in out
    assert not paper_env.exists()


def test_init_refuses_a_garbled_flag(paper_env, monkeypatch, capsys):
    # 'ture' must be seen as the typo it is, not silently read as off.
    monkeypatch.setenv("WQ_PAPER_ENABLED", "ture")
    assert main(["init"]) == 2
    assert "WQ_PAPER_ENABLED" in capsys.readouterr().err


def test_init_opens_the_account_when_enabled(paper_env, monkeypatch, capsys):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    assert main(["init"]) == 0
    assert "opened" in capsys.readouterr().out
    with PaperDatabase(paper_env) as db:
        row = db.execute(
            "SELECT initial_capital_usdc, currency FROM paper_accounts WHERE account_id = ?",
            [ACCOUNT_ID],
        ).fetchone()
    assert row == (100.0, "USDC")


def test_reinit_with_same_capital_is_a_noop(paper_env, monkeypatch, capsys):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    main(["init"])
    assert main(["init"]) == 0
    assert "already open" in capsys.readouterr().out


def test_reinit_with_different_capital_fails(paper_env, monkeypatch):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    main(["init"])
    monkeypatch.setenv("WQ_PAPER_INITIAL_CAPITAL_USDC", "200")
    assert main(["init"]) != 0


def test_status_reports_an_empty_engine(paper_env, monkeypatch, capsys):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    main(["init"])
    capsys.readouterr()
    monkeypatch.setenv("WQ_PAPER_ENABLED", "false")
    assert main(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    # Status stays readable of a disabled engine: answering "what is on
    # disk here?" must not require approving anything.
    assert payload["enabled"] is False
    assert payload["account"]["initial_capital_usdc"] == 100.0
    assert payload["signals"] == {}


def test_consume_requires_enablement(paper_env, capsys):
    assert main(["consume"]) == 2
    assert "WQ_PAPER_ENABLED" in capsys.readouterr().err


def test_consume_requires_an_account(paper_env, monkeypatch, capsys):
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    assert main(["consume"]) == 3
    assert "init" in capsys.readouterr().err


def test_consume_reads_the_published_export(paper_env, monkeypatch, capsys):
    # End to end over the file channel: an export part lands, one consume
    # pass takes it in, a second pass reports caught up.
    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    monkeypatch.setenv("WQ_PAPER_MAX_SIGNAL_AGE_SECONDS", "600")
    main(["init"])
    capsys.readouterr()

    events_dir = paper_env.parent.parent / "decisions-export"  # as the fixture configured
    (events_dir / "parts").mkdir(parents=True)
    row = {
        "decision_id": "a" * 32,
        "run_id": "r" * 32,
        "cycle_id": "c" * 16,
        "market_id": "mkt-1",
        "direction": "buy_yes",
        "state": "ACCEPT",
        "mode": "LIVE",
        "decision_time_utc": "2026-10-26T12:04:00+00:00",
        "book_id": "b" * 64,
        "fee_rate": 0.05,
    }
    with (events_dir / "parts" / "part-000000001.jsonl").open("w") as handle:
        handle.write(json.dumps(row) + "\n")

    assert main(["consume"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["accepted"] == 1
    assert first["signals_recorded"] == 1

    assert main(["consume"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["caught_up"] is True

    monkeypatch.setenv("WQ_PAPER_ENABLED", "false")
    assert main(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["signals"] == {"ACCEPTED": 1}


def test_interrupted_init_rolls_back_and_can_be_retried(paper_env, monkeypatch):
    from weatherquant.paper.ledger import Ledger

    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")

    def interrupted(*args, **kwargs):
        raise RuntimeError("injected failure before OPEN")

    with monkeypatch.context() as fault:
        fault.setattr(Ledger, "_insert_entry", interrupted)
        with pytest.raises(RuntimeError, match="injected failure"):
            main(["init"])

    with PaperDatabase(paper_env) as db:
        for table in ("paper_accounts", "paper_balances", "paper_ledger"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    assert main(["init"]) == 0
    assert main(["init"]) == 0
    with PaperDatabase(paper_env) as db:
        assert db.execute("SELECT available_usdc FROM paper_balances").fetchall() == [(100.0,)]
        assert db.execute("SELECT kind, amount_usdc FROM paper_ledger").fetchall() == [("OPEN", 100.0)]


@pytest.mark.parametrize("with_fill", [False, True])
def test_reinit_restores_only_the_missing_legacy_open(paper_env, monkeypatch, with_fill):
    from paper_fills_support import make_fill
    from weatherquant.paper.ledger import Ledger
    from weatherquant.paper.store import PaperStore

    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    assert main(["init"]) == 0
    with PaperDatabase(paper_env) as db:
        store = PaperStore(db)
        ledger = Ledger(store)
        account = store.account()
        db.execute("DELETE FROM paper_ledger WHERE kind = 'OPEN'")
        if with_fill:
            with db.transaction():
                ledger.apply_buy(make_fill(decision_id="before-repair"), now=account["created_at_utc"])
        before = ledger.balances(), ledger.positions(), ledger.ledger_entries()

    assert main(["init"]) == 0
    assert main(["init"]) == 0
    with PaperDatabase(paper_env) as db:
        ledger = Ledger(PaperStore(db))
        opening, *movements = ledger.ledger_entries()
        assert opening["kind"] == "OPEN"
        assert opening["amount_usdc"] == opening["balance_after_usdc"] == account["initial_capital_usdc"]
        assert opening["ts_utc"] == account["created_at_utc"]
        assert (ledger.balances(), ledger.positions(), movements) == before
        assert ledger.verify()["consistent"]

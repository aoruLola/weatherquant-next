"""WQN-39: the paper CLI's enablement gate and account lifecycle."""

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

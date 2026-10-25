"""WQN-39: paper runtime configuration.

The enablement gate is the point of this module, so most of the tests are
about the ways it can and cannot be opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from weatherquant.paper.config import PaperSettings, parse_bool_flag


class TestEnablementFlag:
    def test_default_is_disabled(self):
        # The single most load-bearing default in the package: absent
        # approval is no approval.
        assert PaperSettings(_env_file=None).enabled is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "enabled", " enabled "])
    def test_true_flags(self, raw):
        assert parse_bool_flag(raw) is True

    @pytest.mark.parametrize("raw", ["false", "0", "disabled", "", None, False])
    def test_false_flags(self, raw):
        assert parse_bool_flag(raw) is False

    @pytest.mark.parametrize("raw", ["ture", "yes", "on", "y", "maybe", "2"])
    def test_unrecognised_value_raises_instead_of_defaulting_off(self, raw):
        # 'yes'/'on' are booleans to a human and unknown to this parser, and
        # an operator who typed one believes they enabled something. Refusing
        # loudly is the only honest answer.
        with pytest.raises(ValueError, match="WQ_PAPER_ENABLED"):
            parse_bool_flag(raw)

    def test_env_var_wiring(self, monkeypatch):
        monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
        monkeypatch.setenv("WQ_PAPER_DB_PATH", "/tmp/x/paper.duckdb")
        monkeypatch.setenv("WQ_PAPER_INITIAL_CAPITAL_USDC", "100")
        settings = PaperSettings(_env_file=None)
        assert settings.enabled is True
        assert settings.db_path == Path("/tmp/x/paper.duckdb")

    def test_env_var_prefix_does_not_duplicate_paper(self, monkeypatch):
        # The prefix + field-name composition is easy to get silently wrong:
        # WQ_PAPER_PAPER_ENABLED would be read while every operator writes
        # WQ_PAPER_ENABLED, and the engine would stay off with no error.
        monkeypatch.setenv("WQ_PAPER_ENABLED", "false")
        monkeypatch.delenv("WQ_PAPER_PAPER_ENABLED", raising=False)
        assert PaperSettings(_env_file=None).enabled is False


class TestAccountSettings:
    def test_initial_capital_defaults_to_100_usdc(self):
        assert PaperSettings(_env_file=None).initial_capital_usdc == 100.0

    def test_initial_capital_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("WQ_PAPER_INITIAL_CAPITAL_USDC", "0")
        with pytest.raises(ValidationError):
            PaperSettings(_env_file=None)

    def test_tick_seconds_must_be_positive(self, monkeypatch):
        monkeypatch.setenv("WQ_PAPER_TICK_SECONDS", "-1")
        with pytest.raises(ValidationError):
            PaperSettings(_env_file=None)

    def test_paths_default_inside_the_paper_directory(self):
        settings = PaperSettings(_env_file=None)
        assert str(settings.db_path).startswith("data/paper/")
        # The decision channel is the shadow's *published* export, never the
        # shadow's database file.
        assert "decisions-export" in str(settings.events_dir)
        assert "shadow.duckdb" not in str(settings.events_dir)

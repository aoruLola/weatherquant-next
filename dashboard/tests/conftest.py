"""Pytest fixtures: assembled data trees and a configured app.

The payload builders live in :mod:`helpers` so tests can import them without
importing conftest; this file only wires fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import build_tree

from weatherquant.dashboard.api import create_app
from weatherquant.dashboard.config import DashboardSettings


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return build_tree(tmp_path)


@pytest.fixture
def settings_for(tmp_path: Path):
    """Build settings pointing at a data tree. ``root`` defaults to tmp_path."""

    def _make(root: Path | None = None, **overrides) -> DashboardSettings:
        base = root or tmp_path
        options = {
            "collector_data_dir": base / "collector",
            "shadow_data_dir": base / "shadow",
            "rate_limit_per_minute": 0,
            "trend_sample_seconds": 3600.0,
            "archive_cache_seconds": 0.0,
        }
        options.update(overrides)
        return DashboardSettings(**options)

    return _make


@pytest.fixture
def client(data_root: Path, settings_for):
    app = create_app(settings_for(data_root))
    with TestClient(app) as test_client:
        yield test_client

"""The DuckDB connection must open with a bounded resource envelope.

DuckDB's own default memory_limit is 80% of physical RAM. On the 16 GB host
this runs on - no swap, sharing RAM with a Shadow, a KVM guest and the whole
Unraid userland - that default is ~12.2 GiB, and nothing else caps it: the
collector container itself runs with no Docker memory limit.

That combination is what lets one process push the host into direct reclaim,
where every other service stalls with it. These tests pin the budget so the
default cannot silently go back to "whatever the engine feels like".
"""

from __future__ import annotations

import pytest

from weatherquant.storage.db import (
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_THREADS,
    _memory_limit,
    _threads,
    connect,
)


def _settings(con, names: list[str]) -> dict[str, str]:
    rows = con.execute(
        "SELECT name, value FROM duckdb_settings() WHERE name IN (SELECT unnest(?))",
        [names],
    ).fetchall()
    return dict(rows)


def test_a_writable_connection_gets_an_explicit_budget(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    try:
        got = _settings(con, ["memory_limit", "threads"])
        # 3GB as DuckDB reports it (binary units).
        assert got["memory_limit"] == "2.7 GiB"
        assert got["threads"] == str(DEFAULT_THREADS)
    finally:
        con.close()


def test_the_default_is_not_ducks_unbounded_80_percent(tmp_path):
    """The regression this exists for: the engine default on this host is
    ~12.2 GiB, which is more than the host can spare."""
    con = connect(tmp_path / "t.duckdb")
    try:
        limit = _settings(con, ["memory_limit"])["memory_limit"]
        assert limit == "2.7 GiB"
        assert limit != "12.2 GiB"
    finally:
        con.close()


def test_the_budget_can_be_overridden(tmp_path):
    con = connect(tmp_path / "t.duckdb", memory_limit="512MB", threads=2)
    try:
        got = _settings(con, ["memory_limit", "threads"])
        assert got["memory_limit"] == "488.2 MiB"
        assert got["threads"] == "2"
    finally:
        con.close()


def test_a_read_only_connection_is_left_alone(tmp_path):
    """It cannot spill, so a limit there would turn a big query into a
    failure rather than a slower answer."""
    con = connect(tmp_path / "t.duckdb")
    con.close()
    read_only = connect(tmp_path / "t.duckdb", read_only=True)
    try:
        # The engine default, not our budget.
        assert _settings(read_only, ["memory_limit"])["memory_limit"] != "2.7 GiB"
    finally:
        read_only.close()


def test_the_default_limit_is_a_bounded_literal():
    assert DEFAULT_MEMORY_LIMIT == "3GB"


@pytest.mark.parametrize("bad", ["3 GB!", "abc", "", "3GB; DROP TABLE x", "3", "-1GB"])
def test_a_malformed_limit_is_refused(bad):
    with pytest.raises(ValueError):
        _memory_limit(bad)


@pytest.mark.parametrize("good", ["3GB", "512MB", "1.5GB", "900kb"])
def test_a_well_formed_limit_is_accepted(good):
    assert _memory_limit(good)


@pytest.mark.parametrize("bad", [0, -1, "x", None])
def test_a_bad_thread_count_is_refused(bad):
    with pytest.raises(ValueError):
        _threads(bad)

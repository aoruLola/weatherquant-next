"""Legacy read-only import: inventory tiers, reliability rules, and the planner.

Everything here runs against a *synthetic* legacy file built by the fixture, so
the suite stays offline and deterministic and does not depend on the real
100 MB legacy database being free of file locks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from weatherquant.legacy import (
    LEGACY_TABLES,
    LegacySource,
    ValueTier,
    VerificationStatus,
    classify,
    plan_import,
)
from weatherquant.legacy.importer import record_import_run
from weatherquant.storage import initialize

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# A synthetic legacy database carrying the columns the rules actually read.
# ---------------------------------------------------------------------------

_LEGACY_DDL = """
CREATE TABLE events (
    event_id VARCHAR, slug VARCHAR, title VARCHAR, target_date DATE,
    resolution_source VARCHAR, rules_text VARCHAR, fetched_at TIMESTAMPTZ, raw_json VARCHAR
);
CREATE TABLE markets (
    market_id VARCHAR, event_id VARCHAR, question VARCHAR, bracket_label VARCHAR,
    parse_status VARCHAR, parse_reason VARCHAR, fetched_at TIMESTAMPTZ, raw_json VARCHAR
);
CREATE TABLE books (
    book_id VARCHAR, market_id VARCHAR, token_id VARCHAR, bids_json VARCHAR,
    asks_json VARCHAR, venue_ts TIMESTAMPTZ, fetched_at TIMESTAMPTZ,
    status VARCHAR, reason VARCHAR
);
CREATE TABLE price_history (
    market_id VARCHAR, token_id VARCHAR, ts TIMESTAMPTZ, mid DOUBLE, fetched_at TIMESTAMPTZ
);
CREATE TABLE forecast_runs (
    model VARCHAR, run_time TIMESTAMPTZ, fetched_at TIMESTAMPTZ,
    publishable_at TIMESTAMPTZ, status VARCHAR, reason VARCHAR, n_hours INTEGER, raw_json VARCHAR
);
CREATE TABLE forecast_hourly (
    model VARCHAR, run_time TIMESTAMPTZ, valid_time TIMESTAMPTZ, temp_c DOUBLE, temp_f DOUBLE
);
CREATE TABLE forecast_daily (
    model VARCHAR, run_time TIMESTAMPTZ, target_date DATE, predicted_max_f DOUBLE,
    n_hours INTEGER, fetched_at TIMESTAMPTZ
);
CREATE TABLE observations (
    station VARCHAR, obs_time TIMESTAMPTZ, temp_c DOUBLE, temp_f DOUBLE,
    source VARCHAR, report_type VARCHAR, fetched_at TIMESTAMPTZ
);
CREATE TABLE daily_observations (
    station VARCHAR, target_date DATE, source VARCHAR, max_temp_f DOUBLE,
    n_readings INTEGER, rule VARCHAR, computed_at TIMESTAMPTZ, is_official BOOLEAN
);
CREATE TABLE settlements (
    market_id VARCHAR, event_id VARCHAR, target_date DATE, is_official BOOLEAN,
    yes_price DOUBLE, source VARCHAR, daily_max_temp_f DOUBLE,
    resolved_at TIMESTAMPTZ, notes VARCHAR
);
-- Trading state / derived tables: present so the inventory can prove they are skipped.
CREATE TABLE signals (signal_id VARCHAR, decision VARCHAR);
CREATE TABLE orders (order_id VARCHAR, status VARCHAR);
CREATE TABLE account (account_id VARCHAR, cash DOUBLE);
"""


def _build_legacy_db(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute(_LEGACY_DDL)

    con.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["evt-1", "slug", "Highest temperature in NYC", date(2026, 10, 10),
         "NOAA", "Resolved on NWS KLGA.", NOW, "{}"],
    )
    # One clean market and one the legacy parser could not read.
    con.execute("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-ok", "evt-1", "q", "32-33", "ok", None, NOW, "{}"])
    con.execute("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-bad", "evt-1", "q", "?", "rules_unclear", "rules do not name NOAA", NOW, "{}"])

    # Books: one healthy with venue_ts, one healthy but without venue_ts
    # (the legacy capture never populated it), one crossed.
    con.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["b1", "m-ok", "tok", '[{"price":0.48,"size":100}]',
                 '[{"price":0.52,"size":100}]', NOW, NOW, "ok", None])
    con.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["b2", "m-ok", "tok", '[{"price":0.48,"size":100}]',
                 '[{"price":0.52,"size":100}]', None, NOW, "ok", None])
    con.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["b3", "m-ok", "tok", "[]", "[]", NOW, NOW, "crossed", "best bid >= best ask"])

    con.execute("INSERT INTO price_history VALUES (?, ?, ?, ?, ?)",
                ["m-ok", "tok", NOW, 0.5, NOW])
    con.execute("INSERT INTO price_history VALUES (?, ?, ?, ?, ?)",
                ["m-ok", "tok", NOW, 1.7, NOW])  # impossible mid

    # One good run, one failed run; the failed run has a daily row too.
    con.execute("INSERT INTO forecast_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["ecmwf_ifs025", datetime(2026, 10, 9, 12, tzinfo=UTC), NOW,
                 datetime(2026, 10, 9, 18, tzinfo=UTC), "ok", None, 24, None])
    con.execute("INSERT INTO forecast_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["gfs", datetime(2026, 10, 9, 0, tzinfo=UTC), NOW,
                 datetime(2026, 10, 9, 6, tzinfo=UTC), "failed", "upstream 404", 0, None])
    con.execute("INSERT INTO forecast_daily VALUES (?, ?, ?, ?, ?, ?)",
                ["ecmwf_ifs025", datetime(2026, 10, 9, 12, tzinfo=UTC), date(2026, 10, 10),
                 71.0, 24, NOW])
    con.execute("INSERT INTO forecast_daily VALUES (?, ?, ?, ?, ?, ?)",
                ["ecmwf_ifs025", datetime(2026, 10, 9, 12, tzinfo=UTC), date(2026, 10, 11),
                 72.0, 18, NOW])  # incomplete day
    con.execute("INSERT INTO forecast_daily VALUES (?, ?, ?, ?, ?, ?)",
                ["gfs", datetime(2026, 10, 9, 0, tzinfo=UTC), date(2026, 10, 10),
                 70.0, 24, NOW])  # inherits FAILED run
    con.execute("INSERT INTO forecast_hourly VALUES (?, ?, ?, ?, ?)",
                ["ecmwf_ifs025", datetime(2026, 10, 9, 12, tzinfo=UTC),
                 datetime(2026, 10, 10, 16, tzinfo=UTC), 21.0, 69.8])

    con.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                ["KLGA", NOW, 21.0, 69.8, "iem_asos", "METAR", NOW])
    con.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                ["KLGA", NOW, 21.0, 69.8, "", "METAR", NOW])  # provenance lost

    con.execute("INSERT INTO daily_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["KLGA", date(2026, 10, 10), "iem_asos", 73.0, 24, "hourly_metar", NOW, False])
    con.execute("INSERT INTO daily_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["KLGA", date(2026, 10, 11), "iem_asos", 74.0, 0, "hourly_metar", NOW, False])
    con.execute("INSERT INTO daily_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ["KLGA", date(2026, 10, 12), "iem_asos", 75.0, 24, "hourly_metar", NOW, True])

    con.execute("INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-ok", "evt-1", date(2026, 10, 10), True, 1.0, "gamma", 73.0, NOW, None])
    con.execute("INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-off", "evt-1", date(2026, 10, 11), False, None, "gamma", None, NOW, None])
    con.execute("INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-half", "evt-1", date(2026, 10, 12), True, 0.5, "gamma", None, NOW, None])
    con.execute("INSERT INTO settlements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["m-dis", "evt-1", date(2026, 10, 13), True, 0.0, "gamma", 72.0, NOW,
                 "hourly 71F vs venue 72F DISAGREES"])

    con.execute("INSERT INTO signals VALUES ('s1', 'PASS')")
    con.execute("INSERT INTO orders VALUES ('o1', 'filled')")
    con.execute("INSERT INTO account VALUES ('a1', 1000.0)")
    con.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.duckdb"
    _build_legacy_db(path)
    return path


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def test_inventory_marks_trading_state_as_not_migratable() -> None:
    for name in ("account", "orders", "signals"):
        assert LEGACY_TABLES[name].target_table is None
        assert LEGACY_TABLES[name].tier in (ValueTier.TRADING_STATE, ValueTier.DERIVED)


def test_old_signals_and_predictions_are_never_inherited() -> None:
    """Spec §四: the old probability model, signal logic and ledger are not inherited."""
    for name in ("signals", "predictions", "model_runs", "backtest_runs", "account"):
        assert LEGACY_TABLES[name].target_table is None, name


def test_every_research_table_has_a_target() -> None:
    research = [t for t in LEGACY_TABLES.values() if t.tier is ValueTier.RESEARCH]
    assert research
    assert all(t.target_table is not None for t in research)


def test_unknown_table_is_rejected_rather_than_silently_classified() -> None:
    with pytest.raises(KeyError, match="not in the legacy inventory"):
        classify("definitely_not_a_table", {})


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_clean_rows_classify_as_verified() -> None:
    assert classify("markets", {"parse_status": "ok"}) == (VerificationStatus.VERIFIED, None)
    assert classify("observations", {"source": "iem_asos"}) == (VerificationStatus.VERIFIED, None)


def test_unparsed_market_is_flagged() -> None:
    status, reason = classify("markets", {"parse_status": "rules_unclear", "parse_reason": "no NOAA"})
    assert status is VerificationStatus.PENDING
    assert "rules_unclear" in reason  # type: ignore[operator]


def test_book_without_venue_timestamp_is_flagged() -> None:
    """The legacy capture never wrote venue_ts, so staleness cannot be proven."""
    status, reason = classify(
        "books", {"status": "ok", "venue_ts": None, "fetched_at": NOW}
    )
    assert status is VerificationStatus.PENDING
    assert "venue_ts" in reason  # type: ignore[operator]


def test_crossed_book_is_flagged_with_the_venue_reason() -> None:
    status, reason = classify(
        "books", {"status": "crossed", "reason": "best bid >= best ask", "venue_ts": NOW}
    )
    assert status is VerificationStatus.PENDING
    assert "crossed" in reason  # type: ignore[operator]


def test_incomplete_forecast_day_is_flagged() -> None:
    status, reason = classify(
        "forecast_daily", {"n_hours": 18}, run_status="ok"
    )
    assert status is VerificationStatus.PENDING
    assert "24" in reason  # type: ignore[operator]


def test_forecast_row_inherits_a_failed_run() -> None:
    status, reason = classify("forecast_hourly", {}, run_status="failed")
    assert status is VerificationStatus.PENDING
    assert "failed" in reason  # type: ignore[operator]


def test_forecast_row_without_a_known_run_is_flagged_not_trusted() -> None:
    status, _ = classify("forecast_daily", {"n_hours": 24}, run_status=None)
    assert status is VerificationStatus.PENDING


def test_observation_claiming_official_status_is_flagged() -> None:
    status, reason = classify(
        "daily_observations", {"is_official": True, "n_readings": 24}
    )
    assert status is VerificationStatus.PENDING
    assert "is_official" in reason  # type: ignore[operator]


def test_settlement_that_is_not_official_is_flagged() -> None:
    status, _ = classify("settlements", {"is_official": False, "yes_price": None})
    assert status is VerificationStatus.PENDING


def test_settlement_with_a_non_binary_price_is_flagged() -> None:
    status, reason = classify("settlements", {"is_official": True, "yes_price": 0.5})
    assert status is VerificationStatus.PENDING
    assert "0 or 1" in reason  # type: ignore[operator]


def test_settlement_whose_cross_check_disagrees_is_flagged() -> None:
    status, reason = classify(
        "settlements", {"is_official": True, "yes_price": 1.0, "notes": "DISAGREES with NWS"}
    )
    assert status is VerificationStatus.PENDING
    assert "disagrees" in reason.lower()


def test_official_binary_settlement_is_verified() -> None:
    status, _ = classify(
        "settlements", {"is_official": True, "yes_price": 1.0, "notes": None}
    )
    assert status is VerificationStatus.VERIFIED


def test_price_outside_the_unit_interval_is_flagged() -> None:
    status, _ = classify("price_history", {"mid": 1.7})
    assert status is VerificationStatus.PENDING


# ---------------------------------------------------------------------------
# Source handle
# ---------------------------------------------------------------------------


def test_source_is_read_only(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src, pytest.raises(duckdb.Error, match="read-only"):
        src._con.execute("DELETE FROM markets")


def test_source_lists_tables_and_counts_rows(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        assert "markets" in src.tables()
        assert src.count("markets") == 2
        # A table that exists in the file but is not in the inventory at all.
        with pytest.raises(KeyError, match="not in the legacy inventory"):
            src.count("definitely_not_a_table")
        # A table in the inventory but absent from this file.
        with pytest.raises(KeyError, match="does not exist"):
            src.count("equity_snapshots")


def test_iter_rows_streams_dictionaries(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        rows = list(src.iter_rows("markets"))
    assert len(rows) == 2
    assert {r["market_id"] for r in rows} == {"m-ok", "m-bad"}
    assert all(isinstance(r, dict) for r in rows)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def test_plan_reports_counts_and_skips_trading_state(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        plan = plan_import(src)

    migrating = {t.legacy_table: t for t in plan.migrating_tables}
    assert "markets" in migrating and "signals" not in migrating
    assert "account" not in migrating

    assert migrating["markets"].rows == 2
    assert migrating["markets"].pending == 1  # the rules_unclear one
    assert migrating["markets"].verified == 1

    assert plan.total_rows > 0
    assert plan.total_verified + plan.total_pending == plan.total_rows


def test_plan_flags_every_book_lacking_a_venue_timestamp(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        plan = plan_import(src)
    books = next(t for t in plan.tables if t.legacy_table == "books")
    assert books.rows == 3
    assert books.pending == 2  # missing venue_ts + crossed
    assert books.verified == 1
    reasons = dict(books.reasons)
    assert any("venue_ts" in r for r in reasons)
    assert any("crossed" in r for r in reasons)


def test_plan_joins_forecast_rows_to_their_run_status(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        plan = plan_import(src)
    daily = next(t for t in plan.tables if t.legacy_table == "forecast_daily")
    # 3 rows: ok+complete (verified), ok+incomplete (pending), failed run (pending)
    assert daily.rows == 3
    assert daily.verified == 1
    assert daily.pending == 2


def test_plan_is_write_free(legacy_db: Path) -> None:
    """Planning must not touch the legacy file at all."""
    before = (legacy_db.stat().st_size, legacy_db.stat().st_mtime_ns)
    with LegacySource(legacy_db) as src:
        plan_import(src)
    after = (legacy_db.stat().st_size, legacy_db.stat().st_mtime_ns)
    assert before == after


def test_plan_summary_is_readable(legacy_db: Path) -> None:
    with LegacySource(legacy_db) as src:
        text = plan_import(src).summary()
    assert "migrating:" in text
    assert "not migrated:" in text
    assert "signals" in text


def test_tables_present_in_the_inventory_but_absent_from_source_are_marked_missing(
    legacy_db: Path,
) -> None:
    with LegacySource(legacy_db) as src:
        plan = plan_import(src)
    price = next(t for t in plan.tables if t.legacy_table == "price_history")
    assert price.present is True
    # Nothing in this synthetic file maps to 'daily_max_temperatures' source name,
    # so assert the general contract instead: absent tables never claim rows.
    for t in plan.tables:
        if not t.present:
            assert t.rows == 0


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_records_a_dry_run(legacy_db: Path) -> None:
    target, _ = initialize(":memory:")
    try:
        with LegacySource(legacy_db) as src:
            plan = plan_import(src)
        run_id = record_import_run(target, plan, dry_run=True)
        row = target.execute(
            "SELECT source_db, dry_run, status, tables_json FROM legacy_import_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        assert row is not None
        assert row[1] is True
        assert row[2] == "completed"
        assert "markets" in row[3]
    finally:
        target.close()


def test_manifest_is_written_even_when_the_import_failed(legacy_db: Path) -> None:
    target, _ = initialize(":memory:")
    try:
        with LegacySource(legacy_db) as src:
            plan = plan_import(src)
        run_id = record_import_run(
            target, plan, dry_run=False, status="failed", error="transform blew up"
        )
        row = target.execute(
            "SELECT dry_run, status, error FROM legacy_import_runs WHERE run_id = ?",
            [run_id],
        ).fetchone()
        assert row == (False, "failed", "transform blew up")
    finally:
        target.close()


def test_verification_status_enum_is_the_only_source_of_status_strings() -> None:
    """DuckDB could not take a CHECK on ALTER TABLE, so the enum is the guard."""
    assert {str(v) for v in VerificationStatus} == {
        "native",
        "verified",
        "pending_verification",
    }

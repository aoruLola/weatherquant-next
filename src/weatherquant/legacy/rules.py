"""Legacy table inventory and the reliability rules applied at import time.

Two separate decisions live here and are deliberately not conflated:

**Value tier** - is the table worth carrying across at all?
    Raw/reference data is; anything our own model or ledger produced is
    reproducible (or is trading state the new system must rebuild from scratch),
    so it stays behind. Inheriting the old signals, predictions or account
    would silently import the old system's conclusions.

**Verification status** - once a row is carried across, is it trustworthy?
    Rules are pure functions of the row's own fields, so a classification can be
    reproduced by anyone holding the same legacy file. A failing rule never
    drops the row; it marks it ``pending_verification``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "LEGACY_TABLES",
    "MigratableTable",
    "ValueTier",
    "VerificationStatus",
    "classify",
]


class VerificationStatus(StrEnum):
    """Provenance verdict recorded on every row.

    DuckDB would not accept a CHECK constraint on ``ALTER TABLE ADD COLUMN``,
    so this enum - not the schema - is what bounds the column's values. Nothing
    outside this enum may be written.
    """

    NATIVE = "native"  # produced by this system
    VERIFIED = "verified"  # migrated, passed every rule
    PENDING = "pending_verification"  # migrated, flagged: do not trust without review


class ValueTier(StrEnum):
    RESEARCH = "research"  # raw/reference data with research value
    DERIVED = "derived"  # our own model output; reproducible, not inherited
    TRADING_STATE = "trading_state"  # paper ledger / locks / audit; rebuilt from scratch


@dataclass(frozen=True, slots=True)
class MigratableTable:
    legacy_table: str
    tier: ValueTier
    target_table: str | None  # None => intentionally not migrated
    note: str


LEGACY_TABLES: dict[str, MigratableTable] = {
    # --- research value: carried across -------------------------------------
    "events": MigratableTable(
        "events", ValueTier.RESEARCH, "events", "Market metadata, rules text and resolution source"
    ),
    "markets": MigratableTable(
        "markets", ValueTier.RESEARCH, "markets", "Bracket definitions, tokens, fee schedule, raw payload"
    ),
    "books": MigratableTable(
        "books", ValueTier.RESEARCH, "order_books", "Captured L2 depth - the only depth that exists"
    ),
    "price_history": MigratableTable(
        "price_history", ValueTier.RESEARCH, "price_history", "Mid-price series; not executable"
    ),
    "forecast_runs": MigratableTable(
        "forecast_runs", ValueTier.RESEARCH, "forecast_runs", "Model initialisations with publishable_at"
    ),
    "forecast_hourly": MigratableTable(
        "forecast_hourly", ValueTier.RESEARCH, "forecast_hourly", "Hourly forecast fields"
    ),
    "forecast_daily": MigratableTable(
        "forecast_daily", ValueTier.RESEARCH, "forecast_daily", "Predicted daily maximum"
    ),
    "observations": MigratableTable(
        "observations", ValueTier.RESEARCH, "observations", "Station readings"
    ),
    "daily_observations": MigratableTable(
        "daily_observations",
        ValueTier.RESEARCH,
        "daily_max_temperatures",
        "Derived daily maxima - cross-check only, never official",
    ),
    "settlements": MigratableTable(
        "settlements", ValueTier.RESEARCH, "settlements", "Venue-resolved outcomes"
    ),
    # --- reproducible: left behind ------------------------------------------
    "predictions": MigratableTable(
        "predictions", ValueTier.DERIVED, None, "Old model output; the new model must regenerate its own"
    ),
    "signals": MigratableTable(
        "signals", ValueTier.DERIVED, None, "Old signal logic; explicitly not inherited (spec §四)"
    ),
    "model_runs": MigratableTable(
        "model_runs", ValueTier.DERIVED, None, "Old fit metadata; superseded by new calibration"
    ),
    "backtest_runs": MigratableTable(
        "backtest_runs", ValueTier.DERIVED, None, "Old backtest results; cannot validate the new one"
    ),
    # --- trading state: rebuilt from scratch ---------------------------------
    "account": MigratableTable("account", ValueTier.TRADING_STATE, None, "Paper ledger; new engine opens its own"),
    "orders": MigratableTable("orders", ValueTier.TRADING_STATE, None, "Old paper orders"),
    "fills": MigratableTable("fills", ValueTier.TRADING_STATE, None, "Old paper fills"),
    "positions": MigratableTable("positions", ValueTier.TRADING_STATE, None, "Old paper positions"),
    "equity_snapshots": MigratableTable(
        "equity_snapshots", ValueTier.TRADING_STATE, None, "Old equity curve"
    ),
    "audit_log": MigratableTable("audit_log", ValueTier.TRADING_STATE, None, "Old run log"),
    "process_locks": MigratableTable(
        "process_locks", ValueTier.TRADING_STATE, None, "Old single-writer leases"
    ),
    "meta": MigratableTable("meta", ValueTier.TRADING_STATE, None, "Old key/value bookkeeping"),
}

# Status domains observed in the legacy schema (verified against its collectors
# and DDL). Anything outside these sets is itself a reason to distrust a row.
LEGACY_MARKET_PARSE_OK = "ok"
LEGACY_BOOK_STATUS_OK = "ok"
LEGACY_FORECAST_STATUS_OK = "ok"
_LEGACY_BOOK_PROBLEM_STATUSES = frozenset({"empty", "no_asks", "crossed"})
_LEGACY_FORECAST_PROBLEM_STATUSES = frozenset({"unavailable", "failed", "no_coverage"})


def _s(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def classify(
    table: str,
    row: Mapping[str, Any],
    *,
    run_status: str | None = None,
) -> tuple[VerificationStatus, str | None]:
    """Classify one legacy row. Returns ``(status, reason_if_not_verified)``.

    ``run_status`` is the parent ``forecast_runs.status`` for
    ``forecast_hourly`` / ``forecast_daily``; a daily row inherits its run's
    verdict, because a forecast from a failed run is no more usable than the
    run itself.

    Every rule is a pure function of the row, so re-running the import against
    the same legacy file reproduces the same verdicts.
    """
    if table not in LEGACY_TABLES:
        raise KeyError(f"table {table!r} is not in the legacy inventory")

    if table == "markets":
        status = _s(row, "parse_status")
        if status is None:
            return VerificationStatus.PENDING, "missing parse_status"
        if status != LEGACY_MARKET_PARSE_OK:
            return VerificationStatus.PENDING, f"legacy parse_status={status}: {_s(row, 'parse_reason')}"

    elif table == "books":
        status = _s(row, "status")
        if status is None:
            return VerificationStatus.PENDING, "missing book status"
        if status != LEGACY_BOOK_STATUS_OK:
            return VerificationStatus.PENDING, f"legacy book status={status}: {_s(row, 'reason')}"
        if row.get("venue_ts") is None:
            # Without the venue's own timestamp the snapshot's age is bounded
            # only by when *we* pulled it; it can never be proven fresh at a
            # decision instant. The legacy capture never populated this column,
            # so this rule currently flags every legacy book row - which is the
            # honest verdict on that batch, not a bug. See
            # docs/legacy-data-migration.md for the resulting counts.
            return VerificationStatus.PENDING, "no venue_ts: staleness at capture time is unknowable"

    elif table == "forecast_runs":
        status = _s(row, "status")
        if status is None:
            return VerificationStatus.PENDING, "missing forecast run status"
        if status != LEGACY_FORECAST_STATUS_OK:
            return VerificationStatus.PENDING, f"legacy forecast run status={status}: {_s(row, 'reason')}"

    elif table in ("forecast_hourly", "forecast_daily"):
        if run_status is None:
            return VerificationStatus.PENDING, "parent forecast run status unknown"
        if run_status != LEGACY_FORECAST_STATUS_OK:
            return VerificationStatus.PENDING, f"inherited from forecast run status={run_status}"
        if table == "forecast_daily":
            hours = row.get("n_hours")
            if hours is not None and int(hours) < 24:
                # A day built from fewer than 24 hourly points understates the
                # true maximum, so its "predicted_max" is not comparable with a
                # complete run's.
                return VerificationStatus.PENDING, f"incomplete day: only {hours}/24 hourly points"

    elif table == "observations":
        if not _s(row, "source"):
            return VerificationStatus.PENDING, "missing observation source: provenance lost"

    elif table == "daily_observations":
        if row.get("is_official"):
            # A station observation can never be the official result - only the
            # venue's resolution is. A legacy row claiming otherwise is a defect.
            return VerificationStatus.PENDING, "observation claims is_official=true"
        if row.get("n_readings") is None:
            return VerificationStatus.PENDING, "n_readings unknown: aggregation basis not auditable"
        if int(row.get("n_readings") or 0) <= 0:
            return VerificationStatus.PENDING, "n_readings=0: nothing was actually aggregated"

    elif table == "settlements":
        if not row.get("is_official"):
            return VerificationStatus.PENDING, "legacy row is not an official resolution"
        price = row.get("yes_price")
        if price is not None and float(price) not in (0.0, 1.0):
            return VerificationStatus.PENDING, f"resolved yes_price={price} is not 0 or 1"
        notes = (_s(row, "notes") or "").upper()
        if "DISAGREE" in notes:
            # The legacy system already recorded that its own cross-check
            # contradicted the venue. Carrying that forward as trusted would
            # discard the one signal that said "look at this".
            return VerificationStatus.PENDING, "cross-check disagrees with the venue resolution"

    elif table == "price_history":
        mid = row.get("mid")
        if mid is None or not 0.0 <= float(mid) <= 1.0:
            return VerificationStatus.PENDING, f"mid price {mid!r} outside [0, 1]"

    elif table == "events":
        if not _s(row, "rules_text"):
            return VerificationStatus.PENDING, "no rules text: resolution terms cannot be checked"

    elif table == "daily_max_temperatures":  # pragma: no cover - not a legacy name
        pass

    # Tables with no rule of their own are verified on provenance alone; the
    # importer separately requires source + capture time columns to be present.
    return VerificationStatus.VERIFIED, None

"""Extract settlement truth and observations from the legacy database into the research database.

Read-only on the source, idempotent on the destination:

* the legacy file is opened through :func:`weatherquant.storage.connect_legacy_read_only`,
  which has no write mode;
* every target table has a natural key, so re-running an import overwrites
  rather than duplicates - a backfill that is interrupted can simply be run
  again;
* the raw ``rules_text`` travels with each row so any parse can be re-audited.

Deliberately **not** extracted: ``signals``, ``predictions``, ``orders``,
``positions``, ``account``. Those are the old system's conclusions, and
carrying them across would import its answers rather than its evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weatherquant.markets.settlement_rules import (
    SETTLEMENT_RULES_COLUMNS,
    parse_rules_text,
    parsed_to_row,
)
from weatherquant.storage import connect_legacy_read_only

__all__ = ["ExtractionStats", "extract_from_legacy"]


@dataclass
class ExtractionStats:
    events: int = 0
    events_rules_parsed: int = 0
    settlement_outcomes: int = 0
    daily_maxima: int = 0
    market_prices: int = 0
    market_tokens: int = 0
    skipped_tables: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "events_rules_parsed": self.events_rules_parsed,
            "settlement_outcomes": self.settlement_outcomes,
            "daily_maxima": self.daily_maxima,
            "market_prices": self.market_prices,
            "market_tokens": self.market_tokens,
            "problems": self.problems,
        }


def extract_from_legacy(
    research_con,
    legacy_path: str | Path,
    *,
    batch_size: int = 500,
) -> ExtractionStats:
    """Copy the legacy evidence needed for settlement verification.

    Writes only into ``research_con``. Returns counts plus any problem worth
    reporting - a problem never aborts the run, because a partial import that
    says what it missed beats an import that silently stops.
    """
    stats = ExtractionStats()
    src = connect_legacy_read_only(legacy_path)
    try:
        _extract_rules(research_con, src, stats)
        _extract_outcomes(research_con, src, stats)
        _extract_daily_maxima(research_con, src, stats)
        _extract_prices(research_con, src, stats)
        stats.skipped_tables = sorted(
            set(_legacy_tables(src))
            - {"events", "markets", "settlements", "daily_observations", "price_history"}
        )
    finally:
        src.close()
    return stats


def _legacy_tables(con) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# A. Rules
# ---------------------------------------------------------------------------


def _extract_rules(dst, src, stats: ExtractionStats) -> None:
    seen: set[str] = set()
    for event_id, slug, target_date, rules_text in src.execute(
        "SELECT event_id, slug, event_date, rules_text FROM events"
    ).fetchall():
        if event_id in seen:
            continue
        seen.add(event_id)
        parsed = parse_rules_text(rules_text, event_id=event_id)
        row = parsed_to_row(
            parsed,
            event_id=event_id,
            target_date=target_date,
            slug=slug,
        )
        dst.execute(
            f"INSERT INTO settlement_rules ({SETTLEMENT_RULES_COLUMNS}) "
            "VALUES ("
            + ",".join("?" for _ in range(len(SETTLEMENT_RULES_COLUMNS.split(","))))
            + ") ON CONFLICT (event_id) DO UPDATE SET "
            + ", ".join(
                f"{col} = excluded.{col}"
                for col in SETTLEMENT_RULES_COLUMNS.split(", ")
                if col != "event_id"
            ),
            list(row),
        )
        stats.events += 1
        if parsed.parse_status == "parsed":
            stats.events_rules_parsed += 1
        else:
            stats.problems.append(
                f"{event_id}: rules parse_status={parsed.parse_status}: "
                + "; ".join(parsed.parse_notes)
            )


# ---------------------------------------------------------------------------
# C. Settlement outcomes - one row per market, winner flagged per event
# ---------------------------------------------------------------------------


def _extract_outcomes(dst, src, stats: ExtractionStats) -> None:
    fetched = datetime.now(tz=UTC)
    rows = src.execute(
        """
        SELECT m.event_id, m.target_date, m.market_id, m.bracket_label,
               m.bracket_low, m.bracket_high, s.yes_price, s.resolved_at
        FROM markets m JOIN settlements s ON s.market_id = m.market_id
        WHERE m.target_date IS NOT NULL
        """
    ).fetchall()

    # Winner counts are per *event* - never per date. Two events can share a
    # date and resolve differently, and folding them together invents a
    # multi-winner event that does not exist.
    winners_per_event: dict[str, int] = {}
    for event_id, _d, _m, _l, _lo, _hi, yes, _r in rows:
        if yes == 1.0:
            winners_per_event[event_id] = winners_per_event.get(event_id, 0) + 1

    payload = []
    for event_id, target_date, market_id, label, low, high, yes, resolved_at in rows:
        payload.append(
            (
                event_id,
                target_date,
                market_id,
                label,
                low,
                high,
                yes == 1.0,
                winners_per_event.get(event_id, 0),
                yes,
                resolved_at,
                "legacy_duckdb",
                fetched,
            )
        )

    dst.executemany(
        "INSERT INTO settlement_outcome "
        "(event_id, target_date, market_id, bracket_label, bracket_low, bracket_high,"
        " is_winner, n_winners_in_event, yes_price, resolved_at_utc, source, fetched_at_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (event_id, market_id, source) DO UPDATE SET "
        "  bracket_label=excluded.bracket_label, bracket_low=excluded.bracket_low,"
        "  bracket_high=excluded.bracket_high, is_winner=excluded.is_winner,"
        "  n_winners_in_event=excluded.n_winners_in_event, yes_price=excluded.yes_price,"
        "  resolved_at_utc=excluded.resolved_at_utc, fetched_at_utc=excluded.fetched_at_utc",
        payload,
    )
    stats.settlement_outcomes = len(payload)


# ---------------------------------------------------------------------------
# B. Daily maxima - both aggregation rules kept separate
# ---------------------------------------------------------------------------

_RULES_TO_KEEP = ("all_readings", "hourly_metar")


def _extract_daily_maxima(dst, src, stats: ExtractionStats) -> None:
    rows = src.execute(
        """
        SELECT station, target_date, max_temp_f, n_readings, rule, source, computed_at
        FROM daily_observations
        WHERE rule IN ('all_readings', 'hourly_metar')
        """
    ).fetchall()

    payload = []
    for station, target_date, max_temp_f, n_readings, rule, source, computed_at in rows:
        # The legacy aggregate stores a reading *count*, not a covered-hour
        # count, and `all_readings` legitimately exceeds 24 per day because one
        # hour can carry several METARs. Judging completeness by
        # `n_readings <= 24` therefore flagged 575 perfectly good days. Hour
        # coverage is simply not derivable from this source, so it is recorded
        # as unknown rather than guessed.
        readings = int(n_readings or 0)
        payload.append(
            (
                station,
                target_date,
                max_temp_f,
                readings,
                -1,  # unknown: the legacy table does not record covered hours
                None,
                None,
                "max of all readings" if rule == "all_readings" else "max of routine hourly METAR",
                source,
                readings > 0,
                computed_at,
                "extracted from legacy daily_observations; reading count only, "
                "hour coverage unknown; not an official result",
            )
        )

    dst.executemany(
        "INSERT INTO klga_daily_max "
        "(station, target_date, max_temp_f, n_readings, n_missing_hours, n_routine,"
        " n_special, aggregation_rule, source, is_complete, computed_at_utc, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (station, target_date, source, aggregation_rule) DO UPDATE SET "
        "  max_temp_f=excluded.max_temp_f, n_readings=excluded.n_readings,"
        "  n_missing_hours=excluded.n_missing_hours, is_complete=excluded.is_complete,"
        "  computed_at_utc=excluded.computed_at_utc, notes=excluded.notes",
        payload,
    )
    stats.daily_maxima = len(payload)


# ---------------------------------------------------------------------------
# E. Market prices - the market baseline's raw material
# ---------------------------------------------------------------------------


def _extract_prices(dst, src, stats: ExtractionStats) -> None:
    """Copy the mid-price series and the bracket->token map.

    Mids are what the market believed, which is the quantity the market
    baseline scores. They are never executable and nothing downstream may
    treat them as such.
    """
    tokens = src.execute(
        """
        SELECT market_id, event_id, target_date, bracket_label, token_yes, token_no
        FROM markets WHERE target_date IS NOT NULL
        """
    ).fetchall()
    payload_tokens = []
    seen: set[tuple[str, str]] = set()
    for market_id, event_id, target_date, label, token_yes, token_no in tokens:
        for token, is_yes in ((token_yes, True), (token_no, False)):
            if not token or (market_id, token) in seen:
                continue
            seen.add((market_id, token))
            payload_tokens.append((event_id, target_date, market_id, label, token, is_yes))
    if payload_tokens:
        dst.executemany(
            "INSERT INTO market_tokens VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (market_id, token_id) DO UPDATE SET "
            "  event_id=excluded.event_id, target_date=excluded.target_date,"
            "  bracket_label=excluded.bracket_label, is_yes_token=excluded.is_yes_token",
            payload_tokens,
        )

    prices = src.execute(
        "SELECT market_id, token_id, ts, mid, fetched_at FROM price_history WHERE mid IS NOT NULL"
    ).fetchall()
    payload = [
        (m, t, ts, mid, "legacy_price_history", fetched)
        for m, t, ts, mid, fetched in prices
        if mid is not None and 0.0 <= float(mid) <= 1.0
    ]
    if payload:
        dst.executemany(
            "INSERT INTO market_price_history (market_id, token_id, ts, mid_usd_per_share,"
            " source, fetched_at_utc) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            payload,
        )
    stats.market_prices = len(payload)
    stats.market_tokens = len(payload_tokens)


def _legacy_tables_v2(con) -> list[str]:  # pragma: no cover - kept for symmetry
    return _legacy_tables(con)

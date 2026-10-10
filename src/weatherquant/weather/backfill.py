"""Persist forecast runs, aggregate them to daily maxima, judge point-in-time eligibility.

One pass does three things that must agree with each other:

1. store the run and its hourly points verbatim (nulls are counted as missing
   hours rather than stored as rows - a gap in the series is information);
2. aggregate each target New York day from those points;
3. decide whether the run could have been known before that day began.

They happen together because a daily maximum without a point-in-time verdict is
unusable, and a verdict without the hours that produced it is unauditable.

Nothing here back-fills a missing run. An unavailable run is stored as
``status='unavailable'`` with the venue's own error text and yields no daily
maximum - a hole in the record is a result, not something to interpolate over.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from weatherquant.weather.daily_max import (
    MARKET_TIMEZONE,
    aggregate_daily_max,
    local_day_bounds_utc,
    point_in_time,
)
from weatherquant.weather.single_runs import PUBLISHABLE_LAG_HOURS, RunFetch

__all__ = [
    "BackfillStats",
    "backfill",
    "persist_run",
    "store_daily_max_and_pip",
    "target_dates_covered",
]


@dataclass
class BackfillStats:
    runs_ok: int = 0
    runs_unavailable: int = 0
    runs_failed: int = 0
    hourly_rows: int = 0
    daily_rows: int = 0
    pip_eligible: int = 0
    pip_ineligible: int = 0
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "runs_ok": self.runs_ok,
            "runs_unavailable": self.runs_unavailable,
            "runs_failed": self.runs_failed,
            "hourly_rows": self.hourly_rows,
            "daily_rows": self.daily_rows,
            "pip_eligible": self.pip_eligible,
            "pip_ineligible": self.pip_ineligible,
            "problems": list(self.problems),
        }


_PUBLISHABLE_NOTE = (
    "publication time is not provided by the endpoint; estimated as run_time + "
    f"{PUBLISHABLE_LAG_HOURS:.0f}h, chosen to exceed the slowest availability "
    "measured while verifying the API (see docs/weather-data-sources.md)"
)


def _publishable_at(run_time_utc: datetime | None) -> datetime | None:
    if run_time_utc is None:
        return None
    return run_time_utc + timedelta(hours=PUBLISHABLE_LAG_HOURS)


def persist_run(con, fetch: RunFetch, *, station: str, provider: str = "open-meteo") -> int:
    """Idempotently store one run and its hourly points. Returns rows written.

    Re-fetching the same ``(provider, model, run_time)`` replaces the previous
    rows, so an interrupted backfill can simply be re-run.
    """
    publishable = _publishable_at(fetch.run_time_utc)
    con.execute(
        "INSERT INTO forecast_runs "
        "(provider, model, model_version, run_time_utc, publishable_at_utc, publishable_basis,"
        " publishable_lag_hours, publishable_note, fetched_at_utc, http_status, status,"
        " raw_path, raw_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (provider, model, run_time_utc) DO UPDATE SET "
        "  model_version=excluded.model_version, publishable_at_utc=excluded.publishable_at_utc,"
        "  publishable_basis=excluded.publishable_basis,"
        "  publishable_lag_hours=excluded.publishable_lag_hours,"
        "  publishable_note=excluded.publishable_note, fetched_at_utc=excluded.fetched_at_utc,"
        "  http_status=excluded.http_status, status=excluded.status,"
        "  raw_path=excluded.raw_path, raw_sha256=excluded.raw_sha256",
        [
            provider,
            fetch.model,
            None,
            fetch.run_time_utc,
            publishable,
            "estimated" if publishable is not None else "unknown",
            PUBLISHABLE_LAG_HOURS if publishable is not None else None,
            _PUBLISHABLE_NOTE if publishable is not None else "run time unknown",
            fetch.fetched_at_utc,
            fetch.http_status,
            fetch.status,
            fetch.raw_path,
            fetch.payload_sha256,
        ],
    )

    if not fetch.is_ok or not fetch.hourly or fetch.run_time_utc is None:
        return 0

    rows = [
        (
            provider,
            fetch.model,
            fetch.run_time_utc,
            p.valid_time_utc,
            (p.valid_time_utc - fetch.run_time_utc).total_seconds() / 3600.0,
            p.temperature_2m,
            station,
            "open-meteo-single-runs",
            fetch.payload_sha256 or "",
        )
        for p in fetch.hourly
        if p.temperature_2m is not None
    ]
    if rows:
        con.executemany(
            "INSERT INTO forecast_hourly "
            "(provider, model, run_time_utc, valid_time_utc, lead_hours, temperature_2m,"
            " station, source, raw_sha256) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            rows,
        )
    return len(rows)


def store_daily_max_and_pip(
    con,
    *,
    fetch: RunFetch,
    target_dates: Iterable[date],
    station: str,
    provider: str = "open-meteo",
) -> tuple[int, int, int]:
    """Aggregate each target date and record its point-in-time verdict.

    Returns ``(daily_rows_written, eligible, ineligible)``.
    """
    if not fetch.is_ok or fetch.run_time_utc is None or not fetch.hourly:
        return 0, 0, 0

    hourly = [
        {"valid_time_utc": p.valid_time_utc, "temperature_2m": p.temperature_2m}
        for p in fetch.hourly
    ]
    publishable = _publishable_at(fetch.run_time_utc)
    written = eligible = ineligible = 0

    for target_date in target_dates:
        agg = aggregate_daily_max(hourly, target_date=target_date)
        if agg.predicted_max_f is None:
            continue

        day_end = local_day_bounds_utc(target_date)[1]
        con.execute(
            "INSERT INTO forecast_daily_max "
            "(provider, model, run_time_utc, target_date, station, predicted_max_f, n_hours,"
            " n_missing_hours, first_valid_utc, last_valid_utc, lead_hours_at_end,"
            " covers_full_local_day, computed_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (provider, model, run_time_utc, target_date, station) DO UPDATE SET "
            "  predicted_max_f=excluded.predicted_max_f, n_hours=excluded.n_hours,"
            "  n_missing_hours=excluded.n_missing_hours, first_valid_utc=excluded.first_valid_utc,"
            "  last_valid_utc=excluded.last_valid_utc,"
            "  lead_hours_at_end=excluded.lead_hours_at_end,"
            "  covers_full_local_day=excluded.covers_full_local_day,"
            "  computed_at_utc=excluded.computed_at_utc",
            [
                provider,
                fetch.model,
                fetch.run_time_utc,
                target_date,
                station,
                agg.predicted_max_f,
                agg.n_hours,
                agg.n_missing_hours,
                agg.first_valid_utc,
                agg.last_valid_utc,
                (day_end - fetch.run_time_utc).total_seconds() / 3600.0,
                agg.covers_full_local_day,
                datetime.now(tz=UTC),
            ],
        )
        written += 1

        pip = point_in_time(
            run_time_utc=fetch.run_time_utc,
            publishable_at_utc=publishable,
            publishable_basis="estimated",
            target_date=target_date,
            publishable_lag_hours=PUBLISHABLE_LAG_HOURS,
            publishable_note=_PUBLISHABLE_NOTE,
        )
        con.execute(
            "INSERT INTO point_in_time "
            "(provider, model, run_time_utc, target_date, station, publishable_at_utc,"
            " publishable_basis, decision_deadline_utc, eligible, ineligible_reason,"
            " checked_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (provider, model, run_time_utc, target_date, station) DO UPDATE SET "
            "  publishable_at_utc=excluded.publishable_at_utc,"
            "  publishable_basis=excluded.publishable_basis,"
            "  decision_deadline_utc=excluded.decision_deadline_utc,"
            "  eligible=excluded.eligible, ineligible_reason=excluded.ineligible_reason,"
            "  checked_at_utc=excluded.checked_at_utc",
            [
                provider,
                fetch.model,
                fetch.run_time_utc,
                target_date,
                station,
                pip.publishable_at_utc,
                pip.publishable_basis,
                pip.decision_deadline_utc,
                pip.eligible,
                pip.ineligible_reason,
                datetime.now(tz=UTC),
            ],
        )
        if pip.eligible:
            eligible += 1
        else:
            ineligible += 1

    return written, eligible, ineligible


def target_dates_covered(fetch: RunFetch, *, horizon_days: int = 10) -> list[date]:
    """Local dates this run has any hourly coverage for."""
    if not fetch.hourly or fetch.run_time_utc is None:
        return []
    start = fetch.run_time_utc.astimezone(MARKET_TIMEZONE).date()
    return [start + timedelta(days=i) for i in range(horizon_days + 1)]


def backfill(
    con,
    *,
    client,
    models: Sequence[str],
    run_times: Sequence[datetime],
    target_dates: Sequence[date],
    station: str,
    provider: str = "open-meteo",
    on_fetch: Callable | None = None,
) -> BackfillStats:
    """Fetch, store and judge every ``(model, run_time)`` in the plan.

    Resumable: every write is keyed, so re-running after an interruption
    overwrites rather than duplicates.
    """
    stats = BackfillStats()
    for model in models:
        for run_time in run_times:
            fetch = client.fetch(model, run_time)
            if fetch.status == "ok":
                stats.runs_ok += 1
            elif fetch.status == "unavailable":
                stats.runs_unavailable += 1
            else:
                stats.runs_failed += 1
                if fetch.error_text:
                    stats.problems.append(f"{model} {run_time.isoformat()}: {fetch.error_text}")

            stats.hourly_rows += persist_run(con, fetch, station=station, provider=provider)
            if fetch.is_ok:
                daily, ok, bad = store_daily_max_and_pip(
                    con,
                    fetch=fetch,
                    target_dates=target_dates,
                    station=station,
                    provider=provider,
                )
                stats.daily_rows += daily
                stats.pip_eligible += ok
                stats.pip_ineligible += bad
            if on_fetch is not None:
                on_fetch(model, run_time, fetch)
    return stats

"""Fetching real forecasts at decision time, and refusing them until they exist.

The rule this module exists to enforce
--------------------------------------
Open-Meteo's Single Runs endpoint answers "which run?" with a run time, and it
will happily answer for a run that has initialised but has not finished
propagating. The endpoint reports no publication time at all, so the project
estimates it as ``run_time + PUBLISHABLE_LAG_HOURS`` and marks the estimate
``publishable_basis='estimated'`` - the same constant, the same basis and the
same arithmetic the point-in-time history uses, because two different
definitions of "available" is how a live engine and its own backtest start
disagreeing.

A run whose ``publishable_at`` is still in the future is not a slow run, it is
a run we do not have, and it is refused rather than approximated.

What is *not* enforced here: the backtest's stricter rule that a run must
predate the start of the target day. That rule exists to stop a run issued
*during* the day it predicts from entering a historical comparison. A live
engine legitimately acts during the day, so both verdicts are produced -
``publishable_at <= now`` for trading, and
:func:`weatherquant.weather.daily_max.point_in_time` for research
comparability - rather than silently applying one where the other belongs.

Retry policy
------------
``ok`` and ``unavailable`` are cached indefinitely: re-asking every cycle for a
run the endpoint will never serve turns a refusal into a load test. ``failed``
- a transport error, not an answer - is retried after
:data:`FAILED_RETRY_HOURS`, which is the only backoff this module needs
because the refresh cycle is already the outer interval.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from weatherquant.weather.daily_max import aggregate_daily_max, local_day_bounds_utc, point_in_time
from weatherquant.weather.single_runs import PUBLISHABLE_LAG_HOURS, RunFetch, SingleRunsClient

__all__ = [
    "ISSUE_HOURS_UTC",
    "MODEL_SPECS",
    "UPSTREAM_MODELS",
    "ForecastCache",
    "ResolvedForecast",
    "candidate_run_times",
    "publishable_at",
    "resolve_models",
]

ISSUE_HOURS_UTC = (0, 6, 12, 18)
FAILED_RETRY_HOURS = 1.0

# Shadow model name -> (upstream source model or models, apply fitted bias).
# Mirrors the baseline design in `models/baselines.py` so the shadow predicts
# the same four models research evaluated, under the names the spec uses.
MODEL_SPECS: dict[str, tuple[tuple[str, ...], bool]] = {
    "ecmwf_raw": (("ecmwf_ifs025",), False),
    "gfs_raw": (("gfs_seamless",), False),
    "fusion": (("ecmwf_ifs025", "gfs_seamless"), False),
    "calibrated": (("ecmwf_ifs025",), True),
}

# Canonical ids to request. `gfs` is an alias that the client resolves; always
# asking for the resolved id keeps cache keys from splitting into two spellings.
UPSTREAM_MODELS: tuple[str, ...] = tuple(
    dict.fromkeys(source for sources, _ in MODEL_SPECS.values() for source in sources)
)


def publishable_at(run_time_utc: datetime) -> datetime:
    return run_time_utc + timedelta(hours=PUBLISHABLE_LAG_HOURS)


def candidate_run_times(now: datetime, lookback_hours: float) -> list[datetime]:
    """Issue hours on the 00/06/12/18Z grid in the past, newest first.

    An issue hour at or after ``now`` is excluded: asking the endpoint for a
    run that has not started is answered with "not available", a refusal we
    would be choosing to provoke every cycle.
    """
    grid = now.replace(minute=0, second=0, microsecond=0)
    grid = grid.replace(hour=grid.hour - (grid.hour % 6))
    earliest = now - timedelta(hours=lookback_hours)
    out: list[datetime] = []
    cursor = grid
    while cursor > earliest:
        if cursor < now:
            out.append(cursor)
        cursor -= timedelta(hours=6)
    return out


@dataclass(frozen=True, slots=True)
class ResolvedForecast:
    """One usable point forecast for one shadow model and target date."""

    shadow_model: str
    source_models: tuple[str, ...]
    run_time_utc: datetime
    publishable_at_utc: datetime
    publishable_basis: str
    received_at_utc: datetime
    target_date: date
    predicted_max_f: float
    n_hours: int
    n_missing_hours: int
    payload_sha256: str
    covers_full_local_day: bool

    @property
    def lead_hours(self) -> float:
        """Run initialisation to the end of the target local day - the same
        definition the residual model was fitted on, so the sigma lookup lands
        in the bucket the fit intended."""
        _day_start, day_end = local_day_bounds_utc(self.target_date)
        return (day_end - self.run_time_utc).total_seconds() / 3600.0

    def usable_at(self, instant_utc: datetime, *, max_age_hours: float) -> tuple[bool, str | None]:
        """``(ok, reason)`` for acting on this forecast at ``instant_utc``."""
        if self.run_time_utc > instant_utc:
            return False, f"run {self.run_time_utc.isoformat()} has not happened yet"
        if self.publishable_at_utc > instant_utc:
            return False, (
                f"run {self.run_time_utc.isoformat()} becomes publishable at "
                f"{self.publishable_at_utc.isoformat()}, after the decision instant "
                f"{instant_utc.isoformat()} (basis={self.publishable_basis})"
            )
        age_hours = (instant_utc - self.run_time_utc).total_seconds() / 3600.0
        if age_hours > max_age_hours:
            return False, (
                f"run {self.run_time_utc.isoformat()} is {age_hours:.1f}h old, "
                f"past the {max_age_hours}h limit"
            )
        if self.predicted_max_f is None:
            return False, "run produced no daily maximum for the target date"
        if self.n_hours <= 0:
            return False, "run covers no hours of the target date"
        return True, None

    def research_eligible_at(self, target_date: date) -> bool:
        """The stricter backtest verdict: recorded for comparability, never
        applied to a live decision."""
        return point_in_time(
            run_time_utc=self.run_time_utc,
            publishable_at_utc=self.publishable_at_utc,
            publishable_basis=self.publishable_basis,
            target_date=target_date,
            publishable_lag_hours=PUBLISHABLE_LAG_HOURS,
        ).eligible


@dataclass(frozen=True, slots=True)
class DailyMax:
    predicted_max_f: float | None
    n_hours: int
    n_missing_hours: int
    is_complete: bool


@dataclass
class _RunState:
    status: str
    fetch: RunFetch
    publishable_at: datetime
    fetched_at: datetime
    daily: dict[date, DailyMax]
    # RunFetch exposes n_points as a property over its hourly list, which a
    # rehydrated run does not carry. The count is provenance rather than data,
    # so it lives on the state instead of being reconstructed.
    n_points: int = 0


class ForecastCache:
    """Fetch each (model, run_time) once and answer queries from memory."""

    def __init__(
        self,
        client: SingleRunsClient,
        *,
        station: str,
        target_dates: Callable[[], list[date]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.station = station
        self.target_dates = target_dates or (lambda: [])
        self.clock = clock or (lambda: datetime.now(tz=UTC))
        self._runs: dict[tuple[str, datetime], _RunState] = {}

    # -- hydration ----------------------------------------------------------

    def load_cached_rows(self, run_rows, daily_rows) -> None:
        """Rehydrate from ``forecast_runs`` / ``forecast_daily_max`` so a restart
        does not re-hit upstream for runs it already holds."""
        for row in run_rows:
            model, run_time, status, http_status, error_text, payload_sha, publishable, _basis, received, n_points = row
            self._runs[(model, run_time)] = _RunState(
                status=status,
                fetch=RunFetch(
                    model=model,
                    run_time_utc=run_time,
                    status=status,
                    http_status=http_status,
                    error_text=error_text,
                    payload_sha256=payload_sha,
                    fetched_at_utc=received,
                ),
                publishable_at=publishable or publishable_at(run_time),
                fetched_at=received or self.clock(),
                daily={},
                n_points=n_points or 0,
            )
        for row in daily_rows:
            model, run_time, target_date, _station, predicted, n_hours, n_missing, is_complete, _computed = row
            state = self._runs.get((model, run_time))
            if state is None:
                continue
            state.daily[target_date] = DailyMax(
                predicted_max_f=predicted,
                n_hours=n_hours,
                n_missing_hours=n_missing,
                is_complete=bool(is_complete),
            )

    @property
    def cached_keys(self) -> set[tuple[str, datetime]]:
        """Which (model, run) pairs are already held - used to prove a restart
        does not re-ask upstream for runs it already has."""
        return set(self._runs)

    # -- fetching -----------------------------------------------------------

    def refresh(
        self,
        *,
        now: datetime,
        lookback_hours: float,
        max_new_per_model: int,
    ) -> dict:
        """Fetch missing publishable runs, capped per model per cycle."""
        wanted = self.target_dates()
        stats = {"fetched": 0, "ok": 0, "unavailable": 0, "failed": 0, "skipped_unavailable": 0, "skipped_full": 0}

        for model in UPSTREAM_MODELS:
            budget = max_new_per_model
            for run_time in candidate_run_times(now, lookback_hours):
                if budget <= 0:
                    break
                if publishable_at(run_time) > now:
                    continue  # not yet ours to have; a later cycle will take it
                state = self._runs.get((model, run_time))
                if state is not None:
                    if state.status == "unavailable":
                        stats["skipped_unavailable"] += 1
                        continue
                    if state.status == "failed" and (now - state.fetched_at) < timedelta(
                        hours=FAILED_RETRY_HOURS
                    ):
                        continue
                    if state.status == "ok" and all(d in state.daily for d in wanted):
                        stats["skipped_full"] += 1
                        continue
                    # ok but a new target date appeared: re-ask for the same run
                    # so the new day's maximum exists too.

                fetch = self.client.fetch(model, run_time)
                self._store(model, fetch, now, wanted)
                stats["fetched"] += 1
                stats[fetch.status] = stats.get(fetch.status, 0) + 1
                budget -= 1
        return stats

    def _store(self, model: str, fetch: RunFetch, now: datetime, wanted: list[date]) -> None:
        run_time = fetch.run_time_utc or now
        state = _RunState(
            status=fetch.status,
            fetch=fetch,
            publishable_at=publishable_at(run_time),
            fetched_at=fetch.fetched_at_utc or now,
            daily={},
            n_points=fetch.n_points,
        )
        if fetch.is_ok:
            for target_date in wanted:
                hourly = [
                    {"valid_time_utc": p.valid_time_utc, "temperature_2m": p.temperature_2m}
                    for p in fetch.hourly
                ]
                result = aggregate_daily_max(hourly, target_date=target_date)
                state.daily[target_date] = DailyMax(
                    predicted_max_f=result.predicted_max_f,
                    n_hours=result.n_hours,
                    n_missing_hours=result.n_missing_hours,
                    is_complete=result.is_complete,
                )
        self._runs[(model, run_time)] = state

    # -- queries ------------------------------------------------------------

    def resolve(self, shadow_model: str, target_date: date, now: datetime) -> ResolvedForecast | None:
        spec = MODEL_SPECS.get(shadow_model)
        if spec is None:
            return None
        sources, _apply_bias = spec
        return resolve_models(
            shadow_model=shadow_model,
            sources=sources,
            target_date=target_date,
            now=now,
            runs=self._runs,
        )

    def rows_for_persistence(self) -> tuple[list[tuple], list[tuple]]:
        run_rows: list[tuple] = []
        daily_rows: list[tuple] = []
        for (model, run_time), state in sorted(self._runs.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            fetch = state.fetch
            run_rows.append(
                (
                    model,
                    run_time,
                    state.status,
                    fetch.http_status,
                    fetch.error_text,
                    fetch.payload_sha256,
                    state.publishable_at,
                    "estimated",
                    state.fetched_at,
                    state.n_points,
                )
            )
            if state.status != "ok":
                continue
            for target_date, row in sorted(state.daily.items()):
                daily_rows.append(
                    (
                        model,
                        run_time,
                        target_date,
                        self.station,
                        row.predicted_max_f,
                        row.n_hours,
                        row.n_missing_hours,
                        row.is_complete,
                        self.clock(),
                    )
                )
        return run_rows, daily_rows


def resolve_models(
    *,
    shadow_model: str,
    sources: tuple[str, ...],
    target_date: date,
    now: datetime,
    runs: dict[tuple[str, datetime], _RunState],
) -> ResolvedForecast | None:
    """The newest *aligned* run that is publishable now and covers the date.

    Alignment matters for fusion: averaging a 00Z ECMWF run with a 06Z GFS run
    blends two different initialisations and calls the result one forecast. So
    a multi-source model resolves only on a run time every source actually has
    - when none exists, there is no forecast, which is a refusal rather than a
    partial blend.
    """
    if not sources:
        return None

    usable: dict[str, dict[datetime, DailyMax]] = {}
    for source in sources:
        per_run: dict[datetime, DailyMax] = {}
        for (model, run_time), state in runs.items():
            if model != source or state.status != "ok":
                continue
            if state.publishable_at > now:
                continue
            row = state.daily.get(target_date)
            if row is None or row.predicted_max_f is None:
                continue
            per_run[run_time] = row
        if not per_run:
            return None
        usable[source] = per_run

    if len(sources) == 1:
        source = sources[0]
        run_time = max(usable[source])
        rows = [usable[source][run_time]]
        source_models = (source,)
    else:
        shared = set(usable[sources[0]])
        for source in sources[1:]:
            shared &= set(usable[source])
        if not shared:
            return None
        run_time = max(shared)
        rows = [usable[source][run_time] for source in sources]
        source_models = tuple(sources)

    state = runs[(source_models[0], run_time)]
    predicted = sum(r.predicted_max_f for r in rows if r.predicted_max_f is not None) / len(rows)
    return ResolvedForecast(
        shadow_model=shadow_model,
        source_models=source_models,
        run_time_utc=run_time,
        publishable_at_utc=state.publishable_at,
        publishable_basis="estimated",
        received_at_utc=state.fetch.fetched_at_utc or run_time,
        target_date=target_date,
        predicted_max_f=predicted,
        n_hours=min(r.n_hours for r in rows),
        n_missing_hours=max(r.n_missing_hours for r in rows),
        payload_sha256=state.fetch.payload_sha256 or "",
        covers_full_local_day=all(r.is_complete for r in rows),
    )

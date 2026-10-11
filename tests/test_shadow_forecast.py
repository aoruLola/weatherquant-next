"""Live forecast resolution: publishable, aligned, and refused when not.

The rules these tests pin down:

* a run is ours only once ``run_time + PUBLISHABLE_LAG_HOURS`` has passed - the
  same estimate, with the same ``estimated`` basis, the point-in-time history
  uses;
* fusion averages forecasts from the *same* issue time, or it does not happen;
* the strict backtest rule (publishable before the target day starts) is
  reported separately and never applied to a live decision;
* a transport failure is retried, an "unavailable" answer is not.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from weatherquant.shadow.forecast import (
    FAILED_RETRY_HOURS,
    MODEL_SPECS,
    UPSTREAM_MODELS,
    ForecastCache,
    candidate_run_times,
    publishable_at,
    resolve_models,
)
from weatherquant.weather.single_runs import PUBLISHABLE_LAG_HOURS

NOW = datetime(2026, 10, 11, 13, 0, tzinfo=UTC)
TARGET = date(2026, 10, 12)


def _state(status: str, run_time: datetime, daily: dict[date, float | None], *, fetched_at=None):
    from weatherquant.shadow.forecast import DailyMax, _RunState
    from weatherquant.weather.single_runs import RunFetch

    return _RunState(
        status=status,
        fetch=RunFetch(
            model="ecmwf_ifs025",
            run_time_utc=run_time,
            status=status,
            fetched_at_utc=fetched_at or run_time,
        ),
        publishable_at=publishable_at(run_time),
        fetched_at=fetched_at or run_time,
        daily={
            d: DailyMax(
                predicted_max_f=value, n_hours=24, n_missing_hours=0, is_complete=True
            )
            for d, value in daily.items()
        },
    )


# --- the four models the spec names ---------------------------------------


def test_the_specified_four_models_are_the_four_fitted() -> None:
    assert set(MODEL_SPECS) == {"ecmwf_raw", "gfs_raw", "fusion", "calibrated"}


def test_upstream_ids_are_canonical_so_cache_keys_do_not_split() -> None:
    assert "gfs" not in UPSTREAM_MODELS
    assert set(UPSTREAM_MODELS) == {"ecmwf_ifs025", "gfs_seamless"}


def test_calibrated_is_the_ecmwf_point_with_a_fitted_bias() -> None:
    sources, apply_bias = MODEL_SPECS["calibrated"]
    assert sources == ("ecmwf_ifs025",)
    assert apply_bias is True


def test_raw_models_apply_no_bias() -> None:
    for name in ("ecmwf_raw", "gfs_raw"):
        assert MODEL_SPECS[name][1] is False


# --- candidate run times ---------------------------------------------------


def test_only_past_issue_hours_are_ever_requested() -> None:
    for run_time in candidate_run_times(NOW, lookback_hours=72):
        assert run_time < NOW
        assert run_time.minute == 0 and run_time.second == 0
        assert run_time.hour % 6 == 0


def test_the_lookback_bounds_how_far_back_we_would_reach() -> None:
    runs = candidate_run_times(NOW, lookback_hours=24)
    assert min(runs) >= NOW - timedelta(hours=24)
    assert len(runs) >= 3  # 12Z yesterday, 18Z, 00Z, 06Z, 12Z today (past ones)


def test_publishability_is_the_verified_twelve_hour_estimate() -> None:
    run_time = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    assert PUBLISHABLE_LAG_HOURS == 12.0
    assert publishable_at(run_time) == run_time + timedelta(hours=12)


# --- usability -------------------------------------------------------------


def _forecast(**overrides):
    from shadow_helpers import make_forecast

    base = dict(
        run_time=NOW - timedelta(hours=14),
        publishable=NOW - timedelta(hours=2),
        received=NOW - timedelta(hours=2),
        target_date=TARGET,
    )
    base.update(overrides)
    return make_forecast(**base)


def test_a_publishable_recent_run_is_usable() -> None:
    ok, why = _forecast().usable_at(NOW, max_age_hours=30.0)
    assert ok is True and why is None


def test_a_run_whose_publication_is_still_ahead_is_not_ours_yet() -> None:
    forecast = _forecast(publishable=NOW + timedelta(hours=8))
    ok, why = forecast.usable_at(NOW, max_age_hours=30.0)
    assert ok is False
    assert "becomes publishable" in why


def test_a_run_from_the_future_is_refused() -> None:
    forecast = _forecast(run_time=NOW + timedelta(hours=6))
    ok, why = forecast.usable_at(NOW, max_age_hours=30.0)
    assert ok is False
    assert "has not happened yet" in why


def test_a_run_older_than_the_configured_limit_is_stale_not_missing() -> None:
    forecast = _forecast(run_time=NOW - timedelta(hours=72), publishable=NOW - timedelta(hours=60),
                         received=NOW - timedelta(hours=60))
    ok, why = forecast.usable_at(NOW, max_age_hours=30.0)
    assert ok is False
    assert "past the 30.0h limit" in why


def test_a_run_that_covered_no_hours_of_the_target_is_refused() -> None:
    from dataclasses import replace

    forecast = replace(_forecast(), n_hours=0)
    ok, why = forecast.usable_at(NOW, max_age_hours=30.0)
    assert ok is False
    assert "covers no hours" in why


def test_lead_hours_is_measured_to_the_end_of_the_target_local_day() -> None:
    """The same definition the residual model was fitted on."""
    from weatherquant.weather.daily_max import local_day_bounds_utc

    forecast = _forecast(run_time=datetime(2026, 10, 11, 12, 0, tzinfo=UTC))
    _start, end = local_day_bounds_utc(TARGET)
    assert forecast.lead_hours == pytest.approx((end - forecast.run_time_utc).total_seconds() / 3600)


def test_the_stricter_backtest_rule_is_reported_separately_from_the_live_one() -> None:
    """A run issued during the target day may be traded live but never backtested.

    Both verdicts are produced rather than one being applied where the other
    belongs - that is the whole reason the two are kept apart.
    """
    morning = _forecast(
        run_time=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
        publishable=datetime(2026, 10, 12, 18, 0, tzinfo=UTC),
        received=datetime(2026, 10, 12, 18, 0, tzinfo=UTC),
    )
    live_decision_time = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)

    ok, _why = morning.usable_at(live_decision_time, max_age_hours=30.0)
    assert ok is True  # live: published before we decided
    assert morning.research_eligible_at(TARGET) is False  # backtest: after the day began


def test_a_run_published_before_the_target_day_started_is_research_eligible() -> None:
    forecast = _forecast(
        run_time=datetime(2026, 10, 11, 0, 0, tzinfo=UTC),
        publishable=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        received=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
    )
    assert forecast.research_eligible_at(TARGET) is True


# --- resolution ------------------------------------------------------------


def test_the_newest_publishable_run_covering_the_date_wins() -> None:
    older = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    runs = {
        ("ecmwf_ifs025", older): _state("ok", older, {TARGET: 60.0}),
        ("ecmwf_ifs025", newer): _state("ok", newer, {TARGET: 61.0}),
    }
    resolved = resolve_models(
        shadow_model="ecmwf_raw",
        sources=("ecmwf_ifs025",),
        target_date=TARGET,
        now=NOW,
        runs=runs,
    )
    assert resolved is not None
    assert resolved.run_time_utc == newer
    assert resolved.predicted_max_f == 61.0


def test_a_run_that_is_not_yet_publishable_is_not_selected() -> None:
    future = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # publishable at 00:00 tomorrow
    runs = {("ecmwf_ifs025", future): _state("ok", future, {TARGET: 61.0})}
    assert (
        resolve_models(
            shadow_model="ecmwf_raw",
            sources=("ecmwf_ifs025",),
            target_date=TARGET,
            now=NOW,
            runs=runs,
        )
        is None
    )


def test_a_run_without_the_target_date_is_not_a_forecast_for_it() -> None:
    run_time = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    runs = {("ecmwf_ifs025", run_time): _state("ok", run_time, {date(2026, 10, 6): 60.0})}
    assert (
        resolve_models(
            shadow_model="ecmwf_raw",
            sources=("ecmwf_ifs025",),
            target_date=TARGET,
            now=NOW,
            runs=runs,
        )
        is None
    )


def test_a_failed_or_unavailable_run_never_resolves() -> None:
    run_time = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    for status in ("failed", "unavailable"):
        runs = {("ecmwf_ifs025", run_time): _state(status, run_time, {TARGET: 61.0})}
        assert (
            resolve_models(
                shadow_model="ecmwf_raw",
                sources=("ecmwf_ifs025",),
                target_date=TARGET,
                now=NOW,
                runs=runs,
            )
            is None
        )


def test_fusion_averages_only_runs_sharing_an_issue_time() -> None:
    run_time = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    runs = {
        ("ecmwf_ifs025", run_time): _state("ok", run_time, {TARGET: 60.0}),
        ("gfs_seamless", run_time): _state_gfs("ok", run_time, {TARGET: 62.0}),
    }
    resolved = resolve_models(
        shadow_model="fusion",
        sources=("ecmwf_ifs025", "gfs_seamless"),
        target_date=TARGET,
        now=NOW,
        runs=runs,
    )
    assert resolved is not None
    assert resolved.predicted_max_f == pytest.approx(61.0)
    assert resolved.source_models == ("ecmwf_ifs025", "gfs_seamless")


def _state_gfs(status, run_time, daily, *, fetched_at=None):
    """The same state, declared as coming from GFS rather than ECMWF."""
    state = _state(status, run_time, daily, fetched_at=fetched_at)
    state.fetch.model = "gfs_seamless"
    return state


def test_fusion_refuses_a_partial_blend_when_the_issue_times_disagree() -> None:
    """Two different initialisations averaged is not one forecast.

    A 06Z ECMWF run and a 00Z GFS run describe different states of the
    atmosphere; calling their mean "fusion" would fabricate a forecast neither
    model issued. When no shared issue time exists there is no fusion - a
    refusal, not a partial blend.
    """
    ecmwf_run = datetime(2026, 10, 11, 6, 0, tzinfo=UTC)
    gfs_run = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    runs = {
        ("ecmwf_ifs025", ecmwf_run): _state("ok", ecmwf_run, {TARGET: 60.0}),
        ("gfs_seamless", gfs_run): _state_gfs("ok", gfs_run, {TARGET: 63.0}),
    }
    assert (
        resolve_models(
            shadow_model="fusion",
            sources=("ecmwf_ifs025", "gfs_seamless"),
            target_date=TARGET,
            now=NOW,
            runs=runs,
        )
        is None
    )


def test_fusion_falls_back_to_the_newest_issue_time_both_models_have() -> None:
    older = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
    runs = {
        # ECMWF has both; GFS only the older one.
        ("ecmwf_ifs025", newer): _state("ok", newer, {TARGET: 61.0}),
        ("ecmwf_ifs025", older): _state("ok", older, {TARGET: 60.0}),
        ("gfs_seamless", older): _state_gfs("ok", older, {TARGET: 64.0}),
    }
    resolved = resolve_models(
        shadow_model="fusion",
        sources=("ecmwf_ifs025", "gfs_seamless"),
        target_date=TARGET,
        now=NOW,
        runs=runs,
    )
    assert resolved is not None
    assert resolved.run_time_utc == older  # the newest *shared* run
    assert resolved.predicted_max_f == pytest.approx(62.0)


def test_a_model_missing_its_spec_is_refused() -> None:
    cache = ForecastCache(client=None, station="KLGA")  # type: ignore[arg-type]
    assert cache.resolve("nope", TARGET, NOW) is None


# --- fetch policy ----------------------------------------------------------


class _FakeClient:
    def __init__(self, status_by_run: dict[datetime, str] | None = None) -> None:
        self.status_by_run = status_by_run or {}
        self.calls: list[tuple[str, datetime]] = []

    def fetch(self, model: str, run_time: datetime) -> object:
        from weatherquant.weather.single_runs import RunFetch

        self.calls.append((model, run_time))
        status = self.status_by_run.get(run_time, "ok")
        fetch = RunFetch(
            model=model,
            run_time_utc=run_time,
            status=status,
            fetched_at_utc=run_time if status == "ok" else NOW,
        )
        if status == "ok":
            from weatherquant.weather.single_runs import HourlyPoint

            fetch.hourly = [
                HourlyPoint(
                    valid_time_utc=datetime(2026, 10, 12, h, tzinfo=UTC),
                    temperature_2m=60.0,
                )
                for h in range(6)
            ]
        return fetch


def _cache(client) -> ForecastCache:
    cache = ForecastCache(client=client, station="KLGA", target_dates=lambda: [TARGET], clock=lambda: NOW)
    return cache


def test_a_successful_run_is_fetched_once_then_reused() -> None:
    client = _FakeClient()
    cache = _cache(client)
    first = cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=100)
    assert first["fetched"] > 0
    calls = len(client.calls)

    again = cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=100)
    assert again["fetched"] == 0
    assert len(client.calls) == calls


def test_the_per_cycle_budget_caps_upstream_calls() -> None:
    """A burst of new issue hours must not become a burst of HTTP requests."""
    client = _FakeClient()
    cache = _cache(client)
    stats = cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=1)
    assert stats["fetched"] == len(UPSTREAM_MODELS)  # one per model, no more


def test_an_unavailable_run_is_never_re_asked() -> None:
    client = _FakeClient({datetime(2026, 10, 10, 6, 0, tzinfo=UTC): "unavailable"})
    cache = _cache(client)
    cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=100)
    assert any(state.status == "unavailable" for state in cache._runs.values())

    before = len(client.calls)
    cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=6)
    asked_again = {run for _model, run in client.calls[before:]}
    assert datetime(2026, 10, 10, 6, 0, tzinfo=UTC) not in asked_again


def test_a_transport_failure_is_retried_after_the_backoff_not_forever() -> None:
    failing = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)  # publishable at NOW-18h
    client = _FakeClient({failing: "failed"})
    cache = _cache(client)
    cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=100)
    assert any(run == failing for _m, run in client.calls)
    assert _times_asked(client, failing) == 1

    # Inside the cool-down: not re-asked.
    cache.refresh(now=NOW + timedelta(hours=0.5), lookback_hours=48, max_new_per_model=100)
    assert _times_asked(client, failing) == 1

    # Past it: asked again, because "failed" is a transport error, not an answer.
    cache.refresh(
        now=NOW + timedelta(hours=FAILED_RETRY_HOURS + 0.1),
        lookback_hours=48,
        max_new_per_model=100,
    )
    assert _times_asked(client, failing) >= 2


def test_nothing_publishable_exists_so_nothing_is_requested() -> None:
    client = _FakeClient()
    cache = _cache(client)
    # Look back only 1 hour: every issue hour in that window is publishable,
    # so there is nothing to fetch... but more importantly nothing *future*.
    early = datetime(2026, 10, 11, 6, 30, tzinfo=UTC)
    cache.refresh(now=early, lookback_hours=1, max_new_per_model=2)
    assert all(run < early for _model, run in client.calls)


# --- persistence round trip ------------------------------------------------


def _times_asked(client, run_time, *, model: str = "ecmwf_ifs025") -> int:
    """How often *one* model was asked for one run.

    Both upstream models are queried for the same issue hours, so counting
    across models would double a single refresh.
    """
    return sum(1 for m, run in client.calls if run == run_time and m == model)


def test_a_restart_rehydrates_the_cache_without_refetching_anything_it_holds() -> None:
    """Proven by asking: not one run already on disk is requested again."""
    client = _FakeClient()
    cache = _cache(client)
    cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=3)
    run_rows, daily_rows = cache.rows_for_persistence()
    assert run_rows and daily_rows
    held = set(cache.cached_keys)

    revived_client = _FakeClient()
    revived = _cache(revived_client)
    revived.load_cached_rows(run_rows, daily_rows)
    assert revived.cached_keys == held

    revived.refresh(now=NOW, lookback_hours=48, max_new_per_model=100)
    refetched = {run for _model, run in revived_client.calls}
    assert not (held & refetched), sorted(held & refetched)


def test_persisted_rows_carry_the_estimated_basis() -> None:
    cache = _cache(_FakeClient())
    cache.refresh(now=NOW, lookback_hours=48, max_new_per_model=2)
    run_rows, _daily = cache.rows_for_persistence()
    for row in run_rows:
        _model, _run, _status, _http, _err, _sha, _pub, basis, _recv, _n = row
        assert basis == "estimated"


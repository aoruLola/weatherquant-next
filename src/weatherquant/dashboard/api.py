"""The HTTP layer: routes, envelope, rate limit, static SPA.

Thin on purpose - every route is *read sources, wrap in envelope, return*.
The interesting decisions live in :mod:`weatherquant.dashboard.builders`.

Endpoints are read-only GETs with Pydantic response models. There is no
endpoint that opens a database, executes a query, edits a file or returns a
raw archive: the worst a caller can do is ask for a page of data that is
already published for the container probes to read.

Static serving: in the container the built frontend sits in ``static_dir`` and
is served by the same process, so the NAS runs one container, one port. In
dev, Vite proxies ``/api`` here and ``static_dir`` is unset.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from weatherquant.dashboard import API_SCHEMA_VERSION
from weatherquant.dashboard import builders as builders
from weatherquant.dashboard import performance as perf
from weatherquant.dashboard.archive import ArchiveTail
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.contracts import (
    CollectorHealthData,
    CollectorMetricsData,
    DecisionsData,
    Envelope,
    ForecastsData,
    Freshness,
    HealthzData,
    MarketDetailData,
    MarketsData,
    OverviewData,
    PerformanceData,
    ShadowHealthData,
    ShadowReportData,
    SystemData,
)
from weatherquant.dashboard.sources import SnapshotSources, parse_utc
from weatherquant.dashboard.trends import TrendBuffer

log = logging.getLogger("weatherquant.dashboard")

__all__ = ["create_app", "main"]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def create_app(settings: DashboardSettings | None = None) -> FastAPI:
    settings = settings or DashboardSettings()
    state = builders.DashboardState(
        settings=settings,
        sources=SnapshotSources(settings),
        archive=ArchiveTail(settings),
        trends=TrendBuffer(max_samples=settings.trend_history_samples),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        sampler = asyncio.create_task(_sample_loop(state))
        try:
            yield
        finally:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sampler

    app = FastAPI(
        title="WeatherQuant Dashboard",
        description=(
            "Read-only monitoring API over published snapshots. Never opens a "
            "database, never writes anything, never touches a wallet."
        ),
        version=f"{API_SCHEMA_VERSION}.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.wq_state = state

    _install_rate_limit(app, settings)
    _install_api_routes(app, state, settings)
    _install_exception_handlers(app)
    _install_static(app, settings)
    return app


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def _env[T](cls: type[T], api: str, data: T, freshness: Freshness, now: datetime, dataset: str) -> Envelope[T]:
    return Envelope[cls](  # type: ignore[valid-type]
        api=api,
        generated_at_utc=now,
        dataset=dataset,  # type: ignore[arg-type]
        freshness=freshness,
        data=data,
    )


def _install_api_routes(app: FastAPI, state: builders.DashboardState, settings: DashboardSettings) -> None:
    dataset = settings.dataset

    @app.get("/healthz", response_model=Envelope[HealthzData], tags=["ops"])
    async def healthz() -> Envelope[HealthzData]:
        """Dashboard liveness, in the same envelope as every other endpoint.

        200 means *this process* is serving. Source health lives in the
        sources map and the envelope's freshness, deliberately: a dead
        collector must not make Docker restart the dashboard, and a healthy
        dashboard says nothing about upstreams.
        """
        now = _utcnow()
        collector = state.sources.collector_heartbeat(now)
        shadow = state.sources.shadow_heartbeat(now)
        report = state.sources.shadow_report(now)
        named = [
            ("collector", collector.freshness),
            ("shadow", shadow.freshness),
            ("report", report.freshness),
        ]
        severity = {s: i for i, s in enumerate(["fresh", "unavailable", "stale", "missing", "corrupt"])}
        overall = max((f for _, f in named), key=lambda f: severity.get(f.state.value, 0))
        reasons = [f"{name}: {f.reason}" for name, f in named if f.state.value != "fresh" and f.reason]
        combined = Freshness(
            state=overall.state,
            age_seconds=overall.age_seconds,
            as_of_utc=overall.as_of_utc,
            stale_after_seconds=overall.stale_after_seconds,
            reason="; ".join(reasons) or overall.reason,
        )
        data = HealthzData(
            status="ok",
            uptime_seconds=round((now - state.started_at).total_seconds(), 1),
            sources={
                "collector_heartbeat": collector.freshness.state.value,
                "shadow_heartbeat": shadow.freshness.state.value,
                "shadow_report": report.freshness.state.value,
            },
        )
        return _env(HealthzData, "healthz", data, combined, now, dataset)

    @app.get("/api/v1/overview", response_model=Envelope[OverviewData], tags=["v1"])
    async def r_overview() -> Envelope[OverviewData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.overview, state, now)
        return _env(OverviewData, "overview", data, freshness, now, dataset)

    @app.get("/api/v1/collector/health", response_model=Envelope[CollectorHealthData], tags=["v1"])
    async def r_collector_health() -> Envelope[CollectorHealthData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.collector_health, state, now)
        return _env(CollectorHealthData, "collector-health", data, freshness, now, dataset)

    @app.get("/api/v1/collector/metrics", response_model=Envelope[CollectorMetricsData], tags=["v1"])
    async def r_collector_metrics() -> Envelope[CollectorMetricsData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.collector_metrics, state, now)
        return _env(CollectorMetricsData, "collector-metrics", data, freshness, now, dataset)

    @app.get("/api/v1/shadow/health", response_model=Envelope[ShadowHealthData], tags=["v1"])
    async def r_shadow_health() -> Envelope[ShadowHealthData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.shadow_health, state, now)
        return _env(ShadowHealthData, "shadow-health", data, freshness, now, dataset)

    @app.get("/api/v1/shadow/report", response_model=Envelope[ShadowReportData], tags=["v1"])
    async def r_shadow_report() -> Envelope[ShadowReportData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.shadow_report, state, now)
        return _env(ShadowReportData, "shadow-report", data, freshness, now, dataset)

    @app.get("/api/v1/markets", response_model=Envelope[MarketsData], tags=["v1"])
    async def r_markets(
        date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="target date, YYYY-MM-DD"),
        status: str = Query("all", pattern="^(all|tradeable|active|closed)$"),
        q: str | None = Query(None, max_length=120, description="substring of question/bracket"),
        book: str = Query("all", pattern="^(all|fresh|stale|missing)$"),
        decision: str | None = Query(
            None,
            pattern="^(ACCEPT|REJECT|NO_TRADE|DATA_UNAVAILABLE)$",
            description="markets whose LATEST shadow decision is this state (needs market-snapshot)",
        ),
        limit: int = Query(50, ge=1, le=500),
        cursor: int = Query(0, ge=0),
    ) -> Envelope[MarketsData]:
        from datetime import date as date_type

        target = None
        if date:
            try:
                target = date_type.fromisoformat(date)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"invalid date {date!r}") from exc
        now = _utcnow()
        data, freshness = await asyncio.to_thread(
            builders.markets,
            state,
            now,
            limit=limit,
            cursor=cursor,
            target_date=target,
            status=status,
            query=q,
            book_state=book,
            decision=decision,
        )
        return _env(MarketsData, "markets", data, freshness, now, dataset)

    @app.get("/api/v1/markets/{market_id}", response_model=Envelope[MarketDetailData], tags=["v1"])
    async def r_market_detail(market_id: str) -> Envelope[MarketDetailData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.market_detail, state, now, market_id)
        return _env(MarketDetailData, "market-detail", data, freshness, now, dataset)

    @app.get("/api/v1/forecasts", response_model=Envelope[ForecastsData], tags=["v1"])
    async def r_forecasts() -> Envelope[ForecastsData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.forecasts, state, now)
        return _env(ForecastsData, "forecasts", data, freshness, now, dataset)

    @app.get("/api/v1/decisions", response_model=Envelope[DecisionsData], tags=["v1"])
    async def r_decisions(
        state_filter: str | None = Query(None, alias="state", pattern="^(ACCEPT|REJECT|NO_TRADE|DATA_UNAVAILABLE|CANDIDATE)$"),
        mode: str | None = Query(None, pattern="^(LIVE|REPLAY)$"),
        market_id: str | None = Query(None, max_length=200),
        model: str | None = Query(None, max_length=80),
        since: str | None = Query(
            None,
            description="only rows decided at/after this instant (ISO-8601, UTC)",
        ),
        limit: int = Query(50, ge=1, le=500),
        cursor: int = Query(0, ge=0),
    ) -> Envelope[DecisionsData]:
        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"invalid since {since!r}") from exc
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=UTC)
        now = _utcnow()
        data, freshness = await asyncio.to_thread(
            builders.decisions,
            state,
            now,
            limit=limit,
            cursor=cursor,
            decision_state=state_filter,
            mode=mode,
            market_id=market_id,
            model=model,
            since=since_dt,
        )
        return _env(DecisionsData, "decisions", data, freshness, now, dataset)

    @app.get("/api/v1/system", response_model=Envelope[SystemData], tags=["v1"])
    async def r_system() -> Envelope[SystemData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(builders.system, state, now)
        return _env(SystemData, "system", data, freshness, now, dataset)

    # -- performance -------------------------------------------------------
    # All seven read snapshots the research process already computed. None
    # of them opens a database, and none of them derives a figure a second
    # time - see docs/performance-data-contract.md section 7.
    perf_state = perf.PerformanceState(state.sources, settings)

    @app.get("/api/v1/performance/summary", response_model=Envelope[PerformanceData], tags=["v1"])
    async def r_perf_summary() -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(perf.performance_summary, perf_state, now)
        return _env(PerformanceData, "performance/summary", data, freshness, now, dataset)

    @app.get("/api/v1/performance/edge", response_model=Envelope[PerformanceData], tags=["v1"])
    async def r_perf_edge() -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(perf.performance_edge, perf_state, now)
        return _env(PerformanceData, "performance/edge", data, freshness, now, dataset)

    @app.get("/api/v1/performance/win-rate", response_model=Envelope[PerformanceData], tags=["v1"])
    async def r_perf_win_rate() -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(perf.performance_win_rate, perf_state, now)
        return _env(PerformanceData, "performance/win-rate", data, freshness, now, dataset)

    @app.get(
        "/api/v1/performance/opportunities",
        response_model=Envelope[PerformanceData],
        tags=["v1"],
    )
    async def r_perf_opportunities(
        limit: int = Query(50, ge=1, le=500),
        cursor: int = Query(0, ge=0),
    ) -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(
            perf.performance_opportunities, perf_state, now, limit=limit, cursor=cursor
        )
        return _env(PerformanceData, "performance/opportunities", data, freshness, now, dataset)

    @app.get(
        "/api/v1/performance/settlements",
        response_model=Envelope[PerformanceData],
        tags=["v1"],
    )
    async def r_perf_settlements(
        limit: int = Query(50, ge=1, le=500),
        cursor: int = Query(0, ge=0),
    ) -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(
            perf.performance_settlements, perf_state, now, limit=limit, cursor=cursor
        )
        return _env(PerformanceData, "performance/settlements", data, freshness, now, dataset)

    @app.get("/api/v1/performance/models", response_model=Envelope[PerformanceData], tags=["v1"])
    async def r_perf_models() -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(perf.performance_models, perf_state, now)
        return _env(PerformanceData, "performance/models", data, freshness, now, dataset)

    @app.get(
        "/api/v1/performance/reference-capital",
        response_model=Envelope[PerformanceData],
        tags=["v1"],
    )
    async def r_perf_reference_capital(
        capital_usdc: float = Query(100.0, gt=0, le=1_000_000),
        mode: str = Query("portfolio", pattern="^(portfolio|single_opportunity)$"),
        max_allocation_fraction: float = Query(0.10, gt=0, le=1),
    ) -> Envelope[PerformanceData]:
        now = _utcnow()
        data, freshness = await asyncio.to_thread(
            perf.performance_reference_capital,
            perf_state,
            now,
            capital_usdc=capital_usdc,
            mode=mode,
            max_allocation_fraction=max_allocation_fraction,
        )
        return _env(
            PerformanceData, "performance/reference-capital", data, freshness, now, dataset
        )


# ---------------------------------------------------------------------------
# middleware / handlers / static
# ---------------------------------------------------------------------------


def _install_rate_limit(app: FastAPI, settings: DashboardSettings) -> None:
    """A small per-client sliding window over API paths.

    Only the dashboard's own UI polls this server, so the default (600/min)
    is generous; it exists to stop a runaway refresh loop from becoming a load
    test, not to defend against an attacker. 0 disables it.
    """
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return
    hits: dict[str, deque[float]] = {}

    @app.middleware("http")
    async def _limit(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        moment = time.monotonic()
        bucket = hits.setdefault(client, deque())
        while bucket and moment - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": "15"},
            )
        bucket.append(moment)
        if len(hits) > 4096:  # unbounded growth guard for odd clients
            hits.clear()
        return await call_next(request)


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def _boom(request: Request, exc: Exception) -> JSONResponse:
        # Full detail to the server log; a generic body to the client, so a
        # stack trace (with host paths in it) never reaches the network.
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})


def _install_static(app: FastAPI, settings: DashboardSettings) -> None:
    static = settings.static_dir
    if static is None:
        return
    root = static.resolve()
    index = root / "index.html"
    if not index.is_file():
        log.warning("static dir has no index.html; UI will not be served")
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str) -> Any:
        if full_path.startswith(("api/", "healthz")):
            return JSONResponse(status_code=404, content={"detail": "not found"})
        candidate = (root / full_path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            headers = {}
            if full_path.startswith("assets/"):
                # Vite fingerprints these; caching hard is safe.
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return FileResponse(candidate, headers=headers)
        # Unknown path -> the SPA shell, so deep links work on reload.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# background sampling
# ---------------------------------------------------------------------------


async def _sample_loop(state: builders.DashboardState) -> None:
    """Append one honest trend sample every interval, forever."""
    interval = state.settings.trend_sample_seconds
    while True:
        try:
            now = _utcnow()
            collector = state.sources.collector_heartbeat(now).payload
            shadow = state.sources.shadow_heartbeat(now).payload
            report = state.sources.shadow_report(now).payload
            report_as_of = parse_utc((report or {}).get("generated_at_utc"))
            raw_window = (report or {}).get("stale_after_seconds")
            try:
                report_window = float(raw_window) if raw_window is not None else None
            except (TypeError, ValueError):
                report_window = None
            sample = TrendBuffer.collect(
                now=now,
                collector_payload=collector,
                shadow_payload=shadow,
                report_as_of=report_as_of,
                report_stale_after=report_window,
            )
            if len(sample) > 1:  # >1 means at least one value besides the clock
                state.trends.append(sample)
        except Exception:
            log.exception("trend sampling failed")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weatherquant-dashboard", description="WeatherQuant read-only dashboard server.")
    parser.add_argument("--host", default=None, help="bind address (default: WQ_HOST or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="port (default: WQ_PORT or 8787)")
    args = parser.parse_args(argv)

    settings = DashboardSettings()
    host = args.host or settings.host
    port = args.port or settings.port

    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


# Module-level app so `uvicorn weatherquant.dashboard.api:app` also works.
app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())

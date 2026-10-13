"""Assemble API payloads from the published sources.

Everything here is synchronous, side-effect free (reads + caches only) and
returns ``(data, freshness)`` - the route layer wraps it in a thread and puts
it in an envelope. Keeping it out of the routes means the interesting logic is
testable without an HTTP client, and the routes stay a page of wiring.

Decision rules that are easy to break, restated where they happen:

* A verdict from ``evaluate_health`` / ``evaluate_shadow_health`` (the very
  functions the container probes run) is reused rather than reimplemented, so
  the dashboard can never disagree with ``docker ps`` about the same file.
* ``LIVE`` and ``REPLAY`` never merge: every decision aggregate is split by
  mode and labelled, because a replay ACCEPT is not a contemporary signal.
* Missing producers produce ``UNAVAILABLE`` with the integration pointer - a
  plausible-looking zero would be a fabrication.
"""

# Chinese UI copy deliberately uses fullwidth CJK punctuation - correct
# typography for these strings, same rationale as the per-file RUF001
# ignores on scripts/forecast_coverage.py and scripts/baseline_report.py.
# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from weatherquant.dashboard.archive import ArchiveResult, ArchiveTail
from weatherquant.dashboard.config import DashboardSettings
from weatherquant.dashboard.contracts import (
    BookQuote,
    CollectorHealthData,
    CollectorMetricsData,
    DecisionRow,
    DecisionsData,
    ForecastDistribution,
    ForecastModelInfo,
    ForecastsData,
    Freshness,
    FreshnessState,
    MarketDetailData,
    MarketsData,
    MarketSummary,
    OverviewData,
    ShadowHealthData,
    ShadowReportData,
    SystemData,
)
from weatherquant.dashboard.sources import SnapshotSources, age_seconds, parse_utc
from weatherquant.dashboard.trends import COLLECTOR_COUNTER_KEYS, TrendBuffer
from weatherquant.data.heartbeat import evaluate_health, read_heartbeat
from weatherquant.markets.models import Side
from weatherquant.shadow.health import evaluate_shadow_health
from weatherquant.weather.daily_max import local_day_bounds_utc

#: What the decision aggregates cover, for the case where the report predates
#: the field. Nothing in the shadow report filters on run_id and nothing
#: deletes decisions, so these totals span the database, not the process.
_DATABASE_LIFETIME_WINDOW = (
    "cumulative over this shadow database's lifetime; a restart does not reset it"
)

__all__ = [
    "DashboardState",
    "collector_health",
    "collector_metrics",
    "decisions",
    "forecasts",
    "market_detail",
    "markets",
    "overview",
    "shadow_health",
    "shadow_report",
    "system",
]

_NO_PRODUCER = (
    "detail data awaits the shadow-side snapshot producer - see "
    "docs/dashboard-data-contract.md §4 for the integration contract"
)


@dataclass
class DashboardState:
    """Everything a builder needs. One instance per running server."""

    settings: DashboardSettings
    sources: SnapshotSources
    archive: ArchiveTail
    trends: TrendBuffer
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def _worst(freshness: Freshness, other: Freshness) -> Freshness:
    severity = {
        FreshnessState.FRESH: 0,
        FreshnessState.UNAVAILABLE: 1,
        FreshnessState.STALE: 2,
        FreshnessState.MISSING: 3,
        FreshnessState.CORRUPT: 4,
    }
    if severity[other.state] > severity[freshness.state]:
        return other
    return freshness


def _combined(named: list[tuple[str, Freshness]]) -> Freshness:
    """Worst verdict across sources, with every non-fresh reason listed.

    Seeded from the *first* source rather than an empty shell: an all-fresh
    combination must still carry an ``as_of`` age, or the UI's freshness pill
    would have nothing to count down from.
    """
    if not named:
        return Freshness(state=FreshnessState.FRESH)
    overall = named[0][1].model_copy(deep=True)
    reasons: list[str] = []
    for name, item in named:
        if _worst(overall, item) is not overall:
            overall = item.model_copy(deep=True)
        if item.state is not FreshnessState.FRESH and item.reason:
            reasons.append(f"{name}: {item.reason}")
    if reasons and overall.state is FreshnessState.FRESH:
        overall.state = FreshnessState.STALE
    overall.reason = "; ".join(reasons) if reasons else None
    return overall


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


def collector_health(state: DashboardState, now: datetime) -> tuple[CollectorHealthData, Freshness]:
    source = state.sources.collector_heartbeat(now)
    if source.payload is None:
        return (
            CollectorHealthData(present=False, reason=source.reason),
            source.freshness,
        )

    payload = source.payload
    # The probe verdict is recomputed with the *same* function the container
    # healthcheck runs, so this panel and `docker ps` can never disagree.
    heartbeat = read_heartbeat(state.settings.collector_health_path)
    verdict = evaluate_health(state.settings.collector_health_path, now=now)

    def ts(key: str) -> datetime | None:
        return parse_utc(payload.get(key))

    stats_raw = payload.get("stats") or {}
    stats = {k: int(v) for k, v in stats_raw.items() if isinstance(v, (int, float))} if isinstance(stats_raw, dict) else {}

    data = CollectorHealthData(
        present=True,
        health_status=str(getattr(verdict.status, "value", verdict.status)),
        health_reasons=list(verdict.reasons),
        process_status=str(payload.get("status") or "unknown"),
        run_id=payload.get("run_id"),
        app_version=payload.get("app_version"),
        hostname=payload.get("hostname"),
        uptime_seconds=_num(payload.get("uptime_seconds")),
        started_at_utc=ts("started_at_utc"),
        updated_at_utc=ts("updated_at_utc"),
        last_upstream_contact_utc=ts("last_upstream_contact_utc"),
        upstream_contact_age_seconds=age_seconds(ts("last_upstream_contact_utc"), now),
        last_data_change_utc=ts("last_data_change_utc"),
        data_change_age_seconds=age_seconds(ts("last_data_change_utc"), now),
        ws_connected=payload.get("ws_connected"),
        ws_last_message_utc=ts("ws_last_message_utc"),
        ws_message_age_seconds=age_seconds(ts("ws_last_message_utc"), now),
        subscribed_tokens=_int(payload.get("subscribed_tokens")),
        rest_last_success_utc=ts("rest_last_success_utc"),
        rest_success_age_seconds=age_seconds(ts("rest_last_success_utc"), now),
        rest_consecutive_failures=_int(payload.get("rest_consecutive_failures")),
        stats=stats,
        db_bytes=_int(payload.get("db_bytes")),
        raw_bytes=_int(payload.get("raw_bytes")),
        disk_free_percent=_num(payload.get("disk_free_percent")),
        disk_total_bytes=_int(payload.get("disk_total_bytes")),
        last_error=payload.get("last_error"),
        last_error_utc=ts("last_error_utc"),
        last_defect=payload.get("last_defect"),
        fatal_error=payload.get("fatal_error"),
        telemetry=payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {},
    )
    # A parse failure inside read_heartbeat means the file is there but not a
    # heartbeat we can stand behind - say so rather than showing defaults.
    if heartbeat is None:
        data.reason = "collector heartbeat exists but is not a valid heartbeat document"
        freshness = Freshness(
            state=FreshnessState.CORRUPT,
            reason=data.reason,
            as_of_utc=None,
        )
        return data, freshness
    return data, source.freshness


def collector_metrics(state: DashboardState, now: datetime) -> tuple[CollectorMetricsData, Freshness]:
    health, freshness = collector_health(state, now)
    if not health.present:
        return CollectorMetricsData(present=False, reason=health.reason), freshness
    trends = {key: state.trends.series(key) for key in COLLECTOR_COUNTER_KEYS}
    trends = {k: v for k, v in trends.items() if v}
    return (
        CollectorMetricsData(
            present=True,
            counters=dict(health.stats),
            trends=trends,
            trend_window_seconds=state.trends.window_seconds(),
            updated_at_utc=health.updated_at_utc,
        ),
        freshness,
    )


# ---------------------------------------------------------------------------
# shadow
# ---------------------------------------------------------------------------


def shadow_health(state: DashboardState, now: datetime) -> tuple[ShadowHealthData, Freshness]:
    source = state.sources.shadow_heartbeat(now)
    if source.payload is None:
        return ShadowHealthData(present=False, reason=source.reason), source.freshness

    payload = source.payload
    verdict = evaluate_shadow_health(state.settings.shadow_health_path, now=now)
    started = parse_utc(payload.get("started_at_utc"))
    updated = parse_utc(payload.get("updated_at_utc"))
    last_decision = parse_utc(payload.get("last_decision_at_utc"))
    disk = payload.get("disk") if isinstance(payload.get("disk"), dict) else {}
    cycle = payload.get("last_cycle") if isinstance(payload.get("last_cycle"), dict) else None
    uptime = age_seconds(started, now)

    data = ShadowHealthData(
        present=True,
        health_status=verdict.status,
        health_reasons=list(verdict.reasons),
        process_status=str(payload.get("status") or "unknown"),
        run_id=payload.get("run_id"),
        updated_at_utc=updated,
        started_at_utc=started,
        uptime_seconds=max(uptime, 0.0) if uptime is not None else None,
        last_decision_at_utc=last_decision,
        last_decision_age_seconds=age_seconds(last_decision, now),
        collector_last_seen_utc=payload.get("collector_last_seen_utc"),
        cycles=_int(payload.get("cycles")),
        mode=payload.get("mode"),
        configured_mode=payload.get("configured_mode"),
        primary_model=payload.get("primary_model"),
        thresholds_version=payload.get("thresholds_version"),
        fatal_error=payload.get("fatal_error"),
        stop_reason=payload.get("stop_reason"),
        disk_free_percent=_num(disk.get("free_percent")) if disk else None,
        last_cycle=cycle,
        app_version=payload.get("app_version"),
        image_tag=payload.get("image_tag"),
        db_bytes=_int(payload.get("db_bytes")),
    )
    if verdict.status == "unhealthy":
        data.reason = "; ".join(verdict.reasons) or None
    return data, source.freshness


def shadow_report(state: DashboardState, now: datetime) -> tuple[ShadowReportData, Freshness]:
    source = state.sources.shadow_report(now)
    if source.payload is None:
        return ShadowReportData(present=False, reason=source.reason), source.freshness

    payload = source.payload
    data = ShadowReportData(
        present=True,
        generated_at_utc=_parse_any(payload.get("generated_at_utc")),
        source=payload.get("source"),
        schema_version=_int(payload.get("schema_version")),
        stale_after_seconds=_num(payload.get("stale_after_seconds")),
        run=_dict(payload.get("run")),
        decisions=_dict(payload.get("decisions")),
        theoretical_edge=_dict(payload.get("theoretical_edge")),
        prediction_quality=_dict(payload.get("prediction_quality")),
        liquidity=_dict(payload.get("liquidity")),
        coverage=_dict(payload.get("coverage")),
        liveness=_dict(payload.get("liveness")),
        data_quality=_dict(payload.get("data_quality")),
        warnings=[str(w) for w in payload.get("warnings", []) if isinstance(w, str)]
        if isinstance(payload.get("warnings"), list)
        else [],
        equity_curve=payload.get("equity_curve"),
        equity_curve_note=payload.get("equity_curve_note"),
    )
    return data, source.freshness


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------


def _book_quote(observation, now: datetime, book_stale_seconds: float) -> BookQuote:
    book = observation.book
    as_of = observation.venue_ts or observation.collector_received_at_utc
    age = age_seconds(as_of, now)
    stale = age is None or age > book_stale_seconds
    defects = book.defects()
    if age is None:
        reason = "book has no usable timestamp"
    elif stale:
        reason = f"book is {age:.0f}s old (limit {book_stale_seconds:.0f}s)"
    elif defects:
        reason = ", ".join(defects)
    else:
        reason = None
    return BookQuote(
        available=True,
        best_bid=book.best_bid_usd_per_share,
        best_ask=book.best_ask_usd_per_share,
        spread=book.spread_usd_per_share,
        bid_depth_usd=book.depth_notional_usd(Side.SELL),
        ask_depth_usd=book.depth_notional_usd(Side.BUY),
        levels_bid=len(book.bids),
        levels_ask=len(book.asks),
        defects=defects,
        quality="ok" if not defects else "incomplete",
        stale=stale,
        age_seconds=age,
        venue_ts_utc=observation.venue_ts,
        received_at_utc=observation.collector_received_at_utc,
        source="archive-tail",
        reason=reason,
    )


def _target_day_ended(target_date: date | None, now: datetime) -> bool:
    """Has this market's civil day in New York already finished?

    The answer must come from the market's own timezone, not from the
    dashboard host's clock: a machine in UTC+8 crosses midnight twelve hours
    before New York does, and a label that fires early is a label nobody can
    trust. ``local_day_bounds_utc`` is the same helper the settlement path
    uses, so "the day ended" means the same thing everywhere.
    """
    if target_date is None:
        return False
    _start, end = local_day_bounds_utc(target_date)
    return now >= end


def _summary_from_catalog(
    market,
    book: BookQuote | None,
    now: datetime,
    *,
    model_reason: str,
) -> MarketSummary:
    target = market.target_date if isinstance(market.target_date, date) else None
    return MarketSummary(
        market_id=market.market_id,
        event_id=market.event_id,
        question=market.question,
        target_date=target,
        target_day_ended=_target_day_ended(target, now),
        bracket_label=market.bracket_label,
        bracket_low=market.bracket.low if market.bracket else None,
        bracket_high=market.bracket.high if market.bracket else None,
        station=market.station,
        active=market.active,
        closed=market.closed,
        accepting_orders=market.accepting_orders,
        tradeable=market.tradeable,
        token_yes=market.token_yes,
        book=book,
        model_probability=None,
        net_edge_usd=None,
        last_decision=None,
        model_source="unavailable",
        model_reason=model_reason,
        updated_at_utc=book.received_at_utc if book else None,
    )


def _producer_markets(payload: dict[str, Any], now: datetime, book_stale_seconds: float) -> list[MarketSummary]:
    """Adapt a published market-snapshot.json (contract §3) to summaries."""
    items: list[MarketSummary] = []
    raw_items = payload.get("data", {}).get("markets", []) if isinstance(payload.get("data"), dict) else []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        book_raw = entry.get("book") if isinstance(entry.get("book"), dict) else None
        book = None
        if book_raw is not None:
            age = age_seconds(_parse_any(book_raw.get("as_of_utc")), now)
            book = BookQuote(
                available=bool(book_raw.get("available", True)),
                best_bid=_num(book_raw.get("best_bid")),
                best_ask=_num(book_raw.get("best_ask")),
                spread=_num(book_raw.get("spread")),
                bid_depth_usd=_num(book_raw.get("bid_depth_usd")),
                ask_depth_usd=_num(book_raw.get("ask_depth_usd")),
                levels_bid=_int(book_raw.get("levels_bid")) or 0,
                levels_ask=_int(book_raw.get("levels_ask")) or 0,
                defects=[str(d) for d in book_raw.get("defects", [])]
                if isinstance(book_raw.get("defects"), list)
                else [],
                quality=str(book_raw.get("quality") or "ok"),
                stale=(age is None) or age > book_stale_seconds,
                age_seconds=age,
                venue_ts_utc=_parse_any(book_raw.get("venue_ts_utc")),
                received_at_utc=_parse_any(book_raw.get("as_of_utc")),
                source="market-snapshot",
                reason=book_raw.get("reason"),
            )
        model_prob = _num(entry.get("model_probability"))
        target = _parse_date(entry.get("target_date"))
        items.append(
            MarketSummary(
                market_id=str(entry.get("market_id") or ""),
                event_id=entry.get("event_id"),
                question=str(entry.get("question") or ""),
                target_date=target,
                target_day_ended=_target_day_ended(target, now),
                bracket_label=str(entry.get("bracket_label") or ""),
                bracket_low=_num(entry.get("bracket_low")),
                bracket_high=_num(entry.get("bracket_high")),
                station=entry.get("station"),
                active=bool(entry.get("active", False)),
                closed=bool(entry.get("closed", False)),
                accepting_orders=bool(entry.get("accepting_orders", False)),
                tradeable=bool(entry.get("tradeable", False)),
                token_yes=entry.get("token_yes"),
                book=book,
                model_probability=model_prob,
                net_edge_usd=_num(entry.get("net_edge_usd")),
                last_decision=entry.get("last_decision")
                if isinstance(entry.get("last_decision"), dict)
                else None,
                model_source="market-snapshot",
                model_reason=None,
                updated_at_utc=_parse_any(entry.get("updated_at_utc")),
            )
        )
    return items


def markets(
    state: DashboardState,
    now: datetime,
    *,
    limit: int = 50,
    cursor: int = 0,
    target_date: date | None = None,
    status: str = "all",
    query: str | None = None,
    book_state: str = "all",
    decision: str | None = None,
) -> tuple[MarketsData, Freshness]:
    producer = state.sources.named_snapshot("market-snapshot", "market-snapshot.json", now)
    if producer.payload is not None:
        items = _producer_markets(producer.payload, now, state.settings.book_stale_seconds)
        source, source_status = "market-snapshot", producer.freshness.state
        reason = producer.freshness.reason
        archive: ArchiveResult | None = None
    else:
        archive = state.archive.read(now)
        source, source_status = "archive-tail", archive.freshness.state
        reason = archive.freshness.reason
        items = []
        if archive.freshness.state not in (FreshnessState.MISSING, FreshnessState.CORRUPT):
            model_reason = _NO_PRODUCER
            for market in archive.catalog.values():
                observation = archive.books.get(market.token_yes)
                quote = (
                    _book_quote(observation, now, state.settings.book_stale_seconds)
                    if observation
                    else BookQuote(
                        available=False,
                        quality="missing",
                        stale=True,
                        source="archive-tail",
                        reason="no book for this token in the archive tail",
                    )
                )
                items.append(_summary_from_catalog(market, quote, now, model_reason=model_reason))
        elif archive.freshness.state is FreshnessState.CORRUPT:
            reason = archive.freshness.reason

    items = _filter_markets(
        items,
        target_date=target_date,
        status=status,
        query=query,
        book_state=book_state,
        decision=decision,
    )
    items.sort(key=lambda m: (m.target_date or date.max, m.market_id))
    total = len(items)
    window = items[cursor : cursor + limit]
    next_cursor = str(cursor + limit) if cursor + limit < total else None
    data = MarketsData(
        items=window,
        total=total,
        next_cursor=next_cursor,
        source=source,
        source_status=source_status,
        reason=reason if source_status is not FreshnessState.FRESH else None,
        filters_applied={
            "date": target_date.isoformat() if target_date else None,
            "status": status,
            "q": query,
            "book": book_state,
            "decision": decision,
            "limit": limit,
            "cursor": cursor,
        },
    )
    return data, producer.freshness if producer.payload is not None else (archive.freshness if archive else Freshness(state=FreshnessState.MISSING))


def _filter_markets(
    items: list[MarketSummary],
    *,
    target_date: date | None,
    status: str,
    query: str | None,
    book_state: str,
    decision: str | None = None,
) -> list[MarketSummary]:
    out = items
    if target_date is not None:
        out = [m for m in out if m.target_date == target_date]
    if status == "tradeable":
        out = [m for m in out if m.tradeable]
    elif status == "active":
        out = [m for m in out if m.active and not m.closed]
    elif status == "closed":
        out = [m for m in out if m.closed]
    if query:
        needle = query.lower()
        out = [
            m
            for m in out
            if needle in m.question.lower() or needle in m.bracket_label.lower()
        ]
    if book_state == "fresh":
        out = [m for m in out if m.book and not m.book.stale and m.book.available]
    elif book_state == "stale":
        out = [m for m in out if m.book and m.book.stale and m.book.available]
    elif book_state == "missing":
        out = [m for m in out if not m.book or not m.book.available]
    if decision:
        # "最近一次决策是 X 的市场" - needs the producer snapshot; without it
        # every market lacks a decision and the filter correctly returns none.
        out = [
            m
            for m in out
            if m.last_decision
            and str(m.last_decision.get("state", "")).upper() == decision.upper()
        ]
    return out


def market_detail(state: DashboardState, now: datetime, market_id: str) -> tuple[MarketDetailData, Freshness]:
    data, freshness = markets(state, now, limit=500, cursor=0)
    match = next((m for m in data.items if m.market_id == market_id), None)
    if match is None:
        return (
            MarketDetailData(reason=f"market {market_id} not found in the current snapshot window"),
            freshness,
        )

    depth: dict[str, Any] | None = None
    if match.book and match.book.available:
        depth = {
            "levels_bid": match.book.levels_bid,
            "levels_ask": match.book.levels_ask,
            "bid_depth_usd": match.book.bid_depth_usd,
            "ask_depth_usd": match.book.ask_depth_usd,
            "note": "notional across all levels present in the last snapshot; quotes are not executable prices",
        }

    decisions = state.sources.named_snapshot("recent-decisions", "recent-decisions.json", now)
    recent: list[dict[str, Any]] = []
    if decisions.payload is not None:
        rows = decisions.payload.get("data", {}).get("decisions", [])
        if isinstance(rows, list):
            recent = [
                r for r in rows if isinstance(r, dict) and r.get("market_id") == market_id
            ][:50]
        status, reason = decisions.freshness.state, decisions.freshness.reason
    else:
        status, reason = FreshnessState.UNAVAILABLE, decisions.reason

    return (
        MarketDetailData(
            market=match,
            depth=depth,
            recent_decisions=recent,
            recent_decisions_status=status,
            recent_decisions_reason=reason,
        ),
        freshness,
    )


# ---------------------------------------------------------------------------
# forecasts
# ---------------------------------------------------------------------------


def forecasts(state: DashboardState, now: datetime) -> tuple[ForecastsData, Freshness]:
    report, report_freshness = shadow_report(state, now)
    shadow, _shadow_freshness = shadow_health(state, now)
    primary = shadow.primary_model

    models: list[ForecastModelInfo] = []
    target_dates: list[date] = []
    coverage: dict[str, Any] | None = None

    if report.present:
        by_model = (report.prediction_quality or {}).get("by_model") or {}
        tracked = (report.coverage or {}).get("models_tracked") or []
        names = sorted(set(by_model) | set(tracked))
        for name in names:
            stats = by_model.get(name) if isinstance(by_model, dict) else None
            stats = stats if isinstance(stats, dict) else {}
            models.append(
                ForecastModelInfo(
                    name=str(name),
                    is_primary=(name == primary),
                    predictions=int(stats.get("predictions") or 0),
                    latest_run_utc=_parse_any(stats.get("latest_run")),
                    training_cutoff_utc=_parse_any(stats.get("training_cutoff_utc")),
                    version=stats.get("latest_version") or stats.get("model_version"),
                )
            )
        for raw_date in (report.coverage or {}).get("open_target_dates") or []:
            parsed = _parse_date(raw_date)
            if parsed:
                target_dates.append(parsed)
        coverage = report.coverage

    if not models and primary:
        models = [ForecastModelInfo(name=primary, is_primary=True, predictions=0)]

    producer = state.sources.named_snapshot("forecast-snapshot", "forecast-snapshot.json", now)
    distributions: list[ForecastDistribution] = []
    if producer.payload is not None:
        raw = producer.payload.get("data", {}).get("distributions", [])
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                buckets = [
                    {
                        "label": str(b.get("label")),
                        "low": _num(b.get("low")),
                        "high": _num(b.get("high")),
                        "probability": _num(b.get("probability")) or 0.0,
                    }
                    for b in entry.get("buckets", [])
                    if isinstance(b, dict)
                ]
                distributions.append(
                    ForecastDistribution(
                        model=str(entry.get("model") or ""),
                        version=entry.get("version"),
                        target_date=_parse_date(entry.get("target_date")),
                        forecast_run_time_utc=_parse_any(entry.get("forecast_run_time_utc")),
                        training_cutoff_utc=_parse_any(entry.get("training_cutoff_utc")),
                        forecast_received_at_utc=_parse_any(entry.get("forecast_received_at_utc")),
                        predicted_max_f=_num(entry.get("predicted_max_f")),
                        station=entry.get("station"),
                        buckets=buckets,
                    )
                )
        dist_status, dist_reason = producer.freshness.state, producer.freshness.reason
    else:
        dist_status, dist_reason = FreshnessState.UNAVAILABLE, producer.reason

    data = ForecastsData(
        station=state.settings.station,
        target_dates=sorted(target_dates),
        models=models,
        coverage=coverage,
        distributions=distributions,
        distributions_status=dist_status,
        distributions_reason=dist_reason,
        observation_status=FreshnessState.UNAVAILABLE,
        observation_reason=(
            "实际观测与官方结算数据暂未接入；结算由研究管线回溯评分，"
            "不会在决策时点被当作已知信息"
        ),
        reason=None if report.present else report.reason,
    )
    return data, _worst(report_freshness, producer.freshness if producer.payload else Freshness(state=FreshnessState.UNAVAILABLE, reason=dist_reason))


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


def decisions(
    state: DashboardState,
    now: datetime,
    *,
    limit: int = 50,
    cursor: int = 0,
    decision_state: str | None = None,
    mode: str | None = None,
    market_id: str | None = None,
    model: str | None = None,
    since: datetime | None = None,
) -> tuple[DecisionsData, Freshness]:
    report, report_freshness = shadow_report(state, now)
    shadow, shadow_freshness = shadow_health(state, now)
    producer = state.sources.named_snapshot("recent-decisions", "recent-decisions.json", now)

    # --- aggregate totals (always attempted; independent of the row list) ---
    if report.present and isinstance(report.decisions, dict):
        dec = report.decisions
        # Passed through whole, not field-by-field. This block used to
        # whitelist keys, which silently dropped `accept_scope` and `by_model`
        # - the report published them and the Decisions page said "unavailable"
        # for weeks, because a whitelist can only ever be as complete as the
        # day it was written. Anything the report considers part of a decision
        # aggregate is part of one here too.
        totals: dict[str, Any] = dict(dec)
        totals.setdefault("by_state", {})
        totals.setdefault("by_mode", {})
        totals.setdefault("primary_reasons", {})
        # Taken from the report, never restated here: the scope is a fact
        # about how the numbers were counted, and a second copy of the
        # sentence is how the two drift apart.
        totals["window"] = dec.get("window") or _DATABASE_LIFETIME_WINDOW
        totals["runs_in_database"] = dec.get("runs_in_database")
        totals_status = report_freshness.state
        totals_source = "shadow-report"
    else:
        totals = None
        totals_status = FreshnessState.MISSING
        totals_source = None

    last_cycle = shadow.last_cycle if shadow.present else None

    # --- row window --------------------------------------------------------
    rows: list[DecisionRow] = []
    total_rows: int | None = None
    next_cursor: str | None = None
    if producer.payload is not None:
        raw = producer.payload.get("data", {}).get("decisions", [])
        raw = raw if isinstance(raw, list) else []
        adapted = [_row_from_entry(entry) for entry in raw if isinstance(entry, dict)]
        adapted = _filter_rows(
            adapted,
            decision_state=decision_state,
            mode=mode,
            market_id=market_id,
            model=model,
            since=since,
        )
        adapted.sort(key=lambda r: (r.decided_at_utc or datetime.min.replace(tzinfo=UTC)), reverse=True)
        total_rows = len(adapted)
        window = adapted[cursor : cursor + limit]
        rows = window
        next_cursor = str(cursor + limit) if cursor + limit < total_rows else None
        rows_status, rows_reason = producer.freshness.state, producer.freshness.reason
    else:
        rows_status, rows_reason = FreshnessState.UNAVAILABLE, producer.reason

    data = DecisionsData(
        rows=rows,
        rows_status=rows_status,
        rows_reason=rows_reason,
        total_rows=total_rows,
        next_cursor=next_cursor,
        totals=totals,
        totals_status=totals_status,
        totals_source=totals_source,
        last_cycle=last_cycle,
        last_cycle_status=(
            shadow_freshness.state
            if (last_cycle or not shadow.present)
            else FreshnessState.MISSING
        ),
        filters_applied={
            "state": decision_state,
            "mode": mode,
            "market_id": market_id,
            "model": model,
            "since": since.isoformat() if since else None,
            "limit": limit,
            "cursor": cursor,
        },
    )
    overall = _combined(
        [
            ("shadow-report", report_freshness),
            ("shadow-heartbeat", shadow_freshness),
            ("recent-decisions", producer.freshness),
        ]
    )
    return data, overall


def _row_from_entry(entry: dict[str, Any]) -> DecisionRow:
    return DecisionRow(
        decision_id=str(entry.get("decision_id") or ""),
        decided_at_utc=_parse_any(entry.get("decided_at_utc")),
        market_id=str(entry.get("market_id") or ""),
        market_question=entry.get("market_question"),
        bracket_label=entry.get("bracket_label"),
        target_date=_parse_date(entry.get("target_date")),
        direction=entry.get("direction"),
        state=str(entry.get("state") or ""),
        mode=entry.get("mode"),
        model_name=entry.get("model_name"),
        model_version=entry.get("model_version"),
        model_probability=_num(entry.get("model_probability")),
        market_price=_num(entry.get("market_price")),
        best_bid=_num(entry.get("best_bid")),
        best_ask=_num(entry.get("best_ask")),
        net_edge_usd=_num(entry.get("net_edge_usd")),
        primary_reason=entry.get("primary_reason"),
        reasons=[str(r) for r in entry.get("reasons", [])] if isinstance(entry.get("reasons"), list) else [],
        snapshot_age_ms=_int(entry.get("snapshot_age_ms")),
        forecast_age_hours=_num(entry.get("forecast_age_hours")),
        available_depth_usd=_num(entry.get("available_depth_usd")),
        thresholds_version=entry.get("thresholds_version"),
        run_id=entry.get("run_id"),
    )


def _filter_rows(
    rows: list[DecisionRow],
    *,
    decision_state: str | None,
    mode: str | None,
    market_id: str | None,
    model: str | None,
    since: datetime | None = None,
) -> list[DecisionRow]:
    out = rows
    if decision_state:
        out = [r for r in out if r.state.upper() == decision_state.upper()]
    if mode:
        out = [r for r in out if (r.mode or "").upper() == mode.upper()]
    if market_id:
        out = [r for r in out if r.market_id == market_id]
    if model:
        out = [r for r in out if r.model_name == model]
    if since is not None:
        # A row without a timestamp cannot prove it is inside the window, so
        # it drops out rather than silently counting as recent.
        out = [
            r
            for r in out
            if r.decided_at_utc is not None and r.decided_at_utc >= since
        ]
    return out


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def overview(state: DashboardState, now: datetime) -> tuple[OverviewData, Freshness]:
    collector, collector_freshness = collector_health(state, now)
    shadow, shadow_freshness = shadow_health(state, now)
    report, report_freshness = shadow_report(state, now)

    # Markets view is optional here: overview must stay useful even when the
    # archive is not mounted, so failures degrade to None rather than raise.
    mk, archive_freshness = markets(state, now, limit=200, cursor=0)
    tradeable = sum(1 for m in mk.items if m.tradeable)

    if report.present and isinstance(report.coverage, dict) and report.coverage.get("open_markets") is not None:
        active_markets: int | None = int(report.coverage["open_markets"])
        active_source = "shadow-report"
    elif mk.source_status in (FreshnessState.FRESH, FreshnessState.STALE):
        active_markets = tradeable
        active_source = "archive-tail"
    else:
        active_markets = None
        active_source = None

    latest_book_age = None
    if report.present and isinstance(report.liveness, dict):
        latest_book_age = _num(report.liveness.get("latest_orderbook_venue_age_seconds"))
    if latest_book_age is None and archive_freshness.age_seconds is not None:
        latest_book_age = archive_freshness.age_seconds

    decision_totals: dict[str, Any] | None = None
    totals_status = FreshnessState.MISSING
    totals_source = None
    if report.present and isinstance(report.decisions, dict):
        dec = report.decisions
        # Same passthrough as `decisions()` above: no field whitelist, for the
        # same reason. See docs/p2-0.1-audit.md - a whitelist dropped
        # accept_scope while the report happily published it.
        decision_totals = dict(dec)
        decision_totals.setdefault("by_mode", {})
        decision_totals["window"] = dec.get("window") or _DATABASE_LIFETIME_WINDOW
        totals_status = report_freshness.state
        totals_source = "shadow-report"

    # Model-vs-market rows: only a producer snapshot carries model probability.
    model_rows: list[MarketSummary] = []
    producer = state.sources.named_snapshot("market-snapshot", "market-snapshot.json", now)
    if producer.payload is not None:
        model_rows = [m for m in _producer_markets(producer.payload, now, state.settings.book_stale_seconds) if m.model_probability is not None]
        mvm_status = producer.freshness.state
        mvm_reason = producer.freshness.reason
    else:
        mvm_status = FreshnessState.UNAVAILABLE
        mvm_reason = producer.reason

    system_health = _system_health(collector, shadow, report, latest_book_age)

    data = OverviewData(
        collector=collector,
        shadow=shadow,
        report=report,
        active_markets=active_markets,
        active_markets_source=active_source,
        subscribed_tokens=collector.subscribed_tokens if collector.present else None,
        latest_book_age_seconds=latest_book_age,
        current_cycle_decisions=shadow.last_cycle if shadow.present else None,
        decision_totals=decision_totals,
        decision_totals_source=totals_source,
        decision_totals_status=totals_status,
        model_vs_market=model_rows,
        model_vs_market_status=mvm_status,
        model_vs_market_reason=mvm_reason,
        system_health=system_health,
        reason=None,
    )
    overall = _combined(
        [
            ("collector", collector_freshness),
            ("shadow", shadow_freshness),
            ("report", report_freshness),
            ("archive", archive_freshness),
        ]
    )
    return data, overall


def _system_health(
    collector: CollectorHealthData,
    shadow: ShadowHealthData,
    report: ShadowReportData,
    latest_book_age: float | None,
) -> dict[str, Any]:
    rest = {
        "state": "ok",
        "consecutive_failures": collector.rest_consecutive_failures,
        "last_success_age_seconds": collector.rest_success_age_seconds,
        "reason": None,
    }
    if not collector.present:
        rest["state"] = "unknown"
        rest["reason"] = collector.reason
    elif collector.rest_consecutive_failures and collector.rest_consecutive_failures >= 10:
        rest["state"] = "degraded"
        rest["reason"] = f"{collector.rest_consecutive_failures} consecutive REST failures"

    ws: dict[str, Any] = {
        "state": "unknown",
        "connected": collector.ws_connected,
        "message_age_seconds": collector.ws_message_age_seconds,
        "reason": None,
    }
    if collector.present:
        if collector.ws_connected is False:
            ws["state"] = "down"
            ws["reason"] = "websocket not connected"
        elif collector.ws_connected is True:
            ws["state"] = "ok"
        else:
            ws["reason"] = "not reported by the heartbeat"
        queue = collector.telemetry.get("ws_queue")
        if isinstance(queue, dict) and queue:
            # Collector >= v0.1.2: the queue numbers themselves, not a note
            # about their absence.
            ws["queue"] = queue

    data_collection: dict[str, Any] = {
        "upstream_contact_age_seconds": collector.upstream_contact_age_seconds,
        "book_age_seconds": latest_book_age,
        "collector_silence_seconds": (report.liveness or {}).get("collector_silence_seconds")
        if report.present
        else None,
    }

    storage: dict[str, Any] = {
        "collector_db_bytes": collector.db_bytes,
        "collector_raw_bytes": collector.raw_bytes,
        "disk_free_percent": collector.disk_free_percent,
        "shadow_db_bytes": shadow.db_bytes,
        # Absent until the shadow heartbeat carries the field. Saying
        # "暂未接入" while a number is on screen would be the lie the note
        # exists to prevent, so the reason only appears with a null.
        "shadow_db_reason": (
            None
            if shadow.db_bytes is not None
            else "shadow heartbeat does not report a database size (暂未接入)"
        ),
    }

    recent_errors: list[dict[str, Any]] = []
    if collector.last_error:
        recent_errors.append(
            {
                "source": "collector",
                "at_utc": collector.last_error_utc.isoformat() if collector.last_error_utc else None,
                "message": collector.last_error,
            }
        )
    if collector.fatal_error:
        recent_errors.append({"source": "collector", "at_utc": None, "message": collector.fatal_error})
    if shadow.fatal_error:
        recent_errors.append({"source": "shadow", "at_utc": None, "message": shadow.fatal_error})
    for warning in report.warnings[:5]:
        recent_errors.append({"source": "shadow-report", "at_utc": None, "message": warning})

    return {
        "rest": rest,
        "ws": ws,
        "data_collection": data_collection,
        "storage": storage,
        "recent_events": recent_errors,
        "shadow_mode": shadow.mode,
        "shadow_health": shadow.health_status,
        "collector_health": collector.health_status,
    }


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def system(state: DashboardState, now: datetime) -> tuple[SystemData, Freshness]:
    collector, collector_freshness = collector_health(state, now)
    metrics, _metrics_freshness = collector_metrics(state, now)
    shadow, shadow_freshness = shadow_health(state, now)
    report, report_freshness = shadow_report(state, now)

    trends = {key: state.trends.series(key) for key in state.trends.series_keys()}
    trends = {k: v for k, v in trends.items() if v}

    storage = {
        "collector_db_bytes": collector.db_bytes,
        "collector_raw_bytes": collector.raw_bytes,
        "disk_free_percent": collector.disk_free_percent,
        "disk_total_bytes": collector.disk_total_bytes,
        "shadow_db_bytes": shadow.db_bytes,
    }

    # Both notes are conditional: Collector >= v0.1.2 publishes the queue in
    # heartbeat.telemetry, and a Shadow built after the field landed publishes
    # db_bytes. Claiming "暂未接入" next to a number that is on screen would be
    # a lie the page immediately contradicts.
    ws_queue = collector.telemetry.get("ws_queue") if collector.present else None
    notes = [
        note
        for note, missing in (
            (
                "WS 队列长度 / 高水位 / 丢帧：Collector 心跳未包含该字段（暂未接入）",
                not isinstance(ws_queue, dict) or not ws_queue,
            ),
            (
                "Shadow 数据库大小：Shadow 心跳未包含该字段（暂未接入）",
                shadow.db_bytes is None,
            ),
        )
        if missing
    ]
    notes.append("趋势线仅覆盖本 Dashboard 进程启动后的观测窗口，不是历史全量")
    unavailable: list[str] = []
    if metrics.trend_window_seconds is None:
        unavailable.append("trends (still collecting samples)")
    if not report.present:
        unavailable.append("shadow report")
    if not collector.present:
        unavailable.append("collector heartbeat")
    if not shadow.present:
        unavailable.append("shadow heartbeat")

    data = SystemData(
        collector=collector,
        collector_metrics=metrics,
        shadow=shadow,
        report=report,
        trends=trends,
        storage=storage,
        notes=notes,
        unavailable=unavailable,
    )
    overall = _combined(
        [
            ("collector", collector_freshness),
            ("shadow", shadow_freshness),
            ("report", report_freshness),
        ]
    )
    return data, overall


# ---------------------------------------------------------------------------
# small parsers shared by builders
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _parse_any(value: Any) -> datetime | None:
    return parse_utc(value)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None

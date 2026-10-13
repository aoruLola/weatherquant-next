/** Typed payload fixtures matching the backend contract, for component and
 * page tests. Values are deliberately realistic: the pages are asserted
 * against what the API actually sends, not against idealised shapes. */
import type {
  CollectorHealth,
  DecisionsData,
  Envelope,
  ForecastsData,
  Freshness,
  MarketSummary,
  MarketsData,
  OverviewData,
  ShadowHealth,
  ShadowReport,
} from '../types'

export function fresh(age = 3): Freshness {
  return {
    state: 'fresh',
    age_seconds: age,
    as_of_utc: new Date(Date.now() - age * 1000).toISOString(),
    stale_after_seconds: 120,
    reason: null,
  }
}

export function envelope<T>(api: string, data: T, freshness = fresh()): Envelope<T> {
  return {
    schema_version: 1,
    api,
    generated_at_utc: new Date().toISOString(),
    dataset: 'live',
    freshness,
    data,
  }
}

export const collectorHealth: CollectorHealth = {
  present: true,
  health_status: 'healthy',
  health_reasons: ['ok'],
  process_status: 'healthy',
  app_version: '0.1.1',
  uptime_seconds: 7200,
  upstream_contact_age_seconds: 2,
  ws_connected: true,
  ws_message_age_seconds: 1,
  subscribed_tokens: 66,
  rest_success_age_seconds: 2,
  rest_consecutive_failures: 0,
  stats: {
    rest_requests_ok: 1860,
    rest_requests_failed: 3,
    ws_messages_received: 9193,
    ws_reconnects: 1,
    errors_total: 0,
    rate_limit_waits: 0,
  },
  db_bytes: 19_148_800,
  raw_bytes: 12_568_370,
  disk_free_percent: 42.5,
  disk_total_bytes: 512_107_737_088,
  telemetry: {
    rest_latency_last_sweep_ms: 41.5,
    poll_budget: { budget_seconds: 3.5, exceeded: false, deferred_tokens: 0 },
    ws_queue: { depth: 2, high_water: 11, capacity: 512, frames_dropped: 0 },
  },
}

export const shadowHealth: ShadowHealth = {
  present: true,
  health_status: 'healthy',
  health_reasons: [],
  process_status: 'running',
  mode: 'LIVE',
  configured_mode: 'auto',
  primary_model: 'calibrated',
  thresholds_version: 'v3',
  cycles: 120,
  uptime_seconds: 3600,
  last_decision_age_seconds: 30,
  last_cycle: {
    cycle_id: 'c-120',
    decisions: 6,
    by_state: { REJECT: 4, NO_TRADE: 2 },
    duration_ms: 812,
    lines_read: 64,
    books: 12,
    markets: 4,
    forecasts_fetched: 0,
    predictions: 4,
  },
}

export const shadowReport: ShadowReport = {
  present: true,
  generated_at_utc: new Date().toISOString(),
  decisions: {
    evaluated: 42,
    accepted: 3,
    rejected: 30,
    no_trade: 9,
    data_unavailable: 0,
    by_mode: { LIVE: { evaluated: 42, accepted: 3 } },
    live_accepted: 3,
    replay_accepted: 0,
    primary_reasons: { EDGE_BELOW_THRESHOLD: 30 },
    window: "cumulative over this shadow database's lifetime (2 run(s) recorded); a restart does not reset it",
    runs_in_database: 2,
    accept_scope: {
      accepted_observations: 3,
      accepted_distinct_markets: 1,
      accepted_distinct_tokens: 1,
      accepted_distinct_target_dates: 1,
      accepted_distinct_market_directions: 1,
      accepted_repeats: 2,
      models_with_decisions: 2,
      scope: 'shadow_database_lifetime',
      runs_in_database: 2,
      window: "cumulative over this shadow database's lifetime (2 run(s) recorded); a restart does not reset it",
      note: 'an ACCEPT is one evaluation of one market in one cycle, not one opportunity',
    },
  },
  coverage: {
    open_markets: 33,
    forecast_coverage: 1,
    orderbook_coverage: 0.94,
    open_markets_with_a_book: 31,
    models_tracked: ['calibrated', 'fusion', 'ecmwf_raw', 'gfs_raw'],
    forecast_available: 8,
    forecast_needed: 8,
  },
  liveness: { latest_orderbook_venue_age_seconds: 4, collector_silence_seconds: 3 },
  warnings: [],
  equity_curve: null,
}

export function overviewFixture(overrides: Partial<OverviewData> = {}): OverviewData {
  return {
    collector: collectorHealth,
    shadow: shadowHealth,
    report: shadowReport,
    active_markets: 33,
    active_markets_source: 'shadow-report',
    subscribed_tokens: 66,
    latest_book_age_seconds: 4,
    current_cycle_decisions: shadowHealth.last_cycle,
    decision_totals: {
      accepted: 3,
      rejected: 30,
      no_trade: 9,
      data_unavailable: 0,
      evaluated: 42,
      live_accepted: 3,
      replay_accepted: 0,
      window: "cumulative over this shadow database's lifetime (2 run(s) recorded); a restart does not reset it",
      runs_in_database: 2,
    },
    decision_totals_status: 'fresh',
    decision_totals_source: 'shadow-report',
    model_vs_market: [],
    model_vs_market_status: 'unavailable',
    model_vs_market_reason: 'detail data awaits the shadow-side snapshot producer',
    system_health: {
      rest: { state: 'ok', consecutive_failures: 0, last_success_age_seconds: 2 },
      ws: { state: 'ok', connected: true, message_age_seconds: 1 },
      data_collection: {
        upstream_contact_age_seconds: 2,
        book_age_seconds: 4,
        collector_silence_seconds: 3,
      },
      storage: {
        collector_db_bytes: 19_148_800,
        collector_raw_bytes: 12_568_370,
        disk_free_percent: 42.5,
      },
      recent_events: [],
      shadow_mode: 'LIVE',
    },
    ...overrides,
  }
}

export function marketFixture(overrides: Partial<MarketSummary> = {}): MarketSummary {
  return {
    market_id: 'm-81',
    question: 'Will the highest temperature in New York City be 81°F on October 12?',
    target_date: '2026-10-12',
    bracket_label: '81°F',
    bracket_low: 81,
    bracket_high: 81,
    station: 'KLGA',
    active: true,
    closed: false,
    accepting_orders: true,
    tradeable: true,
    target_day_ended: false,
    token_yes: 'tok-81-yes',
    book: {
      available: true,
      best_bid: 0.62,
      best_ask: 0.65,
      spread: 0.03,
      bid_depth_usd: 92.5,
      ask_depth_usd: 78.4,
      levels_bid: 2,
      levels_ask: 2,
      defects: [],
      quality: 'ok',
      stale: false,
      age_seconds: 5,
      received_at_utc: new Date(Date.now() - 5000).toISOString(),
      source: 'archive-tail',
      reason: null,
    },
    model_probability: null,
    net_edge_usd: null,
    model_source: 'unavailable',
    model_reason: 'detail data awaits the shadow-side snapshot producer',
    updated_at_utc: new Date(Date.now() - 5000).toISOString(),
    ...overrides,
  }
}

export function marketsFixture(items: MarketSummary[]): MarketsData {
  return {
    items,
    total: items.length,
    next_cursor: null,
    source: 'archive-tail',
    source_status: 'fresh',
    reason: null,
    filters_applied: {},
  }
}

export function decisionRows() {
  const base = {
    decision_id: 'd1',
    decided_at_utc: new Date().toISOString(),
    market_id: 'm-81',
    market_question: 'Will the highest temperature ... be 81°F ...?',
    bracket_label: '81°F',
    target_date: '2026-10-12',
    direction: 'buy_yes',
    state: 'ACCEPT',
    mode: 'LIVE',
    model_name: 'calibrated',
    model_version: 'v1',
    model_probability: 0.41,
    market_price: 0.65,
    best_bid: 0.62,
    best_ask: 0.65,
    net_edge_usd: 0.03,
    primary_reason: null,
    reasons: [],
    snapshot_age_ms: 400,
    forecast_age_hours: 3.5,
    available_depth_usd: 48,
    thresholds_version: 'v3',
    run_id: 'run-1',
  }
  return [
    { ...base, decision_id: 'd1' },
    { ...base, decision_id: 'd2' },
    { ...base, decision_id: 'd3' },
    {
      ...base,
      decision_id: 'd4',
      state: 'REJECT',
      primary_reason: 'EDGE_BELOW_THRESHOLD',
      net_edge_usd: -0.04,
      reasons: ['EDGE_BELOW_THRESHOLD'],
    },
  ]
}

export function decisionsFixture(overrides: Partial<DecisionsData> = {}): DecisionsData {
  return {
    rows: decisionRows(),
    rows_status: 'fresh',
    rows_reason: null,
    total_rows: 4,
    next_cursor: null,
    totals: shadowReport.decisions,
    totals_status: 'fresh',
    totals_source: 'shadow-report',
    last_cycle: shadowHealth.last_cycle,
    last_cycle_status: 'fresh',
    disclaimer:
      'ACCEPT 表示符合当前模型与风险过滤条件，不代表已验证的盈利机会。 ACCEPT means the filters passed; it is not evidence of a profitable trade.',
    filters_applied: {},
    ...overrides,
  }
}

export function forecastsFixture(overrides: Partial<ForecastsData> = {}): ForecastsData {
  return {
    station: 'KLGA',
    target_dates: ['2026-10-12'],
    models: [
      { name: 'calibrated', is_primary: true, predictions: 120, latest_run_utc: new Date().toISOString() },
      { name: 'gfs_raw', is_primary: false, predictions: 120, latest_run_utc: new Date().toISOString() },
      { name: 'ecmwf_raw', is_primary: false, predictions: 96, latest_run_utc: new Date().toISOString() },
      { name: 'fusion', is_primary: false, predictions: 120, latest_run_utc: new Date().toISOString() },
    ],
    coverage: shadowReport.coverage,
    distributions: [
      {
        model: 'calibrated',
        version: 'v1',
        target_date: '2026-10-12',
        forecast_run_time_utc: new Date(Date.now() - 6 * 3600e3).toISOString(),
        training_cutoff_utc: '2026-09-01T00:00:00Z',
        predicted_max_f: 81.4,
        station: 'KLGA',
        buckets: [
          { label: '79°F', probability: 0.08 },
          { label: '80°F', probability: 0.21 },
          { label: '81°F', probability: 0.41 },
          { label: '82°F', probability: 0.22 },
          { label: '83°F', probability: 0.08 },
        ],
        source: 'forecast-snapshot',
      },
      {
        model: 'gfs_raw',
        version: 'v1',
        target_date: '2026-10-12',
        forecast_run_time_utc: new Date(Date.now() - 6 * 3600e3).toISOString(),
        training_cutoff_utc: '2026-09-01T00:00:00Z',
        predicted_max_f: 80.9,
        station: 'KLGA',
        buckets: [
          { label: '79°F', probability: 0.1 },
          { label: '80°F', probability: 0.27 },
          { label: '81°F', probability: 0.38 },
          { label: '82°F', probability: 0.18 },
          { label: '83°F', probability: 0.07 },
        ],
        source: 'forecast-snapshot',
      },
    ],
    distributions_status: 'fresh',
    distributions_reason: null,
    observation_status: 'unavailable',
    observation_reason: '实际观测与官方结算数据暂未接入；结算由研究管线回溯评分',
    ...overrides,
  }
}

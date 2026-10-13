/**
 * TypeScript mirror of the backend contract (src/weatherquant/dashboard/contracts.py).
 *
 * Field names are snake_case exactly as the API returns them: a rename here
 * would silently drift from the server, and the two are checked against each
 * other by rendering real payloads in the tests.
 */

export type FreshnessState =
  | 'fresh'
  | 'stale'
  | 'missing'
  | 'corrupt'
  | 'unavailable'

export interface Freshness {
  state: FreshnessState
  age_seconds?: number | null
  as_of_utc?: string | null
  stale_after_seconds?: number | null
  reason?: string | null
}

export interface Envelope<T> {
  schema_version: number
  api: string
  generated_at_utc: string
  dataset: 'live' | 'fixture'
  freshness: Freshness
  data: T
}

// --- collector -----------------------------------------------------------

export interface CollectorHealth {
  present: boolean
  health_status: string
  health_reasons: string[]
  process_status: string
  run_id?: string | null
  app_version?: string | null
  hostname?: string | null
  uptime_seconds?: number | null
  started_at_utc?: string | null
  updated_at_utc?: string | null
  last_upstream_contact_utc?: string | null
  upstream_contact_age_seconds?: number | null
  last_data_change_utc?: string | null
  data_change_age_seconds?: number | null
  ws_connected?: boolean | null
  ws_last_message_utc?: string | null
  ws_message_age_seconds?: number | null
  subscribed_tokens?: number | null
  rest_last_success_utc?: string | null
  rest_success_age_seconds?: number | null
  rest_consecutive_failures?: number | null
  stats: Record<string, number>
  db_bytes?: number | null
  raw_bytes?: number | null
  disk_free_percent?: number | null
  disk_total_bytes?: number | null
  last_error?: string | null
  last_error_utc?: string | null
  last_defect?: string | null
  fatal_error?: string | null
  /** Collector >= v0.1.2: ws_queue / rest latency / poll budget; {} = not published. */
  telemetry?: Record<string, any>
  reason?: string | null
  [k: string]: unknown
}

export interface CollectorMetrics {
  present: boolean
  counters: Record<string, number>
  trends: Record<string, { t: string; v: number }[]>
  trend_window_seconds?: number | null
  updated_at_utc?: string | null
  reason?: string | null
}

// --- shadow --------------------------------------------------------------

export interface ShadowHealth {
  present: boolean
  health_status: string
  health_reasons: string[]
  process_status: string
  run_id?: string | null
  updated_at_utc?: string | null
  started_at_utc?: string | null
  uptime_seconds?: number | null
  last_decision_at_utc?: string | null
  last_decision_age_seconds?: number | null
  collector_last_seen_utc?: string | null
  cycles?: number | null
  mode?: string | null
  configured_mode?: string | null
  primary_model?: string | null
  thresholds_version?: string | null
  fatal_error?: string | null
  stop_reason?: string | null
  disk_free_percent?: number | null
  last_cycle?: Record<string, unknown> | null
  reason?: string | null
  /** Absent on heartbeats published before these fields existed. */
  app_version?: string | null
  image_tag?: string | null
  db_bytes?: number | null
  [k: string]: unknown
}

export interface ShadowReport {
  present: boolean
  generated_at_utc?: string | null
  source?: string | null
  schema_version?: number | null
  stale_after_seconds?: number | null
  run?: Record<string, unknown> | null
  decisions?: Record<string, any> | null
  theoretical_edge?: Record<string, any> | null
  prediction_quality?: Record<string, any> | null
  liquidity?: Record<string, any> | null
  coverage?: Record<string, any> | null
  liveness?: Record<string, any> | null
  data_quality?: Record<string, any> | null
  warnings?: string[]
  equity_curve?: unknown
  equity_curve_note?: string | null
  reason?: string | null
  [k: string]: unknown
}

// --- markets -------------------------------------------------------------

export interface BookQuote {
  available: boolean
  best_bid?: number | null
  best_ask?: number | null
  spread?: number | null
  bid_depth_usd?: number | null
  ask_depth_usd?: number | null
  levels_bid: number
  levels_ask: number
  defects: string[]
  quality: string
  stale: boolean
  age_seconds?: number | null
  venue_ts_utc?: string | null
  received_at_utc?: string | null
  source: string
  reason?: string | null
}

export interface MarketSummary {
  market_id: string
  event_id?: string | null
  question: string
  target_date?: string | null
  bracket_label: string
  bracket_low?: number | null
  bracket_high?: number | null
  station?: string | null
  active: boolean
  closed: boolean
  accepting_orders: boolean
  tradeable: boolean
  /** Server-derived: the market's civil day in New York has already ended. */
  target_day_ended: boolean
  token_yes?: string | null
  book?: BookQuote | null
  model_probability?: number | null
  net_edge_usd?: number | null
  last_decision?: Record<string, any> | null
  model_source: 'market-snapshot' | 'unavailable'
  model_reason?: string | null
  updated_at_utc?: string | null
}

export interface MarketsData {
  items: MarketSummary[]
  total: number
  next_cursor?: string | null
  source: string
  source_status: FreshnessState
  reason?: string | null
  filters_applied: Record<string, unknown>
}

export interface MarketDetailData {
  market?: MarketSummary | null
  depth?: Record<string, any> | null
  recent_decisions: Record<string, any>[]
  recent_decisions_status: FreshnessState
  recent_decisions_reason?: string | null
  reason?: string | null
}

// --- forecasts -----------------------------------------------------------

export interface ForecastModelInfo {
  name: string
  is_primary: boolean
  predictions: number
  latest_run_utc?: string | null
  training_cutoff_utc?: string | null
  version?: string | null
}

export interface ForecastBucket {
  label: string
  low?: number | null
  high?: number | null
  probability: number
}

export interface ForecastDistribution {
  model: string
  version?: string | null
  target_date?: string | null
  forecast_run_time_utc?: string | null
  training_cutoff_utc?: string | null
  forecast_received_at_utc?: string | null
  predicted_max_f?: number | null
  station?: string | null
  buckets: ForecastBucket[]
  source: string
}

export interface ForecastsData {
  station?: string | null
  target_dates: string[]
  models: ForecastModelInfo[]
  coverage?: Record<string, any> | null
  distributions: ForecastDistribution[]
  distributions_status: FreshnessState
  distributions_reason?: string | null
  observation_status: FreshnessState
  observation_reason?: string | null
  reason?: string | null
}

// --- decisions -----------------------------------------------------------

export interface DecisionRow {
  decision_id: string
  decided_at_utc?: string | null
  market_id: string
  market_question?: string | null
  bracket_label?: string | null
  target_date?: string | null
  direction?: string | null
  state: string
  mode?: string | null
  model_name?: string | null
  model_version?: string | null
  model_probability?: number | null
  market_price?: number | null
  best_bid?: number | null
  best_ask?: number | null
  net_edge_usd?: number | null
  primary_reason?: string | null
  reasons: string[]
  snapshot_age_ms?: number | null
  forecast_age_hours?: number | null
  available_depth_usd?: number | null
  thresholds_version?: string | null
  run_id?: string | null
}

export interface DecisionsData {
  rows: DecisionRow[]
  rows_status: FreshnessState
  rows_reason?: string | null
  total_rows?: number | null
  next_cursor?: string | null
  totals?: Record<string, any> | null
  totals_status: FreshnessState
  totals_source?: string | null
  last_cycle?: Record<string, any> | null
  last_cycle_status: FreshnessState
  disclaimer: string
  filters_applied: Record<string, unknown>
}

// --- overview / system ----------------------------------------------------

export interface OverviewData {
  collector?: CollectorHealth | null
  shadow?: ShadowHealth | null
  report?: ShadowReport | null
  active_markets?: number | null
  active_markets_source?: string | null
  subscribed_tokens?: number | null
  latest_book_age_seconds?: number | null
  current_cycle_decisions?: Record<string, any> | null
  decision_totals?: Record<string, any> | null
  decision_totals_source?: string | null
  decision_totals_status: FreshnessState
  model_vs_market: MarketSummary[]
  model_vs_market_status: FreshnessState
  model_vs_market_reason?: string | null
  system_health: Record<string, any>
  reason?: string | null
}

export interface SystemData {
  collector?: CollectorHealth | null
  collector_metrics?: CollectorMetrics | null
  shadow?: ShadowHealth | null
  report?: ShadowReport | null
  trends: Record<string, { t: string; v: number }[]>
  storage: Record<string, number | null>
  notes: string[]
  unavailable: string[]
}

export interface Healthz {
  status: string
  uptime_seconds: number
  sources: Record<string, string>
}

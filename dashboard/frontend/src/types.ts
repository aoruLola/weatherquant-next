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
  // Multi-layer freshness. Only the performance snapshots populate these;
  // every other source leaves them absent, so consumers must tolerate null.
  export_data_lag_seconds?: number | null
  ingestion_lag_seconds?: number | null
  publication_lag_seconds?: number | null
  snapshot_age_seconds?: number | null
  export_watermark_utc?: string | null
  consumed_watermark_utc?: string | null
  published_at_utc?: string | null
  lagging_layer?: LaggingLayer | null
  publish_interval_seconds?: number | null
}

/** Which layer of the performance pipeline the freshness verdict is about. */
export type LaggingLayer = 'shadow_export' | 'ingestion' | 'publication' | 'publish_interval'

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
  target_date_reason?: string | null
  /** Gamma rules / resolution metadata, when the archive tail carries it. */
  rules_text?: string | null
  resolution_source?: string | null
  rules_source?: string | null
  rules_reason?: string | null
  start_time_utc?: string | null
  end_time_utc?: string | null
  schedule_source?: string | null
  schedule_reason?: string | null
  /**
   * Server-derived from timezone-aware start/end plus venue flags.
   * `null` means the inputs were insufficient - never treat it as open.
   */
  open_now?: boolean | null
  open_now_source?: string | null
  open_now_reason?: string | null
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
  version_reason?: string | null
  target_date?: string | null
  target_date_reason?: string | null
  forecast_run_time_utc?: string | null
  /** Issued / run time, as distinct from publishable and received times. */
  forecast_issued_at_utc?: string | null
  forecast_issued_reason?: string | null
  forecast_publishable_at_utc?: string | null
  forecast_publishable_basis?: string | null
  forecast_publishable_reason?: string | null
  training_cutoff_utc?: string | null
  forecast_received_at_utc?: string | null
  forecast_received_reason?: string | null
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
  /** Forecast run stored on the decision row at decision time. */
  forecast_run_time_utc?: string | null
  snapshot_id?: string | null
  book_id?: string | null
  book_venue_ts_utc?: string | null
  book_collector_received_at_utc?: string | null
  book_shadow_received_at_utc?: string | null
  spread?: number | null
  mode_reason?: string | null
  evidence_version?: number | null
  book_source?: string | null
  book_source_event_id?: string | null
  evidence_source?: string
  evidence_reason?: string | null
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

// --- layered health -------------------------------------------------------

/** One pipeline layer's verdict. LAGGING is within its limit but blocked upstream. */
export type LayerStatus = 'OK' | 'LAGGING' | 'STALE' | 'UNKNOWN'

export type HealthLayerName = 'export' | 'performance_consume' | 'publish' | 'snapshot'

export interface HealthLayer {
  name: HealthLayerName
  watermark_utc?: string | null
  source: string
  age_seconds?: number | null
  lag_seconds?: number | null
  threshold_seconds?: number | null
  status: LayerStatus
  reason?: string | null
}

export interface ServiceHealth {
  name: string
  status: LayerStatus
  age_seconds?: number | null
  reason?: string | null
}

export interface LayeredHealthData {
  generation_id?: string | null
  generation_source?: string | null
  lagging_layer?: HealthLayerName | null
  lagging_basis?: string | null
  layers: HealthLayer[]
  services: Record<string, ServiceHealth>
  thresholds: Record<string, number>
  dataset?: string | null
  reason?: string | null
}

/**
 * A performance snapshot as the API serves it.
 *
 * Deliberately loose: the payload carries `statistical_scope` and
 * `warnings` as *data*, because those two are what stop an edge total
 * being read as a return. Typing the body tightly would drop the caveat
 * that travels with the number.
 */
export interface PerformancePayload<TData = Record<string, any>> {
  reason?: string | null
  statistical_scope: string[]
  warnings: string[]
  sample_size?: number | null
  source?: string | null
  generated_at_utc?: string | null
  as_of_utc?: string | null
  dataset?: string | null
  data?: TData
  items?: Record<string, any>[]
  total?: number
  // A cursor belongs to one published generation, so it is opaque: the API
  // encodes the batch in it and refuses one from another batch.
  next_cursor?: string | number | null
  prev_cursor?: string | number | null
  generation_id?: string | null
  // reference-capital
  mode?: string
  capital_usdc?: number
  allocated_total_usdc?: number
  peak_open_allocation_usdc?: number
  balance_remaining_usdc?: number
  utilization?: number
  capacity_limited_count?: number
  executable_count?: number
  not_executable_count?: number
  backtestable?: boolean
  not_backtestable_reasons?: string[]
  source_endpoint?: string
  [k: string]: any
}

// --- calibration and market-pricing drift ---------------------------------

export type ResearchDriftGranularity = 'iso_week' | 'month' | 'target_date'

export interface ResearchDriftCalibrationBin {
  lower: number
  upper: number
  predicted_mean: number
  observed_frequency: number
  count: number
  gap: number
}

export interface ResearchDriftCalibration {
  n: number
  brier: number | null
  ece: number | null
  bins: ResearchDriftCalibrationBin[]
}

export interface ResearchDriftMarketBias {
  n: number
  mean_market_minus_outcome: number | null
  mean_market_probability: number | null
  mean_outcome: number | null
}

export interface ResearchDriftMarketBaseline extends ResearchDriftCalibration {
  mean_market_minus_outcome: number | null
}

export interface ResearchDriftBaselineComparison {
  market: ResearchDriftMarketBaseline
  climatology: {
    available: boolean
    n: number
    brier: number | null
    ece: number | null
    reason: string
  }
}

export interface ResearchDriftBucket {
  period: string
  start_date: string
  observation_count: number
  episode_count: number
  executable_count: number
  not_executable_count: number
  fee_unknown_count: number
  no_book_count: number
  not_executable_reason_counts: Record<string, number>
  fee_assumption: {
    source: string
    known_count: number
    unknown_count: number
    rates: { fee_rate: number; episode_count: number }[]
  }
  calibration: ResearchDriftCalibration
  market_bias: ResearchDriftMarketBias
  baseline_comparison: ResearchDriftBaselineComparison
  edge_tiers: {
    theoretical_observation_edge_per_share: ResearchDriftEdgeTier
    executable_expected_net_usd: ResearchDriftEdgeTier
    verified_realised_net_usd: ResearchDriftEdgeTier
  }
  small_sample: boolean
  source_episode_ids: string[]
  anomaly_metrics: string[]
}

export interface ResearchDriftEdgeTier {
  n: number
  sum: number | null
  mean: number | null
  unit: string
}

export interface ResearchDriftAnomaly {
  granularity: ResearchDriftGranularity
  period: string
  metric: string
  value: number
  trailing_median: number
  trailing_mad: number
  deviation: number
  threshold: number
  trailing_bucket_count: number
  source_episode_ids: string[]
}

export interface ResearchDriftPayload {
  totals: {
    observation_count: number
    episode_count: number
    target_date_missing_episode_count: number
  }
  reference_capital_usdc: number
  fee_assumption: {
    source: string
    unknown_fee_policy: string
    missing_book_policy: string
  } | null
  calibration_bin_count: number
  small_sample_threshold_episodes: number
  anomaly_rule: {
    statistic: string
    threshold_mad: number
    minimum_trailing_buckets: number
    comparison: string
    metrics: string[]
  } | null
  buckets: Record<ResearchDriftGranularity, ResearchDriftBucket[]>
  anomalies: ResearchDriftAnomaly[]
  statistical_scope: string[]
  warnings: string[]
}

/** Null data means unpublished; it is never replaced by a zero-filled payload. */
export type ResearchDriftData = PerformancePayload<ResearchDriftPayload | null>

// --- Paper Trading (virtual simulation, isolated from reference capital) ---

export interface PaperPosition {
  market_id: string
  token_id: string
  shares: number
  avg_cost_usd_per_share: number
  cost_basis_usdc: number
  mark_usd_per_share: number | null
  market_value_usdc: number | null
  unrealized_pnl_usdc: number | null
  mark_status: 'OK' | 'MARK_UNAVAILABLE'
}

export interface PaperRejection {
  decision_id: string
  market_id: string
  token_id?: string | null
  direction?: string | null
  refusal_reason?: string | null
  refusal_detail?: string | null
  decision_time_utc?: string | null
  net_edge?: number | null
}

export interface PaperSummary {
  status: 'ok' | 'unavailable'
  mode: 'PAPER'
  reason?: string | null
  initial_capital_usdc: number | null
  available_usdc: number | null
  occupied_usdc: number | null
  cost_equity_usdc: number | null
  equity_usdc: number | null
  realized_pnl_usdc: number | null
  unrealized_pnl_usdc: number | null
  roi: number | null
  max_drawdown: number | null
  drawdown_basis: 'ledger_cost'
  equity_curve: {
    basis: 'ledger_cost'
    points: { t: string; v: number; seq: number }[]
  }
  positions: PaperPosition[]
  marks_status: 'fresh' | 'unavailable' | 'not_required'
  marks_reason?: string | null
  rejection_summary: { reason: string; count: number }[]
  rejections: PaperRejection[]
  ledger_consistent?: boolean | null
  conservation_ok?: boolean | null
  fill_count?: number | null
}

export interface PaperFill {
  decision_id: string
  market_id: string
  token_id: string
  direction: string
  side: 'BUY' | 'SELL'
  requested_shares: number
  filled_shares: number
  avg_price_usd_per_share: number
  fill_cost_usd: number
  fees_usd: number
  net_proceeds_usd?: number | null
  complete: boolean
  decision_time_utc?: string | null
  filled_at_utc?: string | null
}

export interface PaperFills {
  status: 'ok' | 'unavailable'
  mode: 'PAPER'
  reason?: string | null
  fills: PaperFill[]
  limit: number
  next_cursor: string | null
  has_more: boolean
}

export interface Healthz {
  status: string
  uptime_seconds: number
  sources: Record<string, string>
}

-- Shadow engine: audit core.
--
-- Separate file on purpose. The NAS collector's schema is owned by whatever
-- image the NAS is running, and the research database holds retrospective
-- tables; a 24/7 observation writer must touch neither. Same checksummed,
-- forward-only migrator, third directory.
--
-- Every decision row is written with the inputs that produced it, so a
-- decision can be re-derived from stored data alone. Nothing here is a fill:
-- there is no position, no cash, no order.

CREATE TABLE shadow_runs (
    run_id              VARCHAR PRIMARY KEY,
    started_at_utc      TIMESTAMPTZ NOT NULL,
    stopped_at_utc      TIMESTAMPTZ,
    status              VARCHAR NOT NULL DEFAULT 'running',   -- running | stopped | failed
    stop_reason         VARCHAR,
    app_version         VARCHAR,
    git_sha             VARCHAR,
    image_tag           VARCHAR,
    hostname            VARCHAR,
    pid                 INTEGER,
    config_json         VARCHAR NOT NULL,
    stats_json          VARCHAR
);

-- The exact parameters a decision was made with, registered at startup.
CREATE TABLE model_versions (
    model_name              VARCHAR NOT NULL,
    model_version           VARCHAR NOT NULL,
    training_cutoff_utc     TIMESTAMPTZ NOT NULL,
    created_at_utc          TIMESTAMPTZ NOT NULL,
    source_data_hash        VARCHAR NOT NULL,
    artifact_sha256         VARCHAR NOT NULL,
    n_pairs                 INTEGER NOT NULL,
    n_dates                 INTEGER NOT NULL,
    loaded_at_utc           TIMESTAMPTZ NOT NULL,
    is_primary              BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (model_name, model_version)
);

-- One probability distribution over one event's brackets.
-- Natural key is the prediction, not the moment: recomputing the same
-- distribution from the same run and version must not create a second row.
CREATE TABLE model_predictions (
    model_name                  VARCHAR NOT NULL,
    model_version               VARCHAR NOT NULL,
    station                     VARCHAR NOT NULL,
    target_date                 DATE NOT NULL,
    event_id                    VARCHAR NOT NULL,
    forecast_run_time_utc       TIMESTAMPTZ NOT NULL,
    probabilities_json          VARCHAR NOT NULL,
    predicted_max_f             DOUBLE,
    lead_hours                  DOUBLE NOT NULL,
    n_hours                     INTEGER,
    n_missing_hours             INTEGER,
    model_run_id                VARCHAR NOT NULL,
    training_cutoff_utc         TIMESTAMPTZ NOT NULL,
    forecast_publishable_at_utc TIMESTAMPTZ NOT NULL,
    forecast_publishable_basis  VARCHAR NOT NULL,
    forecast_received_at_utc    TIMESTAMPTZ NOT NULL,
    decision_time_utc           TIMESTAMPTZ NOT NULL,
    calibration_version         VARCHAR NOT NULL,
    source_data_hash            VARCHAR NOT NULL,
    computed_at_utc             TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (model_name, model_version, station, target_date, event_id, forecast_run_time_utc)
);

-- The market state an evaluation actually looked at.
CREATE TABLE market_snapshots (
    snapshot_id                 VARCHAR PRIMARY KEY,
    market_id                   VARCHAR NOT NULL,
    token_id                    VARCHAR NOT NULL,
    book_id                     VARCHAR NOT NULL,
    book_source                 VARCHAR NOT NULL,           -- rest | ws
    venue_ts_utc                TIMESTAMPTZ,
    venue_ts_origin             VARCHAR NOT NULL,
    collector_received_at_utc   TIMESTAMPTZ NOT NULL,
    shadow_received_at_utc      TIMESTAMPTZ NOT NULL,
    decision_time_utc           TIMESTAMPTZ NOT NULL,
    snapshot_age_ms             BIGINT,                    -- NULL when venue_ts is missing
    latency_ms                  BIGINT,                    -- shadow received - venue
    best_bid                    DOUBLE,
    best_ask                    DOUBLE,
    spread                      DOUBLE,
    bid_depth_usd               DOUBLE,
    ask_depth_usd               DOUBLE,
    levels_bid                  INTEGER NOT NULL,
    levels_ask                  INTEGER NOT NULL,
    defects                     VARCHAR NOT NULL DEFAULT '',
    quality                     VARCHAR NOT NULL DEFAULT 'ok',
    created_at_utc              TIMESTAMPTZ NOT NULL
);

-- One row per (cycle, market, direction). state is one of
-- CANDIDATE | ACCEPT | REJECT | NO_TRADE | DATA_UNAVAILABLE; CANDIDATE is only
-- ever left behind by an interrupted cycle, which is how a crash is detected.
CREATE TABLE shadow_decisions (
    decision_id                 VARCHAR PRIMARY KEY,
    run_id                      VARCHAR NOT NULL,
    cycle_id                    VARCHAR NOT NULL,
    market_id                   VARCHAR NOT NULL,
    event_id                    VARCHAR,
    token_id                    VARCHAR,
    bracket_label               VARCHAR,
    target_date                 DATE,
    station                     VARCHAR NOT NULL,
    direction                   VARCHAR NOT NULL,           -- buy_yes | sell_yes
    state                       VARCHAR NOT NULL,
    primary_reason              VARCHAR,
    model_name                  VARCHAR,
    model_version               VARCHAR,
    model_run_id                VARCHAR,
    forecast_run_time_utc       TIMESTAMPTZ,
    snapshot_id                 VARCHAR,
    book_id                     VARCHAR,
    venue_ts_utc                TIMESTAMPTZ,
    collector_received_at_utc   TIMESTAMPTZ,
    shadow_received_at_utc      TIMESTAMPTZ,
    decision_time_utc           TIMESTAMPTZ NOT NULL,
    snapshot_age_ms             BIGINT,
    model_probability           DOUBLE,
    market_price                DOUBLE,
    best_bid                    DOUBLE,
    best_ask                    DOUBLE,
    spread                      DOUBLE,
    available_depth_usd         DOUBLE,
    proposed_shares             DOUBLE,
    estimated_fill_shares       DOUBLE,
    estimated_fill_price        DOUBLE,
    estimated_fees_usd          DOUBLE,
    estimated_buffer_usd        DOUBLE,
    estimated_cost_usd          DOUBLE,
    expected_net_profit_usd     DOUBLE,
    net_edge                    DOUBLE,                    -- USD per share, after costs
    market_closed               BOOLEAN NOT NULL DEFAULT FALSE,
    forecast_age_hours          DOUBLE,
    thresholds_version          VARCHAR,
    decision_latency_ms         BIGINT,
    created_at_utc              TIMESTAMPTZ NOT NULL,
    UNIQUE (cycle_id, market_id, direction)
);

-- A decision may fail several gates; all reasons are stored, one marked primary.
CREATE TABLE rejection_reasons (
    decision_id     VARCHAR NOT NULL,
    ordinal         INTEGER NOT NULL,
    reason          VARCHAR NOT NULL,
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    detail          VARCHAR,
    PRIMARY KEY (decision_id, reason),
    UNIQUE (decision_id, ordinal)
);

-- Gaps, truncation, silent collector, unparseable payloads. Recorded, never skipped.
CREATE TABLE data_quality_events (
    event_id        VARCHAR PRIMARY KEY,
    run_id          VARCHAR NOT NULL,
    ts_utc          TIMESTAMPTZ NOT NULL,
    kind            VARCHAR NOT NULL,
    severity        VARCHAR NOT NULL,        -- info | warning | error
    source          VARCHAR,
    path            VARCHAR,
    offset_bytes    BIGINT,
    detail          VARCHAR NOT NULL,
    resolved_at_utc TIMESTAMPTZ
);

-- Periodic machine-readable metric snapshots (the report reads these back).
CREATE TABLE shadow_metrics (
    run_id          VARCHAR NOT NULL,
    computed_at_utc TIMESTAMPTZ NOT NULL,
    payload_json    VARCHAR NOT NULL,
    PRIMARY KEY (run_id, computed_at_utc)
);

CREATE INDEX idx_decisions_created ON shadow_decisions (created_at_utc);
CREATE INDEX idx_decisions_state ON shadow_decisions (state, created_at_utc);
CREATE INDEX idx_decisions_market ON shadow_decisions (market_id, decision_time_utc);
CREATE INDEX idx_reasons_reason ON rejection_reasons (reason);
CREATE INDEX idx_quality_ts ON data_quality_events (ts_utc);
CREATE INDEX idx_snapshots_market ON market_snapshots (market_id, decision_time_utc);

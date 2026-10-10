-- WeatherQuant Next - research schema (P1-1), migration 0001.
--
-- This lives in its OWN database file (WQ_RESEARCH_DB_PATH), separate from the
-- collector's weatherquant.duckdb. That separation is deliberate:
--
--   * the NAS collector must never have its schema changed underneath a
--     running process, and this round is forbidden from writing to its file;
--   * research tables are wide and append-heavy in a way a 24/7 writer should
--     not share a file lock with;
--   * a research database can be rebuilt, dropped or archived without touching
--     the order-book history it is meant to be analysed against.
--
-- Conventions match the collector schema: every temporal column is TIMESTAMPTZ
-- in UTC, every unit is in the column name, and provenance columns
-- (`source`, `fetched_at_utc`, `*_hash`) are NOT NULL wherever the upstream
-- payload exists.

-- ---------------------------------------------------------------------------
-- A. What each market says it will settle on.
--
-- Parsed from the venue's own rules_text, kept per event because the rules are
-- per event, and kept alongside the verbatim text so a parse can be audited.
-- ---------------------------------------------------------------------------
CREATE TABLE settlement_rules (
    event_id                VARCHAR PRIMARY KEY,
    target_date             DATE,
    slug                    VARCHAR,

    station                 VARCHAR,          -- e.g. 'KLGA'; NULL when unnamed
    station_source          VARCHAR,          -- 'rules_text' | 'config_default'
    statistic_definition    VARCHAR,          -- verbatim phrase, e.g. 'highest temperature'
    observation_field       VARCHAR,          -- e.g. 'Temp column, hourly data'
    timezone_name           VARCHAR,          -- IANA, e.g. 'America/New_York'
    timezone_hint           VARCHAR,          -- raw text the rules used, e.g. '11:59 PM ET'
    date_boundary           VARCHAR,          -- e.g. 'local midnight-to-midnight'
    unit                    VARCHAR,          -- 'degF' | 'degC'
    precision_deg           DOUBLE,           -- e.g. 1.0 for whole degrees
    rounding                VARCHAR,          -- 'nearest_integer_f' | 'none' | 'unknown'

    primary_source_name     VARCHAR,          -- 'NOAA' | 'WEATHER_UNDERGROUND' | ...
    primary_source_url      VARCHAR,
    fallback_source_name    VARCHAR,
    fallback_source_url     VARCHAR,
    no_data_resolution      VARCHAR,          -- e.g. 'lowest_bracket'
    resolve_by_rule         VARCHAR,          -- verbatim timing clause
    resolution_condition    VARCHAR,          -- verbatim "market will resolve once ..."

    rules_text              VARCHAR NOT NULL,
    rules_sha256            VARCHAR NOT NULL,
    parse_status            VARCHAR NOT NULL, -- 'parsed' | 'partial' | 'unparsed'
    parse_notes             VARCHAR,
    parser_version          VARCHAR NOT NULL,
    parsed_at_utc           TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- B. Ground truth observations for the station.
--
-- Kept structurally separate from settlement outcomes: an observation is what
-- the station measured, a settlement is what the venue decided. They are
-- compared, never merged, and neither may be written into the other's table.
-- ---------------------------------------------------------------------------
CREATE TABLE klga_observations (
    station                 VARCHAR NOT NULL,
    obs_time_utc            TIMESTAMPTZ NOT NULL,
    obs_time_local          TIMESTAMPTZ,      -- in the station's civil zone
    temp_f                  DOUBLE,
    temp_c                  DOUBLE,
    quality_flag            VARCHAR,          -- upstream flag, verbatim
    report_type             VARCHAR,          -- 'routine' | 'special' | upstream label
    source                  VARCHAR NOT NULL,
    source_url              VARCHAR NOT NULL,
    fetched_at_utc          TIMESTAMPTZ NOT NULL,
    payload_sha256          VARCHAR NOT NULL,
    PRIMARY KEY (station, obs_time_utc, source)
);

CREATE TABLE klga_daily_max (
    station                 VARCHAR NOT NULL,
    target_date             DATE NOT NULL,
    max_temp_f              DOUBLE,
    n_readings              INTEGER NOT NULL,
    n_missing_hours         INTEGER NOT NULL,
    n_routine               INTEGER,
    n_special               INTEGER,
    aggregation_rule        VARCHAR NOT NULL,  -- e.g. 'max of all readings'
    source                  VARCHAR NOT NULL,
    is_complete             BOOLEAN NOT NULL,
    computed_at_utc         TIMESTAMPTZ NOT NULL,
    notes                   VARCHAR,
    PRIMARY KEY (station, target_date, source, aggregation_rule)
);

-- ---------------------------------------------------------------------------
-- C. What the venue actually resolved - independent of B.
--
-- The winner is derived from the venue's own yes_price per market within one
-- event. Never from an observation, and never across events that share a date:
-- the legacy data contains an `arch-` duplicate event for 2026-05-19 that
-- resolved to a different bracket than its sibling for the same date.
-- ---------------------------------------------------------------------------
CREATE TABLE settlement_outcome (
    event_id                VARCHAR NOT NULL,
    target_date             DATE NOT NULL,
    market_id               VARCHAR NOT NULL,
    bracket_label           VARCHAR,
    bracket_low             DOUBLE,
    bracket_high            DOUBLE,
    is_winner               BOOLEAN NOT NULL,
    n_winners_in_event      INTEGER NOT NULL,
    yes_price               DOUBLE,
    resolved_at_utc         TIMESTAMPTZ,
    source                  VARCHAR NOT NULL,
    fetched_at_utc          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, market_id, source)
);

CREATE INDEX idx_settlement_outcome_date ON settlement_outcome (target_date);

-- ---------------------------------------------------------------------------
-- D. The cross-check itself. One row per event, whatever the verdict.
--    A mismatch is evidence, never something to edit away.
-- ---------------------------------------------------------------------------
CREATE TABLE settlement_crosscheck (
    event_id                VARCHAR NOT NULL,
    target_date             DATE NOT NULL,
    station                 VARCHAR NOT NULL,
    observation_source      VARCHAR,
    aggregation_rule        VARCHAR,
    observed_max_f          DOUBLE,
    winning_bracket_label   VARCHAR,
    winning_bracket_low     DOUBLE,
    winning_bracket_high    DOUBLE,
    n_winners               INTEGER,
    verdict                 VARCHAR NOT NULL,  -- 'match' | 'mismatch' | 'no_observation'
                                                 -- | 'no_winner' | 'ambiguous_winner' | 'unparsed_rules'
    detail                  VARCHAR,
    checked_at_utc          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, station, observation_source, aggregation_rule)
);

-- ---------------------------------------------------------------------------
-- E. Historical forecast runs (point-in-time).
--
-- `run_time_utc` is when the model was initialised. `publishable_at_utc` is the
-- earliest instant the run may be used for a decision - NEVER the same value.
-- `publishable_basis` records how that instant was established:
--   'official'  - the provider published an explicit publication timestamp
--   'estimated' - derived from the model's official schedule, with the lag
--                 recorded in publishable_lag_hours and the reasoning in
--                 publishable_note
--   'unknown'   - cannot be bounded; the run is then NOT eligible for a strict
--                 point-in-time backtest
-- ---------------------------------------------------------------------------
CREATE TABLE forecast_runs (
    provider                VARCHAR NOT NULL,   -- 'open-meteo'
    model                   VARCHAR NOT NULL,   -- 'ecmwf_ifs025' | 'gfs'
    model_version           VARCHAR,
    run_time_utc            TIMESTAMPTZ NOT NULL,
    publishable_at_utc      TIMESTAMPTZ,
    publishable_basis       VARCHAR NOT NULL,   -- 'official' | 'estimated' | 'unknown'
    publishable_lag_hours   DOUBLE,
    publishable_note        VARCHAR,
    fetched_at_utc          TIMESTAMPTZ NOT NULL,
    http_status             INTEGER,
    status                  VARCHAR NOT NULL,   -- 'ok' | 'unavailable' | 'failed'
    raw_path                VARCHAR,
    raw_sha256              VARCHAR,
    PRIMARY KEY (provider, model, run_time_utc)
);

CREATE INDEX idx_forecast_runs_target ON forecast_runs (model, run_time_utc);

CREATE TABLE forecast_hourly (
    provider                VARCHAR NOT NULL,
    model                   VARCHAR NOT NULL,
    run_time_utc            TIMESTAMPTZ NOT NULL,
    valid_time_utc          TIMESTAMPTZ NOT NULL,
    valid_time_local        TIMESTAMPTZ,
    lead_hours              DOUBLE NOT NULL,
    temperature_2m          DOUBLE NOT NULL,
    station                 VARCHAR NOT NULL,
    source                  VARCHAR NOT NULL,
    raw_sha256              VARCHAR NOT NULL,
    PRIMARY KEY (provider, model, run_time_utc, valid_time_utc, station)
);

-- ---------------------------------------------------------------------------
-- F. Daily aggregation of a forecast run, in the market's civil date.
--    Distinct from klga_daily_max: this is a *prediction*, that is an
--    *observation*. Nothing may substitute one for the other.
-- ---------------------------------------------------------------------------
CREATE TABLE forecast_daily_max (
    provider                VARCHAR NOT NULL,
    model                   VARCHAR NOT NULL,
    run_time_utc            TIMESTAMPTZ NOT NULL,
    target_date             DATE NOT NULL,
    station                 VARCHAR NOT NULL,
    predicted_max_f         DOUBLE NOT NULL,
    n_hours                 INTEGER NOT NULL,
    n_missing_hours         INTEGER NOT NULL,
    first_valid_utc         TIMESTAMPTZ,
    last_valid_utc          TIMESTAMPTZ,
    lead_hours_at_end       DOUBLE,
    covers_full_local_day   BOOLEAN NOT NULL,
    computed_at_utc         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, model, run_time_utc, target_date, station)
);

CREATE INDEX idx_forecast_daily_target ON forecast_daily_max (target_date, model);

-- ---------------------------------------------------------------------------
-- G. Point-in-time eligibility. The answer to "could this run have been known
--    when a decision for this target day was made?"
-- ---------------------------------------------------------------------------
CREATE TABLE point_in_time (
    provider                VARCHAR NOT NULL,
    model                   VARCHAR NOT NULL,
    run_time_utc            TIMESTAMPTZ NOT NULL,
    target_date             DATE NOT NULL,
    station                 VARCHAR NOT NULL,
    publishable_at_utc      TIMESTAMPTZ,
    publishable_basis       VARCHAR NOT NULL,
    decision_deadline_utc   TIMESTAMPTZ,        -- end of the target local day
    eligible                BOOLEAN NOT NULL,
    ineligible_reason       VARCHAR,
    checked_at_utc          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider, model, run_time_utc, target_date, station)
);

-- ---------------------------------------------------------------------------
-- H. Raw payload archive index. Rows point at files; files are never in Git.
-- ---------------------------------------------------------------------------
CREATE TABLE raw_payloads (
    payload_sha256          VARCHAR PRIMARY KEY,
    source                  VARCHAR NOT NULL,   -- 'open-meteo' | 'noaa' | 'iem' | 'gamma'
    url                     VARCHAR NOT NULL,
    request_params          VARCHAR,
    http_status             INTEGER,
    bytes                   INTEGER NOT NULL,
    fetched_at_utc          TIMESTAMPTZ NOT NULL,
    relative_path           VARCHAR NOT NULL,
    notes                   VARCHAR
);

-- ---------------------------------------------------------------------------
-- I. Runs of the research pipeline itself, so a backfill can resume and a
--    partial import can be distinguished from a complete one.
-- ---------------------------------------------------------------------------
CREATE TABLE research_runs (
    run_id                  VARCHAR PRIMARY KEY,
    purpose                 VARCHAR NOT NULL,
    started_at_utc          TIMESTAMPTZ NOT NULL,
    finished_at_utc         TIMESTAMPTZ,
    status                  VARCHAR NOT NULL,   -- 'running' | 'completed' | 'failed'
    params                  VARCHAR,
    stats                   VARCHAR,
    error                   VARCHAR
);

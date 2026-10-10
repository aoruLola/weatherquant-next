-- WeatherQuant Next - initial schema (P0)
--
-- Conventions
--   * Every temporal column is TIMESTAMPTZ; the session is pinned to UTC by
--     weatherquant.storage.db.connect, so an instant round-trips byte-for-byte
--     regardless of host locale.
--   * Every column that carries a unit names it (_usd, _shares, _f, _c,
--     _usd_per_share).
--   * Provenance columns (`source`, `*_at_utc`, `raw`) are NOT NULL wherever the
--     upstream payload exists. Data without provenance is not research data.
--   * No table stores a "probability" that has not been validated to sum to 1;
--     distributions are written as JSON plus their normalisation residual so a
--     reader can audit the sum without reparsing floats.
--   * Plain CREATE TABLE (no IF NOT EXISTS): the migrator tracks state in
--     schema_migrations, so a re-applied migration must fail loudly rather than
--     quietly skip.

-- ---------------------------------------------------------------------------
-- Market reference data
-- ---------------------------------------------------------------------------

CREATE TABLE events (
    event_id            VARCHAR PRIMARY KEY,
    slug                VARCHAR NOT NULL,
    title               VARCHAR NOT NULL,
    target_date         DATE,
    resolution_source   VARCHAR,
    rules_text          VARCHAR,
    fetched_at_utc      TIMESTAMPTZ NOT NULL,
    raw                 VARCHAR NOT NULL
);

CREATE TABLE markets (
    market_id               VARCHAR PRIMARY KEY,
    event_id                VARCHAR NOT NULL REFERENCES events(event_id),
    condition_id            VARCHAR,
    question                VARCHAR NOT NULL,

    -- Outcome bucket. Bounds are the venue's *quoted* degrees; the rounding
    -- convention that maps a continuous temperature onto them is stored
    -- explicitly because a silent assumption here biases every probability.
    bracket_label           VARCHAR NOT NULL,
    bracket_kind            VARCHAR NOT NULL CHECK (bracket_kind IN ('range', 'exact', 'above', 'below')),
    bracket_unit            VARCHAR NOT NULL DEFAULT 'degF',
    bracket_low             DOUBLE,
    bracket_high            DOUBLE,
    bracket_low_inclusive   BOOLEAN NOT NULL DEFAULT true,
    bracket_high_inclusive  BOOLEAN NOT NULL DEFAULT true,
    bracket_rounding        VARCHAR NOT NULL DEFAULT 'nearest_integer_f'
                                CHECK (bracket_rounding IN ('nearest_integer_f', 'none')),

    token_yes               VARCHAR NOT NULL,
    token_no                VARCHAR NOT NULL,
    target_date             DATE NOT NULL,
    station                 VARCHAR NOT NULL,

    tick_size               DOUBLE NOT NULL,
    min_order_size_shares   DOUBLE NOT NULL,

    -- NULL means "venue did not report a fee schedule", NOT "no fee".
    fee_rate                DOUBLE,
    fee_exponent            DOUBLE,

    resolution_source       VARCHAR,
    rules_text              VARCHAR,
    active                  BOOLEAN NOT NULL,
    closed                  BOOLEAN NOT NULL,
    accepting_orders        BOOLEAN NOT NULL,
    fetched_at_utc          TIMESTAMPTZ NOT NULL,
    raw                     VARCHAR NOT NULL,

    CHECK (bracket_low IS NULL OR bracket_high IS NULL OR bracket_low <= bracket_high)
);

CREATE INDEX idx_markets_target_date ON markets (target_date);
CREATE INDEX idx_markets_event ON markets (event_id);

-- ---------------------------------------------------------------------------
-- Order book snapshots
-- ---------------------------------------------------------------------------

-- Levels are stored as JSON arrays ordered exactly as the venue returned them:
--   bids: [{"price_usd_per_share": p, "size_shares": s}, ...]  best (highest) first
--   asks: [{"price_usd_per_share": p, "size_shares": s}, ...]  best (lowest)  first
--
-- `defects` is the snapshot's validity verdict computed once at capture time
-- (NO_ASK_SIDE, CROSSED_OR_LOCKED, NON_MONOTONIC_BID, ...). It is stored rather
-- than derived on read so that "was this book executable when we saw it?" stays
-- answerable even if the validation code later changes.
CREATE TABLE order_books (
    book_id             VARCHAR PRIMARY KEY,
    market_id           VARCHAR NOT NULL,
    token_id            VARCHAR NOT NULL,
    bids_json           VARCHAR NOT NULL,
    asks_json           VARCHAR NOT NULL,
    best_bid            DOUBLE,
    best_ask            DOUBLE,
    spread              DOUBLE,
    bid_depth_usd       DOUBLE NOT NULL DEFAULT 0,
    ask_depth_usd       DOUBLE NOT NULL DEFAULT 0,
    defects             VARCHAR NOT NULL DEFAULT '',
    venue_ts            TIMESTAMPTZ,
    fetched_at_utc      TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_order_books_market ON order_books (market_id, fetched_at_utc);

-- ---------------------------------------------------------------------------
-- Forecasts  (prediction of a FUTURE daily maximum)
-- ---------------------------------------------------------------------------

-- One row per model initialization. A single run publishes forecasts for many
-- target dates, so target_date is deliberately absent here.
CREATE TABLE forecast_runs (
    model_id            VARCHAR NOT NULL,
    run_time_utc        TIMESTAMPTZ NOT NULL,
    publishable_at_utc  TIMESTAMPTZ NOT NULL,
    fetched_at_utc      TIMESTAMPTZ NOT NULL,
    status              VARCHAR NOT NULL DEFAULT 'ok',
    reason              VARCHAR,
    PRIMARY KEY (model_id, run_time_utc),
    CHECK (publishable_at_utc >= run_time_utc)
);

CREATE TABLE forecast_hourly (
    model_id        VARCHAR NOT NULL,
    run_time_utc    TIMESTAMPTZ NOT NULL,
    valid_time_utc  TIMESTAMPTZ NOT NULL,
    temp_c          DOUBLE NOT NULL,
    temp_f          DOUBLE NOT NULL,
    PRIMARY KEY (model_id, run_time_utc, valid_time_utc),
    FOREIGN KEY (model_id, run_time_utc)
        REFERENCES forecast_runs (model_id, run_time_utc),
    CHECK (valid_time_utc >= run_time_utc)
);

CREATE TABLE forecast_daily (
    model_id            VARCHAR NOT NULL,
    run_time_utc        TIMESTAMPTZ NOT NULL,
    target_date         DATE NOT NULL,
    predicted_max_f     DOUBLE NOT NULL,
    n_hourly_points     INTEGER NOT NULL CHECK (n_hourly_points > 0),
    fetched_at_utc      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (model_id, run_time_utc, target_date),
    FOREIGN KEY (model_id, run_time_utc)
        REFERENCES forecast_runs (model_id, run_time_utc)
);

CREATE INDEX idx_forecast_daily_target ON forecast_daily (target_date, model_id);

-- ---------------------------------------------------------------------------
-- Observations  (what the station actually measured)
-- ---------------------------------------------------------------------------

-- Observations are never official results. There is intentionally no
-- `is_official` column: the official result lives in settlements.
CREATE TABLE observations (
    station         VARCHAR NOT NULL,
    obs_time_utc    TIMESTAMPTZ NOT NULL,
    temp_c          DOUBLE NOT NULL,
    temp_f          DOUBLE NOT NULL,
    source          VARCHAR NOT NULL,
    report_type     VARCHAR,
    fetched_at_utc  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (station, obs_time_utc, source)
);

CREATE TABLE daily_max_temperatures (
    station         VARCHAR NOT NULL,
    target_date     DATE NOT NULL,
    max_temp_f      DOUBLE NOT NULL,
    n_readings      INTEGER NOT NULL CHECK (n_readings >= 0),
    rule            VARCHAR NOT NULL,
    source          VARCHAR NOT NULL,
    computed_at_utc TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (station, target_date, source)
);

-- ---------------------------------------------------------------------------
-- Settlement  (what the venue resolved)
-- ---------------------------------------------------------------------------

CREATE TABLE settlements (
    market_id           VARCHAR PRIMARY KEY REFERENCES markets(market_id),
    event_id            VARCHAR NOT NULL,
    target_date         DATE NOT NULL,
    resolved            BOOLEAN NOT NULL,
    -- Official result. Exactly 0 or 1 once resolved; NULL until then.
    yes_price           DOUBLE,
    resolved_at_utc     TIMESTAMPTZ NOT NULL,
    source              VARCHAR NOT NULL,
    -- Our own station reading, recorded ONLY as an audit cross-check.
    cross_check_temp_f  DOUBLE,
    notes               VARCHAR,
    CHECK (yes_price IS NULL OR yes_price IN (0.0, 1.0))
);

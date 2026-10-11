-- Shadow engine: restartable ingest state and the forecast cache.
--
-- The tailer's progress and the in-memory view it feeds are committed in the
-- SAME transaction, so a crash replays a batch rather than skipping it. That
-- is what makes the reader exactly-once without storing one row per archive
-- line: the offset cannot advance past effects that were not applied.

-- Byte offset already consumed, per archive file. `file_identity` is the
-- (size, mtime) observed when the offset was last written; a file that now
-- matches neither the identity nor the expected length has been rotated or
-- truncated, which is a recorded event rather than a silent rewind.
CREATE TABLE ingest_checkpoints (
    file_path               VARCHAR PRIMARY KEY,
    source                  VARCHAR NOT NULL,
    offset_bytes            BIGINT NOT NULL,
    lines_processed         BIGINT NOT NULL DEFAULT 0,
    last_run_id             VARCHAR,
    last_archive_seq        BIGINT,
    last_received_at_utc    TIMESTAMPTZ,
    file_identity           VARCHAR,
    updated_at_utc          TIMESTAMPTZ NOT NULL
);

-- Latest market metadata parsed from `gamma`, first-write-wins on bracket
-- columns mirrors the collector: a bracket is an identity of the market, not
-- something a refresh may silently redefine.
CREATE TABLE market_catalog (
    market_id           VARCHAR PRIMARY KEY,
    event_id            VARCHAR NOT NULL,
    slug                VARCHAR,
    question            VARCHAR,
    bracket_label       VARCHAR,
    bracket_kind        VARCHAR,
    bracket_low         DOUBLE,
    bracket_high        DOUBLE,
    bracket_rounding    VARCHAR,
    token_yes           VARCHAR NOT NULL,
    token_no            VARCHAR NOT NULL,
    target_date         DATE,
    station             VARCHAR NOT NULL,
    tick_size           DOUBLE,
    min_order_size      DOUBLE,
    fee_rate            DOUBLE,
    fee_exponent        DOUBLE,
    active              BOOLEAN NOT NULL DEFAULT FALSE,
    closed              BOOLEAN NOT NULL DEFAULT FALSE,
    accepting_orders    BOOLEAN NOT NULL DEFAULT FALSE,
    raw_sha256          VARCHAR,
    source_run_id       VARCHAR,
    first_seen_utc      TIMESTAMPTZ NOT NULL,
    last_seen_utc       TIMESTAMPTZ NOT NULL
);

-- Latest complete, defect-free book per token. One-sided books are kept: a
-- missing side is a real market state and must reach the decision as
-- INCOMPLETE_ORDERBOOK rather than being dropped on the way in.
CREATE TABLE token_books (
    token_id                    VARCHAR PRIMARY KEY,
    market_id                   VARCHAR NOT NULL,
    book_id                     VARCHAR NOT NULL,
    book_source                 VARCHAR NOT NULL,
    bids_json                   VARCHAR NOT NULL,
    asks_json                   VARCHAR NOT NULL,
    venue_ts_utc                TIMESTAMPTZ,
    venue_ts_origin             VARCHAR NOT NULL,
    collector_received_at_utc   TIMESTAMPTZ NOT NULL,
    shadow_received_at_utc      TIMESTAMPTZ NOT NULL,
    quality                     VARCHAR NOT NULL,
    defects                     VARCHAR NOT NULL DEFAULT '',
    updated_at_utc              TIMESTAMPTZ NOT NULL
);

-- The forecast cache: which runs have been fetched, and their daily maxima.
-- Fetched once per (model, run_time); a restart must not re-hit upstream for
-- a run it already holds, and `status` records the failures too.
CREATE TABLE forecast_runs (
    model                   VARCHAR NOT NULL,
    run_time_utc            TIMESTAMPTZ NOT NULL,
    status                  VARCHAR NOT NULL,        -- ok | unavailable | failed
    http_status             INTEGER,
    error_text              VARCHAR,
    payload_sha256          VARCHAR,
    publishable_at_utc      TIMESTAMPTZ,
    publishable_basis       VARCHAR NOT NULL DEFAULT 'estimated',
    received_at_utc         TIMESTAMPTZ NOT NULL,
    n_points                INTEGER,
    PRIMARY KEY (model, run_time_utc)
);

CREATE TABLE forecast_daily_max (
    model               VARCHAR NOT NULL,
    run_time_utc        TIMESTAMPTZ NOT NULL,
    target_date         DATE NOT NULL,
    station             VARCHAR NOT NULL,
    predicted_max_f     DOUBLE,
    n_hours             INTEGER NOT NULL,
    n_missing_hours     INTEGER NOT NULL,
    is_complete         BOOLEAN NOT NULL,
    computed_at_utc     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (model, run_time_utc, target_date, station)
);

CREATE INDEX idx_catalog_target ON market_catalog (target_date);
CREATE INDEX idx_catalog_open ON market_catalog (active, accepting_orders, closed);
CREATE INDEX idx_books_market ON token_books (market_id);
CREATE INDEX idx_daily_max_target ON forecast_daily_max (target_date, model);

-- WeatherQuant Next - performance streaming state, migration 0001.
--
-- This is the RESEARCH side's own state, and it deliberately lives in a file of
-- its own rather than in the shadow's database (a single writer holds that
-- lease for its whole life) or in the research database (whose writer is the
-- legacy importer / forecast backfill, not this process).
--
-- What is here, and why each table exists:
--
--   performance_ingest          the singleton cursor and its folded parameters
--   performance_parts           one row per consumed export part (integrity proof)
--   performance_episodes        closed AND still-open episodes; the open set is
--                               the checkpoint that survives a crash
--   performance_edge_observations  every ACCEPT with a usable edge, for exact
--                               quantiles and the published edge-band arrays
--   performance_settlements     the settlement layer, revised independently of
--                               the episodes
--   performance_markets_seen / target_dates_seen / models_seen
--                               distinct sets over ALL decisions (coverage)
--
-- Conventions: times are TIMESTAMPTZ in UTC, and a five-dimensional episode
-- key may carry NULLs (DuckDB PRIMARY KEYs may not), so identity is hashed
-- with a sentinel that cannot collide with a real value.

CREATE TABLE performance_ingest (
    id                          INTEGER PRIMARY KEY,
    algorithm_version           VARCHAR NOT NULL,
    input_source                VARCHAR NOT NULL,
    fold_gap_window_seconds     DOUBLE NOT NULL,
    fold_accepted_state         VARCHAR NOT NULL,
    last_part_index             BIGINT NOT NULL,
    last_end_cursor_time        TIMESTAMPTZ,
    last_end_cursor_id          VARCHAR,
    rows_consumed               BIGINT NOT NULL,
    row_seq                     BIGINT NOT NULL,
    last_instant                TIMESTAMPTZ,
    prev_instant                TIMESTAMPTZ,
    decisions_seen              BIGINT NOT NULL,
    decisions_with_market       BIGINT NOT NULL,
    accepts_seen                BIGINT NOT NULL,
    not_accept                  BIGINT NOT NULL,
    accepts_without_edge        BIGINT NOT NULL,
    accepts_without_edge_market BIGINT NOT NULL,
    first_decision_utc          TIMESTAMPTZ,
    last_decision_utc           TIMESTAMPTZ,
    updated_at_utc              TIMESTAMPTZ NOT NULL
);

CREATE TABLE performance_parts (
    part_index          BIGINT PRIMARY KEY,
    rows                BIGINT NOT NULL,
    end_cursor_time     TIMESTAMPTZ NOT NULL,
    end_cursor_id       VARCHAR NOT NULL,
    sha256              VARCHAR NOT NULL,
    consumed_at_utc     TIMESTAMPTZ NOT NULL
);

CREATE TABLE performance_episodes (
    -- Identity is the episode id, not the five-dimensional key: one key can
    -- produce several episodes over time (a gap closes one and a later ACCEPT
    -- opens the next), and keying on the key alone would silently overwrite the
    -- earlier episode with the later one. The key is kept alongside for
    -- lookups.
    episode_id              VARCHAR PRIMARY KEY,
    key_hash                VARCHAR NOT NULL,
    market_id               VARCHAR NOT NULL,
    target_date             VARCHAR,
    model_name              VARCHAR,
    direction               VARCHAR,
    mode                    VARCHAR,
    first_seen_utc          TIMESTAMPTZ NOT NULL,
    last_seen_utc           TIMESTAMPTZ NOT NULL,
    observation_count       BIGINT NOT NULL,
    edge_count              BIGINT NOT NULL,
    sum_edges               DOUBLE NOT NULL,
    first_edge              DOUBLE NOT NULL,
    last_edge               DOUBLE NOT NULL,
    max_edge                DOUBLE NOT NULL,
    min_edge                DOUBLE NOT NULL,
    first_market_price      DOUBLE,
    first_model_probability DOUBLE,
    first_available_depth_usd DOUBLE,
    station                 VARCHAR,
    bracket_label           VARCHAR,
    market_question         VARCHAR,
    fee_rate                DOUBLE,
    is_open                 BOOLEAN NOT NULL,
    end_reason              VARCHAR,
    opened_seq              BIGINT NOT NULL,
    closed_seq              BIGINT,
    ingested_at_utc        TIMESTAMPTZ NOT NULL
);

CREATE TABLE performance_edge_observations (
    seq             BIGINT NOT NULL,
    target_date     VARCHAR,
    model_name      VARCHAR,
    direction       VARCHAR,
    mode            VARCHAR,
    edge            DOUBLE NOT NULL
);

CREATE TABLE performance_settlements (
    key_hash        VARCHAR PRIMARY KEY,
    market_id       VARCHAR NOT NULL,
    target_date     VARCHAR,
    status          VARCHAR NOT NULL,
    payout          DOUBLE,
    verified        BOOLEAN NOT NULL,
    fetched_at_utc  TIMESTAMPTZ NOT NULL,
    updated_at_utc  TIMESTAMPTZ NOT NULL
);

CREATE TABLE performance_markets_seen (
    market_id       VARCHAR PRIMARY KEY
);

CREATE TABLE performance_target_dates_seen (
    target_date     VARCHAR PRIMARY KEY
);

CREATE TABLE performance_models_seen (
    model_name      VARCHAR PRIMARY KEY
);

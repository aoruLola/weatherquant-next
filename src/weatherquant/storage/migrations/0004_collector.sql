-- P1-0 collector support: provenance, run bookkeeping and gap records.
--
-- Provenance rule this migration exists to enforce:
--
--   venue_ts  = what the exchange said, when IT says it happened
--   fetched_at_utc = when WE captured it
--
-- The two are never interchangeable. `venue_ts_origin` records whether a row's
-- venue timestamp came from the exchange ('venue') or was absent upstream
-- ('missing'). There is deliberately no code path that writes a local clock
-- into `venue_ts`: a missing exchange timestamp stays NULL forever, because a
-- fabricated event time silently corrupts every latency and staleness measure
-- computed downstream.
--
-- DuckDB will not accept NOT NULL on ALTER TABLE ADD COLUMN, so these columns
-- are nullable with defaults and the value domain is enforced in Python.

ALTER TABLE order_books ADD COLUMN source VARCHAR DEFAULT 'rest';          -- rest | ws
ALTER TABLE order_books ADD COLUMN venue_ts_origin VARCHAR;                -- venue | missing
ALTER TABLE order_books ADD COLUMN content_hash VARCHAR;
ALTER TABLE order_books ADD COLUMN quality VARCHAR DEFAULT 'ok';           -- ok | <defect code>
ALTER TABLE order_books ADD COLUMN run_id VARCHAR;

-- price_history already carries `source` from migration 0002; only provenance
-- and run attribution are added here.
ALTER TABLE price_history ADD COLUMN venue_ts_origin VARCHAR;              -- venue | missing
ALTER TABLE price_history ADD COLUMN run_id VARCHAR;

ALTER TABLE events ADD COLUMN source VARCHAR DEFAULT 'gamma';
ALTER TABLE events ADD COLUMN run_id VARCHAR;

ALTER TABLE markets ADD COLUMN source VARCHAR DEFAULT 'gamma';
ALTER TABLE markets ADD COLUMN run_id VARCHAR;

CREATE INDEX idx_order_books_hash ON order_books (token_id, content_hash);
CREATE INDEX idx_order_books_venue_ts ON order_books (venue_ts);
CREATE INDEX idx_price_history_run ON price_history (run_id);

-- ---------------------------------------------------------------------------
-- One row per worker process lifetime.
-- ---------------------------------------------------------------------------
CREATE TABLE collector_runs (
    run_id          VARCHAR PRIMARY KEY,
    started_at_utc  TIMESTAMPTZ NOT NULL,
    stopped_at_utc  TIMESTAMPTZ,
    app_version     VARCHAR NOT NULL,
    git_sha         VARCHAR,
    image_tag       VARCHAR,
    hostname        VARCHAR,
    pid             INTEGER,
    status          VARCHAR NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'stopped', 'failed')),
    stop_reason     VARCHAR,
    stats_json      VARCHAR NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Data gaps. Every interruption in collection is recorded, never silently
-- absorbed: an update, a network outage and a container restart all leave a
-- hole in the historical order book that later research must know about.
-- ---------------------------------------------------------------------------
CREATE TABLE collection_gaps (
    gap_id          VARCHAR PRIMARY KEY,
    run_id          VARCHAR,
    reason          VARCHAR NOT NULL,   -- start_after_restart | network_outage | upstream_error | update | shutdown
    started_at_utc  TIMESTAMPTZ NOT NULL,   -- when the gap began (last good data / process start)
    detected_at_utc TIMESTAMPTZ NOT NULL,
    ended_at_utc    TIMESTAMPTZ,            -- NULL while still open
    detail          VARCHAR
);

CREATE INDEX idx_collection_gaps_start ON collection_gaps (started_at_utc);

-- ---------------------------------------------------------------------------
-- Persistent structured events. stdout logs rotate away with the container;
-- this survives so "what happened during the last update?" stays answerable.
-- ---------------------------------------------------------------------------
CREATE TABLE collector_events (
    event_id        BIGINT PRIMARY KEY,
    run_id          VARCHAR,
    ts_utc          TIMESTAMPTZ NOT NULL,
    level           VARCHAR NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error', 'critical')),
    kind            VARCHAR NOT NULL,
    message         VARCHAR,
    details_json    VARCHAR
);

CREATE INDEX idx_collector_events_ts ON collector_events (ts_utc);
CREATE INDEX idx_collector_events_level ON collector_events (level, ts_utc);

-- No sequence for collector_events.event_id.
--
-- DuckDB sequences were tried and are NOT safe here: an increment performed in
-- one session was observed not to survive close/reopen (measured 1 -> 2 -> 2
-- across three sessions with a CHECKPOINT between each), so two runs of the
-- collector could mint the same id and violate the primary key. Event ids are
-- instead derived from the wall clock plus a per-process counter in
-- weatherquant.data.writer.next_event_id hashes (run_id, sequence) instead, which
-- needs no durable state and does not depend on a monotonic clock.

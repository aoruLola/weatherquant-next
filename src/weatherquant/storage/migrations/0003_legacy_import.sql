-- Legacy import provenance.
--
-- Every table that can receive a row carried over from the previous project
-- gains four columns:
--
--   verification_status  'native' | 'verified' | 'pending_verification'
--   verification_reason  why a row is not trusted, or NULL
--   legacy_source_table  which legacy table the row came from, or NULL
--   legacy_source_db     path of the legacy database it was read from, or NULL
--
-- DuckDB rejects NOT NULL and CHECK on ALTER TABLE ADD COLUMN ("Adding columns
-- with constraints not yet supported"), so only DEFAULT could be declared here.
-- The value domain is therefore enforced by weatherquant.legacy.rules
-- .VerificationStatus (an enum) at write time, and by the fact that every writer
-- in this codebase sets the column explicitly rather than relying on the default.
--
-- 'native' means the row was produced by this system (the default), so a query
-- never has to guess whether a row's provenance was recorded. A migrated row is
-- either 'verified' (it passed the rules in weatherquant.legacy.rules) or
-- 'pending_verification' (it did not). Nothing is ever silently promoted to
-- trusted: classification happens once at import and is then immutable.
--
-- The importer itself writes only to THIS database; it opens the legacy file
-- with read_only=True (weatherquant.storage.connect_legacy_read_only).

ALTER TABLE events ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE events ADD COLUMN verification_reason VARCHAR;
ALTER TABLE events ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE events ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE markets ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE markets ADD COLUMN verification_reason VARCHAR;
ALTER TABLE markets ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE markets ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE order_books ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE order_books ADD COLUMN verification_reason VARCHAR;
ALTER TABLE order_books ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE order_books ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE forecast_runs ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE forecast_runs ADD COLUMN verification_reason VARCHAR;
ALTER TABLE forecast_runs ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE forecast_runs ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE forecast_hourly ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE forecast_hourly ADD COLUMN verification_reason VARCHAR;
ALTER TABLE forecast_hourly ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE forecast_hourly ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE forecast_daily ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE forecast_daily ADD COLUMN verification_reason VARCHAR;
ALTER TABLE forecast_daily ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE forecast_daily ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE observations ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE observations ADD COLUMN verification_reason VARCHAR;
ALTER TABLE observations ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE observations ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE daily_max_temperatures ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE daily_max_temperatures ADD COLUMN verification_reason VARCHAR;
ALTER TABLE daily_max_temperatures ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE daily_max_temperatures ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE settlements ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE settlements ADD COLUMN verification_reason VARCHAR;
ALTER TABLE settlements ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE settlements ADD COLUMN legacy_source_db VARCHAR;

ALTER TABLE price_history ADD COLUMN verification_status VARCHAR DEFAULT 'native';
ALTER TABLE price_history ADD COLUMN verification_reason VARCHAR;
ALTER TABLE price_history ADD COLUMN legacy_source_table VARCHAR;
ALTER TABLE price_history ADD COLUMN legacy_source_db VARCHAR;

-- One row per import attempt. Recorded even for a dry run, because "we looked
-- at the legacy database on this date and saw N rows" is itself research state.
CREATE TABLE legacy_import_runs (
    run_id            VARCHAR PRIMARY KEY,
    source_db         VARCHAR NOT NULL,
    source_db_bytes   BIGINT,
    source_db_mtime   TIMESTAMPTZ,
    started_at_utc    TIMESTAMPTZ NOT NULL,
    finished_at_utc   TIMESTAMPTZ,
    dry_run           BOOLEAN NOT NULL,
    status            VARCHAR NOT NULL CHECK (status IN ('completed', 'failed')),
    error             VARCHAR,
    tables_json       VARCHAR NOT NULL DEFAULT '{}'
);

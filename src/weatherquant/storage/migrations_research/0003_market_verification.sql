-- The market-rule verification record: one row per (event, station, verifier
-- version), whatever the verdict. A mismatch is evidence, and evidence whose
-- provenance was discarded cannot be re-examined when the rules change - so
-- the rules hash, parser version and verifier version travel with every row,
-- and a re-run under a NEW verifier version is a new row, never an overwrite
-- of the old one.
--
-- Distinct from `settlement_crosscheck`, which records the legacy-archive
-- comparison: this table is the per-market-rules verification (WQN-33), whose
-- inputs are the parsed settlement terms and the current observation sources.
CREATE TABLE market_rule_verification (
    event_id                VARCHAR NOT NULL,
    target_date             DATE NOT NULL,
    station                 VARCHAR NOT NULL,
    verdict                 VARCHAR NOT NULL,
    detail                  VARCHAR,
    observed_max_f          DOUBLE,
    observed_max_whole_f    INTEGER,
    observation_picks_venue_winner  BOOLEAN,
    venue_bracket_contains_observation BOOLEAN,
    local_day_start_utc     TIMESTAMPTZ NOT NULL,
    local_day_end_utc       TIMESTAMPTZ NOT NULL,
    expected_hours          INTEGER NOT NULL,
    distinct_hours          INTEGER NOT NULL,
    n_readings              INTEGER NOT NULL,
    n_excluded_special      INTEGER NOT NULL DEFAULT 0,
    aggregation_rule        VARCHAR NOT NULL,
    unit                    VARCHAR,
    precision_deg           DOUBLE,
    rules_sha256            VARCHAR,
    parser_version          VARCHAR,
    verifier_version        VARCHAR NOT NULL,
    observation_sources_json VARCHAR,
    verified_at_utc         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, station, target_date, verifier_version)
);

CREATE INDEX idx_market_verify_verdict ON market_rule_verification (verdict);

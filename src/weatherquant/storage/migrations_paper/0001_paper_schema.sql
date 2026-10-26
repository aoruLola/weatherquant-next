-- Paper engine: foundation schema.
--
-- A fourth database file, on purpose and for the same reason the shadow got
-- its own: the paper engine is a fourth lifetime (long-running, single-writer,
-- restart-sensitive) and it must not be able to touch the collector's, the
-- research or the shadow database even by mistake. Same checksummed,
-- forward-only migrator, fourth directory.
--
-- What this migration deliberately does NOT contain: positions, ledger,
-- fills, settlements. Those belong to the tickets that introduce the logic
-- that writes them (WQN-41/42/44); a schema written ahead of its writer is a
-- guess about that writer's shape, and the migrator cannot take a guess back.
--
-- Everything here is simulation state. There is no order, no venue client and
-- no key material anywhere in this schema or the code that owns it.

-- One row per paper process run, mirroring shadow_runs so an operator reads
-- the two the same way.
CREATE TABLE paper_runs (
    run_id              VARCHAR PRIMARY KEY,
    started_at_utc      TIMESTAMPTZ NOT NULL,
    stopped_at_utc      TIMESTAMPTZ,
    status              VARCHAR NOT NULL DEFAULT 'running',   -- running | stopped | failed
    stop_reason         VARCHAR,
    app_version         VARCHAR,
    git_sha             VARCHAR,
    hostname            VARCHAR,
    pid                 INTEGER,
    config_json         VARCHAR NOT NULL,
    stats_json          VARCHAR
);

-- The virtual account. Singleton by natural key: a second `init` must find
-- this row rather than mint a second balance. The balance itself lives with
-- the ledger (WQN-42); here only the account's identity and its one
-- immutable fact - the capital it was opened with.
CREATE TABLE paper_accounts (
    account_id              VARCHAR PRIMARY KEY,      -- singleton 'paper-main'
    initial_capital_usdc    DOUBLE NOT NULL CHECK (initial_capital_usdc > 0),
    currency                VARCHAR NOT NULL DEFAULT 'USDC',
    created_at_utc          TIMESTAMPTZ NOT NULL
);

-- Every LIVE decision event the engine consumed, and what became of it.
-- The primary key IS the shadow's own deterministic decision id, which is
-- what makes "replay the same signal after a restart" a no-op rather than a
-- second trade: the row already exists and the insert does nothing.
-- `outcome` is ACCEPTED | REFUSED | PENDING; every refusal carries a reason
-- from the paper engine's own vocabulary plus the detail that produced it.
CREATE TABLE paper_signals (
    decision_id                 VARCHAR PRIMARY KEY,
    run_id                      VARCHAR NOT NULL,
    cycle_id                    VARCHAR NOT NULL,
    market_id                   VARCHAR NOT NULL,
    event_id                    VARCHAR,
    token_id                    VARCHAR,
    bracket_label               VARCHAR,
    direction                   VARCHAR NOT NULL,       -- buy_yes | sell_yes
    state                       VARCHAR NOT NULL,
    mode                        VARCHAR NOT NULL,       -- LIVE | REPLAY | UNKNOWN
    decision_time_utc           TIMESTAMPTZ NOT NULL,
    net_edge                    DOUBLE,
    best_bid                    DOUBLE,
    best_ask                    DOUBLE,
    book_id                     VARCHAR,
    snapshot_id                 VARCHAR,
    venue_ts_utc                TIMESTAMPTZ,
    collector_received_at_utc   TIMESTAMPTZ,
    thresholds_version          VARCHAR,
    fee_rate                    DOUBLE,
    fee_exponent                DOUBLE,
    tradeable                   BOOLEAN NOT NULL,       -- the contract's verdict (LIVE + ACCEPT)
    outcome                     VARCHAR NOT NULL,
    refusal_reason              VARCHAR,
    refusal_detail              VARCHAR,
    consumed_at_utc             TIMESTAMPTZ NOT NULL,
    event_json                  VARCHAR NOT NULL        -- the full export row, verbatim
);

CREATE INDEX idx_paper_signals_outcome ON paper_signals (outcome, consumed_at_utc);
CREATE INDEX idx_paper_signals_market ON paper_signals (market_id, decision_time_utc);

-- Small durable key/value store for the consumer's read cursor and similar
-- single-slot facts. Written in the SAME transaction as the effects the
-- cursor describes, so a crash either advances both or neither - the same
-- rule the shadow's ingest checkpoints follow.
CREATE TABLE paper_state (
    key             VARCHAR PRIMARY KEY,
    value_json      VARCHAR NOT NULL,
    updated_at_utc  TIMESTAMPTZ NOT NULL
);

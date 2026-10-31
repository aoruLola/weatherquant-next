-- DuckDB 1.1 rejects an UPSERT that assigns a column covered by an ART
-- index, even when the target table is empty. These columns change when
-- the writer refreshes a book or re-observes a decision.
DROP INDEX IF EXISTS idx_books_market;
DROP INDEX IF EXISTS idx_decisions_state;
DROP INDEX IF EXISTS idx_decisions_mode;
DROP INDEX IF EXISTS idx_decisions_created;
CREATE INDEX idx_decisions_cycle ON shadow_decisions (cycle_id);

-- The ordinal is the position in the current decision's ordered reason list.
-- Keep the reason identity as the primary key. The writer replaces stale
-- reasons and assigns each current reason one distinct position.
CREATE TEMP TABLE old_shadow_reasons AS SELECT * FROM rejection_reasons;
DROP TABLE rejection_reasons;
CREATE TABLE rejection_reasons (
    decision_id VARCHAR NOT NULL,
    ordinal INTEGER NOT NULL,
    reason VARCHAR NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    detail VARCHAR,
    PRIMARY KEY (decision_id, reason)
);
INSERT INTO rejection_reasons SELECT decision_id, ordinal, reason, is_primary, detail FROM old_shadow_reasons;
DROP TABLE old_shadow_reasons;
CREATE INDEX idx_reasons_reason ON rejection_reasons (reason);

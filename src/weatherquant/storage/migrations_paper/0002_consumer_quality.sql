-- Paper engine: the consumer's audit trail.
--
-- WQN-40 adds the LIVE signal consumer. The signals themselves already have
-- a table (0001); what was missing is the place for everything a consumed
-- line produces that is NOT a signal: a line that does not parse, an event
-- that fails the contract, and the redelivery of a decision this engine has
-- already taken (the primary key absorbs the row; the redelivery is still a
-- fact worth recording, because "the cursor was rewound and nothing doubled"
-- is exactly the claim this table lets an auditor verify).
--
-- `event_id` is a deterministic hash of (kind, path, line, detail), so a
-- replayed batch re-inserts nothing - the same natural-key rule as every
-- other table in this schema.

CREATE TABLE paper_quality_events (
    event_id    VARCHAR PRIMARY KEY,
    ts_utc      TIMESTAMPTZ NOT NULL,
    kind        VARCHAR NOT NULL,       -- malformed_line | malformed_event | signal_duplicate | consumer_note
    severity    VARCHAR NOT NULL,       -- info | warning | error
    detail      VARCHAR NOT NULL,
    source      VARCHAR,
    path        VARCHAR,
    line_no     BIGINT
);

CREATE INDEX idx_paper_quality_ts ON paper_quality_events (ts_utc);
CREATE INDEX idx_paper_quality_kind ON paper_quality_events (kind, ts_utc);

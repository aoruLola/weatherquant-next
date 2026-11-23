-- Nullable additions leave historical snapshots identifiable after upgrade.
ALTER TABLE token_books ADD COLUMN source_event_id VARCHAR;
ALTER TABLE shadow_decisions ADD COLUMN evidence_version INTEGER;
ALTER TABLE shadow_decisions ADD COLUMN book_source_event_id VARCHAR;
ALTER TABLE shadow_decisions ADD COLUMN book_source VARCHAR;
ALTER TABLE shadow_decisions ADD COLUMN fee_rate DOUBLE;
ALTER TABLE shadow_decisions ADD COLUMN fee_exponent DOUBLE;

-- Depth is content-addressed, so repeated decisions and both trade directions
-- share one copy. The event that supplied it belongs to the individual snapshot.
CREATE TABLE decision_book_depth (
    book_id VARCHAR PRIMARY KEY,
    book_bids_json VARCHAR NOT NULL,
    book_asks_json VARCHAR NOT NULL
);

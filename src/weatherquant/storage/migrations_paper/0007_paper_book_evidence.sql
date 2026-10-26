-- Paper engine: full two-sided book evidence on every fill (WQN-24).
--
-- The consumed_levels a fill walked are the trade's arithmetic; they are
-- not the market's picture. A fill row that must be audited - or re-priced
-- by a backtest - needs BOTH sides' levels as the decision faced them, the
-- token, the timestamps and the identity of the exact archive line the
-- evidence came from. All three columns ride the fill row, so the evidence
-- is joinable to its decision by the row's own key.
--
-- The JSON is the collector's canonical level form, so the stored bids and
-- asks hash back to the same book_id the row already carries.

-- DuckDB refuses ADD COLUMN with constraints (the shadow's own migrations
-- record this), so the columns are nullable with a backfilling DEFAULT.
-- Every write path supplies all three values explicitly, so the default is
-- a floor rather than a state the engine can end up in by omission.
ALTER TABLE paper_fills ADD COLUMN book_bids_json VARCHAR DEFAULT '[]';
ALTER TABLE paper_fills ADD COLUMN book_asks_json VARCHAR DEFAULT '[]';
ALTER TABLE paper_fills ADD COLUMN book_source_event_id VARCHAR DEFAULT '';

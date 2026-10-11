-- Shadow engine: LIVE vs REPLAY provenance.
--
-- A shadow that reads an archive written yesterday must not present its output
-- as a decision made today. The two are different claims:
--
--   LIVE    the engine is tailing a feed that should be current; the decision
--           instant is "now", and silence is COLLECTOR_DISCONNECTED.
--   REPLAY  the engine is re-reading historical archive; the decision instant
--           is the archive's own latest event time, and every input is judged
--           as of THAT instant, not as of the wall clock.
--
-- `mode_reason` records how the mode was chosen (configured, or the archive age
-- that triggered auto-detection), so "why is this row marked REPLAY?" is
-- answerable from the row alone.
--
-- DuckDB refuses `ADD COLUMN ... NOT NULL` ("Adding columns with constraints
-- not yet supported"), so both columns are added nullable. DEFAULT still
-- backfills rows that already exist, and every write path supplies both
-- values explicitly, so the default is a floor rather than a state the engine
-- can end up in by omission.

ALTER TABLE shadow_decisions ADD COLUMN mode VARCHAR DEFAULT 'LIVE';
ALTER TABLE shadow_decisions ADD COLUMN mode_reason VARCHAR;

CREATE INDEX idx_decisions_mode ON shadow_decisions (mode, created_at_utc);

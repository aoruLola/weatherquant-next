-- WeatherQuant Next - performance streaming state, migration 0003.
--
-- Ingest position on the export's write sequence, and late-row accounting.
--
-- The shadow export (WQN-20) moved its positioning cursor from business time
-- (decision_time_utc, decision_id) to a monotonic write sequence,
-- `export_seq`, because business time is revisable by the data: a REPLAY run
-- writes decisions whose business instant is in the past, and such a row used
-- to sit behind the watermark forever. From that change on, a published part
-- can contain rows whose business time sits BELOW what was already consumed.
--
-- Two consequences for this store, both additive:
--
-- * `last_export_seq` is the highest export_seq already folded. It is what
--   makes the order check exact again: rows that carry a sequence must strictly
--   increase in it across the whole concatenation of parts, which detects a
--   duplicated or reordered line without caring about business time at all.
--   NULL means "only legacy parts consumed so far" - a different fact from 0,
--   which would claim the first sequence position was reached.
--
-- * `late_rows_seen` counts the rows that arrived with a business instant at
--   or below the running maximum. It is observability, not logic: a REPLAY
--   backfill shows up here as delivery, and the fold's activity index stays
--   correct because late instants no longer move it (see episodes.py).
--
-- 0001 is not edited. An applied migration is immutable - its checksum is in
-- the ledger, and rewriting it would make every existing store refuse to open.

ALTER TABLE performance_ingest ADD COLUMN last_export_seq BIGINT;
ALTER TABLE performance_ingest ADD COLUMN late_rows_seen BIGINT DEFAULT 0;

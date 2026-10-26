-- Paper engine: a monotonic sequence on the ledger.
--
-- `entry_id` is a content hash with no order, and `ts_utc` ties whenever a
-- batch settles several fills in one instant - which is the normal case, so
-- ordering the ledger by (ts, entry_id) shuffles those entries and breaks
-- the running-balance chain an auditor walks. `seq` is the insertion order:
-- the order the engine actually moved money in, which is the order every
-- replay must follow.
--
-- Added nullable (DuckDB cannot ADD COLUMN ... NOT NULL); every fresh
-- database applies this before any row exists, and the writer always
-- supplies it.

ALTER TABLE paper_ledger ADD COLUMN seq BIGINT;

-- Shadow engine: a monotonic write sequence for the decision export.
--
-- PRODUCTION DEFECT (WQN-20). The historical export positioned itself on
-- (decision_time_utc, decision_id). Business time is set by the engine but it
-- is *revisable by the data*: a REPLAY run re-reads yesterday's archive and
-- writes decisions whose business instant is in the past. Such a row is
-- written late with an early instant, it lands permanently behind the export
-- watermark, and no later batch can ever select it - the cursor only moves
-- forward in business time. The row exists in the database, is absent from
-- the export, and nothing downstream can notice: the parts are internally
-- consistent, they simply do not contain it.
--
-- THE FIX: position the export on a monotonic write sequence instead.
-- `export_seq` is assigned once, when a decision row is first inserted, from
-- a DuckDB sequence. It never moves: the ON CONFLICT path in save_decisions
-- does not update it, so re-writing a decision (the duplicate-write case)
-- neither re-exports it nor duplicates it. Late rows get fresh, high
-- sequence values and are therefore always ahead of the cursor - which is
-- the whole point.
--
-- WHY A COLUMN AND NOT rowid: rowid follows physical storage. A conflict
-- update deletes and re-inserts the row, and the new physical row can reuse
-- a lower rowid, which would move an already-exported decision back into the
-- un-exported range. An explicit sequence is stable under updates.
--
-- BACKFILL ORDER. Existing rows are numbered in the OLD export order
-- (decision_time_utc, decision_id). That makes the backfilled sequence an
-- extension of the ordering the parts on disk already have, so a v2 export
-- continuing a v1 directory keeps the parts' internal order as consistent
-- with the past as the data allows. Rows the v1 export skipped (the late
-- REPLAY rows this migration exists for) are interleaved in that order; the
-- exporter's one-time transition re-assigns them above the v1 floor rather
-- than skipping them - see shadow/export.py.
--
-- Rollback: this migration is additive (a sequence, a nullable column, an
-- index). An older image that does not know `export_seq` inserts NULL for it
-- on new rows; the v2 exporter repairs NULLs in bounded batches before use,
-- so returning to the old image and coming back loses nothing. No data is
-- rewritten, so there is nothing to undo.

CREATE SEQUENCE seq_shadow_decision_export START 1;

ALTER TABLE shadow_decisions ADD COLUMN export_seq BIGINT;

UPDATE shadow_decisions SET export_seq = sq.export_seq
FROM (
    SELECT decision_id,
           row_number() OVER (ORDER BY decision_time_utc, decision_id) AS export_seq
    FROM shadow_decisions
) sq
WHERE shadow_decisions.decision_id = sq.decision_id;

-- The sequence must sit ABOVE every backfilled value before any insert uses
-- it, or the first new row could collide with a backfilled one. DuckDB has
-- no setval, so the sequence is advanced by consuming exactly as many
-- values as the backfill used: nextval is called MAX(export_seq) times,
-- leaving the next value at MAX+1. An empty table consumes nothing and the
-- sequence starts at 1 as created.
SELECT nextval('seq_shadow_decision_export')
FROM range((SELECT COALESCE(MAX(export_seq), 0) FROM shadow_decisions));

CREATE INDEX idx_decisions_export_seq ON shadow_decisions (export_seq);

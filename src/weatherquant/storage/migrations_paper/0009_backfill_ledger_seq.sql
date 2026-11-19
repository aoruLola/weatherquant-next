-- Pre-0006 entries in this append-only ledger have no sequence. Keep every
-- existing cursor and place the legacy prefix before the first assigned one.
CREATE TEMP TABLE wq_legacy_ledger_seq AS
SELECT entry_id,
       COALESCE((SELECT MIN(seq) FROM paper_ledger), count(*) OVER () + 1)
       - count(*) OVER () + row_number() OVER (ORDER BY rowid) - 1 AS restored_seq
FROM paper_ledger WHERE seq IS NULL;

-- Check physical insertion order against the running cash recorded on each
-- entry before making it part of the audit trail. Migration is transactional.
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM (
        SELECT l.balance_after_usdc,
               SUM(l.amount_usdc) OVER (
                   ORDER BY COALESCE(l.seq, h.restored_seq) ROWS UNBOUNDED PRECEDING
               ) AS replayed
        FROM paper_ledger l LEFT JOIN wq_legacy_ledger_seq h USING (entry_id)
        WHERE EXISTS (SELECT 1 FROM wq_legacy_ledger_seq)
    ) audit WHERE ABS(replayed - balance_after_usdc) > 0.000001
) THEN error('Legacy paper ledger order does not reconcile with recorded balances; manual review required')
ELSE TRUE END;

UPDATE paper_ledger AS l SET seq = h.restored_seq
FROM wq_legacy_ledger_seq AS h WHERE l.entry_id = h.entry_id;
DROP TABLE wq_legacy_ledger_seq;

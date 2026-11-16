-- DuckDB 1.1 can reject an UPDATE of an indexed export_seq as a duplicate
-- decision primary key. The exporter must update this column for legacy
-- rows and rollback recovery. The sequence and decision keys stay intact.
DROP INDEX IF EXISTS idx_decisions_export_seq;

-- Shadow engine: the export-sequence index, in its own transaction.
--
-- Split out of 0005 deliberately: creating an index in the same transaction
-- as the backfill UPDATE fails on any non-empty table with "Cannot create
-- index with outstanding updates" (DuckDB 1.5.6), which would turn the
-- production upgrade into a startup outage. The migrator commits 0005
-- first, so this runs against settled data.
--
-- The index serves the exporter's positioning read,
-- `WHERE export_seq > ? ORDER BY export_seq LIMIT n`, which runs inside the
-- engine's tick budget on every export call.

CREATE INDEX idx_decisions_export_seq ON shadow_decisions (export_seq);

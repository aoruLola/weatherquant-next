-- DuckDB 1.1 rewrites a row when an indexed outcome changes, which can
-- conflict with that same signal's decision_id primary key during settlement.
-- Keep the primary key and the immutable market/time index.
DROP INDEX idx_paper_signals_outcome;

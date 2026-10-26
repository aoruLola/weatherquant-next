-- The write cursor remains export_seq. Episode boundaries use business time.
-- The old arrival-order fold discarded late REJECTs and could merge opportunities.
-- Existing derived state is upgraded by a streamed re-fold of its immutable parts.
CREATE TABLE performance_episode_rebuild (
    id INTEGER PRIMARY KEY,
    required BOOLEAN NOT NULL
);
INSERT INTO performance_episode_rebuild
SELECT 1, EXISTS(SELECT 1 FROM performance_ingest WHERE rows_consumed > 0);
UPDATE performance_ingest SET algorithm_version = 'performance-streaming-2'
WHERE algorithm_version = 'performance-streaming-1';

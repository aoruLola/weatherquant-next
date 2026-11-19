-- Previously a later ACCEPT could fill missing evidence on the opening row.
-- Rebuild derived episodes from the verified parts before the next publication.
UPDATE performance_episode_rebuild SET required = TRUE
WHERE id = 1 AND EXISTS (SELECT 1 FROM performance_ingest WHERE rows_consumed > 0);

UPDATE performance_ingest SET algorithm_version = 'performance-streaming-3'
WHERE algorithm_version = 'performance-streaming-2';

-- WeatherQuant Next - performance streaming state, migration 0002.
--
-- Settlement provenance.
--
-- 0001 stored a derived boolean (`verified`) and a timestamp, and dropped the
-- two fields that make the answer auditable: the platform's own verification
-- verdict and where the answer came from. The PR #11 review found that a run
-- without a settlement index keeps the answers already stored - which is the
-- point - but nothing in the published payload said *when* they were fetched
-- or *how* they were verified, so a stale answer was indistinguishable from a
-- fresh one. A reader could not tell whether the settlement layer had been
-- refreshed this morning or last month.
--
-- These are nullable rather than NOT NULL because rows written by 0001 have no
-- value for them, and inventing one would be exactly the failure this
-- migration exists to prevent: `NULL` means "not recorded", which is a
-- different claim from any verdict.
--
-- 0001 is not edited. An applied migration is immutable - its checksum is in
-- the ledger, and rewriting it would make every existing store refuse to open.

ALTER TABLE performance_settlements ADD COLUMN verification_status VARCHAR;
ALTER TABLE performance_settlements ADD COLUMN resolution_source VARCHAR;

-- 0001 made the retrieval time NOT NULL, which forced the writer to invent one
-- when the index did not supply it. That is worse than a missing value: an
-- answer of unknown age would have been indistinguishable from a fresh one.
-- NULL now means "not recorded", and `updated_at_utc` still records when the
-- row was stored - a different fact, kept in a different column.
ALTER TABLE performance_settlements ALTER COLUMN fetched_at_utc DROP NOT NULL;

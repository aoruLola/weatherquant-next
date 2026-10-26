-- Paper engine: official settlement, capital release and corrections.
--
-- Only the platform's VERIFIED resolution moves the ledger: `status` uses
-- performance/settlement.py's five-state vocabulary, and a payout exists
-- only for RESOLVED records whose price was 0 or 1 - the sentinel "69" and
-- every proposal-in-flight state are recorded here and nowhere else applied.
--
-- Revisions are rows, not edits: a later record for the same market carries
-- a higher `revision`, and a changed payout becomes a CORRECTION entry in
-- the ledger (compensation), never an UPDATE of history. When a correction
-- cannot be applied automatically - a clawback that would drive cash
-- negative, or a dispute that reopens a settled market - the row is flagged
-- `review_required` and the balances stay untouched.

CREATE TABLE paper_settlements (
    settlement_id   VARCHAR PRIMARY KEY,      -- deterministic: market_id | revision
    market_id       VARCHAR NOT NULL,
    revision        INTEGER NOT NULL,
    status          VARCHAR NOT NULL,         -- RESOLVED | DISPUTED | PENDING | UNAVAILABLE | UNVERIFIABLE
    yes_payout      DOUBLE,                   -- 0.0 | 1.0 when RESOLVED, NULL otherwise
    settled_at_utc  TIMESTAMPTZ,
    evidence_json   VARCHAR NOT NULL,
    applied         BOOLEAN NOT NULL DEFAULT FALSE,
    applied_at_utc  TIMESTAMPTZ,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    review_reason   VARCHAR,
    UNIQUE (market_id, revision)
);

CREATE INDEX idx_paper_settlements_market ON paper_settlements (market_id, revision);
CREATE INDEX idx_paper_settlements_review ON paper_settlements (review_required, settled_at_utc);

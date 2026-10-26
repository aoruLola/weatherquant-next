-- Paper engine: the double-sided ledger, positions and capital occupation.
--
-- Six things are accounted separately, because collapsing any two of them is
-- how a paper account starts lying:
--
--   available_usdc   cash free to commit to a new fill
--   occupied_usdc    capital locked inside open positions (the "frozen"
--                    bucket: it leaves at settlement or on a sale, never
--                    silently)
--   realized_pnl     from SALES only - an unsettled position has no
--                    realised profit, by rule
--   paper_positions  shares, average cost and cost basis per (market, token)
--   paper_ledger     every movement, append-only, with the running cash
--                    balance stamped on each entry
--   valuation        computed on read from live marks; a position whose
--                    mark cannot be established is MARK_UNAVAILABLE, never
--                    zero and never skipped
--
-- The conservation law the whole schema answers to:
--
--   available + occupied == initial_capital + realized_pnl
--
-- enforced here by CHECK constraints (no negative balances, ever) and by a
-- deterministic `entry_id`: a settle is identified by its reference, so
-- replaying a fill after a crash re-inserts nothing and debits nothing a
-- second time.

CREATE TABLE paper_balances (
    account_id          VARCHAR PRIMARY KEY,
    available_usdc      DOUBLE NOT NULL CHECK (available_usdc >= -1e-9),
    occupied_usdc       DOUBLE NOT NULL CHECK (occupied_usdc >= -1e-9),
    realized_pnl_usdc   DOUBLE NOT NULL,
    updated_at_utc      TIMESTAMPTZ NOT NULL
);

CREATE TABLE paper_positions (
    market_id               VARCHAR NOT NULL,
    token_id                VARCHAR NOT NULL,
    shares                  DOUBLE NOT NULL CHECK (shares >= -1e-9),
    avg_cost_usd_per_share  DOUBLE NOT NULL,
    cost_basis_usdc         DOUBLE NOT NULL CHECK (cost_basis_usdc >= -1e-9),
    opened_at_utc           TIMESTAMPTZ NOT NULL,
    updated_at_utc          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market_id, token_id)
);

CREATE TABLE paper_ledger (
    entry_id            VARCHAR PRIMARY KEY,   -- deterministic: kind | ref_id
    ts_utc              TIMESTAMPTZ NOT NULL,
    kind                VARCHAR NOT NULL,      -- OPEN | BUY_SETTLE | SELL_SETTLE | SETTLE_PAYOUT | CORRECTION | RELEASE
    currency            VARCHAR NOT NULL DEFAULT 'USDC',
    amount_usdc         DOUBLE NOT NULL,       -- signed, from the available-cash bucket's point of view
    balance_after_usdc  DOUBLE NOT NULL,
    ref_type            VARCHAR NOT NULL,      -- account | fill | settlement | manual
    ref_id              VARCHAR NOT NULL,
    detail_json         VARCHAR NOT NULL
);

CREATE INDEX idx_paper_ledger_ts ON paper_ledger (ts_utc);
CREATE INDEX idx_paper_ledger_ref ON paper_ledger (ref_type, ref_id);
CREATE INDEX idx_paper_ledger_kind ON paper_ledger (kind, ts_utc);

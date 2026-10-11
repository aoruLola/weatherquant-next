-- Market price history for the market-probability baseline.
--
-- The three model baselines answer "how good is the forecast". This table
-- answers the question they are all measured against: "how good was the
-- market". Without it, a model can be compared only to itself.
--
-- `mid_usd_per_share` is a MIDPOINT, never an executable price. It is stored
-- here because the baseline is about what the market *believed*, and belief is
-- best read at the mid. Nothing in this project may treat it as a fill price
-- (spec §六).
--
-- `decision_ts` is not a column: the caller chooses an instant inside the
-- point-in-time window and queries for the observation at or before it. Baking
-- one instant into the schema would silently make every date use the same one.
CREATE TABLE market_price_history (
    market_id           VARCHAR NOT NULL,
    token_id            VARCHAR NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    mid_usd_per_share   DOUBLE NOT NULL CHECK (mid_usd_per_share >= 0 AND mid_usd_per_share <= 1),
    source              VARCHAR NOT NULL,
    fetched_at_utc      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market_id, token_id, ts, source)
);

CREATE INDEX idx_market_price_market_ts ON market_price_history (market_id, ts);

-- Which market each bracket belongs to, so a price series can be attached to
-- the bracket it prices. Carried from settlement_outcome for convenience; the
-- authority is still the market table in the collector's database.
CREATE TABLE market_tokens (
    event_id        VARCHAR NOT NULL,
    target_date     DATE NOT NULL,
    market_id       VARCHAR NOT NULL,
    bracket_label   VARCHAR,
    token_id        VARCHAR NOT NULL,
    is_yes_token    BOOLEAN NOT NULL,
    PRIMARY KEY (market_id, token_id)
);

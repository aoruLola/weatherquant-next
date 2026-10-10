-- Market price history (mid prices).
--
-- IMPORTANT: `mid_usd_per_share` is a MIDPOINT. Polymarket publishes no
-- historical order-book API, so historical depth does not exist and these
-- points are not executable. The column is named for what it is so that no
-- call site can quietly treat a mid as a fill price (spec §六). Historical
-- execution costs are only knowable for windows this system captured itself.
CREATE TABLE price_history (
    market_id           VARCHAR NOT NULL,
    token_id            VARCHAR NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    mid_usd_per_share   DOUBLE NOT NULL CHECK (mid_usd_per_share >= 0 AND mid_usd_per_share <= 1),
    source              VARCHAR NOT NULL,
    fetched_at_utc      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market_id, token_id, ts, source)
);

CREATE INDEX idx_price_history_market_ts ON price_history (market_id, ts);

-- Paper engine: the simulated taker fill.
--
-- One row per FILLED signal, keyed by the signal's decision id: a decision
-- is filled exactly once or it is not filled, and a second fill attempt
-- finds the row rather than minting a second trade.
--
-- Every number here is traceable to published evidence: the book identity
-- and clocks name the exact market state the fill walked, `fee_rate` names
-- the schedule it was charged under, and `consumed_levels_json` lists the
-- price and the size taken from each level - so `fill_cost_usd` can be
-- recomputed from the row alone. `total_cost_usd` (buy) and
-- `net_proceeds_usd` (sell) are deliberately exclusive: recording one column
-- that means "cash out" for a buy and "cash in" for a sell is how a sign
-- error survives review.

CREATE TABLE paper_fills (
    decision_id                  VARCHAR PRIMARY KEY,
    market_id                    VARCHAR NOT NULL,
    token_id                     VARCHAR NOT NULL,
    direction                    VARCHAR NOT NULL,      -- buy_yes | sell_yes
    side                         VARCHAR NOT NULL,      -- buy (walks asks) | sell (walks bids)
    book_id                      VARCHAR NOT NULL,
    book_source                  VARCHAR NOT NULL,      -- ws | rest
    book_venue_ts_utc            TIMESTAMPTZ,
    book_collector_received_at_utc TIMESTAMPTZ NOT NULL,
    book_age_ms                  BIGINT,
    requested_shares             DOUBLE NOT NULL,
    filled_shares                DOUBLE NOT NULL,
    avg_price_usd_per_share      DOUBLE NOT NULL,
    best_price_usd_per_share     DOUBLE NOT NULL,
    slippage_usd_per_share       DOUBLE NOT NULL,
    fill_cost_usd                DOUBLE NOT NULL,
    fees_usd                     DOUBLE NOT NULL,
    execution_buffer_usd         DOUBLE NOT NULL,
    total_cost_usd               DOUBLE,                -- buy: fill + fees + buffer
    net_proceeds_usd             DOUBLE,                -- sell: fill - fees - buffer
    levels_consumed              INTEGER NOT NULL,
    complete                     BOOLEAN NOT NULL,
    consumed_levels_json         VARCHAR NOT NULL,
    decision_time_utc            TIMESTAMPTZ NOT NULL,
    filled_at_utc                TIMESTAMPTZ NOT NULL,
    fee_rate                     DOUBLE NOT NULL,
    thresholds_version           VARCHAR
);

CREATE INDEX idx_paper_fills_market ON paper_fills (market_id, decision_time_utc);
CREATE INDEX idx_paper_fills_filled ON paper_fills (filled_at_utc);

"""WQN-42 helper: build :class:`PaperFill` objects for ledger tests.

The defaults mirror a real fill of the fixture book: 100 shares swept at
VWAP 0.558 (55.80 notional), the venue fee for that price at rate 0.05, and
the execution buffer the shadow convention carries on the fill row (never
in the ledger).
"""

from __future__ import annotations

from datetime import UTC, datetime

import inspect

from paper_book_helpers import T_DECISION, TOKEN
from weatherquant.execution.pricing import taker_fee_usd
from weatherquant.paper.fills import PaperFill
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.risk import RiskGate, RiskRules

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


def make_fill(
    *,
    decision_id: str = "d" * 32,
    market_id: str = "mkt-1",
    token_id: str = TOKEN,
    direction: str = "buy_yes",
    side: str = "buy",
    shares: float = 100.0,
    price: float = 0.558,
    best: float = 0.55,
    fill_cost: float | None = None,
    fees_usd: float | None = None,
    fee_rate: float = 0.05,
    book_id: str = "b" * 64,
) -> PaperFill:
    cost = price * shares if fill_cost is None else fill_cost
    fee = (
        taker_fee_usd(filled_shares=shares, price_usd_per_share=price, fee_rate=fee_rate)
        if fees_usd is None
        else fees_usd
    )
    is_buy = side == "buy"
    return PaperFill(
        decision_id=decision_id,
        market_id=market_id,
        token_id=token_id,
        direction=direction,
        side=side,
        book_id=book_id,
        book_source="ws",
        book_venue_ts_utc=T_DECISION,
        book_collector_received_at_utc=T_DECISION,
        book_age_ms=20000,
        requested_shares=shares,
        filled_shares=shares,
        avg_price_usd_per_share=price,
        best_price_usd_per_share=best,
        slippage_usd_per_share=max(price - best, 0.0),
        fill_cost_usd=cost,
        fees_usd=fee,
        execution_buffer_usd=cost * 0.005,
        total_cost_usd=cost + fee + cost * 0.005 if is_buy else None,
        net_proceeds_usd=None if is_buy else cost - fee - cost * 0.005,
        levels_consumed=1,
        complete=True,
        consumed_levels=[{"price_usd_per_share": price, "size_shares_taken": shares}],
        decision_time_utc=T_DECISION,
        filled_at_utc=NOW,
        fee_rate=fee_rate,
        thresholds_version="v2",
    )


def make_risk(store, *, capital: float = 100.0, version: str = "test-unrestricted") -> RiskGate:
    """A gate with the caps off - for tests of pricing mechanics, not rules."""
    return RiskGate(
        Ledger(store),
        RiskRules(
            version=version,
            basis="test: caps off",
            max_opportunity_fraction=1.0,
            max_market_exposure_fraction=1.0,
        ),
        initial_capital_usdc=capital,
    )


def open_ledger_if_needed(store, *, capital: float = 100.0, now) -> None:
    """Open the account's books when this branch's FillEngine settles through them."""
    from weatherquant.paper.fills import FillEngine

    params = inspect.signature(FillEngine.__init__).parameters
    if "ledger" in params or "risk" in params:
        from weatherquant.paper.ledger import Ledger

        Ledger(store).open_account(initial_capital_usdc=capital, now=now)


def build_engine(store, raw_dir, *, clock, **overrides):
    """Construct this branch's FillEngine, adapting to its signature.

    The robustness regressions run on every branch of the stack; the fill
    engine gained `ledger` (WQN-42) and `risk` (WQN-43) along the way, so
    the arguments are supplied exactly when the constructor asks for them.
    """
    from weatherquant.paper.fills import FillEngine

    params = inspect.signature(FillEngine.__init__).parameters
    kwargs = dict(
        store=store,
        book_source=raw_dir,
        buffer_fraction=0.005,
        clock=clock,
    )
    if "ledger" in params and overrides.get("ledger") is not None:
        kwargs["ledger"] = overrides["ledger"]
    if "risk" in params:
        kwargs["risk"] = make_risk(store, capital=overrides.get("capital", 100.0))
    return FillEngine(**kwargs)


"""Shared execution pricing: the single source of truth for cost and edge.

Both the research signal path and the paper-trading fill path import from this
package. They must never recompute cost independently - duplicated pricing is
how fees end up counted twice and how a "signal that was profitable" turns out
to be a signal that was never executable.
"""

from weatherquant.execution.pricing import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    DepthFill,
    ExecutionCost,
    ExecutionIntent,
    FeeRole,
    FeeSchedule,
    FillPlan,
    PricingResult,
    PricingStatus,
    adverse_slippage_usd_per_share,
    build_fill_plan,
    edge_usd_per_share,
    expected_net_profit_usd,
    expected_net_profit_usd_per_share,
    max_fee_usd_per_share,
    price_execution,
    quote_age_seconds,
    sum_executable_expected_net_profit_usd,
    taker_fee_usd,
)

__all__ = [
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "DepthFill",
    "ExecutionCost",
    "ExecutionIntent",
    "FeeRole",
    "FeeSchedule",
    "FillPlan",
    "PricingResult",
    "PricingStatus",
    "adverse_slippage_usd_per_share",
    "build_fill_plan",
    "edge_usd_per_share",
    "expected_net_profit_usd",
    "expected_net_profit_usd_per_share",
    "max_fee_usd_per_share",
    "price_execution",
    "quote_age_seconds",
    "sum_executable_expected_net_profit_usd",
    "taker_fee_usd",
]

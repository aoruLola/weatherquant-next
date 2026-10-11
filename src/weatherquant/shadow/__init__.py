"""Shadow Trading Engine: a real-time decision *observation* system.

Reads the collector's archive read-only, fetches forecasts that are genuinely
publishable at the decision instant, prices against real order-book depth, and
records why every candidate was accepted, refused or never considered.

It cannot place an order. There is no wallet, no key, no order client and no
position ledger anywhere in this package - the boundary is structural, not a
flag someone could flip. See ``docs/shadow-engine.md``.
"""

from weatherquant.shadow.config import ShadowSettings, Thresholds, load_thresholds

__all__ = ["ShadowSettings", "Thresholds", "load_thresholds"]

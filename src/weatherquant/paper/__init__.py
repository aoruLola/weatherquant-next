"""Paper trading: a simulated account fed by the shadow engine's LIVE decisions.

The paper engine answers one question - *what would this desk have if it had
taken every ACCEPT the shadow produced?* - without ever being able to answer
a different one. There is no order, no wallet, no private key and no venue
client anywhere in this package, and no configuration flag that could create
one: a paper fill is a row in this engine's own database, computed from
published decision events and point-in-time book evidence.

Decoupling, one direction, by construction
------------------------------------------
* paper → shadow **published files only** (``decisions-export`` parts). Never
  ``shadow.duckdb``: the shadow holds an exclusive lease on it, and a reader
  that could open it would read state mid-commit.
* paper → collector **raw archive, read-only**, to resolve the point-in-time
  L2 book a signal references.
* shadow → paper: nothing. The shadow does not know this package exists, and
  no module under ``weatherquant.shadow`` may import anything from here.
* The virtual account starts at 100 USDC and the engine is **disabled by
  default** - :attr:`weatherquant.paper.config.PaperSettings.paper_enabled`
  must be turned on explicitly, which is the recorded approval action.

Cost arithmetic has exactly one owner in the codebase
(:mod:`weatherquant.execution.pricing`); this package calls it and never
re-derives a fee, a fill price or an edge.
"""

from __future__ import annotations

__all__ = ["PaperSettings"]

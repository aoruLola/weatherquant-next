"""Settlement truth: what the venue resolved, kept apart from what we measured.

The split is structural, not stylistic:

* :mod:`weatherquant.truth.crosscheck` compares an *observation* against a
  *settlement*; it never writes one into the other's table.
* An observation is evidence about the atmosphere. A settlement is evidence
  about a market. They can disagree, and when they do the disagreement is the
  finding - never something to edit until it matches.
"""

from weatherquant.truth.crosscheck import (
    AMBIGUOUS_WINNER,
    MATCH,
    MISMATCH,
    NO_OBSERVATION,
    NO_RULES,
    NO_WINNER,
    UNPARSED_RULES,
    VERDICTS,
    CrosscheckResult,
    bracket_contains,
    crosscheck_events,
    verdict_counts,
)

__all__ = [
    "AMBIGUOUS_WINNER",
    "MATCH",
    "MISMATCH",
    "NO_OBSERVATION",
    "NO_RULES",
    "NO_WINNER",
    "UNPARSED_RULES",
    "VERDICTS",
    "CrosscheckResult",
    "bracket_contains",
    "crosscheck_events",
    "verdict_counts",
]

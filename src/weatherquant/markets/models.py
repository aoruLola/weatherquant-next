"""Polymarket domain models.

Design notes
------------
* Every field that carries a unit names it in the field (``*_usd``,
  ``*_shares``, ``price_usd_per_share``, ``temp_f``). Nothing in this package
  accepts a bare "price" whose unit has to be guessed.
* These models are *records*: they preserve what the venue returned, including
  malformed books, so that bad upstream data can be stored and audited later.
  Execution-time validity is checked separately (see
  :mod:`weatherquant.execution.pricing`), never by refusing to store a row.
* ``Bracket`` owns the rounding convention that maps a continuous temperature
  onto the discrete bucket a market actually settles on. Getting that wrong
  biases every probability, so it lives in exactly one place.
"""

from __future__ import annotations

import itertools
import math
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "BookLevel",
    "Bracket",
    "BracketDistribution",
    "BracketKind",
    "BracketProbability",
    "Event",
    "Market",
    "OrderBook",
    "RoundingConvention",
    "Settlement",
    "Side",
]

# Probabilities are validated to this absolute tolerance of summing to 1.
PROBABILITY_SUM_TOLERANCE = 1e-6


class Side(StrEnum):
    """Which side of the book an order takes."""

    BUY = "buy"
    SELL = "sell"


class BracketKind(StrEnum):
    """Shape of the outcome bucket a market settles on."""

    RANGE = "range"  # e.g. "32-33 degF"
    EXACT = "exact"  # e.g. "exactly 32 degF"
    ABOVE = "above"  # e.g. ">= 40 degF"
    BELOW = "below"  # e.g. "<= 30 degF"


class RoundingConvention(StrEnum):
    """How a continuous true temperature becomes the discrete value a market sees.

    ``NEAREST_INTEGER_F`` models round-half-up: ``reported = floor(true + 0.5)``.
    This is the default because Polymarket temperature markets quote whole
    degrees, but it is an explicit per-market field precisely because it must
    be confirmed against each market's own rules text rather than assumed -
    see ``docs/roadmap.md`` (P1 task V-1).
    """

    NEAREST_INTEGER_F = "nearest_integer_f"
    NONE = "none"  # bounds already describe a continuous interval


class Bracket(BaseModel):
    """The outcome bucket a market resolves on, plus how to integrate against it."""

    model_config = ConfigDict(frozen=True)

    label: str
    kind: BracketKind
    unit: Annotated[str, Field(pattern=r"^deg[CF]$")] = "degF"
    low: float | None = None
    high: float | None = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    rounding: RoundingConvention = RoundingConvention.NEAREST_INTEGER_F

    @model_validator(mode="after")
    def _check_bounds(self) -> Bracket:
        # EXACT is described by `low` alone; `high` would be a duplicate of it.
        needs_low = self.kind in (BracketKind.RANGE, BracketKind.EXACT, BracketKind.ABOVE)
        needs_high = self.kind in (BracketKind.RANGE, BracketKind.BELOW)
        if needs_low and self.low is None:
            raise ValueError(f"bracket kind {self.kind!r} requires 'low'")
        if needs_high and self.high is None:
            raise ValueError(f"bracket kind {self.kind!r} requires 'high'")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"low ({self.low}) must be <= high ({self.high})")
        if self.kind is BracketKind.RANGE and self.low == self.high:
            raise ValueError("a RANGE bracket with low == high should be modelled as EXACT")
        if self.unit == "degC":
            raise NotImplementedError(
                "degC brackets are not supported yet: Polymarket weather markets settle in degF, "
                "and a silent degC/degF mix would corrupt every probability"
            )
        # Force evaluation so an empty bucket fails at construction, not at integration time.
        self.probability_interval_f()
        return self

    def probability_interval_f(self) -> tuple[float, float]:
        """Return the half-open interval ``[lo, hi)`` of *true continuous* temperatures
        in degF that this market counts as resolving to this bracket.

        This is the interval to integrate a temperature CDF over. It differs from
        ``[low, high]`` whenever ``rounding`` is applied: the market quotes whole
        degrees, so the bracket ``32-33`` covers true temperatures
        ``[31.5, 34.5)``, not ``[32, 33]``.

        ``-math.inf`` / ``math.inf`` are used for unbounded sides.

        Raises ``ValueError`` when the bracket selects no temperature at all.
        """
        if self.rounding is RoundingConvention.NONE:
            lo = -math.inf if self.low is None else self.low
            hi = math.inf if self.high is None else self.high
            # An inclusive high bound is pushed to the next representable cut so that
            # [lo, hi) integration still covers it exactly at probability level.
            if self.high is not None and self.high_inclusive and math.isfinite(hi):
                hi = math.nextafter(hi, math.inf)
            if not lo < hi:
                raise ValueError(f"bracket {self.label!r} selects an empty interval")
            return lo, hi

        # NEAREST_INTEGER_F: collect the whole degrees the bucket contains, then widen
        # by half a degree on each side, because round-half-up maps true t into
        # integer k exactly when t is in [k - 0.5, k + 0.5).
        lo = -math.inf if self.low is None else self.low
        hi = math.inf if self.high is None else self.high

        if self.kind is BracketKind.RANGE:
            first = math.ceil(lo if self.low_inclusive else lo + 1.0)
            last = math.floor(hi if self.high_inclusive else hi - 1.0)
        elif self.kind is BracketKind.EXACT:
            first = last = round(lo) if lo == int(lo) else math.nan
        elif self.kind is BracketKind.ABOVE:
            first = math.ceil(lo if self.low_inclusive else lo + 1.0)
            last = math.inf
        else:  # BELOW
            first = -math.inf
            last = math.floor(hi if self.high_inclusive else hi - 1.0)

        # `math.ceil`/`math.floor` return ints, and the unbounded sides are
        # +/-inf, so only the EXACT-with-fractional-bound case yields NaN here.
        if isinstance(first, float) and math.isnan(first):
            raise ValueError(f"bracket {self.label!r} has non-integral bounds but asks for an exact integer")
        if first > last:
            raise ValueError(f"bracket {self.label!r} selects no whole degree")

        lower = -math.inf if first == -math.inf else first - 0.5
        upper = math.inf if last == math.inf else last + 0.5
        if not lower < upper:
            raise ValueError(f"bracket {self.label!r} selects an empty interval")
        return lower, upper


class BracketProbability(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    probability: Annotated[float, Field(ge=0.0, le=1.0)]


class BracketDistribution(BaseModel):
    """A complete probability distribution over a market's mutually exclusive brackets.

    Invariant: probabilities are non-negative, sum to 1 within
    :data:`PROBABILITY_SUM_TOLERANCE`, and cover every bracket of the event
    exactly once. Enforced at construction - a model that cannot produce a
    proper distribution must fail loudly rather than feed the signal engine.
    """

    model_config = ConfigDict(frozen=True)

    station: str
    target_date: date
    model_id: str
    forecast_run_time_utc: datetime | None
    probabilities: tuple[BracketProbability, ...]

    @model_validator(mode="after")
    def _check_distribution(self) -> BracketDistribution:
        if not self.probabilities:
            raise ValueError("a bracket distribution must contain at least one bracket")
        labels = [p.label for p in self.probabilities]
        if len(set(labels)) != len(labels):
            dupes = sorted({x for x in labels if labels.count(x) > 1})
            raise ValueError(f"duplicate brackets in distribution: {dupes}")
        total = math.fsum(p.probability for p in self.probabilities)
        if not math.isfinite(total) or abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise ValueError(f"probabilities must sum to 1.0 (got {total!r})")
        return self

    def as_dict(self) -> dict[str, float]:
        return {p.label: p.probability for p in self.probabilities}


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    slug: str
    title: str
    target_date: date | None
    resolution_source: str | None
    rules_text: str | None
    fetched_at_utc: datetime


class Market(BaseModel):
    """One tradeable outcome market (one bracket of one event).

    ``fee_rate`` and ``fee_exponent`` are venue-reported; when the venue does not
    report them they are ``None``, and :func:`weatherquant.execution.pricing.taker_fee_usd`
    must treat that as "fee structure unknown" rather than as zero.
    """

    model_config = ConfigDict(frozen=True)

    market_id: str
    event_id: str
    condition_id: str | None
    question: str
    bracket: Bracket
    token_yes: str
    token_no: str
    target_date: date
    station: str

    tick_size: Annotated[float, Field(gt=0.0, lt=1.0)]
    min_order_size_shares: Annotated[float, Field(gt=0.0)]
    fee_rate: Annotated[float, Field(ge=0.0)] | None = None
    fee_exponent: Annotated[float, Field(ge=0.0)] | None = None

    resolution_source: str | None
    rules_text: str | None
    active: bool
    closed: bool
    accepting_orders: bool
    fetched_at_utc: datetime
    raw: dict


class BookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)

    price_usd_per_share: Annotated[float, Field(gt=0.0, lt=1.0)]
    size_shares: Annotated[float, Field(gt=0.0)]

    @property
    def notional_usd(self) -> float:
        return self.price_usd_per_share * self.size_shares


class OrderBook(BaseModel):
    """A captured L2 snapshot for one token.

    Deliberately *not* validated for monotonicity or crossing: a crossed or
    non-monotonic book is exactly the kind of upstream defect the system must
    be able to record. Call :meth:`defects` before using it for execution.

    ``venue_ts`` is the venue's own event time; ``fetched_at_utc`` is when we
    pulled it. Only ``venue_ts`` may be used to judge staleness - ``fetched_at``
    can be much later than the data it carries.
    """

    model_config = ConfigDict(frozen=True)

    market_id: str
    token_id: str
    bids: tuple[BookLevel, ...]  # best (highest) price first
    asks: tuple[BookLevel, ...]  # best (lowest) price first
    venue_ts: datetime | None
    fetched_at_utc: datetime

    @property
    def best_bid_usd_per_share(self) -> float | None:
        return self.bids[0].price_usd_per_share if self.bids else None

    @property
    def best_ask_usd_per_share(self) -> float | None:
        return self.asks[0].price_usd_per_share if self.asks else None

    @property
    def spread_usd_per_share(self) -> float | None:
        """Distance between best bid and best ask, in USD per share.

        Not a transaction price, and never used as one.
        """
        if not self.bids or not self.asks:
            return None
        return self.best_ask_usd_per_share - self.best_bid_usd_per_share

    @property
    def midpoint_usd_per_share(self) -> float | None:
        """Mid price, provided for *analysis only*.

        The spec forbids using this as an execution price; it exists so that
        analysis code does not recompute it ad hoc and drift out of sync.
        """
        if not self.bids or not self.asks:
            return None
        return (self.best_bid_usd_per_share + self.best_ask_usd_per_share) / 2.0

    def defects(self) -> list[str]:
        """Human-readable reasons this book must not be used to execute.

        Empty list means the book is executable. Every string is a stable code.
        """
        problems: list[str] = []
        if not self.bids:
            problems.append("NO_BID_SIDE")
        if not self.asks:
            problems.append("NO_ASK_SIDE")

        # The two sides are ordered in opposite directions because both are
        # listed best-first: bids start at the highest price, asks at the
        # lowest. A side that steps the wrong way means the venue payload was
        # mangled, and walking it would price the trade wrongly.
        for side_name, levels, expect_descending in (
            ("bid", self.bids, True),
            ("ask", self.asks, False),
        ):
            prices = [x.price_usd_per_share for x in levels]
            if expect_descending:
                violated = any(a < b for a, b in itertools.pairwise(prices))
            else:
                violated = any(a > b for a, b in itertools.pairwise(prices))
            if violated:
                problems.append(f"NON_MONOTONIC_{side_name.upper()}")

        if self.spread_usd_per_share is not None and self.spread_usd_per_share <= 0:
            problems.append("CROSSED_OR_LOCKED")
        return problems

    def depth_notional_usd(self, side: Side) -> float:
        """Total USD notional available on one side of the book."""
        levels = self.asks if side is Side.BUY else self.bids
        return math.fsum(x.notional_usd for x in levels)


class Settlement(BaseModel):
    """The venue's own resolved outcome.

    ``yes_price`` is the official result and is always exactly 0 or 1 for a
    resolved binary market. ``cross_check_temp_f`` - if present - is *our*
    observation, recorded only as an audit cross-check; it is never allowed to
    stand in for the venue's resolution.
    """

    model_config = ConfigDict(frozen=True)

    market_id: str
    event_id: str
    target_date: date
    resolved: bool
    yes_price: Annotated[float, Field(ge=0.0, le=1.0)] | None
    resolved_at_utc: datetime
    source: str
    cross_check_temp_f: float | None = None
    notes: str | None = None

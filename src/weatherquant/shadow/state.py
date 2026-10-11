"""The shadow engine's in-memory view of the market, rebuilt from its own DB.

The view is derived state: everything in here can be reconstructed from
``market_catalog`` and ``token_books``. It is kept in memory only so that a
decision cycle does not have to re-parse a day of archive to know which token
belongs to which bracket - and it is written back in the *same transaction* as
the ingest offsets, which is what makes "offset advanced but state did not"
unrepresentable.

Nothing here is a decision. State records what was observed; the decision
layer decides what it means.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from weatherquant.shadow.events import BookObservation, CatalogMarket, market_of_token_table

__all__ = ["MarketGroup", "ShadowState"]


@dataclass(frozen=True, slots=True)
class MarketGroup:
    """All markets settling on one civil date - the ladder a distribution needs."""

    event_id: str
    target_date: date
    markets: tuple[CatalogMarket, ...]

    @property
    def complete_ladder(self) -> bool:
        """Both a below-everything and an above-everything bracket.

        Without both ends the probabilities cannot sum to one without inventing
        mass, which is the refusal already enforced by
        :func:`weatherquant.models.probability.distribution_for_event`.
        """
        lows = [m.bracket.low for m in self.markets if m.bracket is not None]
        highs = [m.bracket.high for m in self.markets if m.bracket is not None]
        return any(v is None for v in lows) and any(v is None for v in highs)

    @property
    def bracketed(self) -> bool:
        return bool(self.markets) and all(m.bracket is not None for m in self.markets)


class ShadowState:
    """Latest market metadata and latest complete book per token."""

    def __init__(self) -> None:
        self.markets: dict[str, CatalogMarket] = {}
        self.books: dict[str, BookObservation] = {}

    # -- hydration ----------------------------------------------------------

    def load_catalog(self, rows) -> None:
        for row in rows:
            (
                market_id,
                event_id,
                slug,
                question,
                bracket_label,
                bracket_kind,
                bracket_low,
                bracket_high,
                bracket_rounding,
                token_yes,
                token_no,
                target_date,
                station,
                tick_size,
                min_order_size,
                fee_rate,
                fee_exponent,
                active,
                closed,
                accepting_orders,
                raw_sha256,
                source_run_id,
            ) = row
            self.markets[market_id] = CatalogMarket(
                market_id=market_id,
                event_id=event_id,
                slug=slug or "",
                question=question or "",
                bracket=_rebuild_bracket(bracket_label, bracket_kind, bracket_low, bracket_high, bracket_rounding),
                bracket_label=bracket_label or "",
                bracket_error=None if bracket_label else "no bracket stored",
                token_yes=token_yes or "",
                token_no=token_no or "",
                target_date=target_date,
                station=station,
                tick_size=tick_size,
                min_order_size=min_order_size,
                fee_rate=fee_rate,
                fee_exponent=fee_exponent,
                active=bool(active),
                closed=bool(closed),
                accepting_orders=bool(accepting_orders),
                raw_sha256=raw_sha256 or "",
                source_run_id=source_run_id or "",
            )

    # -- mutation -----------------------------------------------------------

    def load_books(self, rows) -> None:
        """Rehydrate ``token_books`` so a restart need not replay a day of archive."""
        for row in rows:
            (
                token_id,
                market_id,
                book_id,
                book_source,
                bids_json,
                asks_json,
                venue_ts,
                venue_ts_origin,
                collector_received,
                shadow_received,
                quality,
                _defects,
                _updated,
            ) = row
            self.books[token_id] = BookObservation(
                token_id=token_id,
                market_id=market_id,
                book=_book_from_json(
                    market_id=market_id,
                    token_id=token_id,
                    bids_json=bids_json,
                    asks_json=asks_json,
                    venue_ts=venue_ts,
                    collector_received=collector_received,
                ),
                book_id=book_id,
                book_source=book_source,
                venue_ts=venue_ts,
                venue_ts_origin=venue_ts_origin,
                collector_received_at_utc=collector_received,
                shadow_received_at_utc=shadow_received,
                quality=quality,
            )

    def apply_markets(self, batches) -> int:
        applied = 0
        for batch in batches:
            for market in batch:
                self.markets[market.market_id] = market
                applied += 1
        return applied

    def apply_books(self, observations) -> int:
        applied = 0
        for observation in observations:
            existing = self.books.get(observation.token_id)
            # A newer collector timestamp always wins; an older one arriving
            # late must not roll the book back.
            if existing is None or _ts(observation) >= _ts(existing):
                self.books[observation.token_id] = observation
                applied += 1
        return applied

    # -- queries ------------------------------------------------------------

    @property
    def market_of_token(self) -> dict[str, str]:
        return market_of_token_table(list(self.markets.values()))

    def tradeable_markets(self) -> list[CatalogMarket]:
        return [m for m in self.markets.values() if m.tradeable]

    def groups_for(self, target_date: date) -> list[MarketGroup]:
        """Events with at least one market settling on ``target_date``."""
        by_event: dict[str, list[CatalogMarket]] = {}
        for market in self.markets.values():
            if market.target_date == target_date:
                by_event.setdefault(market.event_id, []).append(market)
        return [
            MarketGroup(
                event_id=event_id,
                target_date=target_date,
                markets=tuple(sorted(ms, key=lambda m: m.market_id)),
            )
            for event_id, ms in sorted(by_event.items())
        ]

    def book_for(self, token_id: str) -> BookObservation | None:
        return self.books.get(token_id)


def _book_from_json(*, market_id, token_id, bids_json, asks_json, venue_ts, collector_received):
    import json

    from weatherquant.markets.models import BookLevel, OrderBook

    def levels(raw: str) -> tuple:
        return tuple(
            BookLevel(
                price_usd_per_share=float(item["price_usd_per_share"]),
                size_shares=float(item["size_shares"]),
            )
            for item in json.loads(raw)
        )

    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        bids=levels(bids_json),
        asks=levels(asks_json),
        venue_ts=venue_ts,
        fetched_at_utc=collector_received,
    )


def _ts(observation: BookObservation) -> object:
    # Venue time when present; otherwise the collector's own receipt time,
    # which is the best available ordering signal for a venue-stamped-missing
    # snapshot. Never the shadow's read time - that would order by our delay.
    return observation.venue_ts or observation.collector_received_at_utc


def _rebuild_bracket(label, kind, low, high, rounding):
    """Rebuild a bracket from stored columns, or ``None`` if it will not hold."""
    from weatherquant.markets.models import Bracket, BracketKind, RoundingConvention

    if not kind:
        return None
    try:
        return Bracket(
            label=label or "",
            kind=BracketKind(kind),
            low=low,
            high=high,
            rounding=RoundingConvention(rounding or "nearest_integer_f"),
        )
    except (ValueError, NotImplementedError):
        return None

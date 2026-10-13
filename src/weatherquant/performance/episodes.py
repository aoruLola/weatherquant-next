"""Opportunity episodes: turning a stream of evaluations into opportunities.

The engine re-evaluates every open market on every cycle, so one opportunity
that stays open for an hour is recorded dozens of times. Counting those rows
as chances is how a shadow log starts to sound like a track record, so this
module groups them into episodes first and keeps the two counts apart.

Contract: docs/performance-data-contract.md section 2. The rules that are
easy to get subtly wrong, and what each one costs:

* **Identity is five-dimensional.** market_id, target_date, model_name,
  direction and mode. Drop `mode` and a REPLAY decision merges into a LIVE
  one; drop `direction` and buying a bracket merges with selling it. Either
  produces a number that looks fine and means nothing.

* **A gap is not a signal.** When an episode stops, the reason matters more
  than the fact. If the whole feed went quiet, nothing ended - the
  observation window did. If other markets kept being decided while this one
  stopped passing, something actually changed. Collapsing the two into one
  "ended" state makes a collector outage look like the model losing
  confidence.

* **Episodes span restarts.** They are built from `decided_at_utc`, not from
  any in-process counter, so a restart does not cut an opportunity in half.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

__all__ = [
    "DEFAULT_GAP_WINDOW_SECONDS",
    "EndReason",
    "Episode",
    "EpisodeKey",
    "build_episodes",
]

#: Default for how long an episode may go unheard before it closes.
#: Matches the engine's collector-silence window, so a stalled feed closes
#: episodes for the same reason the engine reports COLLECTOR_DISCONNECTED.
DEFAULT_GAP_WINDOW_SECONDS = 900.0


class EndReason:
    """Why an episode stopped. See contract section 2.2."""

    STATE_CHANGE = "state_change"
    GAP_DATA_MISSING = "gap_data_missing"
    GAP_SIGNAL_BREAK = "gap_signal_break"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True, slots=True)
class EpisodeKey:
    """Identity of an opportunity. All five fields are load-bearing."""

    market_id: str
    target_date: str | None
    model_name: str | None
    direction: str | None
    mode: str | None


@dataclass(slots=True)
class Episode:
    """One independent opportunity, however many evaluations it took."""

    episode_id: str
    key: EpisodeKey
    first_seen_utc: datetime
    last_seen_utc: datetime
    observation_count: int
    first_edge: float
    last_edge: float
    mean_edge: float
    max_edge: float
    min_edge: float
    first_market_price: float | None
    first_model_probability: float | None
    first_available_depth_usd: float | None
    target_date: str | None
    station: str | None
    bracket_label: str | None
    direction: str | None
    mode: str | None
    end_reason: str
    market_question: str | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.last_seen_utc - self.first_seen_utc).total_seconds()

    @property
    def market_id(self) -> str:
        return self.key.market_id

    @property
    def model_name(self) -> str | None:
        return self.key.model_name

    def as_dict(self) -> dict[str, object]:
        """Flat dict for snapshots and the API. Times are ISO-8601 UTC."""
        return {
            "episode_id": self.episode_id,
            "market_id": self.key.market_id,
            "market_question": self.market_question,
            "target_date": self.key.target_date,
            "model_name": self.key.model_name,
            "direction": self.key.direction,
            "mode": self.key.mode,
            "station": self.station,
            "bracket_label": self.bracket_label,
            "first_seen_utc": self.first_seen_utc.isoformat(),
            "last_seen_utc": self.last_seen_utc.isoformat(),
            "duration_seconds": self.duration_seconds,
            "observation_count": self.observation_count,
            "first_edge": self.first_edge,
            "last_edge": self.last_edge,
            "mean_edge": self.mean_edge,
            "max_edge": self.max_edge,
            "min_edge": self.min_edge,
            "first_market_price": self.first_market_price,
            "first_model_probability": self.first_model_probability,
            "first_available_depth_usd": self.first_available_depth_usd,
            "end_reason": self.end_reason,
        }


@dataclass(slots=True)
class _OpenEpisode:
    """Working state for an episode still accepting observations."""

    key: EpisodeKey
    first_seen: datetime
    last_seen: datetime
    count: int
    edges: list[float] = field(default_factory=list)
    first_market_price: float | None = None
    first_model_probability: float | None = None
    first_available_depth_usd: float | None = None
    station: str | None = None
    bracket_label: str | None = None
    market_question: str | None = None

    def observe(self, row: _DecisionRow) -> None:
        self.last_seen = row.decided_at_utc
        self.count += 1
        if row.net_edge_usd is not None:
            self.edges.append(float(row.net_edge_usd))
        if self.first_market_price is None:
            self.first_market_price = row.market_price
        if self.first_model_probability is None:
            self.first_model_probability = row.model_probability
        if self.first_available_depth_usd is None:
            self.first_available_depth_usd = row.available_depth_usd
        if self.station is None:
            self.station = row.station
        if self.bracket_label is None:
            self.bracket_label = row.bracket_label
        if self.market_question is None:
            self.market_question = row.market_question


@dataclass(frozen=True, slots=True)
class _DecisionRow:
    """The minimum a decision must carry to take part in an episode."""

    market_id: str
    target_date: str | None
    model_name: str | None
    direction: str | None
    mode: str | None
    state: str
    decided_at_utc: datetime
    net_edge_usd: float | None
    market_price: float | None = None
    model_probability: float | None = None
    available_depth_usd: float | None = None
    station: str | None = None
    bracket_label: str | None = None
    market_question: str | None = None

    @property
    def key(self) -> EpisodeKey:
        return EpisodeKey(
            market_id=self.market_id,
            target_date=self.target_date,
            model_name=self.model_name,
            direction=self.direction,
            mode=self.mode,
        )


def _normalise(value: object) -> str | None:
    """None and empty are the same absence; 'None' is not."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite(value: object) -> float | None:
    """Accept a real number, refuse a sentinel.

    ``net_edge_usd`` can arrive as None, NaN or inf from a book that was
    priced against itself. Starting an episode on one of those produces a
    representative edge that is not a number at all, so it is dropped here
    and reported by the caller rather than silently averaged away.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _timestamp(value: object) -> datetime:
    """Accept a datetime or the ISO-8601 string a JSON export carries.

    The export the research process reads (contract section 11) serialises
    timestamps as strings, so treating ``row["decided_at_utc"]`` as already
    parsed works on a database cursor and fails on a file - which is the
    direction the data actually travels. A naive string is read as UTC
    rather than rejected, because a decision recorded without an offset was
    recorded in the engine's own clock, which is UTC.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise TypeError(f"decided_at_utc must be a datetime or ISO-8601 string, got {type(value).__name__}")


def row_from_mapping(row: dict) -> _DecisionRow:
    """Build a row from a decision dict (API shape or a database mapping)."""
    return _DecisionRow(
        market_id=str(row.get("market_id") or ""),
        target_date=_normalise(row.get("target_date")),
        model_name=_normalise(row.get("model_name")),
        direction=_normalise(row.get("direction")),
        mode=_normalise(row.get("mode")),
        state=_normalise(row.get("state")) or "",
        decided_at_utc=_timestamp(row["decided_at_utc"]),
        net_edge_usd=_finite(row.get("net_edge_usd")),
        market_price=_finite(row.get("market_price")),
        model_probability=_finite(row.get("model_probability")),
        available_depth_usd=_finite(row.get("available_depth_usd")),
        station=_normalise(row.get("station")),
        bracket_label=_normalise(row.get("bracket_label")),
        market_question=_normalise(row.get("market_question")),
    )


def _episode_id(key: EpisodeKey, first_seen: datetime) -> str:
    """Stable across runs and machines: same opportunity, same id."""
    import hashlib

    basis = f"{key.market_id}|{key.target_date}|{key.model_name}|{key.direction}|{key.mode}|{first_seen.isoformat()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _close(open_ep: _OpenEpisode, *, reason: str, gap_missing: bool) -> Episode:
    edges = open_ep.edges
    first_edge = edges[0]
    return Episode(
        episode_id=_episode_id(open_ep.key, open_ep.first_seen),
        key=open_ep.key,
        first_seen_utc=open_ep.first_seen,
        last_seen_utc=open_ep.last_seen,
        observation_count=open_ep.count,
        first_edge=first_edge,
        last_edge=edges[-1],
        mean_edge=sum(edges) / len(edges),
        max_edge=max(edges),
        min_edge=min(edges),
        first_market_price=open_ep.first_market_price,
        first_model_probability=open_ep.first_model_probability,
        first_available_depth_usd=open_ep.first_available_depth_usd,
        target_date=open_ep.key.target_date,
        station=open_ep.station,
        bracket_label=open_ep.bracket_label,
        direction=open_ep.key.direction,
        mode=open_ep.key.mode,
        # `end_of_data` beats `gap_*`: the stream simply stopped, so we cannot
        # claim a gap was classified at all.
        end_reason=EndReason.END_OF_DATA if reason == EndReason.END_OF_DATA else reason,
        market_question=open_ep.market_question,
    )


def build_episodes(
    rows: Iterable[_DecisionRow | dict],
    *,
    gap_window_seconds: float = DEFAULT_GAP_WINDOW_SECONDS,
    accepted_state: str = "ACCEPT",
) -> tuple[list[Episode], dict[str, int]]:
    """Group decisions into episodes.

    ``rows`` may be in any order; they are sorted by ``decided_at_utc``
    first, because the state machine is defined over time and an unsorted
    input would otherwise invent boundaries that are not there.

    Returns ``(episodes, counters)``. ``counters`` carries the rows that
    were refused rather than counted, so an episode total can always be
    reconciled against the decisions it came from:

    ``decisions_seen``, ``episodes_built``, ``accepts_without_edge``
    (an ACCEPT with no usable ``net_edge_usd`` - not counted as an
    opportunity), ``open_at_end``.
    """
    normalised: list[_DecisionRow] = []
    accepts_without_edge = 0
    for raw in rows:
        row = raw if isinstance(raw, _DecisionRow) else row_from_mapping(raw)
        if not row.market_id:
            continue
        if row.state == accepted_state and row.net_edge_usd is None:
            accepts_without_edge += 1
        normalised.append(row)

    normalised.sort(key=lambda r: r.decided_at_utc)

    open_by_key: dict[EpisodeKey, _OpenEpisode] = {}
    closed: list[Episode] = []
    # Every instant at which anything was decided. A gap with nothing in it is
    # an outage; a gap with other markets moving is this signal stopping.
    decided_at: list[datetime] = [r.decided_at_utc for r in normalised]
    import bisect

    def activity_between(start: datetime, end: datetime) -> bool:
        i = bisect.bisect_right(decided_at, start)
        return i < len(decided_at) and decided_at[i] < end

    for row in normalised:
        key = row.key
        current = open_by_key.get(key)
        if current is not None:
            gap = (row.decided_at_utc - current.last_seen).total_seconds()
            if gap > gap_window_seconds:
                missing = not activity_between(current.last_seen, row.decided_at_utc)
                closed.append(
                    _close(
                        current,
                        reason=EndReason.GAP_DATA_MISSING if missing else EndReason.GAP_SIGNAL_BREAK,
                        gap_missing=missing,
                    )
                )
                del open_by_key[key]
                current = None

        is_accept = row.state == accepted_state and row.net_edge_usd is not None

        if is_accept:
            if current is None:
                open_by_key[key] = _OpenEpisode(
                    key=key, first_seen=row.decided_at_utc, last_seen=row.decided_at_utc, count=1,
                    edges=[float(row.net_edge_usd)],
                    first_market_price=row.market_price,
                    first_model_probability=row.model_probability,
                    first_available_depth_usd=row.available_depth_usd,
                    station=row.station,
                    bracket_label=row.bracket_label,
                    market_question=row.market_question,
                )
            else:
                current.observe(row)
        elif current is not None:
            # A state that is not ACCEPT ends the episode. It is a real
            # signal, not a gap, so it closes as a state change.
            closed.append(_close(current, reason=EndReason.STATE_CHANGE, gap_missing=False))
            del open_by_key[key]

    # Anything still open simply ran out of data. We do not know it ended.
    for remaining in open_by_key.values():
        closed.append(_close(remaining, reason=EndReason.END_OF_DATA, gap_missing=False))

    closed.sort(key=lambda e: e.first_seen_utc)
    counters = {
        "decisions_seen": len(normalised),
        "episodes_built": len(closed),
        "accepts_without_edge": accepts_without_edge,
        "open_at_end": 0 if not open_by_key else len(open_by_key),
    }
    return closed, counters


def summarize(episodes: Sequence[Episode]) -> dict[str, object]:
    """The five counts that must never be substituted for one another.

    Contract section 2.5. ``accept_observations`` is the number of
    evaluations; ``opportunity_episodes`` is the number of opportunities.
    They are different questions and the gap between them is the measure of
    how much a raw ACCEPT count was overstating.
    """
    markets = {e.key.market_id for e in episodes}
    pairs = {(e.key.market_id, e.key.direction) for e in episodes}
    dates = {e.key.target_date for e in episodes if e.key.target_date is not None}
    edges = [e.first_edge for e in episodes]
    return {
        "opportunity_episodes": len(episodes),
        "accept_observations": sum(e.observation_count for e in episodes),
        "distinct_market_direction_pairs": len(pairs),
        "distinct_markets": len(markets),
        "distinct_target_dates": len(dates),
        "unique_opportunity_edge_sum": sum(edges) if edges else 0.0,
        "observation_edge_sum": sum(
            e.mean_edge * e.observation_count for e in episodes
        ),
        "mean_episode_edge": (sum(edges) / len(edges)) if edges else None,
        "statistical_scope": ["opportunity_episodes", "observation_all_accepts"],
        "warnings": [
            "Observation Edge Sum counts repeated evaluations of one opportunity; "
            "it is not profit, not asset growth and not the expected return of "
            "independent trades.",
            "Unique Opportunity Edge Sum is the model's theoretical expectation "
            "under its own probabilities; it is not realised profit.",
        ],
    }

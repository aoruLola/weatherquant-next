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

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "DEFAULT_GAP_WINDOW_SECONDS",
    "EndReason",
    "Episode",
    "EpisodeFolder",
    "EpisodeKey",
    "EpisodeSummary",
    "FolderState",
    "OpenEpisode",
    "build_episodes",
    "episode_from_mapping",
    "episode_from_open",
    "summarize",
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
    #: The market's own fee rate at the decision instant. Carried because
    #: ``execution_cost`` refuses to assume a fee of zero when none is
    #: reported - without it every sizing comes back FEE_UNKNOWN, and the
    #: whole reference-capital feature would be a fixture that never fires.
    fee_rate: float | None = None

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
            "fee_rate": self.fee_rate,
        }


@dataclass(slots=True)
class OpenEpisode:
    """Working state for an episode still accepting observations.

    Fixed-size on purpose. The whole record is scalars, so the open set can be
    persisted between runs and reloaded without loss - which is what lets the
    streaming engine resume across a crash without cutting an episode in half.

    ``sum_edges`` is a running **left-to-right** sum, the same accumulation the
    original builder got from ``sum(edges)``, and ``max_edge``/``min_edge`` are
    running extremes. Keeping the accumulation rather than the values is what
    makes ``mean_edge`` bit-for-bit identical between the two implementations
    while bounding memory by the number of open episodes instead of the length
    of the history.
    """

    key: EpisodeKey
    first_seen: datetime
    last_seen: datetime
    count: int
    sum_edges: float
    first_edge: float
    last_edge: float
    max_edge: float
    min_edge: float
    #: Position of this episode's first ACCEPT in the global fold order. Used
    #: to publish episodes in the order the original builder produced, which
    #: matters because the representative-edge sum is a float left-fold.
    opened_seq: int = 0
    #: Observations that carried a usable edge. Always equal to ``count`` at
    #: the call sites, kept separately so ``mean_edge`` divides by the same
    #: denominator the original builder used (``len(edges)``).
    edge_count: int = 1
    first_market_price: float | None = None
    first_model_probability: float | None = None
    first_available_depth_usd: float | None = None
    station: str | None = None
    bracket_label: str | None = None
    market_question: str | None = None
    fee_rate: float | None = None

    def observe(self, row: _DecisionRow) -> None:
        # A late row (business instant behind the episode's furthest instant)
        # is counted but never rewinds `last_seen`: the gap clock runs from
        # the furthest activity, and rewinding it would inflate the next gap
        # and close the episode early. For in-order rows the guard is always
        # true and behaviour is unchanged.
        if row.decided_at_utc > self.last_seen:
            self.last_seen = row.decided_at_utc
        self.count += 1
        if row.net_edge_usd is not None:
            number = float(row.net_edge_usd)
            self.edge_count += 1
            self.sum_edges += number
            self.last_edge = number
            if number > self.max_edge:
                self.max_edge = number
            if number < self.min_edge:
                self.min_edge = number
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
        if self.fee_rate is None:
            self.fee_rate = row.fee_rate


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
    fee_rate: float | None = None

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


#: One decision, two spellings. The API and the dashboard publish the
#: public names; the decisions export (contract section 11) carries the
#: engine's own column names, because it is a SELECT over shadow_decisions
#: rather than a re-spelling of it. Only these two differ.
_EXPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "decided_at_utc": ("decision_time_utc",),
    "net_edge_usd": ("net_edge",),
}


def _field(row: Mapping[str, Any], name: str) -> Any:
    """Read a public field, falling back to the export's name for it.

    The export is append-only and already published, so its spelling is a
    fact about data on disk rather than a choice this reader gets to make.
    """
    if name in row:
        return row[name]
    for alias in _EXPORT_ALIASES.get(name, ()):
        if alias in row:
            return row[alias]
    return None


def row_from_mapping(row: dict) -> _DecisionRow:
    """Build a row from a decision dict (API shape or a database mapping).

    Accepts either spelling, so a decisions export feeds the performance
    build directly. Each side was tested against its own vocabulary and
    neither crossed the seam, which is how ``decided_at_utc`` and
    ``decision_time_utc`` came to mean one thing under two names.
    """
    return _DecisionRow(
        market_id=str(row.get("market_id") or ""),
        target_date=_normalise(row.get("target_date")),
        model_name=_normalise(row.get("model_name")),
        direction=_normalise(row.get("direction")),
        mode=_normalise(row.get("mode")),
        state=_normalise(row.get("state")) or "",
        decided_at_utc=_timestamp(_field(row, "decided_at_utc")),
        net_edge_usd=_finite(_field(row, "net_edge_usd")),
        market_price=_finite(row.get("market_price")),
        model_probability=_finite(row.get("model_probability")),
        available_depth_usd=_finite(row.get("available_depth_usd")),
        fee_rate=_finite(row.get("fee_rate")),
        station=_normalise(row.get("station")),
        bracket_label=_normalise(row.get("bracket_label")),
        market_question=_normalise(row.get("market_question")),
    )


def episode_from_mapping(row: Mapping[str, Any]) -> Episode:
    """Rebuild an episode from the shape ``Episode.as_dict`` publishes.

    The reference-capital endpoint has to answer for a stake the snapshot
    was not built with, so it reconstructs the episodes the research process
    recorded and re-runs the *same* sizing code over them. Rebuilding rather
    than storing a second, differently-shaped copy is what keeps the API and
    the backfill from drifting into two answers for one opportunity.
    """
    first = _timestamp(row["first_seen_utc"])
    last = _timestamp(row["last_seen_utc"])
    direction = _normalise(row.get("direction"))
    mode = _normalise(row.get("mode"))
    market_id = str(row.get("market_id") or "")
    target_date = _normalise(row.get("target_date"))
    model_name = _normalise(row.get("model_name"))

    first_edge = _finite(row.get("first_edge"))
    if first_edge is None:
        raise ValueError(f"episode {row.get('episode_id')!r} has no usable first_edge")

    return Episode(
        episode_id=str(row.get("episode_id") or _episode_id(
            EpisodeKey(market_id, target_date, model_name, direction, mode), first
        )),
        key=EpisodeKey(market_id, target_date, model_name, direction, mode),
        first_seen_utc=first,
        last_seen_utc=last,
        observation_count=int(row.get("observation_count") or 1),
        first_edge=first_edge,
        last_edge=_finite(row.get("last_edge")) if _finite(row.get("last_edge")) is not None else first_edge,
        mean_edge=_finite(row.get("mean_edge")) if _finite(row.get("mean_edge")) is not None else first_edge,
        max_edge=_finite(row.get("max_edge")) if _finite(row.get("max_edge")) is not None else first_edge,
        min_edge=_finite(row.get("min_edge")) if _finite(row.get("min_edge")) is not None else first_edge,
        first_market_price=_finite(row.get("first_market_price")),
        first_model_probability=_finite(row.get("first_model_probability")),
        first_available_depth_usd=_finite(row.get("first_available_depth_usd")),
        target_date=target_date,
        station=_normalise(row.get("station")),
        bracket_label=_normalise(row.get("bracket_label")),
        direction=direction,
        mode=mode,
        end_reason=str(row.get("end_reason") or EndReason.END_OF_DATA),
        market_question=_normalise(row.get("market_question")),
        fee_rate=_finite(row.get("fee_rate")),
    )


def _episode_id(key: EpisodeKey, first_seen: datetime) -> str:
    """Stable across runs and machines: same opportunity, same id."""
    import hashlib

    basis = f"{key.market_id}|{key.target_date}|{key.model_name}|{key.direction}|{key.mode}|{first_seen.isoformat()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def episode_from_open(
    open_ep: OpenEpisode,
    *,
    end_reason: str,
    closed_seq: int | None = None,
) -> Episode:
    """Turn an in-flight episode into the published record.

    ``end_reason`` is supplied by the caller because the same open record is
    rendered two different ways: the whole-history builder closes everything
    left over as ``end_of_data`` at the end of its pass, while the streaming
    engine renders the *still open* set the same way for publication and then
    keeps it open in the store so the next run can extend it. Rendering and
    state are separate on purpose - a published ``end_of_data`` must not stop
    the episode from being observed again.

    ``closed_seq`` is carried so a reader can order the published set exactly
    as the original builder ordered it; it is not part of the record itself.
    """
    if open_ep.edge_count <= 0:  # pragma: no cover - guarded by the constructor
        raise ValueError(f"episode {open_ep.key} has no observations to summarise")
    return Episode(
        episode_id=_episode_id(open_ep.key, open_ep.first_seen),
        key=open_ep.key,
        first_seen_utc=open_ep.first_seen,
        last_seen_utc=open_ep.last_seen,
        observation_count=open_ep.count,
        first_edge=open_ep.first_edge,
        last_edge=open_ep.last_edge,
        mean_edge=open_ep.sum_edges / open_ep.edge_count,
        max_edge=open_ep.max_edge,
        min_edge=open_ep.min_edge,
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
        end_reason=EndReason.END_OF_DATA if end_reason == EndReason.END_OF_DATA else end_reason,
        market_question=open_ep.market_question,
        fee_rate=open_ep.fee_rate,
    )


@dataclass(slots=True)
class FolderState:
    """Everything about the fold that has to survive between runs.

    ``prev_instant`` is the whole of the global activity index. It is the
    largest decision instant strictly below ``last_instant``, and it is enough
    because the fold consumes instants in arrival order and the index only
    moves forward: when a row at instant ``t`` arrives, every instant the
    stream has already delivered below ``t`` has been seen. Late rows (since
    the export moved to the write sequence) do not move the index at all, so
    one cannot corrupt the answer for the rows that follow it. With that
    guard, ``activity_between(a, b)`` is exactly ``prev_instant > a`` at that
    moment - no index of the whole history, and no per-episode bookkeeping.
    """

    row_seq: int = 0
    last_instant: datetime | None = None
    prev_instant: datetime | None = None


class EpisodeFolder:
    """The episode state machine, fed one decision at a time.

    One implementation, two drivers. ``build_episodes`` feeds it a whole
    history and keeps the closed episodes in a list; the streaming engine
    feeds it one export part at a time, persists the open set between runs
    through :meth:`snapshot`/:meth:`restore`, and hands each closure to a
    callback that writes it to the research store. The rules - the
    five-dimensional key, what a gap means, what ends an episode - live here
    and nowhere else, which is what keeps the two paths from drifting apart.
    """

    def __init__(
        self,
        *,
        gap_window_seconds: float = DEFAULT_GAP_WINDOW_SECONDS,
        accepted_state: str = "ACCEPT",
        on_close: Callable[[Episode, OpenEpisode, int], None] | None = None,
    ) -> None:
        self.gap_window_seconds = float(gap_window_seconds)
        self.accepted_state = accepted_state
        self._on_close = on_close
        self.open_by_key: dict[EpisodeKey, OpenEpisode] = {}
        self.state = FolderState()
        self.decisions_seen = 0
        self.accepts_without_edge = 0
        self.episodes_built = 0
        #: Only used when no callback was supplied (the whole-history driver).
        self.closed: list[Episode] = []

    # -- resumption --------------------------------------------------------

    def restore(
        self,
        open_episodes: Iterable[OpenEpisode],
        *,
        state: FolderState | None = None,
    ) -> None:
        """Adopt the open set and the fold cursor a previous run left behind.

        Insertion order is preserved because the original builder publishes
        the episodes that are still open in the order they entered the dict.
        """
        for open_ep in open_episodes:
            self.open_by_key[open_ep.key] = open_ep
        if state is not None:
            self.state = state

    def snapshot(self) -> FolderState:
        return FolderState(
            row_seq=self.state.row_seq,
            last_instant=self.state.last_instant,
            prev_instant=self.state.prev_instant,
        )

    # -- the machine -------------------------------------------------------

    def activity_between(self, start: datetime, end: datetime) -> bool:
        """Was anything decided strictly between these two instants?

        ``end`` is only used to document the call; the answer is already
        determined by the position in the stream (see :class:`FolderState`).
        """
        del end
        return self.state.prev_instant is not None and self.state.prev_instant > start

    def add(self, row: _DecisionRow) -> None:
        """Fold one decision in. Rows without a market are not fold input."""
        if not row.market_id:
            return
        self.decisions_seen += 1
        if row.state == self.accepted_state and row.net_edge_usd is None:
            self.accepts_without_edge += 1

        instant = row.decided_at_utc
        # The activity index only advances on rows that carry a market: the
        # original builder built its instant list from the rows it kept.
        # It also only advances FORWARD. Since the export moved to the write
        # sequence (WQN-20) a part can carry late REPLAY rows whose business
        # instant sits below what was already folded; letting one of those
        # set `last_instant` would make the index answer "was anything
        # decided between a and b" wrongly for the next gap, and the answer
        # would stay wrong for a row or two after the stream recovered. For
        # in-order rows `>` behaves exactly like the previous `!=` guard,
        # so nothing changes for any stream that never regresses.
        if self.state.last_instant is None or instant > self.state.last_instant:
            self.state.prev_instant = self.state.last_instant
            self.state.last_instant = instant
        seq = self.state.row_seq
        self.state.row_seq += 1

        key = row.key
        current = self.open_by_key.get(key)
        if current is not None and instant < current.last_seen:
            # A late row against an episode that has already folded past its
            # instant. It cannot close anything - its gap is negative and its
            # "state change" happened before activity the episode has already
            # recorded - so an ACCEPT is folded in where it lands and a
            # non-ACCEPT is a no-op for the episode body. The row still
            # counts toward decisions/accepts; it is never silently dropped
            # from the stream's own accounting.
            if row.state == self.accepted_state and row.net_edge_usd is not None:
                current.observe(row)
            return

        if current is not None:
            gap = (instant - current.last_seen).total_seconds()
            if gap > self.gap_window_seconds:
                missing = not self.activity_between(current.last_seen, instant)
                self._close(
                    current,
                    EndReason.GAP_DATA_MISSING if missing else EndReason.GAP_SIGNAL_BREAK,
                    seq,
                )
                current = None

        is_accept = row.state == self.accepted_state and row.net_edge_usd is not None

        if is_accept:
            assert row.net_edge_usd is not None
            value = float(row.net_edge_usd)
            if current is None:
                self.open_by_key[key] = OpenEpisode(
                    key=key,
                    first_seen=instant,
                    last_seen=instant,
                    count=1,
                    sum_edges=value,
                    first_edge=value,
                    last_edge=value,
                    max_edge=value,
                    min_edge=value,
                    opened_seq=seq,
                    first_market_price=row.market_price,
                    first_model_probability=row.model_probability,
                    first_available_depth_usd=row.available_depth_usd,
                    station=row.station,
                    bracket_label=row.bracket_label,
                    market_question=row.market_question,
                    fee_rate=row.fee_rate,
                )
            else:
                current.observe(row)
        elif current is not None:
            # A state that is not ACCEPT ends the episode. It is a real
            # signal, not a gap, so it closes as a state change.
            self._close(current, EndReason.STATE_CHANGE, seq)

    def finish(self) -> list[Episode]:
        """Close whatever is still open as ``end_of_data`` and return it.

        Only the whole-history driver calls this. The streaming engine leaves
        its episodes open in the store and renders them as ``end_of_data`` at
        publication time, because a stream that has merely stopped is not a
        stream that has ended.
        """
        for open_ep in list(self.open_by_key.values()):
            self._close(open_ep, EndReason.END_OF_DATA, self.state.row_seq)
        return self.closed

    def _close(self, open_ep: OpenEpisode, reason: str, seq: int) -> None:
        del self.open_by_key[open_ep.key]
        episode = episode_from_open(open_ep, end_reason=reason, closed_seq=seq)
        self.episodes_built += 1
        if self._on_close is None:
            self.closed.append(episode)
        else:
            # The open record travels with the episode: the running sum it holds
            # is not recoverable from the finished record, and the store keeps
            # the number that was actually accumulated.
            self._on_close(episode, open_ep, seq)


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
    for raw in rows:
        row = raw if isinstance(raw, _DecisionRow) else row_from_mapping(raw)
        if not row.market_id:
            continue
        normalised.append(row)

    normalised.sort(key=lambda r: r.decided_at_utc)

    folder = EpisodeFolder(
        gap_window_seconds=gap_window_seconds, accepted_state=accepted_state
    )
    for row in normalised:
        folder.add(row)

    open_at_end = len(folder.open_by_key)
    closed = folder.finish()
    closed.sort(key=lambda e: e.first_seen_utc)
    counters = {
        "decisions_seen": folder.decisions_seen,
        "episodes_built": len(closed),
        "accepts_without_edge": folder.accepts_without_edge,
        "open_at_end": open_at_end,
    }
    return closed, counters


class EpisodeSummary:
    """The five counts and two sums, accumulated one episode at a time.

    ``summarize`` formats this; the streaming engine fills it from a store
    cursor instead of a list. Both go through :meth:`as_dict`, so the two
    paths cannot disagree about what a number means or how it is shaped.

    ``unique_opportunity_edge_sum`` is a plain left fold in episode order,
    matching the original ``sum(e.first_edge for e in episodes)`` exactly -
    including its floating-point rounding, which is why the order is fixed
    rather than sorted by value. ``observation_edge_sum`` is the same kind of
    fold over ``mean_edge * observation_count``.
    """

    def __init__(self) -> None:
        self.opportunity_episodes = 0
        self.accept_observations = 0
        self.unique_opportunity_edge_sum = 0.0
        self.observation_edge_sum = 0.0
        self._markets: set[str] = set()
        self._pairs: set[tuple[str, str | None]] = set()
        self._dates: set[str] = set()

    def add(self, episode: Episode) -> None:
        self.opportunity_episodes += 1
        self.accept_observations += episode.observation_count
        self.unique_opportunity_edge_sum += episode.first_edge
        self.observation_edge_sum += episode.mean_edge * episode.observation_count
        self._markets.add(episode.key.market_id)
        self._pairs.add((episode.key.market_id, episode.key.direction))
        if episode.key.target_date is not None:
            self._dates.add(episode.key.target_date)

    def as_dict(self) -> dict[str, object]:
        """The five counts that must never be substituted for one another.

        Contract section 2.5. ``accept_observations`` is the number of
        evaluations; ``opportunity_episodes`` is the number of opportunities.
        They are different questions and the gap between them is the measure of
        how much a raw ACCEPT count was overstating.
        """
        return {
            "opportunity_episodes": self.opportunity_episodes,
            "accept_observations": self.accept_observations,
            "distinct_market_direction_pairs": len(self._pairs),
            "distinct_markets": len(self._markets),
            "distinct_target_dates": len(self._dates),
            "unique_opportunity_edge_sum": self.unique_opportunity_edge_sum,
            "observation_edge_sum": self.observation_edge_sum,
            "mean_episode_edge": (
                self.unique_opportunity_edge_sum / self.opportunity_episodes
                if self.opportunity_episodes
                else None
            ),
            "statistical_scope": ["opportunity_episodes", "observation_all_accepts"],
            "warnings": [
                "Observation Edge Sum counts repeated evaluations of one opportunity; "
                "it is not profit, not asset growth and not the expected return of "
                "independent trades.",
                "Unique Opportunity Edge Sum is the model's theoretical expectation "
                "under its own probabilities; it is not realised profit.",
            ],
        }


def summarize(episodes: Sequence[Episode]) -> dict[str, object]:
    """The summary of a materialised episode list (see :class:`EpisodeSummary`)."""
    summary = EpisodeSummary()
    for episode in episodes:
        summary.add(episode)
    return summary.as_dict()

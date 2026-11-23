"""The settlement verification queue: every market that ever said ACCEPT.

A win rate is only as honest as its coverage. The engine scores whatever
settlements happen to exist; nothing forces the set of *asked* markets to
match the set of markets that actually produced decisions. A pair that
silently never got asked looks identical to a pair the platform never
answered - and over a season, the difference between "our resolved sample"
and "our whole history" quietly becomes selection bias.

This module closes that gap. It takes the decision history and the
settlement layer and answers three questions, all from recorded facts:

1. **Is every ACCEPT market in the pipeline?** Every distinct
   ``(market_id, target_date)`` that produced at least one ACCEPT appears in
   the queue exactly once - no matter how many models, directions, modes or
   episodes it spans. The acceptance criterion "过去产生 ACCEPT 的市场都进入
   核对队列" is this invariant, stated as data rather than hope.

2. **What state is each pair in, and what should happen next?** The bucket
   names mirror the contract's settlement vocabulary exactly:

   ======================  ==================================  ==================
   bucket                  meaning                             action
   ======================  ==================================  ==================
   ``missing``             no settlement record at all         backfill
   ``pending``             platform has not finished           wait / re-backfill
   ``disputed``            disputed on the platform            none - never scored
   ``unavailable``         asked and not found                 investigate
   ``resolved_unverified`` platform resolved, observation      verify against
                           cross-check not done                observations
   ``verified``            resolved AND cross-checked          none - WIN/LOSS
                                                               eligible
   ======================  ==================================  ==================

   Only ``verified`` enters WIN/LOSS - which is the win-rate module's rule
   already; here it becomes a countable, auditable bucket.

3. **Can this pair be asked at all?** Asking the platform needs a
   ``condition_id``, and a decision row does not carry one. The catalog (a
   copy of the collector's ``markets`` table, or any file with the same
   columns) supplies it; a pair the catalog cannot resolve is marked
   ``needs_catalog`` with everything an operator needs to resolve it by
   hand. A queue that pretended to know conditions it does not have would
   send the next run to fetch garbage.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "QUEUE_VERSION",
    "SettlementQueueError",
    "build_settlement_queue",
    "catalog_from_collector_copy",
    "catalog_from_file",
]

#: Bumped when the bucket rules change. The queue is an intermediate artefact,
#: but a report whose semantics changed between runs is not comparable to the
#: one before it unless it says so.
QUEUE_VERSION = "settlement-queue-2"

#: ``state`` values that count as "this market produced an ACCEPT". Only the
#: literal matters: the decision export's other states (REJECT, NO_TRADE, …)
#: are exactly the markets that must NOT enter the queue, because a queue
#: built from every decided market would ask the platform about markets the
#: system never had an opinion on.
ACCEPT_STATES = frozenset({"ACCEPT"})

def _normalise_target(value: object) -> str | None:
    """Normalise a target date onto its civil day.

    A settlement index may carry ``"2026-10-13"`` while a decision export
    carries ``"2026-10-13T00:00:00"``: the same day under two shapes, which
    the pair key must not split into two buckets. Anything that is not
    datetime-shaped passes through untouched - including None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > 10 and text[10] in (" ", "T"):
        return text[:10]
    return text or None


def settlement_fetch_time(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("fetched_at_utc")
    if raw is None:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def latest_settlement_rows(settlements: Iterable[Any]) -> dict[tuple[str, str | None], Mapping[str, Any]]:
    """Use the newest fetch for each market and civil date, keeping the full evidence row."""
    oldest = datetime.min.replace(tzinfo=UTC)
    by_key: dict[tuple[str, str | None], Mapping[str, Any]] = {}
    for record in settlements:
        if not isinstance(record, Mapping):
            continue
        market_id = str(record.get("market_id") or "")
        target = _normalise_target(record.get("target_date"))
        if not market_id:
            continue
        key = (market_id, target)
        existing = by_key.get(key)
        # The same pair can be backfilled twice (a re-run, a revision). The
        # record with the newest fetch time is the answer; file order says
        # nothing about which one that is.
        if existing is None or (settlement_fetch_time(record) or oldest) >= (settlement_fetch_time(existing) or oldest):
            by_key[key] = record

    return by_key


ACCEPT = "ACCEPT"
BUCKET_MISSING = "missing"
BUCKET_PENDING = "pending"
BUCKET_DISPUTED = "disputed"
BUCKET_UNAVAILABLE = "unavailable"
BUCKET_RESOLVED_UNVERIFIED = "resolved_unverified"
BUCKET_VERIFIED = "verified"

#: Bucket → what the operator should do next. A bucket without an action is a
#: dead end in a workflow that is supposed to converge on "everything verified".
ACTIONS = {
    BUCKET_MISSING: "backfill",
    BUCKET_PENDING: "re-backfill",
    BUCKET_DISPUTED: "none",
    BUCKET_UNAVAILABLE: "investigate",
    BUCKET_RESOLVED_UNVERIFIED: "verify_observations",
    BUCKET_VERIFIED: "none",
}

WIN_LOSS_ELIGIBLE = frozenset({BUCKET_VERIFIED})


class SettlementQueueError(RuntimeError):
    """A queue cannot be built from these inputs."""


def build_settlement_queue(
    decisions: Sequence[Mapping[str, Any]],
    *,
    settlements: Sequence[Mapping[str, Any]] = (),
    catalog: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the verification queue from decisions, settlements and a catalog.

    ``decisions`` are decision rows (an export, a file, the state store's
    episodes). ``settlements`` are the records ``backfill_settlements``
    wrote. ``catalog`` rows carry the columns the queue cannot know from
    decisions alone - ``condition_id`` above all - and come from a copy of
    the collector's ``markets`` table.
    """
    now = now or datetime.now(tz=UTC)

    pairs, accept_stats = _accept_pairs(decisions)
    if not pairs:
        raise SettlementQueueError(
            "no ACCEPT decisions in the input: the queue's whole point is the "
            "markets that produced ACCEPTs, and an empty queue would silently "
            "bless an empty settlement pipeline"
        )

    by_key = latest_settlement_rows(settlements)

    catalog_by_id: dict[str, Mapping[str, Any]] = {}
    for row in catalog:
        market_id = str(row.get("market_id") or "")
        if market_id:
            catalog_by_id[market_id] = row

    queue: list[dict[str, Any]] = []
    buckets: dict[str, int] = {}
    catalog_matched = 0
    for (market_id, target_date), facts in sorted(
        pairs.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        bucket, fetched_at, source = _bucket_of(by_key.get((market_id, target_date)))
        buckets[bucket] = buckets.get(bucket, 0) + 1
        entry: dict[str, Any] = {
            "market_id": market_id,
            "target_date": target_date,
            "station": facts.get("station"),
            "bracket_label": facts.get("bracket_label"),
            "first_accept_utc": facts.get("first_accept_utc"),
            "accept_observations": facts.get("accept_observations"),
            "bucket": bucket,
            "action": ACTIONS[bucket],
            "win_loss_eligible": bucket in WIN_LOSS_ELIGIBLE,
            "settlement_fetched_at_utc": fetched_at,
            "settlement_source": source,
        }
        row = catalog_by_id.get(market_id)
        if row is not None:
            catalog_matched += 1
            entry["catalog"] = "matched"
            entry["needs_catalog"] = False
            for key in ("condition_id", "token_yes", "event_id"):
                entry[key] = row.get(key)
        else:
            entry["catalog"] = "unmatched"
            entry["needs_catalog"] = True
            # No invented identifiers: the pair goes to the operator with the
            # lookup that resolves it, not with a guess in the field.
            entry["catalog_lookup_hint"] = f"gamma /markets? id={market_id}"
        queue.append(entry)

    eligible = sum(1 for entry in queue if entry["win_loss_eligible"])
    # The coverage check, honestly: recompute the expected pair set from the
    # ACCEPT decisions through a *different* code path than the accumulation
    # loop that built the queue, and compare the sets. `len(queue) ==
    # len(pairs)` was a tautology - both sides of it came from the same
    # structure, so it could never catch a pair the queue lost. This version
    # catches duplicates, lost pairs and normalisation drift.
    expected_pairs = {
        (str(row.get("market_id")), _normalise_target(row.get("target_date")))
        for row in decisions
        if row.get("market_id")
        and str(row.get("state") or "").upper() in ACCEPT_STATES
    }
    queued_pairs = {(entry["market_id"], entry["target_date"]) for entry in queue}
    return {
        "queue_version": QUEUE_VERSION,
        "generated_at_utc": now.isoformat(),
        "coverage": {
            "accept_markets": len(queue),
            "accept_rows": accept_stats["rows"],
            # An ACCEPT row without a market id is a hole in the coverage
            # claim - it can never be queued. REJECT/NO_TRADE rows without an
            # id are irrelevant here and are not counted: this number exists
            # so an export that loses ids on the rows that matter is visible.
            "accept_rows_without_market_id": accept_stats["missing_id"],
            "buckets": buckets,
            "catalog_matched": catalog_matched,
            "catalog_unmatched": len(queue) - catalog_matched,
            "all_accept_markets_in_queue": queued_pairs == expected_pairs,
            "win_loss_eligible_markets": eligible,
            "win_loss_gating": "official resolved + observation-verified only",
        },
        "queue": queue,
    }


def _accept_pairs(
    decisions: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], dict], dict[str, int]]:
    """Distinct ``(market_id, target_date)`` pairs with at least one ACCEPT.

    Rows without a market id cannot be queued by definition. ACCEPT rows in
    that state are counted (`missing_id`) - they are holes in the coverage
    claim - while non-ACCEPT rows without an id are irrelevant to this queue
    and are not counted at all. The state comparison is case-insensitive: a
    feed that starts saying "accept" must degrade loudly, not vanish.
    """
    pairs: dict[tuple[str, str], dict] = {}
    stats = {"rows": 0, "missing_id": 0}
    for row in decisions:
        state = str(row.get("state") or "").upper()
        if state not in ACCEPT_STATES:
            continue
        stats["rows"] += 1
        market_id = row.get("market_id")
        if not market_id:
            stats["missing_id"] += 1
            continue
        target = _normalise_target(row.get("target_date"))
        key = (str(market_id), target)
        entry = pairs.get(key)
        if entry is None:
            pairs[key] = {
                "station": row.get("station"),
                "bracket_label": row.get("bracket_label"),
                "first_accept_utc": row.get("decision_time_utc")
                or row.get("decided_at_utc"),
                "accept_observations": 1,
            }
        else:
            entry["accept_observations"] += 1
    return pairs, stats


def _bucket_of(
    record: Mapping[str, Any] | None,
) -> tuple[str, str | None, str | None]:
    """Map one settlement record onto a queue bucket.

    The bucket is derived from the record's own fields, in the same order the
    win-rate module scores them - so a pair the queue calls ``verified`` is
    exactly a pair the engine may score, and the two can never drift apart.
    """
    if record is None:
        return BUCKET_MISSING, None, None
    status = str(record.get("settlement_status") or record.get("status") or "")
    verification = str(record.get("verification_status") or "")
    fetched = record.get("fetched_at_utc")
    fetched_text = str(fetched) if fetched is not None else None
    source = record.get("resolution_source")

    if status == "RESOLVED":
        payout = record.get("resolved_outcome")
        if verification == "VERIFIED" and type(payout) in (float, int) and payout in (0, 1):
            return BUCKET_VERIFIED, fetched_text, source
        if verification == "UNVERIFIED":
            return BUCKET_RESOLVED_UNVERIFIED, fetched_text, source
        # A resolved record with no verification label: the cross-check state
        # is unknown, and "unknown" cannot be allowed to read as "verified".
        return BUCKET_RESOLVED_UNVERIFIED, fetched_text, source
    if status == "DISPUTED":
        return BUCKET_DISPUTED, fetched_text, source
    if status in ("PENDING", "UNAVAILABLE"):
        return (
            BUCKET_PENDING if status == "PENDING" else BUCKET_UNAVAILABLE,
            fetched_text,
            source,
        )
    # An unlabelled record exists but says nothing; that is the missing bucket
    # with a stub attached, not a state the contract defines.
    return BUCKET_MISSING, fetched_text, source


# ---------------------------------------------------------------------------
# catalog inputs
# ---------------------------------------------------------------------------


def catalog_from_file(path: str) -> list[dict[str, Any]]:
    """Read a catalog file: a JSON list with the collector markets' columns."""
    import json
    from pathlib import Path

    source = Path(path)
    if not source.exists():
        raise SettlementQueueError(f"catalog file not found: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SettlementQueueError("a catalog file must be a JSON list")
    return [row for row in payload if isinstance(row, dict)]


def catalog_from_collector_copy(db_path: str) -> list[dict[str, Any]]:
    """Read the catalog from a COPY of the collector's database.

    The copy - never the live file, whose writer holds the lease and which a
    byte-read could tear - is opened read-only, and only the columns the
    queue needs leave it. A verified backup (see the recovery drill) is an
    ideal source: it is a copy by construction, and it was verified on the
    way out.
    """
    import duckdb

    catalog: list[dict[str, Any]] = []
    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("SET TimeZone='UTC'")
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main'"
            ).fetchall()
        }
        if "markets" not in tables:
            raise SettlementQueueError(
                f"{db_path} has no markets table - it is not a collector database"
            )
        rows = con.execute(
            "SELECT market_id, event_id, condition_id, token_yes, station, "
            "bracket_label, bracket_low, bracket_high, target_date "
            "FROM markets ORDER BY market_id"
        ).fetchall()
        for row in rows:
            catalog.append(
                {
                    "market_id": str(row[0]),
                    "event_id": row[1],
                    "condition_id": row[2],
                    "token_yes": row[3],
                    "station": row[4],
                    "bracket_label": row[5],
                    "bracket_low": row[6],
                    "bracket_high": row[7],
                    "target_date": str(row[8]) if row[8] is not None else None,
                }
            )
    finally:
        con.close()
    return catalog

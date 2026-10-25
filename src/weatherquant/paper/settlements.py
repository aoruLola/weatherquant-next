"""Official settlement: the verified answer that releases occupied capital.

The vocabulary is :mod:`weatherquant.performance.settlement`'s - the same
five states, parsed from Polymarket's own resolutions endpoint by the
research side. The paper engine consumes the *published record* of that
answer (a JSONL feed keyed by market and revision) and applies exactly one
rule: only ``RESOLVED`` with a payout of 0 or 1 moves anything.

Three properties the acceptance criteria name, and how they live here:

**Unsettled is never profit.** A PENDING, DISPUTED, UNAVAILABLE or
UNVERIFIABLE record is stored for audit - and adjusts nothing. Realised
P&L exists only after a RESOLVED payout lands on a position.

**Revisions are compensation events, not edits.** A later record with a
changed payout writes a CORRECTION entry next to the original SETTLE_PAYOUT
entry; no settled row is ever rewritten. When the correction cannot be
applied automatically - a clawback that would drive cash negative - the
settlement row is flagged ``review_required`` and the balances stay
untouched: fabricating the fix would be worse than asking a human.

**A dispute reopens nothing.** A DISPUTED record arriving after a payout
was applied marks manual review - the final answer is unknown, and
guessing it (either keeping or reversing the payout) would fabricate
realised profit either way.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SettlementEvent",
    "SettlementParseError",
    "parse_settlement_record",
    "read_settlement_feed",
    "settlement_id",
]

#: performance/settlement.py's vocabulary. Imported types would drag the
#: research package's concerns in here; the states are the shared contract.
STATUSES = ("RESOLVED", "DISPUTED", "PENDING", "UNAVAILABLE", "UNVERIFIABLE")

#: The only two payouts an official resolution can carry.
RESOLVED_PAYOUTS = (0.0, 1.0)


class SettlementParseError(ValueError):
    """A feed record is not a decodable settlement.

    ``code`` is stable, like the decision contract's EventError codes: a
    malformed record is a data-quality fact, never silently skipped and
    never half-applied.
    """

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def settlement_id(*, market_id: str, revision: int) -> str:
    basis = f"{market_id}|{revision}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """One published settlement answer, at one revision."""

    market_id: str
    revision: int
    status: str
    yes_payout: float | None
    settled_at_utc: datetime | None
    evidence: dict[str, Any]
    raw: dict[str, Any]

    @property
    def is_payout(self) -> bool:
        """Whether this record, by itself, authorises a ledger movement."""
        return self.status == "RESOLVED" and self.yes_payout in RESOLVED_PAYOUTS

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, sort_keys=True, default=str)


def parse_settlement_record(row: Any) -> SettlementEvent:
    """Decode one settlement-feed record.

    Raises :class:`SettlementParseError` with a stable code for anything
    incomplete or inconsistent. A payout outside {0, 1} is malformed, not
    exotic: the platform's answer is a share of the $1 collateral, and a
    fraction would mean we are reading a proposal, not a resolution.
    """
    if not isinstance(row, dict):
        raise SettlementParseError("MALFORMED_RECORD", f"record is {type(row).__name__}, expected an object")
    missing = [k for k in ("market_id", "revision", "status") if row.get(k) in (None, "")]
    if missing:
        raise SettlementParseError("MALFORMED_RECORD", f"record is missing field(s): {missing}")

    try:
        revision = int(row["revision"])
    except (TypeError, ValueError):
        raise SettlementParseError(
            "MALFORMED_RECORD", f"revision is {row['revision']!r}, expected an integer"
        ) from None
    if revision < 1:
        raise SettlementParseError("MALFORMED_RECORD", f"revision must be >= 1, got {revision}")

    status = str(row["status"]).strip().upper()
    if status not in STATUSES:
        raise SettlementParseError(
            "MALFORMED_RECORD",
            f"unknown status {row['status']!r}; the vocabulary is {list(STATUSES)}",
        )

    payout_raw = row.get("yes_payout")
    payout: float | None = None
    if status == "RESOLVED":
        if payout_raw is None or payout_raw == "":
            raise SettlementParseError(
                "MALFORMED_RECORD", "a RESOLVED record must carry yes_payout (0 or 1)"
            )
        try:
            payout = float(payout_raw)
        except (TypeError, ValueError):
            raise SettlementParseError(
                "MALFORMED_RECORD", f"yes_payout is {payout_raw!r}, expected 0 or 1"
            ) from None
        if payout not in RESOLVED_PAYOUTS:
            raise SettlementParseError(
                "MALFORMED_RECORD",
                f"yes_payout is {payout!r}; only {list(RESOLVED_PAYOUTS)} are resolutions - "
                "anything else is a proposal, a sentinel or corruption",
            )
    elif payout_raw is not None:
        raise SettlementParseError(
            "MALFORMED_RECORD",
            f"status {status} must not carry yes_payout; a payout on a "
            "non-resolution would fabricate profit",
        )

    settled_at: datetime | None = None
    raw_ts = row.get("settled_at_utc")
    if raw_ts not in (None, ""):
        try:
            settled_at = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            raise SettlementParseError(
                "MALFORMED_RECORD", f"settled_at_utc is {raw_ts!r}, expected ISO-8601"
            ) from None

    return SettlementEvent(
        market_id=str(row["market_id"]),
        revision=revision,
        status=status,
        yes_payout=payout,
        settled_at_utc=settled_at,
        evidence={
            k: row[k]
            for k in ("condition_id", "question_id", "transaction_hash", "source", "retrieved_at_utc")
            if row.get(k) is not None
        },
        raw=dict(row),
    )


def read_settlement_feed(path: str | Path) -> list[SettlementEvent]:
    """Read every record in a published settlement feed, in revision order.

    The feed is the paper engine's settlement channel - a JSONL file written
    by whoever fetches the platform's resolutions (the research side's
    ``performance.settlement`` already knows how to ask the endpoint; the
    mapping and the writing are deployment concerns). Records are ordered by
    ``(market_id, revision)`` so a correction is always seen after the
    settlement it corrects; the applier's natural key makes a re-read a
    no-op, so no feed cursor is needed yet.
    """
    target = Path(path)
    if not target.is_file():
        return []
    events: list[SettlementEvent] = []
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SettlementParseError("MALFORMED_LINE", f"feed line is not JSON: {exc}") from exc
            events.append(parse_settlement_record(row))
    events.sort(key=lambda e: (e.market_id, e.revision))
    return events

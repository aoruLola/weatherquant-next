"""Platform settlement, read from Polymarket's own resolutions endpoint.

Everything here was written against a live response captured on 2026-10-13,
not against a guess. Four things about that response are not what the older
notes in this repo implied, and each one is a trap:

* **The host is `data-api.polymarket.com`.** Not clob, not gamma - both
  return 404 for `/v2/resolutions`. Anyone who assumed otherwise gets an
  HTML error page and no idea why.

* **The response is wrapped in ``{"data": [...]}``.** Reading ``payload[
  "status"]`` directly raises, and worse, a caller who "fixes" it with a
  ``.get()`` default ends up silently classifying every market as unsettled.

* **``price`` is a string, and ``"69"`` is a sentinel.** It is not a payout
  of 69 dollars; it means no payout has been determined. A resolved market
  still carries ``reproposed_price: "69"`` alongside a real ``price``, so
  the sentinel has to be read off the right field.

* **A YES winner's ``price`` is ``"1000000000000000000"``** - fixed-point
  with 18 decimals, not the plain ``"1"`` the first fixtures happened to
  use. Parsing only ``{"0", "1"}`` left every winning market PENDING
  forever, which biases a win rate toward losses while looking like a
  sparse-settlement problem. Only 0.0 and 1.0 are answers; everything else
  (the sentinel, intermediate fixed-point values, junk) normalises to None
  and stays out of the denominator.

* **Python's default User-Agent is refused.** ``urllib`` gets HTTP 403 with
  a Cloudflare ``error code: 1010``; ``httpx`` gets 200. The collector
  already speaks httpx, so this module never opens a socket itself - the
  caller injects a getter, and httpx is the requirement.

The weather observation is deliberately *not* part of this module. A
platform resolution and a thermometer reading are two independent facts, and
merging them would let one silently stand in for the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DATA_API_BASE",
    "FIXED_POINT_SCALE",
    "SENTINEL_PRICE",
    "Resolution",
    "SettlementStatus",
    "classify",
    "fetch_resolution",
    "parse_resolutions",
    "yes_payout",
]

DATA_API_BASE = "https://data-api.polymarket.com"

#: Polymarket prices arrive as fixed-point strings with 18 decimals: a YES
#: winner is ``"1000000000000000000"`` (1e18), a loser ``"0"``. The first
#: fixtures only ever showed ``"0"``, and reading ``{"0", "1"}`` made every
#: winning market parse as undetermined - a win rate biased toward losses
#: that looks exactly like a sparse-settlement problem. The scale is part of
#: the wire format, not a detail of one response.
FIXED_POINT_SCALE = 10**18

#: Polymarket's "no payout determined" marker. It shows up in `price` while
#: a proposal is outstanding, and also in `reproposed_price` on a market that
#: *has* resolved - so it is checked against `price` only, never against the
#: proposal fields.
SENTINEL_PRICE = "69"


class SettlementStatus:
    """The five states the contract allows. No others, no silent fallbacks."""

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    DISPUTED = "DISPUTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True, slots=True)
class Resolution:
    """One settlement answer, or the absence of one.

    ``price`` is normalised to a float in ``{0.0, 1.0}`` when the market has
    actually resolved, and ``None`` otherwise. Nothing else in the codebase
    is allowed to see the raw sentinel.
    """

    condition_id: str | None
    question_id: str | None
    status: str | None
    price: float | None
    was_disputed: bool
    transaction_hash: str | None
    log_index: str | None
    last_update_timestamp: str | None
    raw: dict[str, Any] = field(default_factory=dict)


def _normalise_price(value: object) -> float | None:
    """``"1000000000000000000"`` -> 1.0, ``"69"`` -> None, junk -> None.

    The wire format is fixed-point with 18 decimals, so the integer string is
    divided by 1e18 and only 0.0 / 1.0 survive - a payout of anything between
    (the sentinel, ``"500000000000000000"``, a truncated number) is an
    unanswered question, and the caller must report it as such rather than
    inherit a guess. Plain ``"0"`` / ``"1"`` are accepted too: they are the
    same answers in the scale the first fixtures used, and a wider reader
    here costs nothing.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text == SENTINEL_PRICE:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    scaled = number / FIXED_POINT_SCALE
    if scaled in (0.0, 1.0):
        return scaled
    if number in (0, 1):  # the legacy plain-scale answer
        return float(number)
    return None


def parse_resolutions(payload: Any) -> list[Resolution]:
    """Turn one raw ``/v2/resolutions`` body into zero or more records.

    The endpoint answers ``{"data": [ … ]}``, with ``data`` empty when the
    condition is unknown. An empty list therefore means *not found*, which is
    a different fact from *found but not settled*.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []

    out: list[Resolution] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            Resolution(
                condition_id=row.get("condition_id"),
                question_id=row.get("question_id"),
                status=str(row["status"]) if row.get("status") is not None else None,
                price=_normalise_price(row.get("price")),
                was_disputed=bool(row.get("was_disputed", False)),
                transaction_hash=row.get("transaction_hash"),
                log_index=row.get("log_index"),
                last_update_timestamp=row.get("last_update_timestamp"),
                raw=row,
            )
        )
    return out


def classify(resolution: Resolution | None) -> str:
    """Map a settlement answer onto the contract's status vocabulary.

    Order matters: a disputed market is disputed whether or not it also
    resolved, because a reader who sees ``RESOLVED`` will treat it as final.
    """
    if resolution is None:
        return SettlementStatus.UNAVAILABLE
    if resolution.was_disputed:
        return SettlementStatus.DISPUTED
    if resolution.status == "resolved" and resolution.price is not None:
        return SettlementStatus.RESOLVED
    # Found but not answered: `posed`, the sentinel price, or any status this
    # module has not seen. It is still pending, not unavailable - the
    # condition exists, the platform simply has not finished.
    return SettlementStatus.PENDING


def yes_payout(resolution: Resolution | None) -> float | None:
    """What one YES share pays out: 1.0, 0.0, or None if unknowable.

    This is the whole of the BUY YES rule. It deliberately says nothing
    about SELL YES, whose economics are a different question the contract
    refuses to answer until the position semantics are proven.
    """
    if classify(resolution) != SettlementStatus.RESOLVED:
        return None
    assert resolution is not None
    return resolution.price


def fetch_resolution(
    get_json: Callable[..., Any],
    *,
    condition_id: str,
    timeout: float = 20.0,
) -> tuple[str, Resolution | None]:
    """Ask the platform for one condition, returning ``(status, record)``.

    ``get_json`` is injected so this module never owns a socket - it keeps
    the caller's rate limits, retries and httpx User-Agent. The collector's
    ``ResilientHttp.get_json`` fits as-is.

    ``(SettlementStatus.UNAVAILABLE, None)`` is what an unreachable service
    produces; a *missing* condition also yields ``None`` from the parser and
    therefore ``UNAVAILABLE``. Callers that need to tell "the API is down"
    from "the market is unknown" should check the transport separately -
    conflating them would let an outage look like an unfinished market.
    """
    url = f"{DATA_API_BASE}/v2/resolutions"
    try:
        payload = get_json(url, params={"condition": condition_id}, timeout=timeout)
    except Exception:
        return SettlementStatus.UNAVAILABLE, None
    records = parse_resolutions(payload)
    if not records:
        return SettlementStatus.UNAVAILABLE, None
    # The endpoint is one-condition-one-answer; take the first and keep the
    # rest only if a future response ever batches them.
    record = records[0]
    return classify(record), record

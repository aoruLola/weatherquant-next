"""Settlement parsing, judged against responses captured from production.

The fixtures in ``tests/fixtures/polymarket/`` are raw HTTP bodies fetched
from ``data-api.polymarket.com/v2/resolutions`` on 2026-10-13, unmodified.
They are not hand-written shapes, because hand-written shapes are exactly
how a contract gets implemented against something the API never said:

* the host is data-api, not clob and not gamma (both 404);
* the body is wrapped in ``{"data": [...]}``;
* ``price`` is a string and ``"69"`` means undecided, not sixty-nine;
* a resolved market still carries ``reproposed_price: "69"``, so the
  sentinel has to be read off ``price`` alone.

Anything that changes those four facts will fail here before it fails a
backfill.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weatherquant.performance.settlement import (
    DATA_API_BASE,
    Resolution,
    SettlementStatus,
    classify,
    fetch_resolution,
    parse_resolutions,
    yes_payout,
)

FIXTURES = Path(__file__).parent / "fixtures" / "polymarket"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- the four facts the response actually has --------------------------------


def test_the_endpoint_is_data_api_not_clob_or_gamma() -> None:
    """The documented host matters: the wrong one returns HTML, not an error."""
    assert DATA_API_BASE == "https://data-api.polymarket.com"


def test_a_resolved_response_is_wrapped_in_data() -> None:
    body = _load("resolutions_resolved.json")

    assert "status" not in body, "the body is not flat; reading it directly raises"
    (record,) = parse_resolutions(body)
    assert record.status == "resolved"
    assert record.condition_id.startswith("0x1d74")


def test_price_is_a_string_and_resolves_to_zero() -> None:
    """'0' is a payout of nothing - a LOSS for BUY YES, not a missing answer."""
    (record,) = parse_resolutions(_load("resolutions_resolved.json"))

    assert record.raw["price"] == "0"  # string, as the API sends it
    assert record.price == pytest.approx(0.0)
    assert yes_payout(record) == pytest.approx(0.0)


def test_the_sentinel_is_69_and_yields_no_answer() -> None:
    """An undecided market reports price '69'. Treating it as a payout is the
    single worst mistake available here - it would turn every open market
    into a $69-per-share win."""
    (record,) = parse_resolutions(_load("resolutions_pending.json"))

    assert record.raw["price"] == "69"
    assert record.price is None, "the sentinel must never reach a caller as a number"
    assert classify(record) == SettlementStatus.PENDING
    assert yes_payout(record) is None


def test_the_sentinel_in_reproposed_price_does_not_spoil_a_real_answer() -> None:
    """A resolved market carries reproposed_price='69' next to a real price.

    Reading the sentinel off the proposal fields would mark a settled market
    as undecided, which is the opposite error and just as wrong.
    """
    (record,) = parse_resolutions(_load("resolutions_resolved.json"))

    assert record.raw["reproposed_price"] == "69"
    assert record.price == pytest.approx(0.0)
    assert classify(record) == SettlementStatus.RESOLVED


def test_an_unknown_condition_is_an_empty_data_list() -> None:
    """Not-found and found-but-unsettled are different facts."""
    assert parse_resolutions(_load("resolutions_unknown.json")) == []


# --- status vocabulary -------------------------------------------------------


def test_disputed_beats_resolved_so_a_reader_cannot_treat_it_as_final() -> None:
    disputed = Resolution(
        condition_id="0x1", question_id=None, status="resolved", price=1.0,
        was_disputed=True, transaction_hash=None, log_index=None,
        last_update_timestamp=None,
    )
    assert classify(disputed) == SettlementStatus.DISPUTED
    assert yes_payout(disputed) is None, "a disputed payout must not be spent"


def test_missing_record_is_unavailable_not_pending() -> None:
    assert classify(None) == SettlementStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("status", "price", "expected"),
    [
        ("resolved", 1.0, SettlementStatus.RESOLVED),
        ("resolved", 0.0, SettlementStatus.RESOLVED),
        ("posed", None, SettlementStatus.PENDING),
        ("weird-future-status", 0.0, SettlementStatus.PENDING),
    ],
)
def test_status_mapping(status: str, price: float | None, expected: str) -> None:
    record = Resolution(
        condition_id="0x1", question_id=None, status=status, price=price,
        was_disputed=False, transaction_hash=None, log_index=None,
        last_update_timestamp=None,
    )
    assert classify(record) == expected


def test_a_resolved_yes_paying_one_is_a_win_for_buy_yes() -> None:
    record = Resolution(
        condition_id="0x1", question_id=None, status="resolved", price=1.0,
        was_disputed=False, transaction_hash="0xabc", log_index="1",
        last_update_timestamp="1791868379",
    )
    assert yes_payout(record) == pytest.approx(1.0)


def test_only_resolved_and_verified_reach_a_payout() -> None:
    """PENDING, DISPUTED and UNAVAILABLE must all answer None, never 0 or 1.

    Defaulting an unknown outcome to a loss is how a sample silently fills
    with outcomes that never happened.
    """
    for record in (
        None,
        Resolution(None, None, "posed", None, False, None, None, None),
        Resolution(None, None, "resolved", None, False, None, None, None),
        Resolution(None, None, "resolved", 1.0, True, None, None, None),
    ):
        assert yes_payout(record) is None


def test_the_chain_reference_is_kept_for_audit() -> None:
    """Every settled claim has to be traceable back to a transaction."""
    (record,) = parse_resolutions(_load("resolutions_resolved.json"))

    assert record.transaction_hash and record.transaction_hash.startswith("0x")
    assert record.log_index is not None
    assert record.last_update_timestamp is not None


# --- transport ---------------------------------------------------------------


def test_a_transport_failure_is_unavailable_not_a_crash() -> None:
    def boom(*_args, **_kwargs):
        raise ConnectionError("network down")

    status, record = fetch_resolution(boom, condition_id="0x1")
    assert status == SettlementStatus.UNAVAILABLE
    assert record is None


def test_an_empty_body_is_unavailable() -> None:
    def empty(*_args, **_kwargs):
        return {"data": []}

    assert fetch_resolution(empty, condition_id="0x1")[0] == SettlementStatus.UNAVAILABLE


def test_a_malformed_body_never_raises() -> None:
    """A body that is not a dict must not take the collector down with it."""
    for junk in (None, [], "not json", 42, {"data": "oops"}, {"data": [1, 2]}):
        assert parse_resolutions(junk) == [] or isinstance(parse_resolutions(junk), list)


def test_the_request_goes_to_the_right_host_with_the_right_parameter() -> None:
    seen = {}

    def spy(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return _load("resolutions_resolved.json")

    fetch_resolution(spy, condition_id="0xabc", timeout=5.0)

    assert seen["url"] == f"{DATA_API_BASE}/v2/resolutions"
    assert seen["params"] == {"condition": "0xabc"}
    assert seen["timeout"] == 5.0


# --- the wire price is fixed-point with 18 decimals (real API samples) --------


def _fixture(name: str) -> dict:
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "polymarket" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_yes_winner_parses_from_the_1e18_fixed_point_body() -> None:
    """Captured 2026-10-26 from /v2/resolutions: a winning market's price is
    "1000000000000000000". Reading only {"0", "1"} left every winner PENDING
    forever - a win rate biased toward losses (caught in local acceptance)."""
    body = _fixture("resolutions_winner_1e18.json")
    records = parse_resolutions(body)
    assert len(records) == 1
    assert records[0].status == "resolved"
    assert records[0].price == 1.0, f"the winner must parse as a payout, got {records[0].price}"
    assert classify(records[0]) == SettlementStatus.RESOLVED
    assert yes_payout(records[0]) == 1.0


def test_a_loser_parses_from_the_real_body() -> None:
    body = _fixture("resolutions_loser.json")
    record = parse_resolutions(body)[0]
    assert record.price == 0.0
    assert classify(record) == SettlementStatus.RESOLVED
    assert yes_payout(record) == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1000000000000000000", 1.0),  # the wire's YES winner
        ("0", 0.0),
        ("1", 1.0),  # the legacy plain-scale answer the old fixtures used
        ("69", None),  # the sentinel: no payout determined
        ("500000000000000000", None),  # intermediate fixed-point: not an answer
        ("1e18", None),  # not the wire's spelling; refusing beats guessing
        ("", None),
        ("resolved", None),
        (None, None),
        (True, None),
    ],
)
def test_price_normalisation_accepts_only_answers(raw, expected) -> None:
    from weatherquant.performance.settlement import _normalise_price

    assert _normalise_price(raw) == expected

def test_fetch_uses_only_the_requested_condition():
    foreign = {"condition_id": "0xdef", "status": "resolved", "price": "1"}
    matching = {"condition_id": "0xABC", "status": "resolved", "price": "0"}
    status, record = fetch_resolution(lambda *a, **k: {"data": [foreign, matching]}, condition_id="0xabc")
    assert status == SettlementStatus.RESOLVED and record.price == 0.0
    assert fetch_resolution(lambda *a, **k: {"data": [foreign]}, condition_id="0xabc") == (
        SettlementStatus.UNAVAILABLE, None,
    )

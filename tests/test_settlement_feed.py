"""The settlement feed publisher: the loop that releases paper capital.

Without it no paper position ever settles. These tests pin what may be
published (only two agreeing platform reads become VERIFIED), what must not
(an open market, a transport failure), that a stable answer is one line, and
that the published record is exactly what the paper ledger pays out on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from paper_fills_support import make_fill
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.settlements import SettlementParseError, read_settlement_feed
from weatherquant.paper.store import PaperStore
from weatherquant.settlement_feed import (
    GAMMA_API_BASE,
    SettlementPublisher,
    archived_markets,
    due_markets,
    gamma_yes_payout,
    run_once,
)
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
CID = "0x" + "ab" * 32
YES_WIN = "1000000000000000000"


def gamma_market(*, closed=True, prices=("1", "0"), uma="resolved", market_id="mkt-1"):
    return {
        "id": market_id,
        "conditionId": CID,
        "closed": closed,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(list(prices)),
        "umaResolutionStatus": uma,
    }


def resolution(*, price=YES_WIN, status="resolved", disputed=False):
    return {
        "data": [
            {
                "condition_id": CID,
                "question_id": "0x" + "cd" * 32,
                "status": status,
                "was_disputed": disputed,
                "price": price,
                "reproposed_price": "69",
                "transaction_hash": "0xfeed",
                "last_update_timestamp": "1795241348",
            }
        ]
    }


class FakeApi:
    def __init__(self, market, res=None, fail=False):
        self.market, self.res, self.fail = market, res, fail
        self.calls: list[str] = []

    def __call__(self, url, *, params=None, timeout=20.0):
        self.calls.append(url)
        if self.fail:
            raise ConnectionError("down")
        if url.startswith(GAMMA_API_BASE):
            return self.market
        return self.res


def publisher(tmp_path, api):
    return SettlementPublisher(tmp_path / "settlements" / "feed.jsonl", api, clock=lambda: NOW)


def lines(pub):
    if not pub.feed_path.exists():
        return []
    return [json.loads(x) for x in pub.feed_path.read_text().splitlines() if x.strip()]


class TestGammaPayout:
    def test_final_yes_and_no(self):
        assert gamma_yes_payout(gamma_market(prices=("1", "0"))) == 1.0
        assert gamma_yes_payout(gamma_market(prices=("0", "1"))) == 0.0

    @pytest.mark.parametrize(
        "market",
        [
            gamma_market(closed=False),
            gamma_market(uma="proposed"),
            gamma_market(prices=("0.97", "0.03")),
            {**gamma_market(), "outcomes": json.dumps(["No", "Yes"])},
        ],
    )
    def test_not_final(self, market):
        assert gamma_yes_payout(market) is None


class TestPublisher:
    def test_open_market_publishes_nothing_and_skips_data_api(self, tmp_path):
        api = FakeApi(gamma_market(closed=False))
        result = publisher(tmp_path, api).sync(["mkt-1"])
        assert result.open_markets == 1 and result.published == 0
        assert len(api.calls) == 1

    def test_agreeing_reads_publish_one_verified_record(self, tmp_path):
        pub = publisher(tmp_path, FakeApi(gamma_market(), resolution()))
        assert pub.sync(["mkt-1"]).published == 1
        (row,) = lines(pub)
        assert row["status"] == "RESOLVED"
        assert row["verification_status"] == "VERIFIED"
        assert row["yes_payout"] == 1.0 and row["revision"] == 1
        # Stable answer: a second pass neither re-asks nor re-publishes.
        result = pub.sync(["mkt-1"])
        assert result.final == 1 and result.published == 0
        assert len(lines(pub)) == 1

    def test_disagreeing_reads_are_unconfirmed_then_verified_at_a_higher_revision(self, tmp_path):
        api = FakeApi(gamma_market(prices=("0", "1"), uma="proposed"), resolution())
        pub = publisher(tmp_path, api)
        pub.sync(["mkt-1"])
        pub.sync(["mkt-1"])  # unchanged: still one line
        assert [r["verification_status"] for r in lines(pub)] == ["UNCONFIRMED"]
        api.market = gamma_market()
        pub.sync(["mkt-1"])
        rows = lines(pub)
        assert [(r["revision"], r["verification_status"]) for r in rows] == [(1, "UNCONFIRMED"), (2, "VERIFIED")]

    def test_dispute_and_pending_carry_no_payout(self, tmp_path):
        # An open dispute: no final answer on either side yet.
        open_dispute = FakeApi(gamma_market(uma="disputed"), resolution(price="69", status="disputed", disputed=True))
        pub = publisher(tmp_path, open_dispute)
        pub.sync(["mkt-1"])
        (row,) = lines(pub)
        assert row["status"] == "DISPUTED" and "yes_payout" not in row
        # Data-api resolved after a dispute but Gamma not final yet: still disputed.
        pub_half = publisher(tmp_path / "half", FakeApi(gamma_market(uma="proposed"), resolution(disputed=True)))
        pub_half.sync(["mkt-1"])
        assert lines(pub_half)[0]["status"] == "DISPUTED" and "yes_payout" not in lines(pub_half)[0]
        pub2 = publisher(tmp_path / "b", FakeApi(gamma_market(), resolution(price="69", status="proposed")))
        pub2.sync(["mkt-1"])
        assert lines(pub2)[0]["status"] == "PENDING"

    def test_a_resolved_dispute_settles_once_both_reads_agree(self, tmp_path):
        # was_disputed never resets; without this the position would stay open forever.
        api = FakeApi(gamma_market(uma="disputed"), resolution(price="69", status="disputed", disputed=True))
        pub = publisher(tmp_path, api)
        pub.sync(["mkt-1"])
        api.market, api.res = gamma_market(prices=("0", "1")), resolution(price="0", disputed=True)
        pub.sync(["mkt-1"])
        rows = lines(pub)
        assert [(r["revision"], r["status"]) for r in rows] == [(1, "DISPUTED"), (2, "RESOLVED")]
        assert rows[1]["verification_status"] == "VERIFIED" and rows[1]["yes_payout"] == 0.0
        assert rows[1]["was_disputed"] is True
        events = read_settlement_feed(pub.feed_path)
        assert events[-1].is_payout and events[-1].yes_payout == 0.0

    def test_a_resolved_dispute_with_disagreeing_gamma_is_not_paid(self, tmp_path):
        pub = publisher(tmp_path, FakeApi(gamma_market(prices=("0", "1")), resolution(disputed=True)))
        pub.sync(["mkt-1"])
        (row,) = lines(pub)
        assert row["status"] == "DISPUTED" and "yes_payout" not in row

    def test_a_torn_tail_is_repaired_so_the_feed_stays_readable(self, tmp_path):
        pub = publisher(tmp_path, FakeApi(gamma_market(), resolution()))
        pub.sync(["mkt-1"])
        good = pub.feed_path.read_bytes()
        # A crash mid-append: half a record (or zero fill) and no newline.
        pub.feed_path.write_bytes(good + b'{"market_id":"mkt-2","rev\x00\x00')
        with pytest.raises(SettlementParseError, match="not JSON"):
            read_settlement_feed(pub.feed_path)
        assert pub.sync([]).published == 0
        assert pub.feed_path.read_bytes() == good
        assert len(read_settlement_feed(pub.feed_path)) == 1

    def test_transport_failure_is_not_a_settlement_fact(self, tmp_path):
        pub = publisher(tmp_path, FakeApi(gamma_market(), resolution(), fail=True))
        result = pub.sync(["mkt-1"])
        assert result.published == 0 and result.errors
        assert lines(pub) == []

    def test_every_published_line_parses_as_the_ledger_contract(self, tmp_path):
        pub = publisher(tmp_path, FakeApi(gamma_market(), resolution()))
        pub.sync(["mkt-1"])
        (event,) = read_settlement_feed(pub.feed_path)
        assert event.is_payout and event.yes_payout == 1.0


def test_published_record_releases_paper_capital(tmp_path):
    pub = publisher(tmp_path, FakeApi(gamma_market(), resolution()))
    pub.sync(["mkt-1"])
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        ledger = Ledger(store)
        ledger.open_account(initial_capital_usdc=100.0, now=NOW)
        with db.transaction():
            ledger.apply_buy(make_fill(decision_id="b1", fees_usd=0.279), now=NOW)
        assert ledger.balances()["occupied_usdc"] > 0
        with db.transaction():
            (outcome,) = [ledger.apply_settlement(e, now=NOW) for e in read_settlement_feed(pub.feed_path)]
        assert outcome["action"] == "APPLIED"
        balances = ledger.balances()
        assert balances["occupied_usdc"] == pytest.approx(0.0)
        assert balances["realized_pnl_usdc"] == pytest.approx(100.0 - 55.8 - 0.279)
        assert ledger.positions() == []


def test_work_list_comes_from_the_collector_gamma_archive(tmp_path):
    gamma = tmp_path / "raw" / "gamma"
    gamma.mkdir(parents=True)
    event = {
        "id": "e1",
        "endDate": "2026-11-23T12:00:00Z",
        "markets": [
            {"id": "past", "endDate": "2026-11-23T12:00:00Z"},
            {"id": "future", "endDate": "2026-11-30T12:00:00Z"},
        ],
    }
    (gamma / "2026-11-23.jsonl").write_text(
        json.dumps({"source": "gamma", "payload": event}) + "\nnot json\n", encoding="utf-8"
    )
    markets = archived_markets(tmp_path / "raw")
    assert set(markets) == {"past", "future"}
    assert due_markets(markets, NOW) == ["past"]

    api = FakeApi(gamma_market(market_id="past"), resolution())
    pub = publisher(tmp_path, api)
    assert run_once(pub, tmp_path / "raw").published == 1
    assert lines(pub)[0]["market_id"] == "past"


def test_unfinished_markets_are_rechecked_after_leaving_the_archive_window(tmp_path):
    (tmp_path / "raw" / "gamma").mkdir(parents=True)  # the market has aged out
    api = FakeApi(gamma_market(uma="proposed"), resolution(price="69", status="proposed"))
    pub = publisher(tmp_path, api)
    pub.sync(["mkt-1"])
    assert lines(pub)[0]["status"] == "PENDING"
    api.market, api.res = gamma_market(), resolution()
    assert run_once(pub, tmp_path / "raw").published == 1
    assert lines(pub)[-1]["verification_status"] == "VERIFIED"
    # Final markets drop off the work list again.
    api.calls.clear()
    run_once(pub, tmp_path / "raw")
    assert api.calls == []


def test_sidecar_compose_mounts_only_the_feed_writable():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docker" / "compose.settlement-feed.unraid.yaml").read_text()
    assert 'entrypoint: ["weatherquant-settlement-feed"]' in text
    assert "/mnt/user/appdata/weatherquant-next/raw:/collector-raw:ro" in text
    assert "/mnt/user/appdata/weatherquant-paper/settlements:/data/settlements\n" in text
    assert "weatherquant-shadow" not in text
    assert ".duckdb" not in text
    assert "ports:" not in text
    assert "sha-${PAPER_SHA:?" in text

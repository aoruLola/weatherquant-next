"""WQN-45: the paper engine's read-only API.

Under test: GET-only routing with keyset pagination and explicit failure
signals; a surface with no write method anywhere; state the ledger itself
vouches for; and no credential or write capability in any payload.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from paper_fills_support import make_fill
from paper_helpers import export_row
from weatherquant.paper.api import (
    ENDPOINTS,
    fills_payload,
    ledger_payload,
    metrics_payload,
    orders_payload,
    rejections_payload,
)
from weatherquant.paper.events import parse_signal_event
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

NOW = datetime(2026, 10, 26, 12, 5, 0, tzinfo=UTC)


@pytest.fixture()
def populated(tmp_path):
    """An account with two buys (one FILLED), one refusal, and a payout."""
    with PaperDatabase(tmp_path / "paper.duckdb") as db:
        store = PaperStore(db)
        store.ensure_account(initial_capital_usdc=100.0, now=NOW)
        ledger = Ledger(store)
        ledger.open_account(initial_capital_usdc=100.0, now=NOW)
        for i, decision in enumerate(("a" * 32, "b" * 32)):
            event = parse_signal_event(
                export_row(
                    decision_id=decision,
                    market_id=f"mkt-{i}",
                    token_id=f"tok-{i}",
                    proposed_shares=10.0,
                    estimated_fill_shares=10.0,
                )
            )
            store.record_signal(event, tradeable=True, outcome="ACCEPTED", consumed_at_utc=NOW)
        refused = parse_signal_event(export_row(decision_id="c" * 32, token_id="tok-x"))
        store.record_signal(
            refused,
            tradeable=False,
            outcome="REFUSED",
            refusal_reason="NOT_EXECUTABLE",
            refusal_detail="no executable evidence",
            consumed_at_utc=NOW,
        )
        fill = make_fill(decision_id="a" * 32, fees_usd=0.028, shares=10.0, price=0.558)
        fill = make_fill(decision_id="a" * 32, fees_usd=0.028, shares=10.0, price=0.558)
        with db.transaction():
            ledger.apply_buy(fill, now=NOW)
            store.record_fill(fill)
            # A second market's buy, so the ledger pages across a boundary.
            ledger.apply_buy(
                make_fill(decision_id="b" * 32, fees_usd=0.028, shares=10.0, price=0.558, market_id="mkt-9", token_id="tok-9"),
                now=NOW,
            )
        store.set_signal_outcome("a" * 32, outcome="FILLED")
        yield db


class TestBuilders:
    def test_account_positions_metrics_are_ok(self, populated):
        payloads = {
            name: builder(populated, now=NOW)
            for name, builder in (
                ("account", lambda con, now: __import__(
                    "weatherquant.paper.api", fromlist=["account_payload"]
                ).account_payload(con, now=now)),
                ("positions", lambda con, now: __import__(
                    "weatherquant.paper.api", fromlist=["positions_payload"]
                ).positions_payload(con, now=now)),
            )
        }
        assert payloads["account"]["status"] == "ok"
        assert payloads["account"]["data"]["initial_capital_usdc"] == 100.0
        assert payloads["positions"]["status"] == "ok"
        assert len(payloads["positions"]["data"]["positions"]) == 2
        assert payloads["positions"]["data"]["cost_basis_total_usdc"] == pytest.approx(
            payloads["positions"]["data"]["occupied_usdc"]
        )

    def test_ledger_pages_on_seq_with_next_cursor(self, populated):
        first = ledger_payload(populated, now=NOW, query={"limit": 2})
        assert first["status"] == "ok"
        assert len(first["data"]["entries"]) == 2
        assert first["data"]["has_more"] is True  # OPEN + 2 buys = 3 entries
        seqs = [e["seq"] for e in first["data"]["entries"]]
        assert seqs == sorted(seqs)
        second = ledger_payload(populated, now=NOW, query={"limit": 2, "cursor": first["data"]["next_cursor"]})
        assert [e["seq"] for e in second["data"]["entries"]] == [3]
        assert second["data"]["has_more"] is False
        # No row appears on both pages, no row is skipped: the keyset rule.
        assert second["data"]["entries"][0]["seq"] > first["data"]["entries"][-1]["seq"]

    def test_orders_and_rejections_page_on_decision_id(self, populated):
        orders = orders_payload(populated, now=NOW, query={"limit": 2})
        assert orders["data"]["has_more"] is True
        rest = orders_payload(populated, now=NOW, query={"limit": 2, "cursor": orders["data"]["next_cursor"]})
        ids_first = {i["decision_id"] for i in orders["data"]["items"]}
        ids_rest = {i["decision_id"] for i in rest["data"]["items"]}
        assert not ids_first & ids_rest
        rejections = rejections_payload(populated, now=NOW)
        assert [i["decision_id"] for i in rejections["data"]["items"]] == ["c" * 32]
        assert rejections["data"]["items"][0]["refusal_reason"] == "NOT_EXECUTABLE"

    def test_fills_carry_two_sided_evidence(self, populated):
        payload = fills_payload(populated, now=NOW)
        fill = payload["data"]["fills"][0]
        assert fill["book_bids"], "the full bid side is missing"
        assert fill["book_asks"], "the full ask side is missing"
        assert fill["book_source_event_id"]

    def test_metrics_state_the_ledger_vouches_for(self, populated):
        payload = metrics_payload(populated, now=NOW)
        data = payload["data"]
        assert data["ledger_consistent"] is True
        assert data["conservation_ok"] is True
        assert data["signals"] == {"ACCEPTED": 1, "FILLED": 1, "REFUSED": 1}
        assert data["fill_count"] == 1

    def test_failure_is_an_explicit_signal_not_an_exception(self, tmp_path):
        # A connection to a database without the paper tables: every
        # builder reports "unavailable" with a reason, rather than raising
        # or returning a partial payload dressed as ok.
        from weatherquant.storage.db import connect

        con = connect(tmp_path / "empty.duckdb")
        try:
            for builder in (orders_payload, fills_payload, ledger_payload, metrics_payload):
                payload = builder(con, now=NOW)
                assert payload["status"] == "unavailable", payload
                assert payload["reason"]
        finally:
            con.close()

    def test_naive_timestamps_are_refused_not_guessed(self):
        from weatherquant.paper.api import _iso

        with pytest.raises(ValueError, match="UTC only"):
            _iso(datetime(2026, 10, 26, 12, 0, 0))  # no tzinfo


class TestHttpLayer:
    @pytest.fixture()
    def server(self, populated):
        from weatherquant.paper.web import make_server

        srv = make_server(populated, host="127.0.0.1", port=0)
        import threading

        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()
        srv.server_close()

    def _get(self, base: str, path: str):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _post(self, base: str, path: str):
        request = urllib.request.Request(base + path, data=b"{}", method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_every_endpoint_answers(self, server):
        for name in ENDPOINTS:
            status, payload = self._get(server, f"/api/v1/paper/{name}")
            assert status == 200, (name, payload)
            assert payload["kind"] == name
            assert payload["generated_at_utc"].endswith("+00:00")

    def test_unknown_endpoint_is_404_with_the_surface_named(self, server):
        status, payload = self._get(server, "/api/v1/paper/trades")
        assert status == 404
        assert "read-only surface" in payload["reason"]

    def test_post_is_refused_before_any_routing(self, server):
        for path in ("/api/v1/paper/orders", "/api/v1/paper/trades", "/"):
            status, payload = self._post(server, path)
            assert status == 405, (path, status)
            assert "read-only" in payload["reason"]

    def test_paging_parameters_flow_through(self, server):
        status, first = self._get(server, "/api/v1/paper/ledger?limit=1")
        assert status == 200
        cursor = first["data"]["next_cursor"]
        status, second = self._get(server, f"/api/v1/paper/ledger?limit=1&cursor={cursor}")
        assert status == 200
        assert second["data"]["entries"][0]["seq"] > first["data"]["entries"][0]["seq"]

    def test_bad_limit_is_a_400_signal(self, server):
        status, payload = self._get(server, "/api/v1/paper/ledger?limit=abc")
        assert status == 400
        assert "limit" in payload["reason"]

    def test_unavailable_payloads_are_503(self, tmp_path):
        from weatherquant.paper.web import make_server
        from weatherquant.storage.db import connect

        con = connect(tmp_path / "empty.duckdb")
        srv = make_server(con, host="127.0.0.1", port=0)
        import threading

        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            try:
                with urllib.request.urlopen(base + "/api/v1/paper/metrics", timeout=5) as response:
                    status = response.status
                    payload = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, json.loads(exc.read())
            assert status == 503
            assert payload["status"] == "unavailable"
        finally:
            srv.shutdown()
            srv.server_close()
            con.close()

    def test_payloads_carry_no_credentials(self, server):
        """Walk the whole surface and assert no secret-shaped value exists.

        There is nothing secret in this engine to begin with - the assertion
        keeps it that way even if someone later adds a field that does.
        """
        forbidden = ("password", "secret", "token_key", "api_key", "private_key", "wallet")
        for name in ENDPOINTS:
            _status, payload = self._get(server, f"/api/v1/paper/{name}")
            blob = json.dumps(payload).lower()
            for needle in forbidden:
                assert needle not in blob, (name, needle)

import threading
from concurrent.futures import ThreadPoolExecutor

from test_paper_api import NOW
from weatherquant.paper.web import PaperAPIHandler

pytest_plugins = ["test_paper_api"]


def test_parallel_handlers_keep_their_own_query_results(populated):
    barrier = threading.Barrier(2)
    closed = []

    class Cursor:
        def __init__(self):
            self.raw = populated.con.cursor()

        def execute(self, sql, params=None):
            self.raw.execute(sql, params)
            barrier.wait(timeout=5)
            return self.raw

        def close(self):
            self.raw.close()
            closed.append(True)

    class Owner:
        def execute(self, sql, params=None):
            populated.con.execute(sql, params)
            barrier.wait(timeout=5)
            return populated.con

        def cursor(self):
            return Cursor()

    def request(endpoint):
        handler = PaperAPIHandler.__new__(PaperAPIHandler)
        handler.path = "/api/v1/paper/" + endpoint
        handler.con = Owner()
        outputs = []
        handler._send = lambda status, payload: outputs.append((status, payload))
        handler.do_GET()
        return outputs[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders, ledger = list(pool.map(request, ["orders", "ledger"]))
    assert orders[0] == ledger[0] == 200
    assert len(orders[1]["data"]["items"]) == 3
    assert len(ledger[1]["data"]["entries"]) == 3
    assert len(closed) == 2


def test_api_owner_continues_consuming_filling_and_settling_while_serving(tmp_path, monkeypatch):
    import json
    from datetime import timedelta
    from types import SimpleNamespace

    from paper_book_helpers import T_DECISION, TOKEN, two_level_book, write_archive, ws_line
    from paper_helpers import export_row
    from weatherquant.paper import cli
    from weatherquant.paper.api import ledger_payload, orders_payload
    from weatherquant.paper.consumer import SignalConsumer

    monkeypatch.setenv("WQ_PAPER_ENABLED", "true")
    monkeypatch.setenv("WQ_PAPER_DB_PATH", str(tmp_path / "paper.duckdb"))
    monkeypatch.setenv("WQ_PAPER_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("WQ_PAPER_EVENTS_DIR", str(tmp_path / "export"))
    assert cli.main(["init"]) == 0
    write_archive(
        tmp_path / "raw",
        source="ws",
        day="2026-10-26",
        lines=[ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=20))],
    )
    parts = tmp_path / "export/parts"
    parts.mkdir(parents=True)
    row = export_row(decision_id="first", token_id=TOKEN, book_id=None, proposed_shares=10, fee_rate=0)
    (parts / "part-000000001.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    feed = tmp_path / "settlements.jsonl"
    feed.write_text("", encoding="utf-8")

    def consumer(*args, **kwargs):
        return SignalConsumer(*args, **kwargs, clock=lambda: NOW)

    monkeypatch.setattr(cli, "SignalConsumer", consumer)

    def make_server(con, *, on_tick=lambda: None, **kwargs):
        return SimpleNamespace(con=con, tick=on_tick)

    monkeypatch.setattr(cli, "make_server", make_server)

    def serve(server):
        server.tick()
        assert len(orders_payload(server.con, now=NOW)["data"]["items"]) == 1
        assert server.con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 1
        row["decision_id"] = "second"
        (parts / "part-000000002.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        feed.write_text(
            json.dumps({"market_id": "mkt-1", "revision": 1, "status": "RESOLVED", "verification_status": "VERIFIED", "yes_payout": 1}) + "\n",
            encoding="utf-8",
        )
        server.tick()
        assert len(orders_payload(server.con, now=NOW)["data"]["items"]) == 2
        entries = ledger_payload(server.con, now=NOW)["data"]["entries"]
        assert [entry["kind"] for entry in entries] == ["OPEN", "BUY_SETTLE", "BUY_SETTLE", "SETTLE_PAYOUT"]

    monkeypatch.setattr(cli, "serve_forever", serve)
    assert cli.main(["serve", "--settlement-feed", str(feed)]) == 0


def test_service_actions_runs_engine_ticks_on_cadence(monkeypatch):
    from weatherquant.paper import web

    calls = []
    clock = iter([0.0, 1.0, 5.0])
    monkeypatch.setattr(web.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(web.ThreadingHTTPServer, "__init__", lambda self, *args: None)
    server = web.make_server(None, host="127.0.0.1", port=0, on_tick=lambda: calls.append(True), tick_seconds=5)
    server.service_actions()
    server.service_actions()
    server.service_actions()
    assert len(calls) == 2

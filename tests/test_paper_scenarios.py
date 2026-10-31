"""WQN-47: crash recovery, idempotency and conservative fills - as scenarios.

Unit tests prove each mechanism; this file proves the *pipeline*: published
export -> consumer -> risk-sized fills -> ledger -> settlement, under
injected failures, and always against three invariants:

* **Capital conservation** - ``available + occupied == initial + realised``
  after every phase, crashed or not;
* **Ledger agreement** - ``Ledger.verify()`` replays the ledger to exactly
  the stored state, so a restart is a derivation, not a repair;
* **Determinism** - a run that crashed mid-transaction and a run that never
  crashed finish with byte-identical ledgers.

Every input is a file this project wrote, every output a row in this
project's database. No socket is opened anywhere in the paper package; a
structural test at the bottom of this file keeps that true.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from paper_book_helpers import book_payload, ws_line
from paper_helpers import export_row
from weatherquant.data.books import parse_book_payload
from weatherquant.paper.books import ArchiveBookSource
from weatherquant.paper.consumer import SignalConsumer
from weatherquant.paper.fills import FillEngine
from weatherquant.paper.ledger import Ledger
from weatherquant.paper.risk import RiskGate, load_risk_rules
from weatherquant.paper.settlements import read_settlement_feed
from weatherquant.paper.store import PaperStore
from weatherquant.storage.paper import PaperDatabase

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


class Harness:
    """The published channels and a fresh set of engines over one database."""

    def __init__(self, root: Path, *, capital: float = 100.0) -> None:
        self.root = Path(root)
        self.events_dir = self.root / "decisions-export"
        self.raw_dir = self.root / "raw"
        self.feed_path = self.root / "settlements.jsonl"
        self.capital = capital
        self.clock = {"now": T0}
        self.db = PaperDatabase(self.root / "paper.duckdb")
        self.db.open()
        self.store = PaperStore(self.db)
        self.store.ensure_account(initial_capital_usdc=capital, now=self.now())
        self.ledger = Ledger(self.store)
        self.ledger.open_account(initial_capital_usdc=capital, now=self.now())

    # -- time ---------------------------------------------------------------

    def now(self) -> datetime:
        return self.clock["now"]

    def advance(self, seconds: float) -> None:
        self.clock["now"] = self.clock["now"] + timedelta(seconds=seconds)

    # -- engines (rebuilt per phase: a restart is new objects, same disk) ----

    def close(self) -> None:
        self.db.close()

    def restart(self) -> None:
        """Simulate a process death: everything in memory is gone."""
        self.db.close()
        self.db = PaperDatabase(self.root / "paper.duckdb")
        self.db.open()
        self.store = PaperStore(self.db)
        self.ledger = Ledger(self.store)

    def consumer(self) -> SignalConsumer:
        return SignalConsumer(
            self.store,
            events_dir=self.events_dir,
            max_signal_age_seconds=600.0,
            clock=self.now,
        )

    def fill_engine(self) -> FillEngine:
        return FillEngine(
            self.store,
            book_source=ArchiveBookSource(self.raw_dir),
            buffer_fraction=0.005,
            clock=self.now,
            risk=RiskGate(
                self.ledger,
                load_risk_rules("configs/paper-risk.json"),
                initial_capital_usdc=self.capital,
            ),
        )

    # -- publishing -----------------------------------------------------------

    def publish_signal(
        self,
        *,
        market_id: str,
        decision_time: datetime | None = None,
        decision_id: str | None = None,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
        book_lag_seconds: float = 20.0,
        venue_lag_seconds: float = 20.0,
        part: int | None = None,
        duplicate: bool = False,
    ) -> str:
        """One LIVE ACCEPT decision on the export, with its book in the archive."""
        decision_time = decision_time or self.now()
        token = f"tok-{market_id}"
        bids = [(0.52, 500.0)] if bids is None else bids
        asks = [(0.55, 60.0), (0.57, 200.0)] if asks is None else asks
        venue_ts = (decision_time - timedelta(seconds=venue_lag_seconds)).isoformat()
        book = book_payload(bids=bids, asks=asks, token_id=token, timestamp=venue_ts)
        snapshot = parse_book_payload(
            book,
            token_id=token,
            market_id=market_id,
            fetched_at_utc=decision_time,
            run_id="",
            source="ws",
        )
        decision_id = decision_id or hashlib.sha256(market_id.encode()).hexdigest()[:32]
        row = export_row(
            decision_id=decision_id,
            market_id=market_id,
            token_id=token,
            proposed_shares=100.0,
            estimated_fill_shares=100.0,
            book_id=snapshot.content_hash,
            decision_time_utc=decision_time.isoformat(),
        )
        archive = self.raw_dir / "ws" / f"{decision_time.date().isoformat()}.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("a", encoding="utf-8") as handle:
            handle.write(
                ws_line(book, received=decision_time - timedelta(seconds=book_lag_seconds))
                + "\n"
            )
        self._append_export(part, row)
        if duplicate:
            self._append_export(part, row)
        return decision_id

    def _append_export(self, part: int | None, row: dict) -> None:
        if part is None:
            parts = sorted((self.events_dir / "parts").glob("part-*.jsonl")) if (self.events_dir / "parts").exists() else []
            part = (_index_of(parts[-1]) if parts else 0) + 1
        parts_dir = self.events_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        with (parts_dir / f"part-{part:09d}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")

    def publish_settlement_feed(self, rows: list[dict]) -> None:
        self.feed_path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    # -- invariants ------------------------------------------------------------

    def assert_conservation(self) -> None:
        b = self.ledger.balances()
        total = b["available_usdc"] + b["occupied_usdc"]
        expected = self.capital + b["realized_pnl_usdc"]
        assert total == pytest.approx(expected), (
            f"conservation broken: available {b['available_usdc']:.4f} + occupied "
            f"{b['occupied_usdc']:.4f} != {self.capital} + realised {b['realized_pnl_usdc']:.4f}"
        )

    def assert_ledger_agrees(self) -> None:
        report = self.ledger.verify()
        assert report["consistent"] is True, report

    def state(self) -> dict:
        """Everything the final ledger says, for determinism comparisons."""
        b = self.ledger.balances()
        entries = [
            (e["kind"], round(e["amount_usdc"], 6), e["ref_id"])
            for e in self.ledger.ledger_entries(limit=1_000_000)
        ]
        signals = [
            (r[0], r[1], r[2])
            for r in self.db.execute(
                "SELECT decision_id, outcome, COALESCE(refusal_reason, '') FROM paper_signals "
                "ORDER BY decision_id"
            ).fetchall()
        ]
        fills = [
            (r[0], round(r[1], 6), r[2])
            for r in self.db.execute(
                "SELECT decision_id, filled_shares, book_id FROM paper_fills ORDER BY decision_id"
            ).fetchall()
        ]
        return {
            "balances": {k: round(b[k], 6) for k in ("available_usdc", "occupied_usdc", "realized_pnl_usdc")},
            "entries": entries,
            "signals": signals,
            "fills": fills,
        }


def _index_of(path: Path) -> int:
    return int(path.stem.split("-")[1])


@pytest.fixture()
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


class TestCrashRecovery:
    def test_crash_inside_the_consume_transaction_loses_nothing(self, tmp_path):
        """A crash mid-batch commits nothing; a clean retry reaches the same state."""
        clean = Harness(tmp_path / "clean")
        try:
            for market in ("mkt-a", "mkt-b", "mkt-c"):
                clean.publish_signal(market_id=market)
            clean.consumer().consume_once()
            clean.assert_conservation()
            clean.assert_ledger_agrees()

            crashed = Harness(tmp_path / "crashed")
            try:
                for market in ("mkt-a", "mkt-b", "mkt-c"):
                    crashed.publish_signal(market_id=market)
                consumer = crashed.consumer()
                original = crashed.store.record_signal
                calls = {"n": 0}

                def exploding(*args, **kwargs):
                    calls["n"] += 1
                    if calls["n"] == 2:
                        raise RuntimeError("injected: process killed mid-commit")
                    return original(*args, **kwargs)

                crashed.store.record_signal = exploding
                result = consumer.consume_once()
                assert any("consume failed" in note for note in result.notes)
                # Nothing landed: the transaction rolled back whole.
                assert crashed.store.signal_counts() == {}

                # A fresh process replays the batch from the committed cursor.
                crashed.restart()
                crashed.consumer().consume_once()
                assert crashed.state() == clean.state()
            finally:
                crashed.close()
        finally:
            clean.close()

    def test_crash_inside_the_settle_transaction_rolls_back_cash(self, tmp_path):
        """A kill between the debit and the fill row leaves no half trade."""
        clean = Harness(tmp_path / "clean")
        crashed = Harness(tmp_path / "crashed")
        try:
            for harness in (clean, crashed):
                harness.publish_signal(market_id="mkt-a")
                harness.advance(30)
                harness.consumer().consume_once()
            clean.fill_engine().fill_pending()

            engine = crashed.fill_engine()
            original = engine.ledger.apply_buy
            calls = {"n": 0}

            def exploding(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("injected: killed between debit and fill row")
                return original(*args, **kwargs)

            engine.ledger.apply_buy = exploding
            result = engine.fill_pending()
            assert any("fill pass failed" in note for note in result.notes)
            # The signal is still ACCEPTED, the cash is still all there.
            assert crashed.store.signal_counts() == {"ACCEPTED": 1}
            assert crashed.ledger.balances()["available_usdc"] == 100.0

            # The restart prices and settles it exactly once.
            crashed.restart()
            assert crashed.fill_engine().fill_pending().filled == 1
            assert crashed.state()["fills"] == clean.state()["fills"]
            assert crashed.state()["balances"] == clean.state()["balances"]
        finally:
            clean.close()
            crashed.close()

    def test_restarting_between_every_phase_changes_nothing(self, h):
        """A restart is new objects over the same disk - never a new result."""
        h.publish_signal(market_id="mkt-a")
        h.advance(30)
        h.consumer().consume_once()
        h.restart()
        h.consumer().consume_once()  # the second pass must be a no-op
        assert h.store.signal_counts() == {"ACCEPTED": 1}
        h.restart()
        assert h.fill_engine().fill_pending().filled == 1
        h.restart()
        assert h.fill_engine().fill_pending().caught_up is True
        h.assert_conservation()
        h.assert_ledger_agrees()


class TestChannelScenarios:
    def test_missing_part_refuses_then_recovers(self, h):
        h.publish_signal(market_id="mkt-a", part=1)
        h.publish_signal(market_id="mkt-b", part=3)  # part 2 is missing
        h.advance(30)
        result = h.consumer().consume_once()
        assert any("gap" in note.lower() for note in result.notes)
        assert h.store.signal_counts() == {}
        # The gap is filled: the channel completes in order.
        h.publish_signal(market_id="mkt-c", part=2)
        result = h.consumer().consume_once()
        assert result.signals_recorded == 3
        h.assert_ledger_agrees()

    def test_duplicate_live_signal_across_restart_never_trades_twice(self, h):
        # The same decision published twice - and the whole pipeline run
        # twice over it - yields exactly one fill and one debit.
        decision = h.publish_signal(market_id="mkt-a", duplicate=True)
        h.advance(30)
        assert h.consumer().consume_once().signals_recorded == 1
        h.fill_engine().fill_pending()
        h.restart()
        h.consumer().consume_once()
        h.fill_engine().fill_pending()
        fills = h.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0]
        kinds = [e["kind"] for e in h.ledger.ledger_entries()]
        assert fills == 1
        assert kinds.count("BUY_SETTLE") == 1
        assert h.store.signal(decision)["outcome"] == "FILLED"

    def test_stale_book_is_refused_and_cash_untouched(self, h):
        # The book the decision references is an hour old on the venue's own
        # clock - age is judged there, not on our capture time.
        h.publish_signal(market_id="mkt-a", venue_lag_seconds=3600.0)
        h.advance(30)
        h.consumer().consume_once()
        result = h.fill_engine().fill_pending()
        assert result.by_reason == {"BOOK_STALE": 1}
        assert h.ledger.balances()["available_usdc"] == 100.0
        assert h.db.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0


class TestMoneyScenarios:
    def test_parallel_markets_conserve_and_refuse_clearly(self, h):
        """Twelve markets race for 100 USDC at 10 per opportunity."""
        for i in range(12):
            h.publish_signal(market_id=f"mkt-{i:02d}")
        h.advance(30)
        assert h.consumer().consume_once().signals_recorded == 12
        result = h.fill_engine().fill_pending()
        # Ten fills exhaust the cash; the last two refuse by name.
        assert result.filled == 10
        # The remainder cannot fund a venue-legal order (5-share minimum);
        # the refusal names the constraint that actually binds: the cash.
        assert result.by_reason.get("INSUFFICIENT_FUNDS") == 2
        b = h.ledger.balances()
        assert 0.0 <= b["available_usdc"] < 0.05  # sized to the budget, never below zero
        h.assert_conservation()
        h.assert_ledger_agrees()

    def test_same_market_episodes_climb_one_exposure_cap(self, h):
        """Three episodes in one market: 10 + 10 + refused-at-the-cap."""
        for i in range(3):
            h.publish_signal(market_id="mkt-a", decision_id=f"ep{i}")
            h.advance(30)
            h.consumer().consume_once()
            result = h.fill_engine().fill_pending()
            h.advance(30)
        assert result.filled == 0
        # The market's exposure headroom is what binds, and the refusal says so.
        assert result.by_reason == {"RISK_EXPOSURE_EXCEEDED": 1}
        b = h.ledger.balances()
        # Two budget-sized fills occupy just under the 20 USDC market cap.
        assert b["occupied_usdc"] == pytest.approx(19.9956, abs=1e-3)
        h.assert_conservation()

    def test_full_lifecycle_with_settlement_and_correction(self, h):
        """Buy -> settle at 1.0 -> correct to 0.0: history kept, books true."""
        h.publish_signal(market_id="mkt-a")
        h.advance(30)
        h.consumer().consume_once()
        assert h.fill_engine().fill_pending().filled == 1
        h.assert_conservation()

        # The platform resolves YES at 1.0.
        h.publish_settlement_feed(
            [{"market_id": "mkt-a", "revision": 1, "status": "RESOLVED",
              "yes_payout": 1.0, "settled_at_utc": h.now().isoformat(),
              "transaction_hash": "0x1"}]
        )
        with h.db.transaction():
            for event in read_settlement_feed(h.feed_path):
                h.ledger.apply_settlement(event, now=h.now())
        h.advance(60)
        assert h.ledger.balances()["occupied_usdc"] == 0.0
        h.assert_conservation()
        h.assert_ledger_agrees()

        # ... and later corrects the resolution to 0.0.
        h.publish_settlement_feed(
            [{"market_id": "mkt-a", "revision": 2, "status": "RESOLVED",
              "yes_payout": 0.0, "settled_at_utc": h.now().isoformat(),
              "transaction_hash": "0x2"}]
        )
        with h.db.transaction():
            for event in read_settlement_feed(h.feed_path):
                h.ledger.apply_settlement(event, now=h.now())
        b = h.ledger.balances()
        assert b["realized_pnl_usdc"] == pytest.approx(
            -(h.capital - b["available_usdc"])
        )
        h.assert_conservation()
        h.assert_ledger_agrees()
        kinds = [e["kind"] for e in h.ledger.ledger_entries()]
        assert "SETTLE_PAYOUT" in kinds and "CORRECTION" in kinds


class TestExplainability:
    def test_every_decision_is_explainable_from_the_rows(self, h):
        """After a mixed scenario, no row is a dead end."""
        h.publish_signal(market_id="mkt-ok")                       # fills
        h.publish_signal(market_id="mkt-stale", book_lag_seconds=3600.0)  # BOOK_STALE
        h.publish_signal(market_id="mkt-thin", bids=[], asks=[(0.55, 3.0)])  # capacity
        h.publish_signal(market_id="mkt-dup", duplicate=True)      # duplicate
        h.advance(30)
        h.consumer().consume_once()
        h.fill_engine().fill_pending()

        rows = h.db.execute(
            "SELECT outcome, refusal_reason, refusal_detail, decision_id FROM paper_signals"
        ).fetchall()
        assert rows, "the scenario consumed nothing"
        for outcome, reason, detail, decision_id in rows:
            if outcome == "FILLED":
                fill = h.db.execute(
                    "SELECT book_id, consumed_levels_json, levels_consumed, fees_usd, filled_shares "
                    "FROM paper_fills WHERE decision_id = ?",
                    [decision_id],
                ).fetchone()
                assert fill is not None, f"{decision_id} is FILLED with no fill row"
                assert fill[0] and fill[1]  # book identity + consumed levels
                assert fill[2] >= 1 and fill[4] > 0
            else:
                # Every refusal names its reason and explains itself.
                assert reason, f"{decision_id} refused with no reason"
                assert detail, f"{decision_id} refused with no detail"
        # Every ledger movement points at an explainable reference.
        for entry in h.ledger.ledger_entries(limit=1_000_000):
            if entry["ref_type"] == "fill":
                assert h.store.fill_of(entry["ref_id"]) is not None
            elif entry["ref_type"] == "account":
                assert entry["ref_id"] == "paper-main"
        h.assert_conservation()
        h.assert_ledger_agrees()


class TestNoNetworkPath:
    def test_the_paper_package_never_touches_a_socket(self):
        """The hard boundary, asserted structurally.

        No module under ``weatherquant.paper`` may import an HTTP client or
        reach a socket: every input arrives as a file, every output is a row.
        """
        import inspect
        import sys

        import weatherquant.paper as paper_pkg
        from weatherquant.paper import (
            api as paper_api,
        )
        from weatherquant.paper import (
            books as paper_books,
        )
        from weatherquant.paper import (
            cli as paper_cli,
        )
        from weatherquant.paper import (
            config as paper_config,
        )
        from weatherquant.paper import (
            consumer as paper_consumer,
        )
        from weatherquant.paper import (
            events as paper_events,
        )
        from weatherquant.paper import (
            fills as paper_fills,
        )
        from weatherquant.paper import (
            ledger as paper_ledger,
        )
        from weatherquant.paper import (
            risk as paper_risk,
        )
        from weatherquant.paper import (
            settlements as paper_settlements,
        )
        from weatherquant.paper import (
            store as paper_store,
        )
        from weatherquant.paper import (
            web as paper_web,
        )

        # The API module (WQN-45) is a *server* over stdlib http.server; the
        # Only the router's incoming query decoder is allowed from urllib.
        forbidden = ("httpx", "requests", "ResilientHttp", "socket.create_connection", "urllib")
        for module in (
            paper_pkg,
            paper_api,
            paper_books,
            paper_cli,
            paper_config,
            paper_consumer,
            paper_events,
            paper_fills,
            paper_ledger,
            paper_risk,
            paper_settlements,
            paper_store,
            paper_web,
        ):
            source = inspect.getsource(sys.modules[module.__name__])
            if module is paper_web:
                source = source.replace("from urllib.parse import parse_qsl\n", "")
            for needle in forbidden:
                assert needle not in source, (
                    f"{module.__name__} references {needle!r}; the paper engine "
                    "reads published files and writes its own database - it has "
                    "no network path, and none may grow unnoticed"
                )

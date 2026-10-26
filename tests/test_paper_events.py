"""WQN-39: the decision-event contract.

Every test here is a rule the consumer will lean on: what decodes, what is
refused at the boundary, what is a recorded business refusal, and which
events may ever be considered for the account.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from paper_helpers import T0, export_row
from weatherquant.paper.events import (
    DecisionState,
    Direction,
    EventError,
    Mode,
    RefusalReason,
    parse_signal_event,
)


class TestParsing:
    def test_minimal_live_accept_row_decodes(self):
        event = parse_signal_event(export_row())
        assert event.decision_id == "d" * 32
        assert event.direction is Direction.BUY_YES
        assert event.state is DecisionState.ACCEPT
        assert event.mode is Mode.LIVE
        assert event.decision_time_utc == T0
        # Evidence travels with the event.
        assert event.book_id == "b" * 64
        assert event.fee_rate == 0.05
        assert event.thresholds_version == "v2"

    def test_non_object_row_is_malformed(self):
        with pytest.raises(EventError, match="MALFORMED_EVENT"):
            parse_signal_event(["not", "an", "object"])

    @pytest.mark.parametrize(
        "field", ["decision_id", "run_id", "cycle_id", "market_id", "direction", "state", "mode"]
    )
    def test_missing_required_field_names_the_field(self, field):
        row = export_row()
        row[field] = None
        with pytest.raises(EventError, match=field):
            parse_signal_event(row)

    def test_unknown_direction_is_malformed_not_a_garbage_event(self):
        with pytest.raises(EventError, match="direction"):
            parse_signal_event(export_row(direction="buy_nope"))

    def test_unknown_state_is_malformed(self):
        with pytest.raises(EventError, match="state"):
            parse_signal_event(export_row(state="MAYBE"))

    def test_unknown_mode_is_malformed(self):
        with pytest.raises(EventError, match="mode"):
            parse_signal_event(export_row(mode="HISTORICAL"))

    def test_naive_decision_time_is_refused(self):
        # A decision instant without a timezone cannot be compared with book
        # clocks or settlement times; treating it as UTC would be a guess.
        row = export_row(decision_time_utc="2026-10-26T12:00:00")
        with pytest.raises(EventError, match="timezone-naive"):
            parse_signal_event(row)

    def test_unparsable_decision_time_is_malformed(self):
        with pytest.raises(EventError, match="decision_time_utc"):
            parse_signal_event(export_row(decision_time_utc="not-a-time"))

    def test_nonfinite_number_is_malformed(self):
        with pytest.raises(EventError, match="net_edge"):
            parse_signal_event(export_row(net_edge=float("inf")))

    def test_string_number_is_accepted(self):
        # The export is JSON produced with default=str; a caller may hand us
        # strings. Accepting them keeps the reader and the producer honest.
        event = parse_signal_event(export_row(net_edge="0.03"))
        assert event.net_edge == 0.03

    def test_optional_evidence_absent_stays_none(self):
        event = parse_signal_event(export_row(book_id=None, fee_rate=None))
        assert event.book_id is None
        assert event.fee_rate is None
        # A missing fee schedule is evidence the fill layer must refuse on,
        # not a zero. The contract only carries it.
        assert not hasattr(event, "fee_rate_assumed")

    def test_raw_row_is_carried_verbatim(self):
        row = export_row()
        event = parse_signal_event(row)
        assert event.raw == row


class TestTradeability:
    def test_live_accept_is_tradeable(self):
        assert parse_signal_event(export_row()).tradeable is True

    def test_replay_is_never_tradeable_even_when_accept(self):
        # A REPLAY ACCEPT is history re-derived now. Pricing it into a live
        # account is backtesting under a live label.
        event = parse_signal_event(export_row(mode="REPLAY"))
        assert event.tradeable is False

    def test_unknown_mode_is_never_tradeable(self):
        event = parse_signal_event(export_row(mode="UNKNOWN"))
        assert event.tradeable is False

    @pytest.mark.parametrize(
        "state", ["REJECT", "NO_TRADE", "DATA_UNAVAILABLE", "CANDIDATE"]
    )
    def test_non_accept_state_is_never_tradeable(self, state):
        event = parse_signal_event(export_row(state=state))
        assert event.tradeable is False


class TestRefusalVocabulary:
    def test_reasons_are_stable_strings(self):
        # These names land in the database and in the PRs' acceptance
        # criteria; renaming them silently would orphan every recorded row.
        assert RefusalReason.NOT_LIVE.value == "NOT_LIVE"
        assert RefusalReason.DUPLICATE_SIGNAL.value == "DUPLICATE_SIGNAL"
        assert RefusalReason.NO_BOOK_EVIDENCE.value == "NO_BOOK_EVIDENCE"
        assert RefusalReason.NO_INVENTORY.value == "NO_INVENTORY"

    def test_decoded_at_offset_is_preserved(self):
        event = parse_signal_event(export_row())
        assert event.decision_time_utc.utcoffset() is not None
        assert event.decision_time_utc.astimezone(UTC) == T0

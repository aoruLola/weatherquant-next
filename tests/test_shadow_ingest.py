"""Read-only ingest: complete lines only, offsets that survive a restart.

The archive is someone else's file. Every test here is about what the tailer
does when the file is *not* in the state a healthy collector leaves it in -
half-written, shrunk, absent, malformed - because that is exactly where a
reader either reports the problem or quietly invents continuity.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from shadow_helpers import NOW, TARGET_DATE, archive_line, gamma_event, write_lines
from weatherquant.shadow.events import EventKind, classify, market_of_token_table, parse_envelope
from weatherquant.shadow.ingest import ArchiveTailer, Checkpoint


def _tailer(root, *, clock=None) -> ArchiveTailer:
    return ArchiveTailer(
        root,
        station="KLGA",
        clock=clock or (lambda: NOW + timedelta(seconds=5)),
    )


def _reload(tailer: ArchiveTailer) -> ArchiveTailer:
    """Restart: a fresh tailer given exactly what the database would hold."""
    fresh = ArchiveTailer(tailer.raw_dir, station="KLGA", clock=tailer.clock)
    fresh.load_checkpoints(_rows(tailer.checkpoints))
    fresh.set_market_of_token(tailer.market_of_token)
    return fresh


def _rows(checkpoints: dict[str, Checkpoint]) -> list[tuple]:
    return [
        (
            c.file_path,
            c.source,
            c.offset_bytes,
            c.lines_processed,
            c.last_run_id,
            c.last_archive_seq,
            c.last_received_at_utc,
            c.file_identity,
            c.updated_at_utc,
        )
        for c in checkpoints.values()
    ]


def _commit(tailer: ArchiveTailer, result) -> None:
    """Stand in for the runner: commit the batch, then adopt its offsets.

    Not a convenience helper - it is the production sequence, and the first
    version of this file updated only ``checkpoints``. That let every test in
    here pass while the runner adopted nothing at all, which is how the re-read
    defect reached production behind a green suite. Whatever this does, the
    runner must do, and vice versa.
    """
    mapping = tailer.market_of_token
    for batch in result.market_updates:
        mapping.update(market_of_token_table(batch))
    tailer.set_market_of_token(mapping)
    tailer.adopt(result.checkpoints, result.file_dates)


# --- complete lines --------------------------------------------------------


def test_a_half_written_line_is_not_consumed(tmp_path) -> None:
    """A line without its newline must not be decoded, and must not move the offset."""
    write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    path = tmp_path / "gamma" / f"{TARGET_DATE}.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"archive_seq": 2, "source": "gamma"')  # no newline

    tailer = _tailer(tmp_path)
    first = tailer.poll()
    assert first.stats.lines_read == 1
    checkpoint = first.checkpoints[str(path)]
    assert checkpoint.offset_bytes == path.stat().st_size - len(
        '{"archive_seq": 2, "source": "gamma"'
    )

    # Appending the terminator makes exactly one more line available.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(",}\n")
    _commit(tailer, first)
    second = tailer.poll()
    assert second.stats.lines_read == 1
    assert second.stats.lines_malformed == 1  # it claims to be JSON but is not


def test_a_restarted_tailer_does_not_reprocess_lines(tmp_path) -> None:
    """Offsets are the dedup key: the same bytes are never applied twice."""
    write_lines(
        tmp_path,
        "gamma",
        TARGET_DATE,
        [archive_line(i, "gamma", gamma_event()) for i in range(1, 4)],
    )
    tailer = _tailer(tmp_path)
    first = tailer.poll()
    assert first.stats.lines_read == 3
    _commit(tailer, first)

    restarted = _reload(tailer)
    second = restarted.poll()
    assert second.stats.lines_read == 0
    assert second.stats.markets == 0


def test_new_lines_after_a_restart_are_picked_up(tmp_path) -> None:
    write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    tailer = _tailer(tmp_path)
    _commit(tailer, tailer.poll())

    write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(2, "gamma", gamma_event())])
    restarted = _reload(tailer)
    result = restarted.poll()
    assert result.stats.lines_read == 1


# --- damage ----------------------------------------------------------------


def test_truncation_is_recorded_and_the_tail_restarts_from_zero(tmp_path) -> None:
    path = write_lines(
        tmp_path,
        "gamma",
        TARGET_DATE,
        [archive_line(i, "gamma", gamma_event()) for i in range(1, 6)],
    )
    tailer = _tailer(tmp_path)
    _commit(tailer, tailer.poll())

    # The collector (or a rotation) replaced the file wholesale.
    path.write_text(archive_line(1, "gamma", gamma_event()) + "\n", encoding="utf-8")

    restarted = _reload(tailer)
    result = restarted.poll()
    kinds = {q.kind for q in result.quality}
    assert "archive_file_truncated" in kinds
    assert result.stats.lines_read == 1  # re-read from byte 0, not from the old offset

    # The rewound offset is persisted, so the same shrink is not re-reported.
    _commit(restarted, result)
    again = restarted.poll()
    assert "archive_file_truncated" not in {q.kind for q in again.quality}


def test_a_missing_checkpointed_file_is_an_error_not_a_silence(tmp_path) -> None:
    path = write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    tailer = _tailer(tmp_path)
    _commit(tailer, tailer.poll())

    path.unlink()
    restarted = _reload(tailer)
    result = restarted.poll()
    assert [q.kind for q in result.quality] == ["archive_file_missing"]
    assert result.quality[0].severity == "error"


def test_a_malformed_complete_line_is_recorded_and_skipped_forward(tmp_path) -> None:
    """It is skipped *and* reported - never silently, and never repeatedly."""
    good = archive_line(1, "gamma", gamma_event())
    write_lines(tmp_path, "gamma", TARGET_DATE, [good, "this is not json at all"])
    tailer = _tailer(tmp_path)
    result = tailer.poll()

    assert result.stats.lines_read == 2
    assert result.stats.lines_malformed == 1
    assert [q.kind for q in result.quality] == ["archive_line_malformed"]

    _commit(tailer, result)
    assert _tailer_replay_is_empty(tailer)


def _tailer_replay_is_empty(tailer: ArchiveTailer) -> bool:
    return _reload(tailer).poll().stats.lines_read == 0


def test_whole_missing_days_between_files_are_a_recorded_gap(tmp_path) -> None:
    write_lines(tmp_path, "rest", date(2026, 10, 1), [archive_line(1, "rest", {"a": 1})])
    tailer = _tailer(tmp_path)
    _commit(tailer, tailer.poll())

    write_lines(tmp_path, "rest", date(2026, 10, 5), [archive_line(2, "rest", {"a": 2})])
    result = tailer.poll()
    gaps = [q for q in result.quality if q.kind == "archive_gap"]
    assert len(gaps) == 1
    assert "3 day(s)" in gaps[0].detail


def test_a_line_larger_than_the_cap_parks_the_tail_instead_of_growing(tmp_path) -> None:
    path = write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", gamma_event())])
    tailer = ArchiveTailer(
        tmp_path, station="KLGA", clock=lambda: NOW, max_bytes_per_poll=64
    )
    # Replace with a single unterminated run longer than the cap.
    path.write_bytes(b'{"x": "' + b"a" * 500)
    result = tailer.poll()
    assert [q.kind for q in result.quality] == ["archive_line_oversized"]
    assert result.checkpoints[str(path)].offset_bytes == 0


# --- classification --------------------------------------------------------


def test_price_change_deltas_are_classified_and_never_become_books(tmp_path) -> None:
    delta = {
        "event_type": "price_change",
        "timestamp": "1791605394391",
        "price_changes": [
            {"asset_id": "tok-0-yes", "price": "0.46", "size": "10", "side": "BUY"}
        ],
    }
    assert classify("ws", delta) is EventKind.BOOK_DELTA

    write_lines(tmp_path, "ws", TARGET_DATE, [archive_line(1, "ws", delta)])
    tailer = _tailer(tmp_path)
    result = tailer.poll()

    assert result.stats.books == 0
    assert result.stats.deltas_ignored == 1
    assert result.book_updates == []
    assert any("one level only" in note for _kind, note in result.notes)


def test_a_ws_book_frame_is_a_full_snapshot(tmp_path) -> None:
    write_lines(
        tmp_path,
        "gamma",
        TARGET_DATE,
        [archive_line(1, "gamma", gamma_event())],
    )
    book_frame = [
        {
            "event_type": "book",
            "market": "0xcond0",
            "asset_id": "tok-0-yes",
            "timestamp": "1791605394391",
            "bids": [{"price": "0.45", "size": "400"}],
            "asks": [{"price": "0.47", "size": "400"}],
            "tick_size": "0.01",
        }
    ]
    write_lines(tmp_path, "ws", TARGET_DATE, [archive_line(2, "ws", book_frame)])

    tailer = _tailer(tmp_path)
    result = tailer.poll()
    assert len(result.book_updates) == 1
    assert result.book_updates[0].book_source == "ws"
    assert result.book_updates[0].complete


def test_trade_prints_and_control_frames_carry_no_depth(tmp_path) -> None:
    for payload, expected in (
        ({"event_type": "last_trade_price", "asset_id": "x", "price": "0.5"}, EventKind.TRADE_PRINT),
        ({"event_type": "tick_size_change", "new_tick_size": "0.01"}, EventKind.CONTROL),
    ):
        assert classify("ws", payload) is expected
        write_lines(tmp_path, "ws", TARGET_DATE, [archive_line(1, "ws", payload)])
        tailer = _tailer(tmp_path)
        result = tailer.poll()
        assert result.stats.books == 0
        assert result.book_updates == []


def test_a_rest_book_before_its_market_metadata_is_reported_not_dropped(tmp_path) -> None:
    write_lines(
        tmp_path,
        "rest",
        TARGET_DATE,
        [archive_line(1, "rest", {"asset_id": "unknown-token", "bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]})],
    )
    tailer = _tailer(tmp_path)
    result = tailer.poll()
    assert result.stats.books == 0
    assert result.stats.unattached_books == 1
    assert any("before any market metadata" in note for _kind, note in result.notes)


def test_gamma_metadata_reaches_the_catalog_and_fails_soft_on_a_bad_bracket(tmp_path) -> None:
    event = gamma_event(labels=["somewhere around 60", "34-35°F"])
    write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", event)])
    tailer = _tailer(tmp_path)
    result = tailer.poll()

    assert len(result.market_updates) == 1
    markets = result.market_updates[0]
    assert len(markets) == 2
    assert markets[0].bracket is None  # refused, not guessed
    assert markets[1].bracket is not None
    assert any(q.kind == "gamma_parse_problem" for q in result.quality)


def test_non_weather_events_are_ignored_without_a_noise_entry(tmp_path) -> None:
    unrelated = {"id": "x", "title": "Will it rain on Mars?", "markets": []}
    write_lines(tmp_path, "gamma", TARGET_DATE, [archive_line(1, "gamma", unrelated)])
    tailer = _tailer(tmp_path)
    result = tailer.poll()
    assert result.market_updates == []
    assert result.quality == []


def test_envelope_rejects_a_naive_or_incomplete_line() -> None:
    line = json.dumps(
        {
            "archive_seq": 1,
            "source": "rest",
            "received_at_utc": "2026-10-11T12:00:00",  # no timezone
            "payload": {},
        }
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_envelope(line)

    with pytest.raises(ValueError, match="missing field"):
        parse_envelope('{"source": "rest"}')

    with pytest.raises(ValueError, match="not JSON"):
        parse_envelope("nope")


def test_lag_is_measured_against_the_collector_clock_not_ours(tmp_path) -> None:
    received = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
    write_lines(
        tmp_path,
        "rest",
        TARGET_DATE,
        [archive_line(1, "rest", {"asset_id": "t"}, received=received)],
    )
    tailer = ArchiveTailer(tmp_path, station="KLGA", clock=lambda: received + timedelta(seconds=7))
    result = tailer.poll()
    assert result.stats.shadow_lag_ms_max == pytest.approx(7000.0, rel=1e-6)
    assert result.stats.collector_received_max == received

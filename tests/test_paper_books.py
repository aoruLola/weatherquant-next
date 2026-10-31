"""WQN-41: resolving point-in-time book evidence from the raw archive.

The rules under test: never future data, newest frame at or before the
decision wins, identity is the collector's own content hash, and the scan
is bounded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from paper_book_helpers import T_DECISION, TOKEN, book_payload, two_level_book, write_archive, ws_line
from weatherquant.paper.books import ArchiveBookSource


def _source(tmp_path, **kwargs) -> ArchiveBookSource:
    return ArchiveBookSource(tmp_path, **kwargs)


class TestResolution:
    def test_newest_frame_at_or_before_decision_wins(self, tmp_path):
        earlier = two_level_book()
        later = book_payload(
            bids=[(0.52, 500.0)],
            asks=[(0.56, 100.0)],
            timestamp="2026-10-26T11:59:55Z",
        )
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[
                ws_line(earlier, received=T_DECISION - timedelta(seconds=30), seq=1),
                ws_line(later, received=T_DECISION - timedelta(seconds=5), seq=2),
            ],
        )
        evidence = _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book.asks[0].price_usd_per_share == 0.56
        assert evidence.book_source == "ws"
        assert evidence.collector_received_at_utc == T_DECISION - timedelta(seconds=5)

    def test_future_frames_are_excluded(self, tmp_path):
        # The only frame for the token arrived after the decision: it was
        # not knowable then, so the honest answer is "no evidence".
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(two_level_book(), received=T_DECISION + timedelta(seconds=1))],
        )
        assert _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION) is None

    def test_frames_for_other_tokens_are_ignored(self, tmp_path):
        other = book_payload(bids=[(0.10, 5.0)], asks=[(0.90, 5.0)], token_id="other-token")
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(other, received=T_DECISION - timedelta(seconds=10))],
        )
        assert _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION) is None

    def test_rest_frames_resolve_too(self, tmp_path):
        write_archive(
            tmp_path,
            source="rest",
            day="2026-10-26",
            lines=[
                ws_line(
                    book_payload(
                        bids=[(0.52, 500.0)],
                        asks=[(0.55, 100.0)],
                        timestamp="2026-10-26T11:59:00Z",
                    ),
                    received=T_DECISION - timedelta(seconds=15),
                )
            ],
        )
        evidence = _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None
        assert evidence.book_source == "rest"

    def test_decision_just_after_midnight_reads_the_previous_day(self, tmp_path):
        decision = datetime(2026, 10, 27, 0, 0, 30, tzinfo=UTC)
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(two_level_book(), received=decision - timedelta(seconds=10))],
        )
        evidence = _source(tmp_path).resolve(token_id=TOKEN, decision_time=decision)
        assert evidence is not None

    def test_book_id_is_the_collector_content_hash(self, tmp_path):
        # Same levels parsed through the collector's own parser must yield
        # the same content hash the shadow records - this is what the fill's
        # identity check relies on.
        from weatherquant.data.books import parse_book_payload

        payload = two_level_book()
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[ws_line(payload, received=T_DECISION - timedelta(seconds=5))],
        )
        evidence = _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        reference = parse_book_payload(
            payload,
            token_id=TOKEN,
            market_id="",
            fetched_at_utc=T_DECISION,
            run_id="",
            source="ws",
        )
        assert evidence is not None
        assert evidence.book_id == reference.content_hash


class TestBoundedScan:
    def test_frame_outside_the_tail_window_is_not_found(self, tmp_path):
        # 1 KB of padding pushes the real frame beyond a 200-byte window:
        # the resolver must answer "not found", never read the whole file.
        padding = ws_line(
            book_payload(bids=[(0.5, 1.0)], asks=[(0.5, 1.0)], token_id="pad"),
            received=T_DECISION - timedelta(seconds=60),
        )
        real = ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=5))
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[padding, real])
        source = _source(tmp_path, tail_bytes=200)
        assert source.resolve(token_id=TOKEN, decision_time=T_DECISION) is None
        # And with a window that reaches the frame, it is found.
        source = _source(tmp_path, tail_bytes=64 * 1024)
        assert source.resolve(token_id=TOKEN, decision_time=T_DECISION) is not None

    def test_lookback_window_excludes_old_frames(self, tmp_path):
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[
                ws_line(two_level_book(), received=T_DECISION - timedelta(hours=2))
            ],
        )
        source = _source(tmp_path, lookback_seconds=3600.0)
        assert source.resolve(token_id=TOKEN, decision_time=T_DECISION) is None

    def test_partial_line_at_window_edge_is_skipped_whole(self, tmp_path):
        # The window opens mid-line: the resolver discards the partial line
        # and starts at the next line boundary rather than parsing half a
        # frame.
        padding = "{\"archive_seq\": 1, \"source\": \"ws\", \"payload\": {\"note\": \"filler\"}}" + " " * 120
        real = ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=5))
        write_archive(tmp_path, source="ws", day="2026-10-26", lines=[padding, real])
        real_bytes = len((real + "\n").encode())
        # The window opens 30 bytes before the padding line ends, i.e.
        # mid-line - the partial line must be discarded whole.
        evidence = _source(tmp_path, tail_bytes=real_bytes + 30).resolve(
            token_id=TOKEN, decision_time=T_DECISION
        )
        assert evidence is not None

    def test_malformed_lines_do_not_break_the_scan(self, tmp_path):
        write_archive(
            tmp_path,
            source="ws",
            day="2026-10-26",
            lines=[
                "{not json",
                json.dumps({"archive_seq": 2, "source": "ws"}),
                ws_line(two_level_book(), received=T_DECISION - timedelta(seconds=5)),
            ],
        )
        evidence = _source(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION)
        assert evidence is not None



def test_archive_requires_a_complete_frame_and_an_explicit_token(tmp_path):
    payload = two_level_book()
    payload.pop("asset_id")
    line = ws_line(payload, received=T_DECISION)
    path = write_archive(tmp_path, source="ws", day="2026-10-26", lines=[line])
    target = ws_line(two_level_book(), received=T_DECISION).encode()
    with path.open("ab") as handle:
        handle.write(target)
    source = ArchiveBookSource(tmp_path, tail_bytes=len(target) + 1)
    assert source.resolve(token_id=TOKEN, decision_time=T_DECISION) is None
    assert ArchiveBookSource(tmp_path).resolve(token_id=TOKEN, decision_time=T_DECISION) is None
    with path.open("ab") as handle:
        handle.write(b"\n")
    assert source.resolve(token_id=TOKEN, decision_time=T_DECISION).token_id == TOKEN

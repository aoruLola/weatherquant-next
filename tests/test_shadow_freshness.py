"""Order-book freshness analysis: the statistics behind the 90 s gate.

The real archive happened to contain no collector outage, so the
``archive_silent`` branch would ship untested if this file did not exist.
These tests feed the classifier the two shapes it has to tell apart: a hole
where *everything* stopped, and a hole where one token did.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shadow_helpers import TARGET_DATE, archive_line, book_payload, write_lines

# `scripts/` is deliberately not a package - the repo ships it as loose tools.
# Loading by path is how tests/test_workflows.py reads the healthcheck, for the
# same reason.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "shadow_freshness.py"
_spec = importlib.util.spec_from_file_location("wq_shadow_freshness", _SCRIPT)
assert _spec and _spec.loader
freshness = importlib.util.module_from_spec(_spec)
# Registered before exec: `@dataclass` looks the module up in sys.modules while
# building annotations, and an unregistered module reads back as None.
sys.modules[_spec.name] = freshness
_spec.loader.exec_module(freshness)

ARCHIVE_SILENT_SECONDS = freshness.ARCHIVE_SILENT_SECONDS
Observation = freshness.Observation
Stats = freshness.Stats
archive_is_silent_between = freshness.archive_is_silent_between
silence_threshold = freshness.silence_threshold
bucket_for = freshness.bucket_for
build = freshness.build
render = freshness.render

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _obs(token: str, *, at: datetime, depth: float = 500.0) -> Observation:
    return Observation(
        token_id=token, venue_ts=at, received_at=at + timedelta(seconds=1), depth_usd=depth
    )


# --- percentile maths ------------------------------------------------------


def test_percentiles_interpolate_and_never_exceed_the_sample() -> None:
    stats = Stats()
    for value in (10.0, 20.0, 30.0, 40.0, 100.0):
        stats.add(value)
    pct = stats.percentiles()
    assert pct["p50"] == 30.0
    assert pct["max"] == 100.0
    assert pct["p90"] >= pct["p50"]
    assert pct["p99"] <= pct["max"]
    assert pct["n"] == 5


def test_an_empty_sample_reports_none_rather_than_zero() -> None:
    """Zero would read as 'perfectly fresh'; there is no data and it must say so."""
    assert Stats().percentiles()["p50"] is None


def test_a_single_sample_is_its_own_everything() -> None:
    stats = Stats()
    stats.add(42.0)
    pct = stats.percentiles()
    assert pct["p50"] == pct["p99"] == pct["max"] == 42.0


# --- liquidity buckets -----------------------------------------------------


def test_liquidity_buckets_are_contiguous_and_closed_at_the_top() -> None:
    assert bucket_for(0.0) == "<$100"
    assert bucket_for(99.99) == "<$100"
    assert bucket_for(100.0) == "$100-1k"
    assert bucket_for(9_999.0) == "$1k-10k"
    assert bucket_for(10_000.0) == ">$10k"
    assert bucket_for(1e12) == ">$10k"  # no bucket may be unreachable


# --- the classifier --------------------------------------------------------


def test_a_hole_in_the_whole_archive_is_attributed_to_the_collector() -> None:
    """Nothing written anywhere between the two observations: an outage.

    The cadence floor cannot save this one - the archive produced no evidence
    of life at all inside a gap far longer than the floor.
    """
    long = float(ARCHIVE_SILENT_SECONDS) + 60
    timeline = [NOW, NOW + timedelta(seconds=long)]
    threshold = silence_threshold(timeline)
    assert archive_is_silent_between(timeline, NOW, NOW + timedelta(seconds=long), threshold)


def test_a_single_token_lagging_while_the_archive_flows_is_token_stale() -> None:
    """The archive keeps producing lines, so the feed is alive; this token is not.

    This is the decisive test for the classifier: one interior line is enough
    to rule out an outage, and no amount of gap length changes that.
    """
    start, end = NOW, NOW + timedelta(seconds=200)
    timeline = [start + timedelta(seconds=i * 10) for i in range(21)]
    threshold = silence_threshold(timeline)
    assert not archive_is_silent_between(timeline, start, end, threshold)


def test_a_short_whole_archive_hole_is_buffering_not_an_outage() -> None:
    """The archive flushes every 5 s / 200 records, so a few seconds is normal."""
    timeline = [NOW, NOW + timedelta(seconds=10)]
    threshold = silence_threshold(timeline)
    assert threshold >= 10.0
    assert not archive_is_silent_between(timeline, NOW, NOW + timedelta(seconds=10), threshold)


def test_an_empty_window_is_an_outage_only_if_the_gap_is_long_enough() -> None:
    threshold = float(ARCHIVE_SILENT_SECONDS)
    assert not archive_is_silent_between([], NOW, NOW + timedelta(seconds=threshold - 1), threshold)
    assert archive_is_silent_between([], NOW, NOW + timedelta(seconds=threshold + 1), threshold)


def test_a_line_exactly_on_the_endpoint_is_not_evidence_of_activity_within() -> None:
    """The endpoints *define* the gap; only interior lines show life inside it."""
    threshold = float(ARCHIVE_SILENT_SECONDS)
    timeline = [NOW, NOW + timedelta(seconds=50)]
    assert archive_is_silent_between(timeline, NOW, NOW + timedelta(seconds=50), threshold)


def test_the_silence_threshold_scales_with_the_archives_own_cadence() -> None:
    """A slow archive must not be diagnosed as dead for being slow.

    The first draft used a fixed 30 s, which called a 30 s-cadence archive
    permanently silent - every gap in it measured exactly 30 s.
    """
    # Enough intervals for the estimate to be meaningful (>= min_intervals).
    slow = [NOW + timedelta(seconds=30 * i) for i in range(12)]
    assert silence_threshold(slow) >= 150.0  # 5 x the 30 s cadence

    fast = [NOW + timedelta(milliseconds=400 * i) for i in range(50)]
    assert silence_threshold(fast) >= float(ARCHIVE_SILENT_SECONDS)  # floor still applies

    # Too little to estimate from: the floor stands rather than a guess.
    assert silence_threshold([NOW, NOW + timedelta(seconds=900)]) == float(
        ARCHIVE_SILENT_SECONDS
    )


def test_a_long_interval_cannot_inflate_its_own_threshold() -> None:
    """The stalls being looked for must not widen the definition of a stall.

    One 600 s hole among otherwise-tight intervals must leave the threshold
    where the tight intervals put it, or the hole would exempt itself.
    """
    timeline = [NOW + timedelta(seconds=i) for i in range(20)]  # 1 s cadence
    timeline.append(timeline[-1] + timedelta(seconds=600))
    assert silence_threshold(timeline) < 600.0

    assert archive_is_silent_between(
        timeline, timeline[-2], timeline[-1], silence_threshold(timeline)
    )


# --- end to end -----------------------------------------------------------


def _archive(root, token_gaps: dict[str, list[float]], start: datetime = NOW) -> None:
    """Write one `rest` line per token per gap step, with venue timestamps."""
    for token, gaps in token_gaps.items():
        clock = start
        write_lines(
            root,
            "rest",
            TARGET_DATE,
            [
                archive_line(
                    1,
                    "rest",
                    book_payload(token_id=token, venue_ts_ms=int(clock.timestamp() * 1000)),
                    received=clock,
                )
            ],
        )
        for gap in gaps:
            clock = clock + timedelta(seconds=gap)
            write_lines(
                root,
                "rest",
                TARGET_DATE,
                [
                    archive_line(
                        2,
                        "rest",
                        book_payload(token_id=token, venue_ts_ms=int(clock.timestamp() * 1000)),
                        received=clock,
                    )
                ],
            )


def test_build_separates_a_flowing_archive_from_a_stalled_token(tmp_path) -> None:
    _archive(
        tmp_path,
        {
            # A token polled normally, then left behind while the feed continues.
            # Steady for the whole capture, so the archive is provably alive
            # while `slow` sits in its 400 s hole.
            "steady": [30.0] * 18,
            "slow": [30.0, 400.0, 30.0],
        },
    )
    report = build(tmp_path, gate_ms=90_000.0)

    # The threshold rises above the fixed floor (two streams interleaving put
    # the archive's own cadence near 15 s), yet stays far below the 400 s hole
    # that triggered this classification - the stall cannot widen the
    # definition of "stalled".
    threshold = report["archive_silence_threshold_seconds"]
    assert 30.0 < threshold < 400.0, threshold
    assert report["tokens"] == 2
    assert report["refresh_gap_seconds"]["max"] >= 400.0
    classes = report["classification"]
    assert classes["normal_poll"] >= 3
    assert classes["token_stale"] >= 1  # the 400 s hole, with the archive flowing
    assert classes["archive_silent"] == 0  # nothing global happened here
    assert report["gaps_over_gate_by_class"]["token_stale"] >= 1


def test_report_renders_every_section_the_audit_asks_for(tmp_path) -> None:
    _archive(tmp_path, {"fast": [30.0, 30.0]})
    text = render(build(tmp_path, gate_ms=90_000.0))
    for section in (
        "[refresh gap, seconds - all tokens]",
        "[by market liquidity",
        "[gap classification]",
        "[stalled tokens",
        "[slowest tokens by median gap]",
    ):
        assert section in text, f"missing section: {section}"


def test_a_missing_archive_is_reported_as_an_error_not_an_empty_success(tmp_path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    report = build(empty, gate_ms=90_000.0)
    assert "error" in report


def test_percentiles_grow_monotonically() -> None:
    """p50 <= p90 <= p95 <= p99 <= max, on a sample wide enough to separate them."""
    stats = Stats()
    for i in range(1, 101):
        stats.add(float(i))
    pct = stats.percentiles()
    ordered = [pct["p50"], pct["p90"], pct["p95"], pct["p99"], pct["max"]]
    assert ordered == sorted(ordered)

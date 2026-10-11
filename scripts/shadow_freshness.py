#!/usr/bin/env python3
"""Order-book freshness: where the 90 s gate comes from, and what sits behind it.

The threshold itself is not re-derived here - it lives in
``configs/shadow-thresholds.json`` with its version and its ``corrected_from``
record, and this script exists to be run *against* it rather than to move it.

What this answers
-----------------
* How old is the newest book, per token, in percentiles?
* Is a long gap the collector stopping, one token going stale, or ordinary
  polling? Those three are indistinguishable from a single number and very
  different in meaning:

    ``archive_silent``   the whole archive stopped - the collector or the
                         network is down. Every token is affected at once.
    ``token_stale``      the archive kept flowing but *this* token did not -
                         it fell out of the sweep, or a 404 parked it.
    ``normal_poll``      inside the expected cadence.

* Does freshness track liquidity? A thin book nobody quotes and a thick one
  are refreshed by the same sweep, but they fail the gate for different
  reasons and the fix for each is different.

Usage::

    python scripts/shadow_freshness.py --raw data/raw
    python scripts/shadow_freshness.py --raw data/raw --gate-ms 90000 --json

Nothing here writes to the archive or the shadow database.
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# A single gap is attributed to the collector only when the archive itself went
# quiet for at least this long. Shorter holes are normal buffering (the archive
# flushes every 5 s or 200 records), not an outage.
ARCHIVE_SILENT_SECONDS = 30.0

LIQUIDITY_BUCKETS = (
    # (upper bound in USD notional, label)
    (100.0, "<$100"),
    (1_000.0, "$100-1k"),
    (10_000.0, "$1k-10k"),
    (float("inf"), ">$10k"),
)


@dataclass
class Observation:
    token_id: str
    venue_ts: datetime
    received_at: datetime
    depth_usd: float


@dataclass
class Stats:
    values: list[float] = field(default_factory=list)

    def add(self, seconds: float) -> None:
        self.values.append(seconds)

    def percentiles(self) -> dict[str, float | None]:
        if not self.values:
            return {k: None for k in ("p50", "p90", "p95", "p99", "max")}
        ordered = sorted(self.values)
        n = len(ordered)

        def pct(p: float) -> float:
            if n == 1:
                return ordered[0]
            rank = (p / 100.0) * (n - 1)
            low = int(rank)
            high = min(low + 1, n - 1)
            weight = rank - low
            return ordered[low] * (1 - weight) + ordered[high] * weight

        return {
            "p50": round(pct(50), 2),
            "p90": round(pct(90), 2),
            "p95": round(pct(95), 2),
            "p99": round(pct(99), 2),
            "max": round(ordered[-1], 2),
            "n": n,
        }


def read_observations(raw_dir: Path) -> list[Observation]:
    out: list[Observation] = []
    for path in sorted(raw_dir.glob("rest/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = envelope.get("payload")
            if not isinstance(payload, dict):
                continue
            token = payload.get("asset_id")
            ts = payload.get("timestamp")
            if not token or not ts:
                continue
            try:
                venue = datetime.fromtimestamp(int(ts) / 1000.0, tz=UTC)
            except (OverflowError, OSError, ValueError):
                continue
            depth = 0.0
            for side in ("bids", "asks"):
                for level in payload.get(side) or []:
                    if not isinstance(level, dict):
                        continue
                    try:
                        depth += float(level["price"]) * float(level["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
            received = envelope.get("received_at_utc")
            received_dt = None
            if received:
                try:
                    received_dt = datetime.fromisoformat(received)
                except ValueError:
                    received_dt = None
            if received_dt is None:
                received_dt = venue
            out.append(
                Observation(
                    token_id=str(token), venue_ts=venue, received_at=received_dt, depth_usd=depth
                )
            )
    out.sort(key=lambda o: o.venue_ts)
    return out


def archive_timeline(observations: list[Observation]) -> list[datetime]:
    return sorted(o.received_at for o in observations)


def silence_threshold(timeline: list[datetime], *, min_intervals: int = 10) -> float:
    """How long a hole must be before this archive counts as stopped.

    Cadence is estimated from the *short* intervals - those within twice the
    smallest observed one - so that the stalls being looked for cannot inflate
    the estimate of how fast the archive normally writes. A fixed 30 s floor
    always applies underneath it.

    Below ``min_intervals`` there is nothing to estimate a rate from, and the
    floor stands alone: an archive of three lines cannot tell you its cadence,
    and pretending otherwise would make the answer depend on the very gap in
    question.
    """
    if len(timeline) < min_intervals + 1:
        return float(ARCHIVE_SILENT_SECONDS)
    intervals = [
        (b - a).total_seconds() for a, b in itertools.pairwise(timeline) if b > a
    ]
    if not intervals:
        return float(ARCHIVE_SILENT_SECONDS)
    fastest = min(intervals)
    near = [i for i in intervals if i <= 2.0 * fastest] or intervals
    return max(float(ARCHIVE_SILENT_SECONDS), statistics.median(near) * 5.0)


def archive_is_silent_between(
    timeline: list[datetime], start: datetime, end: datetime, threshold: float
) -> bool:
    """True when the archive wrote nothing at all inside ``(start, end)``.

    Two conditions, both required:

    * **no line strictly inside the gap** - the decisive one. Another token
      being written while this one stalls is what separates "this token fell
      out of the sweep" from "the collector stopped". It needs no cadence
      estimate at all, which is why it carries the argument.
    * **the gap is at least ``threshold`` long** - so an archive that simply
      writes slowly is not called dead for being slow. ``threshold`` comes
      from :func:`silence_threshold`.

    A line exactly at either endpoint does not count: those are the
    observations that define the gap, not evidence of activity within it.
    """
    lo = bisect.bisect_left(timeline, start)
    hi = bisect.bisect_right(timeline, end)
    if any(start < t < end for t in timeline[lo:hi]):
        return False
    return (end - start).total_seconds() >= threshold


def bucket_for(depth: float) -> str:
    for bound, label in LIQUIDITY_BUCKETS:
        if depth < bound:
            return label
    return LIQUIDITY_BUCKETS[-1][1]


def build(raw_dir: Path, gate_ms: float) -> dict:
    observations = read_observations(raw_dir)
    if not observations:
        return {"error": f"no rest observations under {raw_dir}"}

    gate_s = gate_ms / 1000.0
    timeline = archive_timeline(observations)
    silent_s = silence_threshold(timeline)

    per_token: dict[str, list[Observation]] = defaultdict(list)
    for obs in observations:
        per_token[obs.token_id].append(obs)

    overall = Stats()
    by_bucket: dict[str, Stats] = defaultdict(Stats)
    per_token_gaps: dict[str, Stats] = defaultdict(Stats)
    classification: dict[str, int] = {
        "normal_poll": 0,
        "archive_silent": 0,
        "token_stale": 0,
    }
    over_gate_by_class: dict[str, int] = defaultdict(int)
    bucket_gate_breaches: dict[str, int] = defaultdict(int)

    for token, obs_list in per_token.items():
        obs_list.sort(key=lambda o: o.venue_ts)
        mean_depth = statistics.fmean(o.depth_usd for o in obs_list)
        bucket = bucket_for(mean_depth)
        for prev, cur in itertools.pairwise(obs_list):
            gap = (cur.venue_ts - prev.venue_ts).total_seconds()
            if gap <= 0 or gap > 86_400:
                continue  # clock step or a day boundary, not a refresh interval
            overall.add(gap)
            by_bucket[bucket].add(gap)
            per_token_gaps[token].add(gap)

            if archive_is_silent_between(
                timeline, prev.received_at, cur.received_at, silent_s
            ):
                kind = "archive_silent"
            elif gap > gate_s:
                kind = "token_stale"
            else:
                kind = "normal_poll"
            classification[kind] += 1
            if gap > gate_s:
                over_gate_by_class[kind] += 1
                bucket_gate_breaches[bucket] += 1

    # A token that simply stops never produces a final gap, so count the ones
    # whose last observation is far behind the archive head: that is the shape
    # a stalled token has.
    head = timeline[-1]
    stalled = []
    for token, obs_list in per_token.items():
        tail_gap = (head - max(o.received_at for o in obs_list)).total_seconds()
        if tail_gap > gate_s:
            mean_depth = statistics.fmean(o.depth_usd for o in obs_list)
            stalled.append(
                {
                    "token_id": token,
                    "seconds_behind_archive_head": round(tail_gap, 1),
                    "liquidity_bucket": bucket_for(mean_depth),
                    "observations": len(obs_list),
                }
            )
    stalled.sort(key=lambda x: -x["seconds_behind_archive_head"])

    return {
        "archive": str(raw_dir),
        "gate_ms": gate_ms,
        "observations": len(observations),
        "tokens": len(per_token),
        "archive_head_utc": head.isoformat(),
        "archive_silence_threshold_seconds": round(silent_s, 2),
        "refresh_gap_seconds": overall.percentiles(),
        "by_liquidity_bucket": {
            label: stats.percentiles() for label, stats in sorted(by_bucket.items())
        },
        "gaps_over_gate_by_bucket": dict(sorted(bucket_gate_breaches.items())),
        "classification": {
            **classification,
            "note": (
                f"archive_silent = the whole archive stopped for >= the silence threshold "
                f"({ARCHIVE_SILENT_SECONDS:.0f}s floor, adapted to its own cadence) inside "
                "the gap (collector or network); token_stale = the archive kept flowing "
                "but this token did not; normal_poll = inside the freshness gate"
            ),
        },
        "gaps_over_gate_by_class": dict(sorted(over_gate_by_class.items())),
        "stalled_tokens": stalled[:20],
        "stalled_token_count": len(stalled),
        "slowest_tokens": [
            {
                "token_id": token,
                **stats.percentiles(),
            }
            for token, stats in sorted(
                per_token_gaps.items(), key=lambda kv: -(kv[1].percentiles()["p50"] or 0)
            )[:10]
        ],
    }


def render(report: dict) -> str:
    if "error" in report:
        return report["error"]
    lines: list[str] = []
    lines.append(f"order-book freshness  archive={report['archive']}")
    lines.append(
        f"  observations={report['observations']}  tokens={report['tokens']}  "
        f"archive_head={report['archive_head_utc']}  gate={report['gate_ms']:.0f}ms  "
        f"silence_threshold={report.get('archive_silence_threshold_seconds')}s"
    )

    lines.append("\n[refresh gap, seconds - all tokens]")
    for key, value in report["refresh_gap_seconds"].items():
        lines.append(f"  {key:<6} {value}")

    lines.append("\n[by market liquidity - mean notional per side]")
    lines.append(f"  {'bucket':<10} {'p50':>9} {'p90':>9} {'p95':>9} {'p99':>9} {'max':>10} {'n':>7}")
    for label, stats in report["by_liquidity_bucket"].items():
        lines.append(
            f"  {label:<10} {stats.get('p50') or 0:9.1f} {stats.get('p90') or 0:9.1f} "
            f"{stats.get('p95') or 0:9.1f} {stats.get('p99') or 0:9.1f} "
            f"{stats.get('max') or 0:10.1f} {stats.get('n') or 0:7d}"
        )

    lines.append("\n[gap classification]")
    for kind, count in report["classification"].items():
        if kind == "note":
            lines.append(f"  {count}")
        else:
            lines.append(f"  {kind:<16} {count}")
    lines.append(f"  {'over gate:':<16} {report['gaps_over_gate_by_class']}")
    lines.append(f"  {'over gate by bucket:':<16} {report['gaps_over_gate_by_bucket']}")

    lines.append(f"\n[stalled tokens - {report['stalled_token_count']} behind the gate]")
    for item in report["stalled_tokens"][:10]:
        lines.append(
            f"  {item['token_id'][:24]}…  {item['seconds_behind_archive_head']:8.1f}s  "
            f"{item['liquidity_bucket']}  n={item['observations']}"
        )

    lines.append("\n[slowest tokens by median gap]")
    for item in report["slowest_tokens"]:
        lines.append(
            f"  {item['token_id'][:24]}…  p50={item.get('p50')}  p90={item.get('p90')}  "
            f"p99={item.get('p99')}  max={item.get('max')}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", default="data/raw", help="collector raw archive directory")
    parser.add_argument("--gate-ms", type=float, default=90_000.0, help="shadow freshness gate")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    report = build(Path(args.raw), args.gate_ms)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render(report))
    return 0 if "error" not in report else 1


if __name__ == "__main__":
    raise SystemExit(main())

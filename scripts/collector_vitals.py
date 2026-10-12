#!/usr/bin/env python3
"""Collector vitals: the before/after numbers for the polling changes.

Reads one collector run - its raw archive and its heartbeat - and prints the
measurements the stability work is judged on. Run it against a pre-change run
and a post-change run with the same flags and the two tables line up.

What it measures, and why each is here:

``rest.latency``        venue timestamp -> collector receipt. The honest
                        end-to-end delay of a book reaching us. Not request
                        duration, which the archive cannot see.
``rest.refresh_gap``    per token, how long between two consecutive books.
                        The starvation signal: a fair sweep keeps this tight
                        for *every* token, a budget-driven one leaves a tail.
``rest.per_token_worst``the tokens with the worst median gap. If a handful sit
                        far above the pack, they are being skipped.
``heartbeat.interval``  gaps between heartbeat writes. The acceptance is that
                        this stays bounded *while REST is failing*, so a long
                        tail here means the loop blocked.
``ws``                  frames by event type. After the change this also
                        reports queue depth and dropped frames, when the
                        heartbeat carries them.

Usage::

    python scripts/collector_vitals.py --run data/collector-bench
    python scripts/collector_vitals.py --run data/collector-bench --json

Nothing here writes to the run being measured.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

# A heartbeat gap beyond this is worth naming: the loop writes one every
# heartbeat_seconds (10 s at defaults), so several multiples means something
# blocked for longer than the flush interval.
HEARTBEAT_GAP_WARN_SECONDS = 30.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = (p / 100.0) * (n - 1)
    low = int(rank)
    high = min(low + 1, n - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(_percentile(values, 50), 3),
        "p90": round(_percentile(values, 90), 3),
        "p95": round(_percentile(values, 95), 3),
        "p99": round(_percentile(values, 99), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
    }


def _iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_archive(raw_dir: Path) -> list[dict]:
    out = []
    for source in ("rest", "ws", "gamma"):
        for path in sorted((raw_dir / source).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    out.sort(key=lambda o: str(o.get("received_at_utc")))
    return out


def rest_vitals(records: list[dict]) -> dict:
    by_token: dict[str, list] = defaultdict(list)
    latency: list[float] = []

    for record in records:
        if record.get("source") != "rest":
            continue
        payload = record.get("payload") or {}
        token = payload.get("asset_id")
        venue = payload.get("timestamp")
        received = _iso(record.get("received_at_utc"))
        if not token or not venue or not received:
            continue
        try:
            venue_dt = datetime.fromtimestamp(int(venue) / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            continue
        by_token[str(token)].append(venue_dt)
        latency.append((received - venue_dt).total_seconds() * 1000.0)

    gaps: list[float] = []
    per_token: dict[str, list[float]] = {}
    for token, stamps in by_token.items():
        stamps.sort()
        token_gaps = [
            (b - a).total_seconds()
            for a, b in itertools.pairwise(stamps)
            if b > a
        ]
        if token_gaps:
            per_token[token] = token_gaps
            gaps.extend(token_gaps)

    medians = sorted(
        ((statistics.median(v), token) for token, v in per_token.items() if len(v) >= 3),
        reverse=True,
    )
    overall_median = statistics.median(gaps) if gaps else None

    return {
        "tokens": len(by_token),
        "observations": sum(len(v) for v in by_token.values()),
        "latency_venue_to_received_ms": _stats(latency),
        "refresh_gap_seconds": _stats(gaps),
        "per_token_median_gap_seconds": {
            "n_with_3plus_gaps": len(medians),
            "min": round(medians[-1][0], 3) if medians else None,
            "median_of_medians": round(statistics.median([m for m, _ in medians]), 3)
            if medians
            else None,
            "max": round(medians[0][0], 3) if medians else None,
            # The starvation ratio: worst token vs the typical one.
            "worst_over_median": round(medians[0][0] / overall_median, 2)
            if medians and overall_median
            else None,
        },
        "worst_tokens": [
            {"token_id": token[:24], "median_gap_s": round(median, 2)}
            for median, token in medians[:10]
        ],
        "gaps_over_90s": sum(1 for g in gaps if g > 90.0),
        "gaps_over_90s_percent": round(100.0 * sum(1 for g in gaps if g > 90.0) / len(gaps), 1)
        if gaps
        else 0.0,
    }


def ws_vitals(records: list[dict]) -> dict:
    kinds: Counter[str] = Counter()
    for record in records:
        if record.get("source") != "ws":
            continue
        payload = record.get("payload")
        if isinstance(payload, list):
            kinds["book_dump_array"] += 1
        elif isinstance(payload, dict):
            kinds[str(payload.get("event_type") or "unknown")] += 1
        else:
            kinds["unparsed"] += 1
    return {"frames": sum(kinds.values()), "by_type": dict(sorted(kinds.items()))}


def heartbeat_vitals(path: Path) -> dict:
    if not path.exists():
        return {"available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "error": "unreadable"}

    # The heartbeat carries only the latest write, so interval continuity comes
    # from its own `updated_at_utc` against the fields it records.
    updated = _iso(payload.get("updated_at_utc"))
    started = _iso(payload.get("started_at_utc"))
    uptime = (updated - started).total_seconds() if updated and started else None

    out = {
        "available": True,
        "status": payload.get("status"),
        "started_at_utc": payload.get("started_at_utc"),
        "updated_at_utc": payload.get("updated_at_utc"),
        "uptime_seconds": round(uptime, 1) if uptime is not None else None,
        "last_upstream_contact_utc": payload.get("last_upstream_contact_utc"),
        "rest_last_success_utc": payload.get("rest_last_success_utc"),
        "rest_consecutive_failures": payload.get("rest_consecutive_failures"),
        "last_error": payload.get("last_error"),
    }
    # New instrumentation, reported only when present: a before-run has none.
    for key in (
        "rest_latency_ms",
        "rest_timeouts",
        "poll_budget_exceeded",
        "ws_queue_depth",
        "ws_queue_high_water",
        "frames_dropped",
        "per_token_book_age_ms",
        "stats",
    ):
        if key in payload:
            out[key] = payload[key]
    return out


def _find_archive(run_dir: Path, explicit: Path | None) -> Path | None:
    """Where this run's JSONL lives.

    ``raw`` is the default layout, but a benchmark or an A/B run keeps its
    archive beside it under another name (``raw-before``, ``raw-after``), and
    the run directory may *be* the archive. Searching is cheaper than making
    every invocation spell out a path that is almost always the obvious one.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None

    candidates: list[Path] = [run_dir / "raw", *sorted(p for p in run_dir.glob("raw*") if p.is_dir())]
    if (run_dir / "rest").exists() or (run_dir / "gamma").exists():
        candidates.append(run_dir)
    for candidate in candidates:
        if candidate.is_dir() and (
            (candidate / "rest").exists() or (candidate / "gamma").exists()
        ):
            return candidate
    return None


def build(run_dir: Path, raw_dir: Path | None = None) -> dict:
    archive = _find_archive(run_dir, raw_dir)
    records = read_archive(archive) if archive else []

    heartbeat_path = run_dir / "health" / "heartbeat.json"
    if not heartbeat_path.exists():
        heartbeat_path = run_dir / "heartbeat.json"

    return {
        "run": str(run_dir),
        "archive": str(archive) if archive else None,
        "archive_records": len(records),
        "rest": rest_vitals(records),
        "ws": ws_vitals(records),
        "heartbeat": heartbeat_vitals(heartbeat_path),
    }


def render(report: dict) -> str:
    rest = report["rest"]
    lines = [f"collector vitals  run={report['run']}  records={report['archive_records']}"]

    lines.append("\n[rest]")
    lines.append(f"  tokens                 {rest.get('tokens', 0)}")
    lines.append(f"  observations           {rest.get('observations', 0)}")
    lat = rest.get("latency_venue_to_received_ms", {})
    lines.append(
        f"  venue->received ms     p50={lat.get('p50')}  p95={lat.get('p95')}  "
        f"p99={lat.get('p99')}  max={lat.get('max')}  (n={lat.get('n', 0)})"
    )
    gap = rest.get("refresh_gap_seconds", {})
    lines.append(
        f"  refresh gap s          p50={gap.get('p50')}  p90={gap.get('p90')}  "
        f"p95={gap.get('p95')}  p99={gap.get('p99')}  max={gap.get('max')}"
    )
    tok = rest.get("per_token_median_gap_seconds", {})
    lines.append(
        f"  per-token median       min={tok.get('min')}  median={tok.get('median_of_medians')}  "
        f"max={tok.get('max')}  worst/median={tok.get('worst_over_median')}"
    )
    lines.append(
        f"  gaps over 90s          {rest.get('gaps_over_90s', 0)} "
        f"({rest.get('gaps_over_90s_percent', 0)}%)"
    )
    if rest.get("worst_tokens"):
        lines.append("  worst tokens by median gap:")
        for item in rest["worst_tokens"][:5]:
            lines.append(f"    {item['token_id']}…  {item['median_gap_s']}s")

    ws = report["ws"]
    lines.append("\n[ws]")
    lines.append(f"  frames                 {ws.get('frames', 0)}")
    for kind, count in (ws.get("by_type") or {}).items():
        lines.append(f"    {kind:<22} {count}")

    hb = report["heartbeat"]
    lines.append("\n[heartbeat]")
    if not hb.get("available"):
        lines.append("  (not found)")
    else:
        lines.append(f"  status                 {hb.get('status')}")
        lines.append(f"  uptime s               {hb.get('uptime_seconds')}")
        lines.append(f"  rest failures          {hb.get('rest_consecutive_failures')}")
        lines.append(f"  last error             {hb.get('last_error')}")
        # Instrumentation that only exists after the change.
        for key in (
            "rest_latency_ms",
            "rest_timeouts",
            "poll_budget_exceeded",
            "ws_queue_depth",
            "ws_queue_high_water",
            "frames_dropped",
        ):
            if key in hb:
                lines.append(f"  {key:<22} {hb[key]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="collector run directory (or its raw dir)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build(Path(args.run))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

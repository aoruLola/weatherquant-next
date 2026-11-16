#!/usr/bin/env python
"""Monitor the performance snapshots: sizes, growth, retention, and latency.

The streaming pipeline's whole memory story (PR #11-#13, contract §14) rests
on three size properties that nothing continuously checks - until now:

1. **A page is a page.** The aggregates (summary/win-rate/edge/models/
   settlements) and the opportunities *pointer* are bounded by design and
   must stay KB-scale; the browsable detail lives in parts, each part bounded
   by the detail window. A snapshot file that starts growing with the
   history is the pre-§14 shape coming back, and the Dashboard's
   ``json.load`` dies long before any gate would notice.
2. **The generations tree is on a retention diet.** Every publish makes a
   new generation; at a 5-minute cadence that is 288/day, ~2.2 GB/day
   without pruning. The monitor reports the tree's size and how much a
   prune would reclaim, and fails past a total-size gate.
3. **The served endpoints stay cheap.** Against a live dashboard: latency
   p95 per core endpoint, response bytes, and the limit clamp - a request
   for a million rows must come back as at most 200, enforced server-side.

Everything is measured, then judged against gates; nothing is asserted in
advance. Usage::

    uv run python scripts/performance_monitor.py --performance /path/to/performance
    uv run python scripts/performance_monitor.py --performance DIR \\
        --dashboard http://127.0.0.1:8787 --dashboard-token TOKEN \\
        --probes 10 --report /tmp/perf-monitor.json

Exit status is 0 when every gate holds, 1 otherwise. It never writes to the
performance directory; the retention *action* stays with
``scripts/prune_performance_generations.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

MONITOR_VERSION = "performance-monitor-2"

#: Files whose size must not depend on the length of the history. The
#: opportunities pointer is here too: after contract §14 the payload lives in
#: shards under opportunities/, and the pointer is metadata again.
ALWAYS_SMALL = (
    "summary.json",
    "edge.json",
    "win-rate.json",
    "models.json",
    "settlements.json",
    "opportunities.json",
)

CORE_PROBES = (
    ("summary", "/api/v1/performance/summary"),
    ("edge", "/api/v1/performance/edge"),
    ("win-rate", "/api/v1/performance/win-rate"),
    ("opportunities page 1", "/api/v1/performance/opportunities?limit=50"),
    ("reference capital", "/api/v1/performance/reference-capital?capital_usdc=250"),
    ("opportunities max page", "/api/v1/performance/opportunities?limit=200"),
)


def _generation_of(performance_dir: Path) -> tuple[Path | None, str | None]:
    pointer = performance_dir / "current.json"
    if not pointer.exists():
        return None, None
    try:
        generation = json.loads(pointer.read_text(encoding="utf-8"))["generation_id"]
    except (json.JSONDecodeError, KeyError):
        return None, None
    directory = performance_dir / "generations" / generation
    return (directory if directory.exists() else None), generation


def check_snapshot_profile(generation: Path, *, max_small_mb: float, max_part_mb: float) -> dict:
    """Per-file size profile of the current generation, with gates."""
    files: dict[str, dict] = {}
    failures: list[str] = []
    small_limit = max_small_mb * 1024 * 1024
    part_limit = max_part_mb * 1024 * 1024

    for name in ALWAYS_SMALL:
        path = generation / name
        if not path.exists():
            continue
        size = path.stat().st_size
        files[name] = {"bytes": size, "kind": "aggregate"}
        if size > small_limit:
            failures.append(
                f"{name} is {size / 1024 / 1024:.1f} MiB (gate {max_small_mb} MiB): "
                "an aggregate is growing with the history again"
            )

    detail_dir = generation / "opportunities"
    if detail_dir.is_dir():
        parts = sorted(detail_dir.glob("*.json"))
        total_detail = sum(p.stat().st_size for p in parts)
        largest = max((p.stat().st_size for p in parts), default=0)
        files["opportunities/"] = {
            "bytes": total_detail,
            "kind": "sharded_detail",
            "parts": len(parts),
            "largest_part_bytes": largest,
        }
        if largest > part_limit:
            failures.append(
                f"largest detail part is {largest / 1024 / 1024:.1f} MiB "
                f"(gate {max_part_mb} MiB): the detail window is too wide"
            )

    return {"files": files, "failures": failures}


def check_generations(performance_dir: Path, *, max_total_mb: float) -> dict:
    """The generations tree: size, growth, and what a prune could reclaim."""
    generations_dir = performance_dir / "generations"
    stats = {
        "count": 0,
        "total_bytes": 0,
        "oldest": None,
        "newest": None,
        "failures": [],
    }
    if not generations_dir.is_dir():
        stats["failures"].append("no generations directory: nothing has ever published")
        return stats

    sizes: list[tuple[str, int]] = []
    for directory in sorted(generations_dir.iterdir()):
        if not directory.is_dir():
            continue
        size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        sizes.append((directory.name, size))
    stats["count"] = len(sizes)
    stats["total_bytes"] = sum(size for _, size in sizes)
    if sizes:
        stats["oldest"] = sizes[0][0]
        stats["newest"] = sizes[-1][0]
        stats["avg_bytes"] = stats["total_bytes"] // len(sizes)

    limit = max_total_mb * 1024 * 1024
    if stats["total_bytes"] > limit:
        stats["failures"].append(
            f"generations tree is {stats['total_bytes'] / 1024 / 1024 / 1024:.2f} GiB "
            f"(gate {max_total_mb} MiB): run scripts/prune_performance_generations.py"
        )
    return stats


def probe_dashboard(
    base_url: str,
    *,
    token: str | None,
    probes: int,
    max_p95_ms: float,
    max_response_mb: float,
    previous_cursor: str | None = None,
    transport=None,
) -> dict:
    """Latency, payload size and the limit clamp, against a live dashboard.

    ``transport`` is injectable so the tests can drive the gates against a
    stand-in without a socket, the same way the gateway's e2e does.
    """
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    report: dict = {"probes": [], "failures": []}
    response_limit = max_response_mb * 1024 * 1024

    client_kwargs: dict = {"base_url": base_url, "headers": headers, "timeout": 30.0}
    if transport is not None:
        client_kwargs["transport"] = transport
    client = httpx.Client(**client_kwargs)

    with client:
        # Reachability first: every gate below is meaningless against a 500.
        try:
            health = client.get("/api/v1/performance/summary")
        except Exception as exc:
            report["failures"].append(f"dashboard unreachable: {exc}")
            return report
        if health.status_code != 200:
            report["failures"].append(f"/summary answered {health.status_code}; gates need a healthy source")
            return report

        for name, path in CORE_PROBES:
            samples: list[float] = []
            payload_bytes = 0
            status = 200
            for _ in range(probes):
                started = perf_counter()
                try:
                    response = client.get(path)
                except Exception as exc:
                    samples.append(float("inf"))
                    report["failures"].append(f"{name}: request failed: {exc}")
                    break
                samples.append((perf_counter() - started) * 1000)
                status = response.status_code
                if status != 200:
                    report["failures"].append(f"{name} probe answered {status}")
                if name == "opportunities max page" and status == 200:
                    page = response.json().get("data", {}).get("items")
                    if not isinstance(page, list) or len(page) > 200:
                        report["failures"].append("limit=200 did not return a bounded items list")
                payload_bytes = max(payload_bytes, len(response.content))
            if samples and samples[-1] != float("inf"):
                samples.sort()
                p95 = samples[math.ceil(len(samples) * 0.95) - 1]
                entry = {
                    "endpoint": name,
                    "status": status,
                    "bytes": payload_bytes,
                    "p95_ms": round(p95, 1),
                }
                report["probes"].append(entry)
                # A non-200 is a failure in its own right, before any latency
                # or size gate: a 429 (rate limited) or 422 (malformed shard)
                # arrives fast and small, so the latency gates would pass it
                # and the monitor would go green on a broken endpoint - the
                # false-green the local acceptance measured (30x 429, PASSED).
                if status != 200:
                    report["failures"].append(
                        f"{name} answered {status}; a degraded endpoint is a "
                        "failed gate regardless of how fast it answers"
                    )
                    continue
                if p95 > max_p95_ms:
                    report["failures"].append(f"{name} p95 {p95:.0f}ms above the gate {max_p95_ms}ms")
                if payload_bytes > response_limit:
                    report["failures"].append(
                        f"{name} served {payload_bytes / 1024 / 1024:.1f} MiB (gate {max_response_mb} MiB)"
                    )

        # The public route rejects oversized limits. Older servers can return a bounded page.
        try:
            clamped = client.get("/api/v1/performance/opportunities?limit=1000000")
        except httpx.RequestError as exc:
            report["failures"].append(f"limit clamp probe failed: {exc}")
            return report
        items = None
        rejected_limit = False
        if clamped.status_code == 200:
            body = clamped.json()
            data = body.get("data", {})
            rows = data.get("items")
            if isinstance(rows, list):
                items = len(rows)
        elif clamped.status_code == 422:
            try:
                error_body = clamped.json()
            except ValueError:
                error_body = None
            detail = error_body.get("detail") if isinstance(error_body, dict) else None
            rejected_limit = isinstance(detail, list) and any(
                isinstance(error, dict) and error.get("loc") == ["query", "limit"]
                and error.get("type") == "less_than_equal" and error.get("ctx", {}).get("le") == 200
                for error in detail
            )
        report["limit_clamp"] = {
            "requested": 1000000,
            "status": clamped.status_code,
            "served_items": items,
        }
        if rejected_limit:
            report["limit_clamp"]["rejected_above"] = 200
        elif clamped.status_code != 200:
            report["failures"].append(
                f"limit clamp probe answered {clamped.status_code}; expected a bounded page or query validation for limit <= 200"
            )
        elif items is None:
            report["failures"].append("limit clamp response missing items list")
        elif items > 200:
            report["failures"].append(f"limit=1000000 served {items} items: the server-side clamp is gone")

        if previous_cursor:
            try:
                stale = client.get("/api/v1/performance/opportunities", params={"cursor": previous_cursor})
            except httpx.RequestError as exc:
                report["failures"].append(f"previous generation cursor probe failed: {exc}")
                return report
            report["previous_cursor_status"] = stale.status_code
            if stale.status_code != 409:
                report["failures"].append(f"previous generation cursor answered {stale.status_code}; expected 409")

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--performance", required=True, help="published performance directory")
    parser.add_argument("--dashboard", help="optional: probe a live dashboard's endpoints")
    parser.add_argument("--dashboard-token", default=None)
    parser.add_argument("--probes", type=int, default=8, help="requests per endpoint")
    parser.add_argument("--max-small-file-mb", type=float, default=5.0)
    parser.add_argument("--max-part-mb", type=float, default=64.0)
    parser.add_argument("--max-total-mb", type=float, default=8192.0, help="generations tree size gate (default 8 GiB)")
    parser.add_argument("--max-p95-ms", type=float, default=1000.0)
    parser.add_argument("--max-response-mb", type=float, default=32.0)
    parser.add_argument("--previous-cursor", help="cursor saved from the previous published generation")
    parser.add_argument("--dashboard-pid", type=int, help="local Linux Dashboard worker PID for the peak RSS gate")
    parser.add_argument("--max-dashboard-rss-mib", type=float, help="maximum Dashboard process peak RSS in MiB")
    parser.add_argument("--report", help="write the JSON report here")
    args = parser.parse_args(argv)

    if (args.dashboard_pid is None) != (args.max_dashboard_rss_mib is None):
        parser.error("--dashboard-pid and --max-dashboard-rss-mib must be used together")
    if (args.previous_cursor or args.dashboard_pid is not None) and not args.dashboard:
        parser.error("cursor and process measurements require --dashboard")
    if args.max_dashboard_rss_mib is not None and not (0 < args.max_dashboard_rss_mib < math.inf):
        parser.error("--max-dashboard-rss-mib must be positive and finite")
    if args.probes < 1:
        parser.error("--probes must be positive")

    performance_dir = Path(args.performance).resolve()
    if not performance_dir.exists():
        print(f"no performance directory at {performance_dir}", file=sys.stderr)
        return 2

    failures: list[str] = []
    generation, generation_id = _generation_of(performance_dir)
    print(f"performance monitor {MONITOR_VERSION} over {performance_dir}")

    profile: dict = {"files": {}, "failures": []}
    generations: dict = {"count": 0, "total_bytes": 0, "failures": []}
    if generation is None:
        failures.append("current.json names no readable generation")
    else:
        print(f"  current generation: {generation_id}")
        profile = check_snapshot_profile(generation, max_small_mb=args.max_small_file_mb, max_part_mb=args.max_part_mb)
        for name, facts in profile["files"].items():
            size = facts["bytes"]
            extra = f" ({facts['parts']} parts)" if "parts" in facts else ""
            print(f"    {name:<22} {size / 1024:12.1f} KiB{extra}")
        failures.extend(profile["failures"])

        generations = check_generations(performance_dir, max_total_mb=args.max_total_mb)
        print(
            f"  generations: {generations['count']} total, "
            f"{generations['total_bytes'] / 1024 / 1024:.1f} MiB, "
            f"oldest={generations.get('oldest')}, newest={generations.get('newest')}"
        )
        failures.extend(generations["failures"])

    probes: dict = {"probes": [], "failures": []}
    if args.dashboard:
        print(f"  dashboard: {args.dashboard} ({args.probes} probes/endpoint)")
        probes = probe_dashboard(
            args.dashboard,
            token=args.dashboard_token,
            probes=args.probes,
            previous_cursor=args.previous_cursor,
            max_p95_ms=args.max_p95_ms,
            max_response_mb=args.max_response_mb,
        )
        for probe in probes["probes"]:
            print(f"    {probe['endpoint']:<24} {probe['bytes']:>10,} B  p95 {probe['p95_ms']:>7.1f} ms")
        if "limit_clamp" in probes:
            clamp = probes["limit_clamp"]
            if "rejected_above" in clamp:
                print(f"    limit clamp: {clamp['requested']} rejected above {clamp['rejected_above']}")
            else:
                print(f"    limit clamp: asked {clamp['requested']}, status {clamp['status']}, items {clamp['served_items']}")
        if args.dashboard_pid is not None:
            from soak_memory import read_rss_mb

            _, peak = read_rss_mb(args.dashboard_pid)
            probes["process_rss"] = {"pid": args.dashboard_pid, "peak_mib": peak or None,
                                    "max_mib": args.max_dashboard_rss_mib}
            if not peak:
                probes["failures"].append(f"no peak RSS available for Dashboard PID {args.dashboard_pid}")
            elif peak > args.max_dashboard_rss_mib:
                probes["failures"].append(f"Dashboard peak RSS {peak:.1f} MiB exceeds {args.max_dashboard_rss_mib} MiB")
        failures.extend(probes["failures"])

    print("\n" + "=" * 72)
    if failures:
        print("RESULT: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        code = 1
    else:
        print("RESULT: PASSED")
        print(
            "  aggregates bounded, detail sharded, generations on a diet,"
            + (" endpoints cheap." if args.dashboard else ".")
        )
        code = 0

    if args.report:
        report = {
            "monitor_version": MONITOR_VERSION,
            "generated_at_utc": datetime.now(tz=UTC).isoformat(),
            "performance_dir": str(performance_dir),
            "generation_id": generation_id,
            "snapshot_profile": profile,
            "generations": generations,
            "dashboard_probes": probes if args.dashboard else None,
            "failures": failures,
            "verdict": "FAILED" if failures else "PASSED",
        }
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"  report written to {args.report}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

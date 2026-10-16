"""Reproducible command-line entry point for the research pipeline.

Every command is idempotent and resumable: re-running one after an interruption
overwrites rather than duplicates, because each target table has a natural key.
Nothing here writes to the collector's database or to the legacy database.

    weatherquant-research init
    weatherquant-research extract-legacy [--legacy PATH]
    weatherquant-research crosscheck
    weatherquant-research rules [--event-id ID] [--source NAME]
    weatherquant-research fit-models --cutoff YYYY-MM-DD [--pin]
    weatherquant-research report [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from weatherquant.config import Settings
from weatherquant.storage import ResearchDatabase
from weatherquant.storage.migrator import applied_migrations


def _db(settings: Settings) -> ResearchDatabase:
    return ResearchDatabase(settings.research_db_path)


def cmd_init(settings: Settings, _args: argparse.Namespace) -> int:
    # ResearchDatabase.__enter__ opens (and migrates) the database, so there is
    # nothing to call again here - opening twice would report "none applied"
    # for a file this very call just created.
    with _db(settings) as db:
        applied = list(applied_migrations(db.con))
        print(f"research database: {db.path}")
        print(f"migrations applied this run: {applied or 'none (already current)'}")
        print(f"tables: {', '.join(sorted(db.table_counts()))}")
    return 0


def cmd_extract_legacy(settings: Settings, args: argparse.Namespace) -> int:
    from weatherquant.truth.legacy_extract import extract_from_legacy

    legacy = Path(args.legacy) if args.legacy else settings.legacy_db_path
    if not legacy.exists():
        print(f"legacy database not found: {legacy}", file=sys.stderr)
        return 2

    with _db(settings) as db:
        db.open()
        run_id = db.start_run("extract-legacy", {"legacy": str(legacy)})
        try:
            stats = extract_from_legacy(db, legacy)
        except Exception as exc:
            db.finish_run(run_id, status="failed", error=str(exc))
            raise
        db.finish_run(run_id, status="completed", stats=stats.as_dict())

        print(f"legacy source: {legacy}")
        for key, value in stats.as_dict().items():
            if key == "problems":
                continue
            print(f"  {key:24} {value}")
        if stats.problems:
            print(f"  problems: {len(stats.problems)}")
            for problem in stats.problems[:10]:
                print(f"    - {problem}")
        print("  counts:", db.table_counts())
    return 0


def cmd_crosscheck(settings: Settings, _args: argparse.Namespace) -> int:
    from weatherquant.truth.crosscheck import run_crosscheck

    with _db(settings) as db:
        db.open()
        if db.table_counts().get("settlement_outcome", 0) == 0:
            print("no settlement outcomes loaded; run `extract-legacy` first", file=sys.stderr)
            return 2
        run_id = db.start_run("crosscheck", {})
        summary = run_crosscheck(db, station=settings.station)
        db.finish_run(run_id, status="completed", stats=summary)

    for rule, counts in summary.items():
        total = counts.get("total", 0)
        matched = counts.get("match", 0)
        mismatched = counts.get("mismatch", 0)
        rate = (100.0 * matched / total) if total else 0.0
        print(f"{rule}")
        print(f"  events            {total}")
        print(f"  match             {matched}  ({rate:.2f}%)")
        print(f"  mismatch          {mismatched}")
        for key in ("no_observation", "no_winner", "ambiguous_winner", "unparsed_rules", "no_rules"):
            if counts.get(key):
                print(f"  {key:19} {counts[key]}")
    return 0


def cmd_rules(settings: Settings, args: argparse.Namespace) -> int:
    with _db(settings) as db:
        db.open()
        sql = (
            "SELECT event_id, target_date, station, primary_source_name, fallback_source_name, "
            "unit, precision_deg, rounding, timezone_name, timezone_hint, "
            "observation_field, no_data_resolution, parse_status, parse_notes "
            "FROM settlement_rules"
        )
        params: list = []
        if args.event_id:
            sql += " WHERE event_id = ?"
            params.append(args.event_id)
        if args.source:
            sql += (" AND" if args.event_id else " WHERE") + " primary_source_name = ?"
            params.append(args.source)
        sql += " ORDER BY target_date NULLS LAST, event_id"
        rows = db.con.execute(sql, params).fetchall()

    columns = [
        "event_id", "target_date", "station", "source", "fallback", "unit",
        "precision", "rounding", "timezone", "tz_hint", "field", "no_data",
        "status", "notes",
    ]
    if args.json:
        print(json.dumps([dict(zip(columns, r, strict=True)) for r in rows], default=str, indent=2))
        return 0
    for row in rows:
        d = dict(zip(columns, row, strict=True))
        print(
            f"{d['event_id']:>10}  {d['target_date']!s:10} {d['station']!s:5} "
            f"{d['source']!s:20} {d['unit']!s:4} {d['precision']!s:4} "
            f"{d['status']}"
        )
        detail = {
            k: v for k, v in d.items()
            if k in ("fallback", "rounding", "timezone", "tz_hint", "field", "no_data", "notes")
            and v
        }
        if detail and args.verbose:
            print(f"{'':>10}  {detail}")
    print(f"\n{len(rows)} event(s)")
    return 0


def cmd_fit_models(settings: Settings, args: argparse.Namespace) -> int:
    """Fit the four baselines and freeze them as versioned artifacts.

    The cutoff is mandatory. A fit that defaulted to "everything in the
    database" would have a training window nobody chose, and the shadow engine
    would then load parameters fitted on data that includes the very day it is
    asked to decide about.
    """
    from datetime import date as _date

    from weatherquant.models.factory import FIT_SPECS, fit_models

    try:
        cutoff = _date.fromisoformat(args.cutoff)
    except ValueError as exc:
        print(f"invalid --cutoff {args.cutoff!r}: {exc}", file=sys.stderr)
        return 2

    names = None
    if args.models:
        names = [n.strip() for n in args.models.split(",") if n.strip()]
        unknown = [n for n in names if n not in FIT_SPECS]
        if unknown:
            print(f"unknown model(s): {unknown}; known: {sorted(FIT_SPECS)}", file=sys.stderr)
            return 2

    from weatherquant.shadow.config import ShadowSettings

    models_dir = Path(args.out) if args.out else ShadowSettings().shadow_models_dir
    version = args.version or f"cut-{cutoff.isoformat()}"

    if not settings.research_db_path.exists():
        print(f"research database not found: {settings.research_db_path}", file=sys.stderr)
        return 2

    with _db(settings) as db:
        try:
            artifacts, problems = fit_models(
                db.con,
                cutoff=cutoff,
                models_dir=models_dir,
                version=version,
                models=names,
                observation_rule=args.observation_rule,
                pin=args.pin,
            )
        except FileExistsError as exc:
            print(
                f"{exc}\nmodel versions are immutable; choose a new --version",
                file=sys.stderr,
            )
            return 3

    for artifact in artifacts:
        print(f"{artifact.model_name}@{artifact.model_version}")
        print(f"  cutoff      {artifact.training_cutoff_utc.date()}  pairs={artifact.n_pairs}  dates={artifact.n_dates}")
        print(f"  bias        {artifact.bias_f:+.4f} F")
        print(f"  sigma       {artifact.sigma_by_lead}")
        print(f"  source hash {artifact.source_data_hash[:16]}…")
        for note in problems.get(artifact.model_name, []):
            print(f"  note        {note}")
    print(f"\nwritten to {models_dir}")
    if args.pin:
        print(f"pinned in {models_dir / 'manifest.json'}")
    else:
        print("not pinned; pass --pin to make these the versions the shadow loads")
    return 0


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    with _db(settings) as db:
        db.open()
        counts = db.table_counts()
        report: dict = {
            "research_db": str(db.path),
            "tables": counts,
        }

        if counts.get("settlement_rules"):
            report["rules"] = {
                "total": counts["settlement_rules"],
                "by_status": dict(
                    db.con.execute(
                        "SELECT parse_status, COUNT(*) FROM settlement_rules GROUP BY 1"
                    ).fetchall()
                ),
                "by_primary_source": dict(
                    db.con.execute(
                        "SELECT primary_source_name, COUNT(*) FROM settlement_rules GROUP BY 1"
                    ).fetchall()
                ),
                "by_station": dict(
                    db.con.execute(
                        "SELECT COALESCE(station, 'UNIDENTIFIED'), COUNT(*) "
                        "FROM settlement_rules GROUP BY 1"
                    ).fetchall()
                ),
            }

        if counts.get("settlement_crosscheck"):
            by_rule = {}
            for rule, verdict, n in db.con.execute(
                "SELECT aggregation_rule, verdict, COUNT(*) FROM settlement_crosscheck "
                "GROUP BY 1, 2 ORDER BY 1, 3 DESC"
            ).fetchall():
                by_rule.setdefault(rule, {})[verdict] = n
            report["crosscheck"] = by_rule
            report["mismatches"] = [
                {
                    "event_id": r[0],
                    "target_date": str(r[1]),
                    "rule": r[2],
                    "observed_f": r[3],
                    "winning_bracket": r[4],
                    "detail": r[5],
                }
                for r in db.con.execute(
                    "SELECT event_id, target_date, aggregation_rule, observed_max_f, "
                    "winning_bracket_label, detail FROM settlement_crosscheck "
                    "WHERE verdict = 'mismatch' ORDER BY target_date"
                ).fetchall()
            ]

        if counts.get("klga_daily_max"):
            report["observations"] = {
                r[0]: {"days": r[1], "from": str(r[2]), "to": str(r[3]), "incomplete": r[4]}
                for r in db.con.execute(
                    "SELECT aggregation_rule, COUNT(DISTINCT target_date), "
                    "MIN(target_date), MAX(target_date), SUM(CASE WHEN NOT is_complete THEN 1 ELSE 0 END) "
                    "FROM klga_daily_max GROUP BY 1"
                ).fetchall()
            }

        if counts.get("forecast_runs"):
            report["forecasts"] = {
                "runs": counts["forecast_runs"],
                "by_model": dict(
                    db.con.execute(
                        "SELECT model, COUNT(*) FROM forecast_runs GROUP BY 1"
                    ).fetchall()
                ),
                "by_status": dict(
                    db.con.execute(
                        "SELECT status, COUNT(*) FROM forecast_runs GROUP BY 1"
                    ).fetchall()
                ),
                "by_publishable_basis": dict(
                    db.con.execute(
                        "SELECT publishable_basis, COUNT(*) FROM forecast_runs GROUP BY 1"
                    ).fetchall()
                ),
            }
        if counts.get("point_in_time"):
            report["point_in_time"] = {
                "eligible": db.con.execute(
                    "SELECT COUNT(*) FROM point_in_time WHERE eligible"
                ).fetchone()[0],
                "ineligible": db.con.execute(
                    "SELECT COUNT(*) FROM point_in_time WHERE NOT eligible"
                ).fetchone()[0],
            }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


def _print_report(report: dict) -> None:
    print(f"research database: {report['research_db']}")
    print("\n[table rows]")
    for name, n in sorted(report.get("tables", {}).items()):
        if n:
            print(f"  {name:26} {n}")

    rules = report.get("rules")
    if rules:
        print("\n[settlement rules]")
        print(f"  total events          {rules['total']}")
        print(f"  parse status           {rules['by_status']}")
        print(f"  primary source         {rules['by_primary_source']}")
        print(f"  station                {rules['by_station']}")

    cross = report.get("crosscheck")
    if cross:
        print("\n[settlement cross-check: observed daily max vs the winning bracket]")
        for rule, verdicts in cross.items():
            total = sum(verdicts.values())
            match = verdicts.get("match", 0)
            rate = 100.0 * match / total if total else 0.0
            print(f"  {rule}")
            print(f"    events {total}   match {match} ({rate:.2f}%)   {verdicts}")

    mismatches = report.get("mismatches") or []
    if mismatches:
        print(f"\n[mismatches: {len(mismatches)}] - recorded, never edited away")
        for item in mismatches:
            print(
                f"  {item['target_date']}  {item['event_id']}  [{item['rule']}]  "
                f"observed {item['observed_f']}F  winner {item['winning_bracket']!r}"
            )
            print(f"      {item['detail']}")

    obs = report.get("observations")
    if obs:
        print("\n[observations]")
        for rule, stats in obs.items():
            print(
                f"  {rule:30} {stats['days']:5} days  {stats['from']} .. {stats['to']}"
                f"  incomplete={stats['incomplete']}"
            )

    fc = report.get("forecasts")
    if fc:
        print("\n[forecasts]")
        print(f"  runs {fc['runs']}  by model {fc['by_model']}  by status {fc['by_status']}")
        print(f"  publishable basis {fc['by_publishable_basis']}")

    pit = report.get("point_in_time")
    if pit:
        print("\n[point-in-time eligibility]")
        print(f"  eligible   {pit['eligible']}")
        print(f"  ineligible {pit['ineligible']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weatherquant-research",
        description="WeatherQuant Next research pipeline (settlement truth, forecasts).",
    )
    parser.add_argument("--db", help="research database path (overrides WQ_RESEARCH_DB_PATH)")
    parser.add_argument("--legacy", help="legacy database path (overrides WQ_LEGACY_DB_PATH)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or migrate the research database")
    sub.add_parser("extract-legacy", help="read the legacy DB into the research DB (read-only)")
    sub.add_parser("crosscheck", help="compare observations against venue resolutions")

    p = sub.add_parser("rules", help="show parsed settlement rules")
    p.add_argument("--event-id")
    p.add_argument("--source", help="NOAA or WEATHER_UNDERGROUND")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("fit-models", help="fit the baselines and freeze them as versioned artifacts")
    p.add_argument("--cutoff", required=True, help="last target date the fit may see (YYYY-MM-DD)")
    p.add_argument("--version", help="artifact version (default: cut-<cutoff>)")
    p.add_argument("--out", help="models directory (default: <project>/models, committed and baked)")
    p.add_argument("--models", help=f"comma-separated subset of {', '.join(['ecmwf_raw', 'gfs_raw', 'fusion', 'calibrated'])}")
    p.add_argument("--observation-rule", default="max of all readings")
    p.add_argument("--pin", action="store_true", help="write manifest.json so the shadow loads these")

    p = sub.add_parser("report", help="data-quality report")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser(
        "backfill-settlements",
        help="ask the platform about conditions and write a settlement index",
    )
    p.add_argument(
        "--markets",
        required=True,
        help="JSON list of {market_id, event_id, target_date, condition_id, token_yes}",
    )
    p.add_argument(
        "--verified",
        help="optional JSON list of {market_id, target_date} that passed observation cross-check",
    )
    p.add_argument("--out", required=True, help="where to write the settlement index (JSON)")
    p.add_argument(
        "--dataset",
        required=True,
        choices=("live", "fixture"),
        help="what the data actually is; there is no default",
    )
    p.add_argument("--timeout", type=float, default=20.0)

    p = sub.add_parser(
        "build-performance",
        help="build the six performance snapshots from a decisions export",
    )
    p.add_argument(
        "--export",
        required=True,
        help=(
            "the shadow's decisions-export directory (streaming engine), or a "
            ".json / .jsonl / .parquet file for the in-memory builder"
        ),
    )
    p.add_argument("--settlements", help="settlement index written by backfill-settlements")
    p.add_argument("--out", required=True, help="directory to publish the six snapshots into")
    p.add_argument(
        "--dataset",
        required=True,
        choices=("live", "fixture"),
        help="what the data actually is; there is no default",
    )
    p.add_argument("--capital-usdc", default="100")
    p.add_argument("--max-allocation-fraction", default="0.10")
    p.add_argument("--gap-window-seconds", default="900")
    p.add_argument(
        "--state-db",
        help=(
            "research state store for the streaming engine "
            "(default: WQ_PERFORMANCE_STATE_DB_PATH)"
        ),
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="drop the state store first and rebuild it from the export",
    )
    p.add_argument(
        "--no-ingest",
        action="store_true",
        help="rebuild the snapshots from the state store without reading the export",
    )
    p.add_argument(
        "--no-publish",
        action="store_true",
        help=(
            "consume what is new but do not publish a snapshot. Use with a 5-minute "
            "cadence to separate ingest from publish (e.g., ingest every 5 min, "
            "publish every 20 min)"
        ),
    )
    p.add_argument(
        "--settlements-replace",
        action="store_true",
        help=(
            "treat --settlements as the complete settlement set: answers it does "
            "not contain are retracted. Without this the index is a union, so an "
            "index covering today's markets cannot erase yesterday's answers. An "
            "empty index is refused unless --confirm-empty-retraction is also given"
        ),
    )
    p.add_argument(
        "--confirm-empty-retraction",
        action="store_true",
        help=(
            "required in addition to --settlements-replace when the index is "
            "empty, because that retracts every answer. An empty index is what a "
            "failed backfill looks like at least as often as it is what a genuine "
            "total retraction looks like: the platform may have been unreachable, "
            "or no market may have matched. Refusing is the default because the "
            "settlement layer cannot be rebuilt from the export"
        ),
    )
    p.add_argument(
        "--retraction-reason",
        default=None,
        help=(
            "recorded in the backup a total retraction writes, so the audit "
            "says why the settlement layer was emptied. Recorded as null when "
            "omitted, never guessed"
        ),
    )
    p.add_argument("--memory-limit", default="1GB", help="DuckDB memory limit for the state store")
    p.add_argument(
        "--detail-window-episodes",
        type=int,
        default=20000,
        help=(
            "how many episodes the dashboard can page through, most recent first "
            "(default 20000). The aggregates always cover the whole history; this "
            "bounds the browsable detail, and with it every page request"
        ),
    )
    p.add_argument(
        "--detail-window-days",
        type=int,
        default=90,
        help="how far back the browsable detail reaches, in days (default 90)",
    )
    p.add_argument("--temp-dir", help="where the state store spills when it exceeds the limit")
    p.add_argument(
        "--verify-parts",
        action="store_true",
        help="re-hash every consumed part, not only the boundary one",
    )
    p.add_argument(
        "--max-parts-per-run",
        type=int,
        help="consume at most this many parts in one run (a first backfill can be split)",
    )
    p.add_argument(
        "--in-memory",
        action="store_true",
        help=(
            "use the whole-history builder instead of the streaming engine. "
            "It holds every decision in memory (~6.8 KB each) and cannot scale; "
            "it exists as the reference implementation the streaming engine is "
            "checked against"
        ),
    )
    return parser


def cmd_backfill_settlements(settings: Settings, args: argparse.Namespace) -> int:
    """Ask the platform about a list of conditions, and write what it said.

    Never opens the shadow's database: the market list is a file the caller
    supplies. That is the single-writer rule - the engine owns its DuckDB,
    and this process reads an export.
    """
    from weatherquant.performance.backfill import backfill_settlements

    markets_path = Path(args.markets)
    if not markets_path.exists():
        print(f"markets file not found: {markets_path}", file=sys.stderr)
        return 2
    with markets_path.open(encoding="utf-8") as handle:
        markets = json.load(handle)
    if not isinstance(markets, list):
        print("markets file must be a JSON list", file=sys.stderr)
        return 2

    verified_path = Path(args.verified) if args.verified else None
    verified: list[dict] = []
    if verified_path is not None:
        if not verified_path.exists():
            print(f"verified file not found: {verified_path}", file=sys.stderr)
            return 2
        with verified_path.open(encoding="utf-8") as handle:
            verified = json.load(handle)
        if not isinstance(verified, list):
            print("verified file must be a JSON list", file=sys.stderr)
            return 2

    # httpx, never urllib: the platform refuses Python's default agent.
    import httpx

    timeout = float(getattr(args, "timeout", 20.0))
    with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False) as client:
        rows = backfill_settlements(
            markets,
            get_json=_http_get(client),
            dataset=args.dataset,
            verified=verified,
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written via a temp file and a rename so a reader never sees half a list.
    _atomic_write_json(out, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["settlement_status"]] = counts.get(row["settlement_status"], 0) + 1
    print(f"wrote {len(rows)} settlement record(s) to {out} (dataset={args.dataset})")
    print(f"  by status: {counts}")
    return 0


def _http_get(client):
    """Adapt httpx.Client to the (url, params, timeout) shape fetch_resolution wants."""
    def _get(url, *, params=None, timeout=20.0):
        return client.get(url, params=params, timeout=timeout).json()

    return _get


def _atomic_write_json(path: Path, payload) -> None:
    """Write JSON so a reader never sees half a list.

    Temp file in the same directory, fsync, rename. The rename is what makes
    it atomic on POSIX, and the directory is the same one so it cannot cross
    a filesystem.
    """
    import os
    import tempfile

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    Path(temporary).replace(path)


def cmd_build_performance(settings: Settings, args: argparse.Namespace) -> int:
    """Turn a decisions export plus settlements into the six snapshots.

    Two engines, chosen by what the input is:

    * a **directory** is the shadow's published export, and it is built by the
      streaming engine - one part at a time, resuming from a state store, with
      memory bounded by the batch rather than by the history;
    * a **file** is a hand-carried export with no parts and no cursor, and it is
      built by the whole-history builder, which is also the reference the
      streaming engine is checked against.

    ``--in-memory`` forces the second path for a directory, and refuses to do so
    quietly: that path is what took the host offline at 960,000 rows.
    """
    from weatherquant.performance.backfill import (
        build_performance_snapshots,
        load_decisions_with_watermark,
        load_settlements,
    )

    if args.confirm_empty_retraction and not args.settlements_replace:
        print(
            "--confirm-empty-retraction only means something together with "
            "--settlements-replace: on its own it confirms nothing. Refusing it "
            "keeps the two flags from looking interchangeable, because one of "
            "them can delete every settlement answer and the other cannot.",
            file=sys.stderr,
        )
        return 2

    export = Path(args.export)
    if not export.exists():
        print(f"could not read decisions export: not found: {export}", file=sys.stderr)
        return 2

    settlements: list[dict] = []
    if args.settlements:
        try:
            settlements = load_settlements(args.settlements)
        except (OSError, ValueError) as exc:
            print(f"could not read settlement index: {exc}", file=sys.stderr)
            return 2

    streaming = export.is_dir() and not args.in_memory
    if streaming:
        return _build_performance_streaming(settings, args, settlements)
    if args.state_db and not export.is_dir():
        print(
            f"--state-db was given but {export} is a file, not a directory of "
            "published parts. The streaming engine consumes the shadow's export "
            "parts; drop --state-db to use the in-memory builder on this file.",
            file=sys.stderr,
        )
        return 2
    if export.is_dir() and args.in_memory:
        print(
            "WARNING: --in-memory holds every decision as a Python dict (~6.8 KB "
            "each) and has taken a 16 GB host offline at 960,000 rows. It is the "
            "reference implementation, not the production path.",
            file=sys.stderr,
        )

    try:
        decisions, watermark = load_decisions_with_watermark(export)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"could not read decisions export: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    try:
        written = build_performance_snapshots(
            decisions=decisions,
            settlements=settlements,
            out_dir=out,
            dataset=args.dataset,
            capital_usdc=float(args.capital_usdc),
            max_allocation_fraction=float(args.max_allocation_fraction),
            gap_window_seconds=float(args.gap_window_seconds),
            # Read from the same enumeration as the rows, so the manifest can
            # never advertise a cursor past the last decision it contains. A
            # file input has no cursor and stays null.
            watermark=watermark,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"could not build performance snapshots: {exc}", file=sys.stderr)
        return 2

    print(f"published {len(written)} snapshot(s) to {out} (dataset={args.dataset})")
    for path in written:
        print(f"  {path.name}")
    return 0


def _build_performance_streaming(
    settings: Settings, args: argparse.Namespace, settlements: list[dict]
) -> int:
    """The production path: ingest what is new, then publish one generation."""
    from weatherquant.performance.state import PerformanceStateError, StateMismatch
    from weatherquant.performance.streaming import build_performance_streaming

    state_db = Path(args.state_db) if args.state_db else settings.performance_state_db_path

    def progress(message: str) -> None:
        print(f"  {message}")

    try:
        result = build_performance_streaming(
            export=args.export,
            out_dir=args.out,
            state_db=state_db,
            dataset=args.dataset,
            settlements=settlements,
            settlements_replace=args.settlements_replace,
            confirm_empty_retraction=args.confirm_empty_retraction,
            settlements_source=args.settlements,
            retraction_reason=args.retraction_reason,
            capital_usdc=float(args.capital_usdc),
            max_allocation_fraction=float(args.max_allocation_fraction),
            gap_window_seconds=float(args.gap_window_seconds),
            detail_max_episodes=args.detail_window_episodes,
            detail_window_days=args.detail_window_days,
            memory_limit=args.memory_limit,
            temp_directory=args.temp_dir,
            reset=args.reset,
            ingest=not args.no_ingest,
            publish=not args.no_publish,
            deep_verify=args.verify_parts,
            max_parts_per_run=args.max_parts_per_run,
            progress=progress,
        )
    except StateMismatch as exc:
        print(f"the state store does not match this run: {exc}", file=sys.stderr)
        return 2
    except (PerformanceStateError, ValueError, RuntimeError) as exc:
        print(f"could not build performance snapshots: {exc}", file=sys.stderr)
        return 2

    ingest = result["ingest"]
    if ingest is not None:
        print(
            f"consumed {ingest['rows_consumed']} row(s) from {ingest['parts_consumed']} of "
            f"{ingest['parts_available']} part(s)"
        )
    else:
        print("no ingest requested; rebuilding from the state store")
    print(f"watermark: {result['watermark']}")
    print(f"state store: {state_db}")
    print(f"published {len(result['written'])} snapshot(s) to {args.out} (dataset={args.dataset})")
    print(f"generation: {result['generation_id']}")
    summary = result["summary"]
    print(
        "  episodes {opportunity_episodes}, accept observations {accept_observations}, "
        "win rate {win_rate}".format(
            win_rate=summary["realized_directional_win_rate"], **summary
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
    except Exception as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "db", None):
        settings = settings.model_copy(update={"research_db_path": Path(args.db)})

    handlers = {
        "init": cmd_init,
        "extract-legacy": cmd_extract_legacy,
        "crosscheck": cmd_crosscheck,
        "rules": cmd_rules,
        "fit-models": cmd_fit_models,
        "report": cmd_report,
        "backfill-settlements": cmd_backfill_settlements,
        "build-performance": cmd_build_performance,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())

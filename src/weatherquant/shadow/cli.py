"""Command-line entry point for the shadow engine.

    weatherquant-shadow init      # create/migrate the database, load the models
    weatherquant-shadow run       # run the loop until SIGTERM
    weatherquant-shadow report    # metrics from the shadow database
    weatherquant-shadow export-verify  # reconcile the decision export (engine stopped)
    weatherquant-shadow status    # one-line health summary for a script

Every command is read-only with respect to the collector: it never opens
another engine's database, never writes to the raw archive, and never reaches
for a wallet.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from weatherquant.config import Settings
from weatherquant.shadow.config import ShadowSettings, load_thresholds
from weatherquant.shadow.report import (
    build_report,
    format_report,
    load_report_snapshot,
    snapshot_staleness,
)
from weatherquant.storage.shadow import ShadowDatabase


def _config() -> tuple[Settings, ShadowSettings]:
    settings = Settings()
    shadow = ShadowSettings()
    # The raw archive the shadow tails must be reachable; a path that does not
    # exist is a misconfiguration worth saying out loud at startup rather than
    # an empty feed discovered hours later.
    return settings, shadow


def _load_thresholds(shadow: ShadowSettings):
    return load_thresholds(shadow.shadow_thresholds_path)


def cmd_init(settings: Settings, shadow: ShadowSettings, _args: argparse.Namespace) -> int:
    from weatherquant.storage.migrator import available_migrations
    from weatherquant.storage.shadow import SHADOW_MIGRATIONS_DIR

    thresholds = _load_thresholds(shadow)
    with ShadowDatabase(shadow.shadow_db_path) as db:
        applied = list(db.applied_migrations())
        print(f"shadow database : {db.path}")
        print(f"migrations      : {', '.join(applied) or 'already current'}")
        print(f"available       : {', '.join(m.name for m in available_migrations(SHADOW_MIGRATIONS_DIR))}")
        print(f"tables          : {', '.join(sorted(db.table_counts()))}")

    models_dir = Path(shadow.shadow_models_dir)
    manifest = models_dir / "manifest.json"
    if not manifest.exists():
        print(
            f"\nno model manifest at {manifest}\n"
            "run `weatherquant-research fit-models --pin` first; the engine refuses to "
            "choose a model version at runtime",
            file=sys.stderr,
        )
        return 2
    print(f"models dir      : {models_dir}")
    print(f"thresholds      : {thresholds.version} ({thresholds.basis[:60]}...)")
    print(f"raw archive     : {shadow.collector_raw_dir} (read-only)")
    return 0


def cmd_run(settings: Settings, shadow: ShadowSettings, args: argparse.Namespace) -> int:
    from weatherquant.shadow.runner import RunnerConfig, ShadowRunner

    logging.basicConfig(
        level=getattr(logging, str(getattr(args, "log_level", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        thresholds = _load_thresholds(shadow)
    except (OSError, ValueError) as exc:
        print(f"cannot load thresholds: {exc}", file=sys.stderr)
        return 2

    config = RunnerConfig(settings=settings, shadow=shadow, thresholds=thresholds)
    runner = ShadowRunner(config)

    if getattr(args, "once", False):
        from weatherquant.data.lock import LockError

        try:
            runner.start()
            stats = runner.cycle()
        except LockError as exc:
            # Exit code 4 is the documented single-instance refusal, and
            # `--once` is exactly how an operator (and CI) probes for it.
            # Falling through to the generic handler below would report 1,
            # which reads as a crash rather than as a correct refusal.
            print(f"refused: {exc}", file=sys.stderr)
            return 4
        except Exception as exc:
            print(f"cycle failed: {exc}", file=sys.stderr)
            runner.close(status="failed", reason=str(exc))
            return 1
        print(json.dumps(stats.as_dict(), indent=2, default=str))
        runner.close(status="stopped", reason="--once")
        return 0

    return runner.run_forever()


def cmd_report(settings: Settings, shadow: ShadowSettings, args: argparse.Namespace) -> int:
    """Report from the published snapshot; the database only when offline.

    The engine holds an exclusive lease on shadow.duckdb for its whole life,
    and DuckDB refuses a second process the file *at all* - read-only
    included. So the default path never opens it: the engine computes the
    report from the connection it already has and swaps a JSON file into
    place, and this command reads that.

    `--offline` (or an explicit `--db`) forces the direct read, which is the
    right thing when nothing is running and there is no snapshot to hand.
    """
    explicit_db = getattr(args, "db", None) is not None
    if args.offline or explicit_db:
        return _report_from_database(shadow, args, forced=args.offline or explicit_db)

    snapshot = load_report_snapshot(shadow.shadow_report_path)
    if snapshot is None:
        # Nothing published yet: the engine may simply not have started, in
        # which case the database is free and a direct read is correct.
        return _report_from_database(shadow, args, forced=False)

    stale, age, past_expiry = snapshot_staleness(snapshot)
    if args.json:
        payload = dict(snapshot)
        payload["stale"] = stale
        payload["age_seconds"] = None if age is None else round(age, 3)
        payload["seconds_past_expiry"] = None if past_expiry is None else round(past_expiry, 3)
        payload["snapshot_path"] = str(shadow.shadow_report_path)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(snapshot))
        _print_freshness(snapshot, stale, age, past_expiry, shadow.shadow_report_path)
    return 0


def _print_freshness(
    snapshot: dict, stale: bool, age: float | None, past: float | None, path: Path
) -> None:
    generated = snapshot.get("generated_at_utc", "unknown")
    print(f"\n[snapshot] {path}")
    print(f"  generated at  {generated}")
    if age is None:
        print("  WARNING        generated_at_utc is missing or unparseable - age unknown")
    elif stale:
        print(
            f"  EXPIRED        {age:.0f}s old, {past:.0f}s past the "
            f"{snapshot.get('stale_after_seconds', '?')}s freshness window"
        )
        print("                 these numbers describe the past; the engine may have moved on")
    else:
        print(f"  fresh          {age:.0f}s old")


def _report_from_database(shadow: ShadowSettings, args: argparse.Namespace, *, forced: bool) -> int:
    """Direct read. Only correct when nothing else holds the file."""
    if not shadow.shadow_db_path.exists():
        print(f"no shadow database at {shadow.shadow_db_path}", file=sys.stderr)
        if not forced:
            print("and no published snapshot to read instead", file=sys.stderr)
        return 2
    try:
        with ShadowDatabase(shadow.shadow_db_path) as db:
            report = build_report(db.con, horizon_days=args.horizon)
    except Exception as exc:
        print(f"cannot open {shadow.shadow_db_path}: {exc}", file=sys.stderr)
        print(
            "The shadow engine holds an exclusive lease on that file while it "
            "runs, and DuckDB blocks other processes even read-only. Drop "
            "--offline (and --db) to read the snapshot the engine publishes "
            "at " + str(shadow.shadow_report_path),
            file=sys.stderr,
        )
        return 3
    if args.json:
        payload = dict(report)
        payload["source"] = "offline-db"
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(report))
        print()
        print("[source] offline database read (no snapshot was used)")
    return 0


def cmd_export_verify(settings: Settings, shadow: ShadowSettings, args: argparse.Namespace) -> int:
    """Reconcile the published decision export against the database.

    Opens the shadow database directly, so it only works while the engine is
    stopped - the same lease `report --offline` lives under. The check it
    runs is the one WQN-20 demands of the export: nothing lost (every row at
    or below the cursor is in the parts), nothing twice (sequences strictly
    increase across the whole concatenation, and the counts agree).
    """
    if not shadow.shadow_db_path.exists():
        print(f"no shadow database at {shadow.shadow_db_path}", file=sys.stderr)
        return 2
    export_dir = shadow.shadow_report_path.parent / "decisions-export"
    try:
        with ShadowDatabase(shadow.shadow_db_path) as db:
            from weatherquant.shadow.export import DecisionExporter

            exporter = DecisionExporter(db.con, export_dir)
            report = exporter.reconcile()
            report.update(exporter.verify())
            report["export_dir"] = str(export_dir)
    except Exception as exc:
        print(
            f"cannot reconcile the export: {exc}\n"
            "The shadow engine holds an exclusive lease on the database while it "
            "runs; stop it first, or read state.json for a cache-only answer.",
            file=sys.stderr,
        )
        return 3
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        for key in sorted(report):
            print(f"{key:32} {report[key]}")
        print()
        print("consistent" if report.get("consistent") else "INCONSISTENT - see first_divergence")
    return 0 if report.get("consistent") else 1


def cmd_status(settings: Settings, shadow: ShadowSettings, args: argparse.Namespace) -> int:
    """Exit code mirrors the health probe: 0 healthy/degraded, 1 unhealthy."""
    from weatherquant.shadow.health import evaluate_shadow_health

    verdict = evaluate_shadow_health(shadow.shadow_health_dir / "heartbeat.json")
    if args.json:
        print(
            json.dumps(
                {"status": verdict.status, "reasons": list(verdict.reasons), "exit_code": verdict.exit_code},
                indent=2,
            )
        )
    else:
        suffix = f" ({'; '.join(verdict.reasons)})" if verdict.reasons else ""
        print(f"{verdict.status}{suffix}")
    return verdict.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weatherquant-shadow",
        description="Shadow trading engine: read-only decision observation over live market data.",
    )
    parser.add_argument("--db", help="shadow database path (overrides WQ_SHADOW_DB_PATH)")
    parser.add_argument("--raw", help="collector raw archive path (overrides WQ_COLLECTOR_RAW_DIR)")
    parser.add_argument("--models", help="models directory (overrides WQ_SHADOW_MODELS_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create or migrate the shadow database and check the models")

    p = sub.add_parser("run", help="run the shadow loop until SIGTERM")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--log-level", default="INFO")

    p = sub.add_parser(
        "report",
        help="metrics from the snapshot the engine publishes (never the live database)",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--horizon", type=int, default=7)
    p.add_argument(
        "--offline",
        action="store_true",
        help="read shadow.duckdb directly. Only works when the engine is stopped; "
        "while it runs, DuckDB refuses the file to any other process.",
    )

    p = sub.add_parser(
        "export-verify",
        help="reconcile the decision export against the database (engine must be stopped)",
    )
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="health summary; exit code matches the probe")
    p.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings, shadow = _config()
    except Exception as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "db", None):
        shadow = shadow.model_copy(update={"shadow_db_path": Path(args.db)})
    if getattr(args, "raw", None):
        shadow = shadow.model_copy(update={"collector_raw_dir": Path(args.raw)})
    if getattr(args, "models", None):
        shadow = shadow.model_copy(update={"shadow_models_dir": Path(args.models)})

    handlers = {
        "init": cmd_init,
        "run": cmd_run,
        "report": cmd_report,
        "export-verify": cmd_export_verify,
        "status": cmd_status,
    }
    return handlers[args.command](settings, shadow, args)


if __name__ == "__main__":
    raise SystemExit(main())

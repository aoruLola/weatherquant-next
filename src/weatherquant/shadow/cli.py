"""Command-line entry point for the shadow engine.

    weatherquant-shadow init      # create/migrate the database, load the models
    weatherquant-shadow run       # run the loop until SIGTERM
    weatherquant-shadow report    # metrics from the shadow database
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
from weatherquant.shadow.report import build_report, format_report
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
    if not shadow.shadow_db_path.exists():
        print(f"no shadow database at {shadow.shadow_db_path}", file=sys.stderr)
        return 2
    with ShadowDatabase(shadow.shadow_db_path) as db:
        report = build_report(db.con, horizon_days=args.horizon)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(format_report(report))
    return 0


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

    p = sub.add_parser("report", help="metrics from the shadow database")
    p.add_argument("--json", action="store_true")
    p.add_argument("--horizon", type=int, default=7)

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
        "status": cmd_status,
    }
    return handlers[args.command](settings, shadow, args)


if __name__ == "__main__":
    raise SystemExit(main())

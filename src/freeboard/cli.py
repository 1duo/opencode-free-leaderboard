from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .config import Settings, save_credential, week
from .db import DB
from .discovery import discover
from .grading import DockerGrader
from .panels import prepare
from .publication import export, publish, refresh_publications
from .runner import Runner
from .scheduling import install
from .scoring import snapshot


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Resumable benchmarks through the installed OpenCode client")
    root.add_argument("--state", type=Path, default=Settings().state)
    source_checkout = Path(__file__).resolve().parents[2]
    root.add_argument("--checkout", type=Path, default=source_checkout if (source_checkout / "web").is_dir() else Path.cwd())
    commands = root.add_subparsers(dest="command", required=True)
    for name in ["discover", "export", "status", "daily"]:
        commands.add_parser(name)
    for name in ["pilot", "run"]:
        sub = commands.add_parser(name)
        sub.add_argument("--limit", type=int, help="Maximum jobs processed in this invocation")
        sub.add_argument("--season", help="Evaluate a validated candidate before promotion")
        sub.add_argument('--model', action='append', help='Select exact verified free IDs for explicit public evaluations')
        if name == 'run':
            sub.add_argument('--public-only', action='store_true', help='Finish 20 LiveBench and 40 LiveCodeBench questions in a separate unranked public screen')
        else:
            sub.add_argument('--all-models', action='store_true', help='Check every currently verified free model')
    prepare_parser = commands.add_parser("prepare-panels")
    prepare_parser.add_argument("--promote", action="store_true")
    prepare_parser.add_argument('--public-only', action='store_true', help='Prepare a separate unranked public compatibility pilot while GPQA access is pending')
    prepare_parser.add_argument('--season', help='Validate a previously frozen panel without downloading or resampling datasets')
    confirmation = commands.add_parser("confirm")
    confirmation.add_argument("model")
    publisher = commands.add_parser("publish")
    publisher.add_argument("--create-repository", action="store_true")
    auth = commands.add_parser("auth")
    auth.add_argument("provider", choices=["zen", "hf"])
    schedule = commands.add_parser("schedule")
    schedule.add_argument("action", choices=["install"])
    return root


def daily_work(runner: Runner) -> dict:
    try:
        result = runner.run()
    except RuntimeError as exc:
        result = {'blocked': str(exc)}
    full = runner.db.one("SELECT id FROM seasons WHERE active=1 AND validated=1 AND COALESCE(json_extract(manifest,'$.partial'),0)=0")
    public = runner.db.one("SELECT * FROM seasons WHERE validated=1 AND json_extract(manifest,'$.partial')=1 ORDER BY created_at DESC LIMIT 1")
    if result.get('blocked') and not full and public:
        from .public_run import run_public
        runner.active_season = lambda: public
        result['public_pilot'] = runner.run(pilot=True, all_models=True)
        result['public_screen'] = run_public(runner, public)
    return result


def main() -> None:
    args = parser().parse_args()
    settings = Settings(state=args.state)
    checkout = args.checkout.resolve()
    settings.initialize(checkout)
    try:
        if args.command == "auth":
            save_credential(args.provider, getpass.getpass(f"{args.provider} credential (stored in Keychain): "))
            print("Credential saved in macOS Keychain")
            return
        if args.command == 'schedule':
            # RunAtLoad may start immediately; do not hold the runner lock during bootstrap.
            print(json.dumps({'launch_agent': str(install(settings, checkout)),
                              'schedule': '09:15 local time daily and at login'}, indent=2))
            return
        db = DB(settings.state)
        with db.locked():
            runner = Runner(db, settings, checkout)
            if args.command == "discover":
                result = discover(db, runner.budget)
            elif args.command == "status":
                refresh_publications(db, settings, checkout)
                report = snapshot(db, settings)
                result = {"state": str(settings.state), "discovery_ok": report.discovery_ok,
                          "blockers": report.blockers, "budget": report.budget,
                          "queue_size": report.queue_size,
                          'pilots': [p.model_dump() for p in report.pilots],
                          'public_screens': [p.model_dump() for p in report.public_screens],
                          "publication": db.one("SELECT snapshot_id,created_at,commit_sha,deployment_status FROM publications ORDER BY id DESC LIMIT 1"),
                          "models": [{"id": r.model_id, "status": r.availability, "cap_verified": r.cap_verified,
                                      "reasoning_setting": r.reasoning_setting, "reasoning_variant": r.reasoning_variant}
                                     for r in report.rows if r.tier == "screen"]}
            elif args.command == "prepare-panels":
                if args.public_only and args.promote:
                    raise ValueError('A public-only compatibility pilot cannot be promoted')
                if args.season:
                    existing = db.one('SELECT * FROM seasons WHERE id=?', (args.season,))
                    if not existing:
                        raise ValueError('Unknown frozen season')
                    result = {'season': args.season}
                else:
                    result = prepare(db, settings, checkout, args.public_only)
                season = db.one("SELECT * FROM seasons WHERE id=?", (result["season"],))
                manifest = json.loads(season["manifest"])
                grader = DockerGrader(settings)
                image = grader.build(manifest, checkout)
                grader.validate(image)
                manifest["grader_image"] = image
                db.execute("UPDATE seasons SET validated=1,manifest=? WHERE id=?", (json.dumps(manifest), season["id"]))
                active = db.one("SELECT * FROM seasons WHERE active=1")
                if manifest.get('partial') and args.promote:
                    raise ValueError('A public-only compatibility pilot cannot be promoted')
                if not manifest.get('partial') and (not active or args.promote):
                    if active and active["id"] != season["id"]:
                        for model in runner.models():
                            if not json.loads(model["profile"]).get("cap_verified"):
                                continue
                            if not db.one("SELECT id FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind='screen' AND completed_at IS NOT NULL",
                                          (model["id"], model["epoch"], season["id"])):
                                raise RuntimeError("Candidate screens must complete for every runnable model before promotion")
                    with db.conn:
                        db.conn.execute("UPDATE seasons SET active=0")
                        db.conn.execute("UPDATE seasons SET active=1 WHERE id=?", (season["id"],))
                result["validated"] = True
            elif args.command in {"pilot", "run"}:
                if args.season:
                    candidate = db.one("SELECT * FROM seasons WHERE id=? AND validated=1", (args.season,))
                    if not candidate:
                        raise ValueError("Unknown or unvalidated candidate season")
                    runner.active_season = lambda: candidate
                public = getattr(args, 'public_only', False)
                if args.command == 'run' and args.model and not public:
                    raise ValueError('Explicit model screen selection requires --public-only')
                if public and not args.season:
                    raise ValueError('Select a validated public season with --season')
                if public:
                    from .public_run import run_public
                    result = run_public(runner, runner.active_season(), args.model, args.limit)
                else:
                    result = runner.run(pilot=args.command == "pilot", limit=args.limit,
                                        pilot_models=getattr(args, 'model', None), all_models=getattr(args, 'all_models', False))
            elif args.command == "confirm":
                result = runner.confirm(args.model)
            elif args.command == "export":
                result = export(db, settings, checkout)
            elif args.command == "publish":
                result = publish(db, settings, checkout, args.create_repository)
            else:  # daily
                refresh_publications(db, settings, checkout)
                result = daily_work(runner)
                # Avoid identical publication churn while required setup is missing.
                if result.get("blocked"):
                    result["local_export"] = export(db, settings, checkout)
                    progressed = any(result.get(k, {}).get('processed', 0) for k in ['public_pilot', 'public_screen'])
                    if progressed or not db.one("SELECT id FROM publications WHERE created_at>=?", (week(),)):
                        result["publication"] = publish(db, settings, checkout)
                else:
                    result["publication"] = publish(db, settings, checkout)
            db.backup()
            print(json.dumps(result, indent=2, default=str))
    except KeyboardInterrupt:
        print(json.dumps({'interrupted': True, 'next': 'Resume the same command; durable work is retained'}), file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Dataset authentication failures can include request URLs; do not print raw SDK errors.
        safe = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else f"{type(exc).__name__}: operation failed; check prerequisites and official source availability"
        print(json.dumps({"error": safe}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

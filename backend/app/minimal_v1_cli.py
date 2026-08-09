from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.database import SessionLocal
from app.core.database_identity import REPOSITORY_ROOT
from app.services.forward_paper_validation import ForwardPaperValidationRunner
from app.services.minimal_v1 import MinimalV1Facade


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal V1 read-compatible research facade")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("datasets")
    commands.add_parser("strategies")
    runs = commands.add_parser("runs")
    runs.add_argument("run_id", nargs="?")
    reproduce = commands.add_parser("reproduce")
    reproduce.add_argument("run_id", nargs="?")
    reproduce.add_argument("--output-dir", type=Path)
    commands.add_parser("forward-status")
    forward_start = commands.add_parser("forward-start")
    forward_start.add_argument("--mode", choices=("forward", "replay"), default="forward")
    forward_start.add_argument("--start-date", type=datetime.fromisoformat)
    forward_start.add_argument("--end-date", type=datetime.fromisoformat)
    forward_start.add_argument("--starting-cash", type=Decimal, default=Decimal("1000000.00"))
    forward_start.add_argument("--poll-seconds", type=int, default=60)
    forward_start.add_argument("--resume-emergency", action="store_true")
    commands.add_parser("forward-stop")
    emergency = commands.add_parser("forward-emergency")
    emergency.add_argument("reason")
    commands.add_parser("forward-portfolio")
    commands.add_parser("forward-decision")
    commands.add_parser("forward-reconcile")
    return parser


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def main() -> int:
    args = build_parser().parse_args()
    with SessionLocal() as db:
        facade = MinimalV1Facade(db)
        if args.command == "status":
            payload: Any = facade.safety_status()
        elif args.command == "datasets":
            payload = [item.model_dump(mode="json") for item in facade.active_datasets()]
        elif args.command == "strategies":
            payload = [item.model_dump(mode="json") for item in facade.registered_strategies()]
        elif args.command == "runs":
            payload = (
                facade.historical_run(args.run_id)
                if args.run_id
                else [item.model_dump(mode="json") for item in facade.historical_runs()]
            )
        elif args.command == "reproduce":
            output = args.output_dir or (
                REPOSITORY_ROOT
                / "reports"
                / "minimal_v1"
                / f"reproduction-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            )
            reproduced = facade.reproduce_archived_run(output, run_id=args.run_id)
            payload = {
                "summary": reproduced["summary"].model_dump(mode="json"),
                "metric_differences": reproduced["metric_differences"],
                "output_dir": str(reproduced["output_dir"]),
                "artifacts": {name: str(path) for name, path in reproduced["artifacts"].items()},
                "json_sha256": reproduced["json_sha256"],
                "trade_rows": reproduced["trade_rows"],
            }
        else:
            runner = ForwardPaperValidationRunner(db)
            if args.command == "forward-status":
                payload = runner.status()
            elif args.command == "forward-start":
                with runner.lock:
                    if args.mode == "replay":
                        if args.start_date is None or args.end_date is None:
                            raise ValueError("Replay requires --start-date and --end-date")
                        payload = runner.run_replay(
                            args.start_date.date(),
                            args.end_date.date(),
                            starting_cash=args.starting_cash,
                            resume_emergency=args.resume_emergency,
                        )
                    else:
                        runner.run_forever(
                            starting_cash=args.starting_cash,
                            poll_seconds=args.poll_seconds,
                            resume_emergency=args.resume_emergency,
                        )
                        payload = runner.status()
            elif args.command == "forward-stop":
                payload = runner.stop()
            elif args.command == "forward-emergency":
                payload = runner.emergency_halt(args.reason)
            elif args.command == "forward-portfolio":
                payload = runner.portfolio()
            elif args.command == "forward-decision":
                payload = runner.latest_decision()
            else:
                payload = runner.reconcile()
        print(_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

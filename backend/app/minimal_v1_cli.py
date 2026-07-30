from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.database import SessionLocal
from app.core.database_identity import REPOSITORY_ROOT
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
        else:
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
        print(_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

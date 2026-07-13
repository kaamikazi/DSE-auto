from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database import SessionLocal
from app.services.infrastructure_incidents import (
    run_all_controlled_exercises,
    run_controlled_exercise,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run fail-closed infrastructure incident exercises"
    )
    parser.add_argument("exercise", nargs="?", default="all")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports/incidents")
    args = parser.parse_args()
    with SessionLocal() as db:
        result: object = (
            run_all_controlled_exercises(db, args.output_dir)
            if args.exercise == "all"
            else run_controlled_exercise(db, args.exercise, args.output_dir)
        )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

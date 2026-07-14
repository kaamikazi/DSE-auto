from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.services.runtime_observation import evaluate_runtime_observation  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a measured low-memory runtime")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8-sig"))
    result = evaluate_runtime_observation(
        source["samples"],
        project_footprint_gib=float(source["project_footprint_gib"]),
        database_healthy=bool(source["database_healthy"]),
        audit_valid=bool(source["audit_valid"]),
    )
    payload = {"stage": source["stage"], **result}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    raise SystemExit(0 if result["passed"] else 2)


if __name__ == "__main__":
    main()

from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.recovery_bundle import create_recovery_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a secret-free paper-trading recovery bundle"
    )
    parser.add_argument(
        "--database", type=Path, default=ROOT / "backend/data/dse_autotrader.db"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or (
        ROOT
        / "reports/recovery"
        / f"dse-paper-recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    result = create_recovery_bundle(
        ROOT,
        args.database,
        output,
        evidence_roots=(ROOT / "data/audit_archives", ROOT / "reports/campaigns"),
    )
    print(json.dumps(result, indent=2, default=str))
    raise SystemExit(0 if result["passed"] else 2)


if __name__ == "__main__":
    main()

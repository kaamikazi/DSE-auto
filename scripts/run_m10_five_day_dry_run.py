from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database import Base  # noqa: E402
from app.models import ValidationCampaign  # noqa: E402
from app.services.real_market_operations import run_five_day_workflow_dry_run  # noqa: E402


def _write_evidence(path: Path, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run isolated Milestone 10 workflow dry-run"
    )
    parser.add_argument("--start-date", default="2026-01-05")
    parser.add_argument(
        "--output",
        default=str(ROOT / "reports" / "real_market_dry_run" / "m10_five_day.json"),
    )
    args = parser.parse_args()
    start_date = date.fromisoformat(args.start_date)
    with tempfile.TemporaryDirectory(prefix="dse-m10-dry-run-") as directory:
        database = Path(directory) / "dry-run.db"
        engine = create_engine(f"sqlite:///{database.as_posix()}")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            campaign = ValidationCampaign(
                id="m10-workflow-dry-run",
                name="real-market operations workflow dry-run",
                start_date=start_date,
                planned_end_date=start_date,
                approved_symbols=["GP"],
                approved_strategies=["dry-run-governed-strategy"],
                starting_capital=Decimal("1000000"),
                risk_profile={"paper_only": True},
                data_source_policy={"classification": "test_data"},
                timestamp_trust_requirement="operator_attested",
                fill_model="pessimistic",
                benchmark="DSEX",
                operator_notes="Isolated deterministic workflow verification only",
                state="completed",
                active_rule_set_id="dry-run-rule-set",
                active_fee_profile_id="dry-run-fee-profile",
                evidence_class="synthetic",
                daily_reviewer_assignments={"default": "test-reviewer"},
            )
            db.add(campaign)
            db.commit()
            result = run_five_day_workflow_dry_run(db, campaign, start_date=start_date)
        engine.dispose()
    evidence = {
        **result,
        "generated_at": datetime.now(UTC).isoformat(),
        "isolated_temporary_database_removed": True,
        "source_classification": "historical or test data",
        "profitability_claimed": False,
        "live_trading_readiness_claimed": False,
    }
    output = Path(args.output).resolve()
    digest = _write_evidence(output, evidence)
    print(json.dumps({**evidence, "path": str(output), "sha256": digest}, indent=2))
    return 0 if result["real_market_qualifying_days"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

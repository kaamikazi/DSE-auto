from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    PaperSession,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.evidence_workspace import (  # noqa: E402
    generate_scoped_approval_pack,
    initialize_default_cases,
    workspace_summary,
)

SCOPES = (
    "rules",
    "fees",
    "risk_limits",
    "real_dataset",
    "ma_crossover_promotion",
    "campaign_creation",
)


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError(
            "Evidence workspace initialization requires paper-only safety settings"
        )


def _activation_counts(db: Session) -> dict[str, int]:
    return {
        "campaigns": int(
            db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0
        ),
        "sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "transactions_fills": int(
            db.scalar(select(func.count()).select_from(Transaction)) or 0
        ),
        "promoted_strategies": int(
            db.scalar(
                select(func.count())
                .select_from(StrategyRegistration)
                .where(
                    StrategyRegistration.lifecycle_state.in_(
                        ["paper_candidate", "paper_active"]
                    )
                )
            )
            or 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the review-only Milestone 11 evidence workspace"
    )
    parser.add_argument("--collector", default="operator")
    parser.add_argument("--reviewer")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "evidence_workspace" / "approval_packs",
    )
    args = parser.parse_args()
    _assert_safety()
    with SessionLocal() as db:
        before = _activation_counts(db)
        cases = initialize_default_cases(
            db, collector=args.collector, reviewer=args.reviewer
        )
        packs = [
            generate_scoped_approval_pack(
                db,
                scope=scope,
                output_dir=args.output,
                generated_by=args.collector,
            )
            for scope in SCOPES
        ]
        after = _activation_counts(db)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise RuntimeError(
                f"Fail-closed: evidence initialization changed trading state: {delta}"
            )
        summary = workspace_summary(db)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "approval_packs": [
                    {
                        "scope": pack.scope,
                        "path": pack.output_path,
                        "sha256": pack.pack_hash,
                    }
                    for pack in packs
                ],
                "preexisting_operational_records": before,
                "post_initialization_operational_records": after,
                "milestone_11_activation_delta": delta,
                "audit_valid": summary["audit_valid"],
                "qualification": summary["qualification"],
                "automatic_activation": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

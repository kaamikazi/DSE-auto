from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuthoritativeEvidence,
    GovernanceItemApproval,
    ResearchDataset,
    ReviewerInvitation,
    StrategyRegistration,
)
from app.services.authoritative_evidence import (  # noqa: E402
    calibrate_risk_limits,
    create_approval_matrix,
    pre_campaign_state,
    promotion_readiness,
)


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _write(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate fail-closed pre-campaign approval pack"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "governance")
    args = parser.parse_args()
    rules = _load(ROOT / "reports" / "governance" / "rule_verification_table.json")
    fees = _load(ROOT / "reports" / "governance" / "fee_verification_table.json")
    risks = _load(ROOT / "reports" / "governance" / "risk_limit_approval_pack.json")
    with SessionLocal() as db:
        registration = db.scalar(
            select(StrategyRegistration).where(
                StrategyRegistration.strategy_id == "ma_crossover",
                StrategyRegistration.version == "1.0.0",
            )
        )
        if registration is None:
            raise RuntimeError(
                "ma_crossover@1.0.0 operational research registration is missing"
            )
        rule_rows = create_approval_matrix(
            db,
            approval_type="rule",
            draft_version="dse-paper-rules-v1-draft",
            items=rules["items"],
        )
        fee_rows = create_approval_matrix(
            db, approval_type="fee", draft_version="1.0-draft", items=fees["items"]
        )
        risk_rows = create_approval_matrix(
            db,
            approval_type="risk",
            draft_version="pre-campaign-risk-limits-draft",
            items=risks["limits"],
        )
        calibration = calibrate_risk_limits(
            db,
            strategy_registration_id=registration.id,
            proposed_limits=risks["limits"],
        )
        readiness = promotion_readiness(db, registration)
        evidence = list(db.scalars(select(AuthoritativeEvidence)))
        datasets = list(db.scalars(select(ResearchDataset)))
        reviewers = list(db.scalars(select(ReviewerInvitation)))

        def approval_view(row: GovernanceItemApproval) -> dict[str, Any]:
            return {
                "item": row.item_key,
                "current_draft": row.current_draft,
                "linked_evidence": row.evidence_ids,
                "conflicts": row.conflicts,
                "missing_evidence": row.missing_evidence,
                "verification_status": row.verification_status,
                "proposed_approved_value": row.proposed_value,
                "approval_status": row.approval_status,
                "effective_date": row.effective_date,
                "reviewer_independence": row.reviewer_independence,
                "conservative_fallback": row.conservative_fallback,
            }

        state = pre_campaign_state(db)
        pack = {
            "classification": "authoritative_evidence_pre_campaign_readiness",
            "safety": {
                "trading_mode": "paper",
                "live_trading_enabled": False,
                "broker_adapter": "disabled",
                "automatic_activation": False,
            },
            "evidence_registry": {
                "total": len(evidence),
                "items": [
                    {
                        "id": item.id,
                        "category": item.category,
                        "title": item.title,
                        "status": item.verification_status,
                        "confidence": item.confidence,
                        "reviewer_independence": item.reviewer_independence,
                        "file_hash": item.file_hash,
                        "audit_event_ids": item.audit_event_ids,
                    }
                    for item in evidence
                ],
                "unavailable_external_evidence": "missing; not fabricated",
            },
            "rule_approval_matrix": [approval_view(row) for row in rule_rows],
            "fee_approval_matrix": [approval_view(row) for row in fee_rows],
            "risk_limit_calibration": calibration.report,
            "risk_approval_matrix": [approval_view(row) for row in risk_rows],
            "reviewer_independence": {
                "proposed_reviewer": "operator-reviewer",
                "classification_when_operator": "non_independent",
                "invitations": [
                    {
                        "identity": item.reviewer_identity,
                        "state": item.state,
                        "independence": item.independence,
                    }
                    for item in reviewers
                ],
            },
            "research_dataset": {
                "status": "missing" if not datasets else "submitted",
                "datasets": [
                    {
                        "id": item.id,
                        "dataset_hash": item.dataset_hash,
                        "status": item.status,
                        "timestamp_trust": item.timestamp_trust,
                        "quality": item.quality_report,
                    }
                    for item in datasets
                ],
                "qualification_days": 0,
            },
            "ma_crossover_research": {
                "status": "workflow_complete_real_data_missing",
                "operational_state": registration.lifecycle_state,
                "code_hash": registration.code_hash,
                "parameter_hash": registration.parameters.get("parameter_set_hash"),
                "real_data_result": None,
                "profitability_claim": False,
                "promotion_authorized": False,
            },
            "promotion_readiness": {
                "status": readiness.status,
                "checks": readiness.checks,
                "missing_items": readiness.missing_items,
                "automatic_transition": False,
            },
            "unresolved_conflicts": [
                item.id
                for item in evidence
                if item.verification_status == "conflicting"
            ],
            "exact_missing_approvals": readiness.missing_items,
            "campaign_draft": {
                "created": False,
                "active": False,
                "qualification": "0/60",
                "simulated_capital_bdt": 1000000,
                "symbols_research_only": ["GP", "ACI", "BRACBANK", "DSEX"],
            },
            "proof_no_operational_activation": state["proof_no_activation"],
            "audit_valid": state["audit_valid"],
        }
    json_path = args.output / "authoritative_pre_campaign_approval.json"
    json_bytes = json.dumps(pack, indent=2, sort_keys=True, default=str).encode()
    json_hash = _write(json_path, json_bytes)
    calibration_path = args.output / "risk_limit_calibration.json"
    calibration_hash = _write(
        calibration_path,
        json.dumps(calibration.report, indent=2, sort_keys=True).encode(),
    )
    markdown = (
        "# Authoritative Evidence Pre-Campaign Approval\n\n"
        "Status: **EVIDENCE INCOMPLETE — NO ACTIVATION AUTHORIZED**\n\n"
        f"- Evidence registry items: {len(evidence)}\n"
        f"- Rules approved: 0/{len(rule_rows)}\n"
        f"- Fees approved: 0/{len(fee_rows)}\n"
        f"- Risk limits approved: 0/{len(risk_rows)}\n"
        "- Real-market research dataset: missing\n"
        f"- ma_crossover promotion readiness: {readiness.status}\n"
        "- Campaign: not created; qualification 0/60\n"
        "- Reviewer operator-reviewer: non-independent whenever also operator\n"
        "- PAPER TRADING; LIVE TRADING DISABLED; BROKER ADAPTER DISABLED\n\n"
        "No external evidence or performance result has been fabricated. Each rule, fee, risk limit, dataset, reviewer decision, and strategy promotion requires its own future approval.\n\n"
        f"JSON SHA-256: `{json_hash}`\n"
    ).encode()
    markdown_path = args.output / "authoritative_pre_campaign_approval.md"
    markdown_hash = _write(markdown_path, markdown)
    print(
        json.dumps(
            {
                "json": {"path": str(json_path), "sha256": json_hash},
                "markdown": {"path": str(markdown_path), "sha256": markdown_hash},
                "calibration": {
                    "path": str(calibration_path),
                    "sha256": calibration_hash,
                },
                "readiness": readiness.status,
                "campaign_created": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import CampaignDay, ImportBatch, ValidationCampaign
from app.risk.kill_switch import set_state
from app.services.attested_imports import (
    activate_attested_import,
    preview_attested_import,
    rollback_attested_import,
)
from app.services.audit import audit_status, verify_audit_chain
from app.services.campaigns import (
    campaign_view,
    create_campaign,
    evaluate_campaign_readiness,
    start_campaign_day,
    transition_campaign,
)
from app.services.evidence_review import submit_review
from app.services.portfolio_imports import (
    activate_real_portfolio,
    preview_real_portfolio,
)
from app.services.qualification import calculate_qualification
from app.services.real_market_operations import (
    complete_real_market_day,
    generate_weekly_report,
    run_five_day_workflow_dry_run,
)


def _json_file(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON input must be an object")
    return payload


def _campaign(db: Session, campaign_id: str) -> ValidationCampaign:
    item = db.get(ValidationCampaign, campaign_id)
    if item is None:
        raise ValueError("Campaign not found")
    return item


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Fail-closed real-market paper operations"
    )
    sub = root.add_subparsers(dest="command", required=True)
    create = sub.add_parser("campaign-create")
    create.add_argument("--config", required=True)
    premarket = sub.add_parser("premarket-check")
    premarket.add_argument("campaign_id")
    premarket.add_argument("--market-date", required=True)
    premarket.add_argument("--acknowledgement", required=True)
    start = sub.add_parser("session-start")
    start.add_argument("campaign_id")
    start.add_argument("--market-date", required=True)
    start.add_argument("--acknowledgement", required=True)
    state = sub.add_parser("campaign-state")
    state.add_argument("campaign_id")
    state.add_argument(
        "action",
        choices=["await-data", "ready", "pause", "resume", "invalidate", "archive"],
    )
    state.add_argument("--reason", required=True)
    emergency = sub.add_parser("emergency-stop")
    emergency.add_argument("--reason", required=True)
    preview = sub.add_parser("data-import-preview")
    preview.add_argument("campaign_id")
    preview.add_argument("file")
    preview.add_argument("--kind", required=True)
    preview.add_argument("--market-date", required=True)
    preview.add_argument("--attestation", required=True)
    activate = sub.add_parser("data-activate")
    activate.add_argument("batch_id")
    activate.add_argument("--approval", required=True)
    rollback = sub.add_parser("data-rollback")
    rollback.add_argument("batch_id")
    rollback.add_argument("--reason", required=True)
    eod = sub.add_parser("eod-run")
    eod.add_argument("campaign_id")
    eod.add_argument("--market-date", required=True)
    eod.add_argument("--backup-evidence", required=True)
    review = sub.add_parser("day-review")
    review.add_argument("review_id")
    review.add_argument("--reviewer", required=True)
    review.add_argument(
        "--decision",
        choices=["accepted", "concerns_found", "rejected", "requires_rerun"],
        required=True,
    )
    review.add_argument("--checklist", required=True)
    review.add_argument("--comments", required=True)
    weekly = sub.add_parser("weekly-report")
    weekly.add_argument("campaign_id")
    qualify = sub.add_parser("qualification-status")
    qualify.add_argument("campaign_id")
    dry = sub.add_parser("five-day-dry-run")
    dry.add_argument("campaign_id")
    dry.add_argument("--start-date", required=True)
    portfolio = sub.add_parser("portfolio-preview")
    portfolio.add_argument("file")
    portfolio.add_argument("--statement-date", required=True)
    portfolio.add_argument("--source-description", required=True)
    portfolio.add_argument("--attestation", required=True)
    portfolio_activate = sub.add_parser("portfolio-activate")
    portfolio_activate.add_argument("file")
    portfolio_activate.add_argument("--statement-date", required=True)
    portfolio_activate.add_argument("--source-description", required=True)
    portfolio_activate.add_argument("--attestation", required=True)
    sub.add_parser("audit-verify")
    return root


def main() -> None:
    args = parser().parse_args()
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise SystemExit("Paper-only safety configuration failed")
    with SessionLocal() as db:
        if args.command == "campaign-create":
            payload = _json_file(args.config)
            payload["starting_capital"] = Decimal(str(payload["starting_capital"]))
            result = campaign_view(create_campaign(db, **payload))
        elif args.command == "premarket-check":
            result = evaluate_campaign_readiness(
                db,
                _campaign(db, args.campaign_id),
                settings,
                date.fromisoformat(args.market_date),
                operator_acknowledgement=args.acknowledgement,
            )
        elif args.command == "session-start":
            day = start_campaign_day(
                db,
                _campaign(db, args.campaign_id),
                settings,
                date.fromisoformat(args.market_date),
                operator_acknowledgement=args.acknowledgement,
            )
            result = {
                "day_id": day.id,
                "state": day.state,
                "session_id": day.session_id,
            }
        elif args.command == "campaign-state":
            target = {
                "await-data": "awaiting_data",
                "ready": "ready",
                "pause": "paused",
                "resume": "active",
                "invalidate": "invalidated",
                "archive": "archived",
            }[args.action]
            result = campaign_view(
                transition_campaign(
                    db, _campaign(db, args.campaign_id), target, args.reason
                )
            )
        elif args.command == "emergency-stop":
            result = {
                "state": set_state(
                    db, "emergency_stop", args.reason, actor="operator"
                ).state
            }
        elif args.command == "data-import-preview":
            path = Path(args.file)
            result = preview_attested_import(
                db,
                filename=path.name,
                raw=path.read_bytes(),
                import_kind=args.kind,
                market_date=date.fromisoformat(args.market_date),
                operator_attestation=args.attestation,
                raw_dir=ROOT / "data" / "raw_imports",
                campaign_id=args.campaign_id,
            )
        elif args.command in {"data-activate", "data-rollback"}:
            batch = db.get(ImportBatch, args.batch_id)
            if batch is None:
                raise ValueError("Import batch not found")
            if args.command == "data-activate":
                result = {
                    "batch_id": activate_attested_import(db, batch, args.approval).id,
                    "status": batch.status,
                }
            else:
                result = {
                    "batch_id": rollback_attested_import(db, batch, args.reason).id,
                    "status": batch.status,
                }
        elif args.command == "eod-run":
            campaign = _campaign(db, args.campaign_id)
            market_date = date.fromisoformat(args.market_date)
            eod_day = db.scalar(
                select(CampaignDay).where(
                    CampaignDay.campaign_id == campaign.id,
                    CampaignDay.market_date == market_date,
                )
            )
            if eod_day is None:
                raise ValueError("Campaign day not found")
            result = complete_real_market_day(
                db,
                campaign,
                eod_day,
                settings,
                backup_evidence=_json_file(args.backup_evidence),
                evidence_root=ROOT / "reports" / "real_market",
            )
        elif args.command == "day-review":
            from app.models import EvidenceReview

            review = db.get(EvidenceReview, args.review_id)
            if review is None:
                raise ValueError("Review not found")
            checklist = _json_file(args.checklist)
            result_review = submit_review(
                db,
                review,
                reviewer=args.reviewer,
                reviewer_role="reviewer",
                target_state=args.decision,
                data_quality_verdict="pass"
                if args.decision == "accepted"
                else "concern",
                strategy_behavior_verdict="pass"
                if args.decision == "accepted"
                else "concern",
                risk_engine_verdict="pass",
                execution_model_verdict="pass"
                if args.decision == "accepted"
                else "concern",
                incidents_reviewed=[],
                comments=args.comments,
                approval_decision=args.decision,
                review_checklist={
                    str(key): bool(value) for key, value in checklist.items()
                },
                concerns=[] if args.decision == "accepted" else [args.comments],
                linked_evidence_hashes=[review.evidence_pack_hash],
            )
            result = {"review_id": result_review.id, "state": result_review.state}
        elif args.command == "weekly-report":
            result = generate_weekly_report(
                db,
                _campaign(db, args.campaign_id),
                output_root=ROOT / "reports" / "real_market",
            )
        elif args.command == "qualification-status":
            snapshot = calculate_qualification(
                db, args.campaign_id, qualification_scope="real_market"
            )
            result = {
                "counts": snapshot.counts,
                "remaining": snapshot.remaining_qualifying_days,
                "qualifying": snapshot.qualifying,
                "failure_reasons": snapshot.failure_reasons,
            }
        elif args.command == "five-day-dry-run":
            result = run_five_day_workflow_dry_run(
                db,
                _campaign(db, args.campaign_id),
                start_date=date.fromisoformat(args.start_date),
            )
        elif args.command in {"portfolio-preview", "portfolio-activate"}:
            path = Path(args.file)
            call = (
                preview_real_portfolio
                if args.command == "portfolio-preview"
                else activate_real_portfolio
            )
            value = call(
                db,
                path.name,
                path.read_bytes(),
                statement_date=args.statement_date,
                source_description=args.source_description,
                attestation=args.attestation,
            )
            result = (
                value
                if isinstance(value, dict)
                else {
                    "batch_id": value.id,
                    "status": value.status,
                    "source_hash": value.source_hash,
                }
            )
        else:
            result = {
                "audit": audit_status(db),
                "audit_chain_valid": verify_audit_chain(db),
            }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

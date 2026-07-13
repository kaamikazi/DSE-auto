from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.
import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from sqlalchemy import select

from app.brokers.paper import PaperBroker
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.data.providers.factory import create_provider
from app.models import ImportBatch, PaperSession, ValidationCampaign
from app.risk.kill_switch import set_state
from app.services.attested_imports import (
    activate_attested_import,
    preview_attested_import,
    rollback_attested_import,
)
from app.services.audit import (
    audit_status,
    initialize_canonical_chain,
    verify_audit_chain,
)
from app.services.campaign_simulation import run_accelerated_campaign
from app.services.campaigns import (
    archive_campaign,
    campaign_summary,
    complete_campaign_day,
    recover_missed_eod,
    start_campaign_day,
    transition_campaign,
)
from app.services.imported_session import run_complete_imported_session
from app.services.paper_sessions import summary, transition_session
from app.services.provider_diagnostics import diagnose_provider
from app.services.readiness import evaluate_readiness


def main() -> None:
    parser = argparse.ArgumentParser(description="DSE paper-only operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-data")
    verify.add_argument("--provider", required=True)
    verify.add_argument("--symbol", default="GP")
    action = sub.add_parser("session")
    action.add_argument("action", choices=["status", "start", "pause", "resume", "stop"])
    action.add_argument("name")
    sub.add_parser("verify-audit")
    sub.add_parser("audit-status")
    recover = sub.add_parser("audit-recover")
    recover.add_argument("--acknowledgement", required=True)
    recover.add_argument("--dry-run", action="store_true")
    readiness = sub.add_parser("readiness")
    readiness.add_argument("--provider", default="attested_csv")
    readiness.add_argument("--symbol", default="GP")
    readiness.add_argument("--acknowledgement", required=True)
    imported = sub.add_parser("run-imported-session")
    imported.add_argument("--acknowledgement", required=True)
    sub.add_parser("reconcile")
    sub.add_parser("emergency-stop")
    campaign = sub.add_parser("campaign")
    campaign.add_argument(
        "action",
        choices=[
            "status",
            "activate",
            "pause",
            "resume",
            "complete",
            "fail",
            "archive",
        ],
    )
    campaign.add_argument("campaign_id")
    campaign.add_argument("--reason", default="Operator CLI action")
    daily = sub.add_parser("campaign-day")
    daily.add_argument("action", choices=["start", "eod", "recover"])
    daily.add_argument("campaign_id")
    daily.add_argument("--date", required=True)
    simulation = sub.add_parser("simulate-campaign")
    simulation.add_argument("--output-dir", default="../reports/campaigns")
    simulation.add_argument("--backup-dir", default="../data/backups")
    preview_data = sub.add_parser("import-preview")
    preview_data.add_argument("file")
    preview_data.add_argument("--kind", required=True, choices=["quote", "ohlcv", "dsex"])
    preview_data.add_argument("--market-date", required=True)
    preview_data.add_argument("--attestation", required=True)
    preview_data.add_argument("--campaign-id")
    activate_data = sub.add_parser("import-activate")
    activate_data.add_argument("batch_id")
    activate_data.add_argument("--approval", required=True)
    rollback_data = sub.add_parser("import-rollback")
    rollback_data.add_argument("batch_id")
    rollback_data.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command == "verify-data":
        provider = create_provider(args.provider, Path("./data/imports"))
        print(
            json.dumps(
                diagnose_provider(
                    provider, args.symbol, Path("../data/reports/provider_diagnostics")
                ),
                indent=2,
                default=str,
            )
        )
        return
    with SessionLocal() as db:
        if args.command == "verify-audit":
            print(json.dumps({"audit_chain_valid": verify_audit_chain(db)}))
            return
        if args.command == "audit-status":
            print(json.dumps(audit_status(db), indent=2))
            return
        if args.command == "audit-recover":
            print(
                json.dumps(
                    initialize_canonical_chain(
                        db,
                        Path("../data/audit_archives"),
                        args.acknowledgement,
                        dry_run=args.dry_run,
                    ),
                    indent=2,
                )
            )
            return
        if args.command == "readiness":
            settings = get_settings()
            provider = create_provider(args.provider, settings.CSV_DATA_DIR)
            print(
                json.dumps(
                    evaluate_readiness(db, settings, provider, args.symbol, args.acknowledgement),
                    indent=2,
                    default=str,
                )
            )
            return
        if args.command == "run-imported-session":
            print(
                json.dumps(
                    run_complete_imported_session(
                        db,
                        get_settings(),
                        Path("../data/reports/milestone5"),
                        args.acknowledgement,
                    ),
                    indent=2,
                    default=str,
                )
            )
            return
        if args.command == "reconcile":
            print(json.dumps(PaperBroker(db).reconcile(), indent=2))
            return
        if args.command == "emergency-stop":
            state = set_state(db, "emergency_stop", "CLI emergency stop", "operator_cli")
            print(json.dumps({"state": state.state, "reason": state.reason}))
            return
        if args.command == "simulate-campaign":
            print(
                json.dumps(
                    run_accelerated_campaign(
                        db,
                        get_settings(),
                        Path(args.output_dir),
                        Path(args.backup_dir),
                    ),
                    indent=2,
                    default=str,
                )
            )
            return
        if args.command == "campaign":
            validation_campaign = db.get(ValidationCampaign, args.campaign_id)
            if validation_campaign is None:
                raise SystemExit("Campaign not found")
            if args.action == "status":
                print(json.dumps(campaign_summary(db, validation_campaign), indent=2, default=str))
                return
            if args.action == "archive":
                archive_campaign(db, validation_campaign, args.reason)
            else:
                targets = {
                    "activate": "active",
                    "pause": "paused",
                    "resume": "active",
                    "complete": "completed",
                    "fail": "failed",
                }
                transition_campaign(
                    db,
                    validation_campaign,
                    targets[args.action],
                    args.reason,
                    "operator_cli",
                )
            print(json.dumps(campaign_summary(db, validation_campaign), indent=2, default=str))
            return
        if args.command == "campaign-day":
            validation_campaign = db.get(ValidationCampaign, args.campaign_id)
            if validation_campaign is None:
                raise SystemExit("Campaign not found")
            market_date = date.fromisoformat(args.date)
            if args.action == "start":
                started_day = start_campaign_day(
                    db, validation_campaign, get_settings(), market_date
                )
                payload: object = {"day_id": started_day.id, "state": started_day.state}
            elif args.action == "eod":
                from app.models import CampaignDay

                existing_day = db.scalar(
                    select(CampaignDay).where(
                        CampaignDay.campaign_id == validation_campaign.id,
                        CampaignDay.market_date == market_date,
                    )
                )
                if existing_day is None:
                    raise SystemExit("Campaign day not found")
                payload = complete_campaign_day(
                    db, validation_campaign, existing_day, get_settings()
                )
            else:
                payload = {
                    "recovered_day_ids": recover_missed_eod(
                        db, validation_campaign, get_settings(), as_of=market_date
                    )
                }
            print(json.dumps(payload, indent=2, default=str))
            return
        if args.command == "import-preview":
            source = Path(args.file)
            print(
                json.dumps(
                    preview_attested_import(
                        db,
                        filename=source.name,
                        raw=source.read_bytes(),
                        import_kind=args.kind,
                        market_date=date.fromisoformat(args.market_date),
                        operator_attestation=args.attestation,
                        raw_dir=Path("../data/raw_imports"),
                        campaign_id=args.campaign_id,
                    ),
                    indent=2,
                    default=str,
                )
            )
            return
        if args.command in {"import-activate", "import-rollback"}:
            batch = db.get(ImportBatch, args.batch_id)
            if batch is None:
                raise SystemExit("Import batch not found")
            if args.command == "import-activate":
                activate_attested_import(db, batch, args.approval)
            else:
                rollback_attested_import(db, batch, args.reason)
            print(json.dumps({"batch_id": batch.id, "status": batch.status}))
            return
        session = db.scalar(select(PaperSession).where(PaperSession.name == args.name))
        if session is None:
            raise SystemExit("Session not found")
        targets = {
            "start": "warming_up",
            "pause": "paused",
            "resume": "running",
            "stop": "stopped",
        }
        if args.action != "status":
            transition_session(db, session, targets[args.action], f"cli_{args.action}", "cli")
        print(json.dumps(summary(session), indent=2))


if __name__ == "__main__":
    main()

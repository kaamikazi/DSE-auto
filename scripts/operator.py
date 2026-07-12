from __future__ import annotations

# ruff: noqa: E402 -- repository backend is intentionally placed first before imports.
import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from sqlalchemy import select

from app.brokers.paper import PaperBroker
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.data.providers.factory import create_provider
from app.models import PaperSession
from app.risk.kill_switch import set_state
from app.services.audit import audit_status, initialize_canonical_chain, verify_audit_chain
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
                        db, get_settings(), Path("../data/reports/milestone5"), args.acknowledgement
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
        session = db.scalar(select(PaperSession).where(PaperSession.name == args.name))
        if session is None:
            raise SystemExit("Session not found")
        targets = {"start": "warming_up", "pause": "paused", "resume": "running", "stop": "stopped"}
        if args.action != "status":
            transition_session(db, session, targets[args.action], f"cli_{args.action}", "cli")
        print(json.dumps(summary(session), indent=2))


if __name__ == "__main__":
    main()

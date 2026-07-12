from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from app.core.database import SessionLocal
from app.data.providers.factory import create_provider
from app.models import PaperSession
from app.services.audit import verify_audit_chain
from app.services.paper_sessions import summary, transition_session
from app.services.provider_diagnostics import diagnose_provider


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
        session = db.scalar(select(PaperSession).where(PaperSession.name == args.name))
        if session is None:
            raise SystemExit("Session not found")
        targets = {"start": "warming_up", "pause": "paused", "resume": "running", "stop": "stopped"}
        if args.action != "status":
            transition_session(db, session, targets[args.action], f"cli_{args.action}", "cli")
        print(json.dumps(summary(session), indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.brokers.paper import PaperBroker  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import OperationalIncident  # noqa: E402
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.incidents import open_incident, transition_incident  # noqa: E402
from app.services.infrastructure_incidents import EXERCISES  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit real serialized outage exercises")
    subparsers = parser.add_subparsers(dest="action", required=True)
    opened = subparsers.add_parser("open")
    opened.add_argument("--exercise", required=True, choices=sorted(EXERCISES))
    opened.add_argument("--evidence", type=Path, required=True)
    resolved = subparsers.add_parser("resolve")
    resolved.add_argument("--incident-id", required=True)
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.action == "open":
            definition = EXERCISES[args.exercise]
            evidence = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
            evidence.update(
                {
                    "exercise": args.exercise,
                    "execution_mode": "real_serialized_substage_verification",
                    "trading_mode": "paper",
                    "live_trading_enabled": False,
                    "broker_adapter": "disabled",
                }
            )
            incident = open_incident(
                db,
                definition.get("incident", args.exercise),
                definition["severity"],
                evidence=evidence,
                owner="milestone9-real-exercise",
            )
            print(json.dumps({"incident_id": incident.id, "audit": incident.linked_audit_events}))
            return

        incident = db.get(OperationalIncident, args.incident_id)
        if incident is None:
            raise SystemExit("Incident not found")
        reconciliation = PaperBroker(db).reconcile()
        audit_valid = verify_audit_chain(db)
        if not reconciliation["healthy"] or not audit_valid:
            print(
                json.dumps(
                    {
                        "resolved": False,
                        "reconciliation": reconciliation,
                        "audit_valid": audit_valid,
                    }
                )
            )
            raise SystemExit(2)
        transition_incident(
            db,
            incident,
            "resolved",
            owner="milestone9-real-exercise",
            root_cause="Operator-approved real serialized infrastructure restart",
            remediation="Service recovered; paper account reconciled; canonical audit revalidated",
        )
        print(
            json.dumps(
                {
                    "resolved": True,
                    "incident_id": incident.id,
                    "audit": incident.linked_audit_events,
                    "reconciliation": reconciliation,
                    "audit_valid": audit_valid,
                }
            )
        )


if __name__ == "__main__":
    main()

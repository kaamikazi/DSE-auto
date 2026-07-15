from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.brokers.paper import PaperBroker  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    OperationalIncident,
    Order,
    OutboxEvent,
    TaskRecord,
    Transaction,
)
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.incidents import open_incident, transition_incident  # noqa: E402
from app.services.task_queue import RedisBroker, recover_stale_workers  # noqa: E402


def _broker(queue: str) -> RedisBroker:
    redis_url = get_settings().REDIS_URL
    if not redis_url:
        raise RuntimeError("Real distributed probes require REDIS_URL")
    return RedisBroker(redis_url, queue)


def _effect_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
            "fills": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
        }


def _task_payload(task: TaskRecord | None) -> dict[str, object] | None:
    if task is None:
        return None
    return {
        "id": task.id,
        "task_name": task.task_name,
        "idempotency_key": task.idempotency_key,
        "correlation_id": task.correlation_id,
        "state": task.state,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "lease_owner": task.lease_owner,
        "lease_expires_at": task.lease_expires_at,
        "last_error": task.last_error,
        "result": task.result,
    }


def _open(args: argparse.Namespace) -> None:
    evidence = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
    evidence.update(
        {
            "exercise": args.exercise,
            "execution_mode": "real_distributed_verification",
            "trading_mode": "paper",
            "live_trading_enabled": False,
            "broker_adapter": "disabled",
            "opened_effect_counts": _effect_counts(),
        }
    )
    with SessionLocal() as db:
        incident = open_incident(
            db,
            args.incident_type,
            args.severity,
            evidence=evidence,
            owner="milestone9-distributed-probe",
        )
        print(json.dumps({"incident_id": incident.id, "audit": incident.linked_audit_events}))


def _resolve(args: argparse.Namespace) -> None:
    result = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
    with SessionLocal() as db:
        incident = db.get(OperationalIncident, args.incident_id)
        if incident is None:
            raise SystemExit("Incident not found")
        reconciliation = PaperBroker(db).reconcile()
        audit_valid = verify_audit_chain(db)
        closed_counts = _effect_counts()
        passed = bool(result.get("passed"))
        opened_counts = dict(incident.evidence.get("opened_effect_counts") or {})
        business_effects_unchanged = opened_counts == closed_counts
        incident.evidence = {
            **incident.evidence,
            "result": result,
            "closed_effect_counts": closed_counts,
            "reconciliation": reconciliation,
            "audit_valid": audit_valid,
            "business_effects_unchanged": business_effects_unchanged,
        }
        db.commit()
        if (
            not passed
            or not business_effects_unchanged
            or not reconciliation["healthy"]
            or not audit_valid
        ):
            print(
                json.dumps(
                    {
                        "resolved": False,
                        "passed": passed,
                        "reconciliation": reconciliation,
                        "audit_valid": audit_valid,
                        "business_effects_unchanged": business_effects_unchanged,
                    },
                    default=str,
                )
            )
            raise SystemExit(2)
        transition_incident(
            db,
            incident,
            "resolved",
            owner="milestone9-distributed-probe",
            root_cause=f"Operator-approved {incident.evidence['exercise']} verification",
            remediation="Durable state, idempotency, reconciliation, and audit revalidated",
        )
        print(
            json.dumps(
                {
                    "resolved": True,
                    "incident_id": incident.id,
                    "audit": incident.linked_audit_events,
                    "reconciliation": reconciliation,
                    "audit_valid": audit_valid,
                    "effect_counts": closed_counts,
                    "business_effects_unchanged": business_effects_unchanged,
                },
                default=str,
            )
        )


def _abort(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        incidents = db.scalars(
            select(OperationalIncident).where(
                OperationalIncident.owner == "milestone9-distributed-probe",
                OperationalIncident.state == "open",
            )
        ).all()
        for incident in incidents:
            incident.evidence = {
                **incident.evidence,
                "result": {
                    "passed": False,
                    "classification": "aborted_before_injection",
                    "reason": args.reason,
                },
            }
            db.commit()
            transition_incident(
                db,
                incident,
                "resolved",
                owner="milestone9-distributed-probe",
                root_cause="Verification harness stopped before fault or worker injection",
                remediation=args.reason,
            )
        tasks = db.scalars(
            select(TaskRecord).where(
                TaskRecord.idempotency_key.like("m9-competition-%"),
                TaskRecord.state == "queued",
                TaskRecord.attempts == 0,
            )
        ).all()
        for task in tasks:
            task.state = "aborted"
            task.last_error = args.reason
        db.commit()
        print(
            json.dumps(
                {
                    "aborted_incidents": [item.id for item in incidents],
                    "aborted_tasks": [item.id for item in tasks],
                    "audit_valid": verify_audit_chain(db),
                }
            )
        )


def _prepare_task(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        existing = db.scalar(
            select(TaskRecord).where(TaskRecord.idempotency_key == args.idempotency_key)
        )
        if existing is not None:
            raise SystemExit("Probe idempotency key already exists; refusing to reuse evidence")
        task = TaskRecord(
            task_name=args.task_name,
            queue=args.queue,
            payload={
                "day": 1,
                "symbols": ["GP", "ACI", "BRACBANK"],
                "strategies": ["ma_crossover", "momentum_dsex"],
                "verification_only": True,
            },
            idempotency_key=args.idempotency_key,
            correlation_id=args.correlation_id,
            max_attempts=args.max_attempts,
        )
        db.add(task)
        db.commit()
        pushes = args.pushes
        broker = _broker(args.queue)
        for _ in range(pushes):
            broker.push(task.id)
        print(json.dumps({"task": _task_payload(task), "pushes": pushes}, default=str))


def _task_status(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        task = db.scalar(
            select(TaskRecord).where(TaskRecord.idempotency_key == args.idempotency_key)
        )
        events = int(
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.idempotency_key == args.effect_key)
            )
            or 0
        )
        print(
            json.dumps(
                {"task": _task_payload(task), "effect_events": events, **_effect_counts()},
                default=str,
            )
        )


def _recover(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        recovered = recover_stale_workers(db, stale_after_seconds=args.stale_after_seconds)
        task = db.scalar(
            select(TaskRecord).where(TaskRecord.idempotency_key == args.idempotency_key)
        )
        if task is None:
            raise SystemExit("Probe task not found")
        if task.state == "retry":
            _broker(args.queue).push(task.id)
        print(
            json.dumps({"recovered_workers": recovered, "task": _task_payload(task)}, default=str)
        )


def _replay(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        task = db.scalar(
            select(TaskRecord).where(TaskRecord.idempotency_key == args.idempotency_key)
        )
        if task is None or task.state != "dead_letter":
            raise SystemExit("Only a preserved dead-letter task may be replayed")
        task.state = "retry"
        task.available_at = datetime.now(UTC)
        task.lease_owner = None
        task.lease_expires_at = None
        db.commit()
        _broker(args.queue).push(task.id)
        print(json.dumps({"task": _task_payload(task), "replayed": True}, default=str))


def _final() -> None:
    with SessionLocal() as db:
        reconciliation = PaperBroker(db).reconcile()
        audit_valid = verify_audit_chain(db)
        print(
            json.dumps(
                {
                    "reconciliation": reconciliation,
                    "audit_valid": audit_valid,
                    "effect_counts": _effect_counts(),
                },
                default=str,
            )
        )
        if not reconciliation["healthy"] or not audit_valid:
            raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real distributed verification probe")
    sub = parser.add_subparsers(dest="action", required=True)
    opened = sub.add_parser("open")
    opened.add_argument("--exercise", required=True)
    opened.add_argument("--incident-type", required=True)
    opened.add_argument("--severity", choices=["low", "medium", "high", "critical"], required=True)
    opened.add_argument("--evidence", type=Path, required=True)
    resolved = sub.add_parser("resolve")
    resolved.add_argument("--incident-id", required=True)
    resolved.add_argument("--evidence", type=Path, required=True)
    aborted = sub.add_parser("abort-open")
    aborted.add_argument("--reason", required=True)
    prepared = sub.add_parser("prepare-task")
    prepared.add_argument("--task-name", required=True)
    prepared.add_argument("--queue", required=True)
    prepared.add_argument("--idempotency-key", required=True)
    prepared.add_argument("--correlation-id", required=True)
    prepared.add_argument("--max-attempts", type=int, default=5)
    prepared.add_argument("--pushes", type=int, default=1)
    status = sub.add_parser("task-status")
    status.add_argument("--idempotency-key", required=True)
    status.add_argument("--effect-key", default="unused")
    recovered = sub.add_parser("recover")
    recovered.add_argument("--idempotency-key", required=True)
    recovered.add_argument("--queue", required=True)
    recovered.add_argument("--stale-after-seconds", type=int, default=60)
    replayed = sub.add_parser("replay")
    replayed.add_argument("--idempotency-key", required=True)
    replayed.add_argument("--queue", required=True)
    sub.add_parser("final")
    args = parser.parse_args()
    actions = {
        "open": _open,
        "resolve": _resolve,
        "abort-open": _abort,
        "prepare-task": _prepare_task,
        "task-status": _task_status,
        "recover": _recover,
        "replay": _replay,
    }
    if args.action == "final":
        _final()
    else:
        actions[args.action](args)


if __name__ == "__main__":
    main()

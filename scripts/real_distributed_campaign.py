from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import select

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.brokers.paper import PaperBroker  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    CampaignDay,
    EvidenceReview,
    OperationalIncident,
    OutboxEvent,
    TaskRecord,
    ValidationCampaign,
)
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.distributed_simulation import (  # noqa: E402
    SIMULATION_STRATEGIES,
    SIMULATION_SYMBOLS,
    _campaign,
    _complete_and_review_day,
    _trading_date,
)
from app.services.events import emit_event  # noqa: E402
from app.services.incidents import open_incident, transition_incident  # noqa: E402
from app.services.task_queue import RedisBroker, enqueue_task  # noqa: E402


def _broker() -> RedisBroker:
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.REDIS_URL:
        raise RuntimeError("Distributed campaign requires Redis")
    return RedisBroker(settings.REDIS_URL, settings.TASK_QUEUE_NAME)


def _campaign_by_name(name: str) -> ValidationCampaign:
    with SessionLocal() as db:
        campaign = db.scalar(select(ValidationCampaign).where(ValidationCampaign.name == name))
        if campaign is None:
            raise SystemExit("Campaign not found")
        db.expunge(campaign)
        return campaign


def _start(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        campaign = _campaign(db, args.campaign, args.total_days)
        print(
            json.dumps(
                {
                    "campaign_id": campaign.id,
                    "campaign_name": campaign.name,
                    "total_days": args.total_days,
                    "symbols": SIMULATION_SYMBOLS,
                    "strategies": SIMULATION_STRATEGIES,
                    "execution_mode": "accelerated_distributed_infrastructure_validation",
                    "paper_only": True,
                    "profitability_claimed": False,
                }
            )
        )


def _enqueue_day(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        campaign = db.scalar(
            select(ValidationCampaign).where(ValidationCampaign.name == args.campaign)
        )
        if campaign is None:
            raise SystemExit("Campaign not found")
        broker = _broker()
        task = enqueue_task(
            db,
            broker,
            "simulation_day",
            {
                "day": args.day,
                "symbols": SIMULATION_SYMBOLS,
                "strategies": SIMULATION_STRATEGIES,
                "campaign_id": campaign.id,
                "verification_only": True,
            },
            f"m9-campaign:{campaign.id}:day:{args.day}",
            correlation_id=campaign.id,
        )
        if args.duplicate_delivery:
            broker.push(task.id)
        print(
            json.dumps(
                {
                    "campaign_id": campaign.id,
                    "task_id": task.id,
                    "day": args.day,
                    "duplicate_delivery": args.duplicate_delivery,
                }
            )
        )


def _task_status(args: argparse.Namespace) -> None:
    campaign = _campaign_by_name(args.campaign)
    with SessionLocal() as db:
        task = db.scalar(
            select(TaskRecord).where(
                TaskRecord.idempotency_key == f"m9-campaign:{campaign.id}:day:{args.day}"
            )
        )
        print(
            json.dumps(
                {
                    "task_id": task.id if task else None,
                    "state": task.state if task else None,
                    "attempts": task.attempts if task else None,
                    "lease_owner": task.lease_owner if task else None,
                    "result": task.result if task else None,
                },
                default=str,
            )
        )


def _complete_day(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        campaign = db.scalar(
            select(ValidationCampaign).where(ValidationCampaign.name == args.campaign)
        )
        if campaign is None:
            raise SystemExit("Campaign not found")
        task = db.scalar(
            select(TaskRecord).where(
                TaskRecord.idempotency_key == f"m9-campaign:{campaign.id}:day:{args.day}"
            )
        )
        if task is None or task.state != "succeeded" or task.attempts != 1:
            raise SystemExit("Campaign day task did not complete exactly once")
        market_date = _trading_date(campaign.start_date, args.day)
        day = _complete_and_review_day(db, campaign, args.day, market_date)
        injected: list[str] = []
        if args.stale_quote:
            incident = open_incident(
                db,
                "stale_data",
                "high",
                campaign_id=campaign.id,
                evidence={"day": args.day, "orders_blocked": True, "verification_only": True},
                owner="m9-distributed-campaign",
            )
            transition_incident(
                db,
                incident,
                "resolved",
                owner="m9-distributed-campaign",
                root_cause="Injected stale quote",
                remediation="Quote rejected; no order submitted",
            )
            injected.append("stale_quote_blocked")
        if args.rejected_order:
            emit_event(
                db,
                "risk_rejected",
                aggregate_type="campaign_day",
                aggregate_id=str(day.id),
                payload={"day": args.day, "reason": "verification_risk_rejection"},
                idempotency_key=f"m9-campaign:{campaign.id}:day:{args.day}:risk-rejected",
                correlation_id=campaign.id,
            )
            injected.append("rejected_order_event")
        if args.partial_fill:
            emit_event(
                db,
                "partial_fill",
                aggregate_type="campaign_day",
                aggregate_id=str(day.id),
                payload={"day": args.day, "filled": 40, "requested": 100},
                idempotency_key=f"m9-campaign:{campaign.id}:day:{args.day}:partial-fill",
                correlation_id=campaign.id,
            )
            injected.append("partial_fill_event")
        day.summary = {**day.summary, "injected_events": injected}
        db.commit()
        print(
            json.dumps(
                {
                    "campaign_id": campaign.id,
                    "day": args.day,
                    "campaign_day_id": day.id,
                    "state": day.state,
                    "eod_completed": day.eod_completed,
                    "injected": injected,
                },
                default=str,
            )
        )


def _record_restart(args: argparse.Namespace) -> None:
    evidence = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
    with SessionLocal() as db:
        campaign = db.scalar(
            select(ValidationCampaign).where(ValidationCampaign.name == args.campaign)
        )
        if campaign is None:
            raise SystemExit("Campaign not found")
        incident = open_incident(
            db,
            "worker_failure",
            "high",
            campaign_id=campaign.id,
            evidence={**evidence, "verification_only": True},
            owner="m9-distributed-campaign",
        )
        transition_incident(
            db,
            incident,
            "resolved",
            owner="m9-distributed-campaign",
            root_cause="Operator-approved campaign worker restart",
            remediation="Worker restarted; durable campaign tasks preserved",
        )
        print(json.dumps({"incident_id": incident.id, "audit": incident.linked_audit_events}))


def _checkpoint(args: argparse.Namespace) -> None:
    backup_hash = None
    backup_bytes = None
    if args.backup:
        backup_hash = hashlib.sha256(args.backup.read_bytes()).hexdigest()
        backup_bytes = args.backup.stat().st_size
    dead_letter_hash = None
    dead_letter_passed = False
    if args.dead_letter_report:
        dead_letter_payload = json.loads(args.dead_letter_report.read_text(encoding="utf-8-sig"))
        dead_letter_hash = hashlib.sha256(args.dead_letter_report.read_bytes()).hexdigest()
        dead_letter_passed = bool(dead_letter_payload.get("passed"))
    with SessionLocal() as db:
        campaign = db.scalar(
            select(ValidationCampaign).where(ValidationCampaign.name == args.campaign)
        )
        if campaign is None:
            raise SystemExit("Campaign not found")
        days = db.scalars(
            select(CampaignDay)
            .where(CampaignDay.campaign_id == campaign.id)
            .order_by(CampaignDay.market_date)
        ).all()
        tasks = db.scalars(select(TaskRecord).where(TaskRecord.correlation_id == campaign.id)).all()
        reviews = db.scalars(
            select(EvidenceReview).where(EvidenceReview.campaign_id == campaign.id)
        ).all()
        events = db.scalars(
            select(OutboxEvent).where(OutboxEvent.correlation_id == campaign.id)
        ).all()
        incidents = db.scalars(
            select(OperationalIncident).where(OperationalIncident.campaign_id == campaign.id)
        ).all()
        reconciliation = PaperBroker(db).reconcile()
        audit_valid = verify_audit_chain(db)
        completed_days = [day for day in days if day.eod_completed and day.state == "completed"]
        expected_tasks = [
            task
            for task in tasks
            if task.idempotency_key.startswith(f"m9-campaign:{campaign.id}:day:")
        ]
        event_types = {event.event_type for event in events}
        required_injections = {"risk_rejected", "partial_fill"}
        passed = bool(
            len(completed_days) >= args.expected_days
            and len(expected_tasks) >= args.expected_days
            and all(task.state == "succeeded" and task.attempts == 1 for task in expected_tasks)
            and len(reviews) >= args.expected_days
            and all(
                review.state in {"accepted", "rejected"} for review in reviews[: args.expected_days]
            )
            and required_injections <= event_types
            and any(
                item.incident_type == "stale_data" and item.state == "resolved"
                for item in incidents
            )
            and any(
                item.incident_type == "worker_failure" and item.state == "resolved"
                for item in incidents
            )
            and reconciliation["healthy"]
            and audit_valid
            and backup_hash
            and dead_letter_passed
        )
        if args.final and passed:
            campaign.state = "completed"
            db.commit()
        report = {
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "classification": "accelerated distributed infrastructure validation",
            "phase_days": args.expected_days,
            "passed": passed,
            "symbols": campaign.approved_symbols,
            "strategies": campaign.approved_strategies,
            "completed_days": len(completed_days),
            "task_states": [
                {"id": task.id, "state": task.state, "attempts": task.attempts}
                for task in expected_tasks
            ],
            "reviews": [review.state for review in reviews],
            "event_types": sorted(event_types),
            "incidents": [
                {"id": item.id, "type": item.incident_type, "state": item.state}
                for item in incidents
            ],
            "reconciliation": reconciliation,
            "audit_valid": audit_valid,
            "backup": {
                "path": str(args.backup) if args.backup else None,
                "sha256": backup_hash,
                "bytes": backup_bytes,
            },
            "dead_letter_recovery": {
                "path": str(args.dead_letter_report) if args.dead_letter_report else None,
                "sha256": dead_letter_hash,
                "passed": dead_letter_passed,
            },
            "paper_only": True,
            "profitability_claimed": False,
            "real_market_evidence": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(json.dumps(report, indent=2, default=str))
        if not passed:
            raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Real accelerated distributed campaign probe")
    sub = parser.add_subparsers(dest="action", required=True)
    started = sub.add_parser("start")
    started.add_argument("--campaign", required=True)
    started.add_argument("--total-days", type=int, default=10)
    enqueued = sub.add_parser("enqueue-day")
    enqueued.add_argument("--campaign", required=True)
    enqueued.add_argument("--day", type=int, required=True)
    enqueued.add_argument("--duplicate-delivery", action="store_true")
    status = sub.add_parser("task-status")
    status.add_argument("--campaign", required=True)
    status.add_argument("--day", type=int, required=True)
    completed = sub.add_parser("complete-day")
    completed.add_argument("--campaign", required=True)
    completed.add_argument("--day", type=int, required=True)
    completed.add_argument("--stale-quote", action="store_true")
    completed.add_argument("--rejected-order", action="store_true")
    completed.add_argument("--partial-fill", action="store_true")
    restarted = sub.add_parser("record-worker-restart")
    restarted.add_argument("--campaign", required=True)
    restarted.add_argument("--evidence", type=Path, required=True)
    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--campaign", required=True)
    checkpoint.add_argument("--expected-days", type=int, required=True)
    checkpoint.add_argument("--backup", type=Path)
    checkpoint.add_argument("--dead-letter-report", type=Path)
    checkpoint.add_argument("--output", type=Path, required=True)
    checkpoint.add_argument("--final", action="store_true")
    args = parser.parse_args()
    actions = {
        "start": _start,
        "enqueue-day": _enqueue_day,
        "task-status": _task_status,
        "complete-day": _complete_day,
        "record-worker-restart": _record_restart,
        "checkpoint": _checkpoint,
    }
    actions[args.action](args)


if __name__ == "__main__":
    main()

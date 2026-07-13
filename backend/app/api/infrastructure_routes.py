from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import database_health_metadata, engine, get_db
from app.core.security import (
    Principal,
    authenticate_and_create_session,
    require_operator,
    require_reviewer,
)
from app.models import (
    DatabaseMigrationRun,
    DataQualityReport,
    DisasterRecoveryRun,
    EvidenceReview,
    OperationalIncident,
    OutboxEvent,
    PaperQualification,
    RiskValidationRun,
    TaskRecord,
    WorkerHeartbeat,
)
from app.services.data_quality import generate_data_quality_report
from app.services.disaster_recovery import run_sqlite_disaster_recovery_exercise
from app.services.events import replay_event
from app.services.evidence_review import review_view, submit_review
from app.services.migration_preflight import migration_preflight
from app.services.qualification import calculate_qualification
from app.services.risk_validation import validate_risk_controls
from app.services.task_queue import create_broker

router = APIRouter(prefix="/api/v1/infrastructure", tags=["infrastructure"])
Db = Annotated[Session, Depends(get_db)]
Operator = Annotated[Principal, Depends(require_operator)]
Reviewer = Annotated[Principal, Depends(require_reviewer)]


def _counts(db: Session, model: type[Any], state_column: Any) -> dict[str, int]:
    rows = db.execute(select(state_column, func.count()).select_from(model).group_by(state_column))
    return {str(state): int(count) for state, count in rows}


@router.post("/auth/session")
def create_auth_session(
    payload: dict[str, str],
    db: Db,
    x_client_fingerprint: str = Header(default="local-client"),
) -> dict[str, Any]:
    try:
        token, session = authenticate_and_create_session(
            db,
            role=payload.get("role", ""),
            supplied_key=payload.get("api_key", ""),
            actor=payload.get("actor", "local-user"),
            fingerprint=x_client_fingerprint,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(401, str(exc)) from exc
    return {
        "token": token,
        "role": session.role,
        "expires_at": session.expires_at.isoformat(),
    }


@router.get("/summary")
def infrastructure_summary(db: Db) -> dict[str, Any]:
    now = datetime.now(UTC)
    workers = list(
        db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()))
    )
    latest_quality = db.scalar(
        select(DataQualityReport).order_by(DataQualityReport.created_at.desc()).limit(1)
    )
    latest_qualification = db.scalar(
        select(PaperQualification).order_by(PaperQualification.calculated_at.desc()).limit(1)
    )
    latest_recovery = db.scalar(
        select(DisasterRecoveryRun).order_by(DisasterRecoveryRun.created_at.desc()).limit(1)
    )
    latest_migration = db.scalar(
        select(DatabaseMigrationRun).order_by(DatabaseMigrationRun.started_at.desc()).limit(1)
    )
    broker_health = create_broker().health()
    backup_files = list(Path("../data/backups").glob("*.db"))
    latest_backup = (
        max(backup_files, key=lambda item: item.stat().st_mtime) if backup_files else None
    )
    backup_age_seconds = (
        max(now.timestamp() - latest_backup.stat().st_mtime, 0) if latest_backup else None
    )

    def heartbeat_age(value: datetime) -> float:
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return max((now - normalized.astimezone(UTC)).total_seconds(), 0)

    active_leases = int(
        db.scalar(select(func.count()).select_from(TaskRecord).where(TaskRecord.state == "leased"))
        or 0
    )
    retries = int(
        db.scalar(select(func.count()).select_from(TaskRecord).where(TaskRecord.state == "retry"))
        or 0
    )
    task_dead_letters = int(
        db.scalar(
            select(func.count()).select_from(TaskRecord).where(TaskRecord.state == "dead_letter")
        )
        or 0
    )
    incidents = list(
        db.scalars(
            select(OperationalIncident)
            .where(OperationalIncident.state.in_(["open", "acknowledged", "mitigated"]))
            .order_by(OperationalIncident.opened_at.desc())
            .limit(25)
        )
    )
    return {
        "paper_trading": True,
        "live_trading_enabled": False,
        "database": database_health_metadata(db),
        "api": {"healthy": True, "checked_at": now.isoformat()},
        "redis": broker_health,
        "workers": [
            {
                "id": item.worker_id,
                "state": item.state,
                "queues": item.queues,
                "heartbeat_at": item.heartbeat_at.isoformat(),
                "heartbeat_age_seconds": heartbeat_age(item.heartbeat_at),
            }
            for item in workers
            if not item.worker_id.startswith("scheduler:")
        ],
        "scheduler": [
            {
                "id": item.worker_id,
                "state": item.state,
                "heartbeat_at": item.heartbeat_at.isoformat(),
                "heartbeat_age_seconds": heartbeat_age(item.heartbeat_at),
            }
            for item in workers
            if item.worker_id.startswith("scheduler:")
        ],
        "task_queue": _counts(db, TaskRecord, TaskRecord.state),
        "event_outbox": _counts(db, OutboxEvent, OutboxEvent.state),
        "queue_depth": int(broker_health.get("depth", 0) or 0),
        "active_leases": active_leases,
        "retries": retries,
        "task_dead_letters": task_dead_letters,
        "dead_letter_events": int(
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.state == "dead_letter")
            )
            or 0
        ),
        "data_latency": latest_quality.metrics if latest_quality else None,
        "daily_review_queue": _counts(db, EvidenceReview, EvidenceReview.state),
        "qualification": {
            "qualifying": latest_qualification.qualifying,
            "remaining_qualifying_days": latest_qualification.remaining_qualifying_days,
            "counts": latest_qualification.counts,
            "failure_reasons": latest_qualification.failure_reasons,
        }
        if latest_qualification
        else None,
        "disaster_recovery": {
            "status": latest_recovery.status,
            "rpo_seconds": latest_recovery.recovery_point_seconds,
            "rto_seconds": latest_recovery.recovery_time_seconds,
        }
        if latest_recovery
        else None,
        "postgresql_migration": {
            "status": latest_migration.status,
            "record_counts": latest_migration.record_counts,
        }
        if latest_migration
        else migration_preflight(engine),
        "database_pool_health": database_health_metadata(db).get("pool"),
        "backup": {
            "path": str(latest_backup) if latest_backup else None,
            "age_seconds": backup_age_seconds,
            "within_24_hours": backup_age_seconds is not None and backup_age_seconds <= 86400,
        },
        "recovery_readiness": bool(
            latest_recovery
            and latest_recovery.status == "passed"
            and backup_age_seconds is not None
            and backup_age_seconds <= 86400
        ),
        "infrastructure_incidents": [
            {
                "id": item.id,
                "type": item.incident_type,
                "severity": item.severity,
                "state": item.state,
                "opened_at": item.opened_at.isoformat(),
            }
            for item in incidents
        ],
    }


@router.get("/workers", dependencies=[Depends(require_reviewer)])
def workers(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "worker_id": item.worker_id,
            "process_id": item.process_id,
            "state": item.state,
            "queues": item.queues,
            "started_at": item.started_at.isoformat(),
            "heartbeat_at": item.heartbeat_at.isoformat(),
        }
        for item in db.scalars(
            select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc())
        )
    ]


@router.get("/tasks", dependencies=[Depends(require_reviewer)])
def tasks(db: Db, state: str | None = None) -> list[dict[str, Any]]:
    query = select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(200)
    if state:
        query = query.where(TaskRecord.state == state)
    return [
        {
            "id": item.id,
            "task_name": item.task_name,
            "state": item.state,
            "attempts": item.attempts,
            "max_attempts": item.max_attempts,
            "lease_owner": item.lease_owner,
            "last_error": item.last_error,
        }
        for item in db.scalars(query)
    ]


@router.get("/outbox", dependencies=[Depends(require_reviewer)])
def outbox(db: Db, state: str | None = None) -> list[dict[str, Any]]:
    query = select(OutboxEvent).order_by(OutboxEvent.created_at.desc()).limit(200)
    if state:
        query = query.where(OutboxEvent.state == state)
    return [
        {
            "id": item.id,
            "event_type": item.event_type,
            "schema_version": item.schema_version,
            "state": item.state,
            "attempts": item.attempts,
            "correlation_id": item.correlation_id,
            "causation_id": item.causation_id,
            "audit_event_id": item.audit_event_id,
            "last_error": item.last_error,
        }
        for item in db.scalars(query)
    ]


@router.post("/outbox/{event_id}/replay")
def replay_outbox(event_id: str, db: Db, _: Operator) -> dict[str, Any]:
    try:
        event = replay_event(db, event_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": event.id, "state": event.state}


@router.get("/reviews")
def reviews(db: Db, _: Reviewer, state: str | None = None) -> list[dict[str, Any]]:
    query = select(EvidenceReview).order_by(EvidenceReview.created_at.desc())
    if state:
        query = query.where(EvidenceReview.state == state)
    return [review_view(item) for item in db.scalars(query)]


@router.post("/reviews/{review_id}")
def decide_review(
    review_id: str, payload: dict[str, Any], db: Db, principal: Reviewer
) -> dict[str, Any]:
    review = db.get(EvidenceReview, review_id)
    if review is None:
        raise HTTPException(404, "Evidence review not found")
    try:
        result = submit_review(
            db,
            review,
            reviewer=principal.actor,
            reviewer_role=principal.role,
            target_state=str(payload.get("state", "")),
            data_quality_verdict=str(payload.get("data_quality_verdict", "")),
            strategy_behavior_verdict=str(payload.get("strategy_behavior_verdict", "")),
            risk_engine_verdict=str(payload.get("risk_engine_verdict", "")),
            execution_model_verdict=str(payload.get("execution_model_verdict", "")),
            incidents_reviewed=[str(item) for item in payload.get("incidents_reviewed", [])],
            comments=str(payload.get("comments", "")),
            approval_decision=str(payload.get("approval_decision", "")),
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return review_view(result)


@router.post("/qualification/{campaign_id}")
def qualification(campaign_id: str, db: Db, _: Operator) -> dict[str, Any]:
    result = calculate_qualification(db, campaign_id)
    return {
        "campaign_id": result.campaign_id,
        "qualifying": result.qualifying,
        "remaining_qualifying_days": result.remaining_qualifying_days,
        "counts": result.counts,
        "failure_reasons": result.failure_reasons,
    }


@router.post("/data-quality/report")
def data_quality_report(payload: dict[str, Any], db: Db, _: Operator) -> dict[str, Any]:
    try:
        result = generate_data_quality_report(
            db,
            scope=str(payload["scope"]),
            start_date=date.fromisoformat(str(payload["start_date"])),
            end_date=date.fromisoformat(str(payload["end_date"])),
            campaign_id=str(payload["campaign_id"]) if payload.get("campaign_id") else None,
            output_dir=Path("../reports/data_quality"),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": result.id,
        "passed": result.passed,
        "metrics": result.metrics,
        "json_path": result.json_path,
        "csv_path": result.csv_path,
        "chart_path": result.chart_path,
        "integrity_hash": result.integrity_hash,
    }


@router.post("/risk-validation")
def risk_validation(payload: dict[str, Any], db: Db, _: Operator) -> dict[str, Any]:
    run = validate_risk_controls(db, campaign_id=payload.get("campaign_id"))
    return {"id": run.id, "report": run.report, "integrity_hash": run.integrity_hash}


@router.post("/disaster-recovery")
def disaster_recovery(
    db: Db, _: Operator, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    run = run_sqlite_disaster_recovery_exercise(
        db,
        settings,
        exercise_dir=Path("../reports/disaster_recovery") / date.today().isoformat(),
        evidence_roots=(Path("../reports/campaigns"),),
        configuration_files=(Path("../.env.example"),),
    )
    return {
        "id": run.id,
        "status": run.status,
        "rpo_seconds": run.recovery_point_seconds,
        "rto_seconds": run.recovery_time_seconds,
        "checks": run.checks,
        "evidence_path": run.evidence_path,
    }


@router.get("/risk-validation/latest")
def latest_risk_validation(db: Db, _: Reviewer) -> dict[str, Any] | None:
    run = db.scalar(
        select(RiskValidationRun).order_by(RiskValidationRun.created_at.desc()).limit(1)
    )
    return (
        {"id": run.id, "report": run.report, "integrity_hash": run.integrity_hash} if run else None
    )

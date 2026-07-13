from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.database import SessionLocal, engine
from app.models import (
    CampaignDay,
    DataQualityReport,
    OperationalIncident,
    PaperAccount,
    TaskRecord,
    ValidationCampaign,
)
from app.services.events import emit_event
from app.services.evidence_review import queue_campaign_day_review, submit_review
from app.services.qualification import calculate_qualification
from app.services.task_queue import TaskBroker, TaskWorker, enqueue_task

SIMULATION_SYMBOLS = ["GP", "ACI", "BRACBANK"]
SIMULATION_STRATEGIES = ["ma_crossover", "momentum_dsex"]


def _trading_date(start: date, day_number: int) -> date:
    current = start
    remaining = day_number - 1
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _campaign(db: Session, name: str, total_days: int) -> ValidationCampaign:
    campaign = db.scalar(select(ValidationCampaign).where(ValidationCampaign.name == name))
    if campaign is not None:
        return campaign
    start = date(2026, 1, 4)
    campaign = ValidationCampaign(
        name=name,
        start_date=start,
        planned_end_date=_trading_date(start, total_days),
        approved_symbols=SIMULATION_SYMBOLS,
        approved_strategies=SIMULATION_STRATEGIES,
        starting_capital=Decimal("1000000"),
        risk_profile={"paper_only": True},
        data_source_policy={"adapter": "fake_certified", "test_only": True},
        timestamp_trust_requirement="exchange_verified",
        fill_model="pessimistic",
        benchmark="DSEX",
        operator_notes="Accelerated infrastructure validation; no profitability claim.",
        state="active",
        active_rule_set_id="simulation-rule-set",
        active_fee_profile_id="simulation-fee-profile",
    )
    db.add(campaign)
    if db.get(PaperAccount, 1) is None:
        db.add(
            PaperAccount(
                id=1,
                cash=Decimal("1000000"),
                starting_cash=Decimal("1000000"),
            )
        )
    db.commit()
    return campaign


def _quality_report(db: Session, campaign_id: str, market_date: date) -> None:
    existing = db.scalar(
        select(DataQualityReport).where(
            DataQualityReport.scope == "daily",
            DataQualityReport.campaign_id == campaign_id,
            DataQualityReport.start_date == market_date,
            DataQualityReport.end_date == market_date,
        )
    )
    if existing is None:
        digest = hashlib.sha256(f"{campaign_id}:{market_date}:quality".encode()).hexdigest()
        db.add(
            DataQualityReport(
                scope="daily",
                campaign_id=campaign_id,
                start_date=market_date,
                end_date=market_date,
                metrics={
                    "passed": True,
                    "symbol_coverage": 1.0,
                    "market_session_coverage": 1.0,
                    "adapter": "fake_certified",
                },
                json_path=f"simulated/{market_date}/quality.json",
                csv_path=f"simulated/{market_date}/quality.csv",
                chart_path=f"simulated/{market_date}/quality.svg",
                integrity_hash=digest,
                passed=True,
            )
        )
        db.commit()


def _complete_and_review_day(
    db: Session,
    campaign: ValidationCampaign,
    day_number: int,
    market_date: date,
) -> CampaignDay:
    existing = db.scalar(
        select(CampaignDay).where(
            CampaignDay.campaign_id == campaign.id,
            CampaignDay.market_date == market_date,
        )
    )
    if existing is not None:
        return existing
    day = CampaignDay(
        campaign_id=campaign.id,
        market_date=market_date,
        state="completed",
        premarket_completed=True,
        eod_completed=True,
        summary={
            "audit_valid": True,
            "reconciliation": {"healthy": True},
            "backup": {
                "successful": True,
                "sha256": hashlib.sha256(str(day_number).encode()).hexdigest(),
            },
            "symbols": SIMULATION_SYMBOLS,
            "strategies": SIMULATION_STRATEGIES,
            "paper_only": True,
            "profitability_claimed": False,
        },
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        evidence_path=f"simulated/{market_date}/daily_evidence.json",
    )
    db.add(day)
    db.flush()
    emit_event(
        db,
        "campaign_session_started",
        aggregate_type="campaign_day",
        aggregate_id=str(day.id),
        payload={"campaign_id": campaign.id, "day": day_number},
        idempotency_key=f"simulation:{campaign.id}:day:{day_number}:start",
        correlation_id=campaign.id,
    )
    for symbol in SIMULATION_SYMBOLS:
        quote_key = f"simulation:{campaign.id}:day:{day_number}:quote:{symbol}"
        first = emit_event(
            db,
            "quote_received",
            aggregate_type="quote",
            aggregate_id=symbol,
            payload={"symbol": symbol, "adapter": "fake_certified", "day": day_number},
            idempotency_key=quote_key,
            correlation_id=campaign.id,
        )
        if day_number == 5 and symbol == "GP":
            duplicate = emit_event(
                db,
                "quote_received",
                aggregate_type="quote",
                aggregate_id=symbol,
                payload={"duplicate_delivery": True},
                idempotency_key=quote_key,
                correlation_id=campaign.id,
            )
            if duplicate.id != first.id:
                raise RuntimeError("Duplicate event idempotency failed")
    for strategy in SIMULATION_STRATEGIES:
        emit_event(
            db,
            "signal_generated",
            aggregate_type="strategy",
            aggregate_id=strategy,
            payload={"strategy": strategy, "day": day_number, "paper_only": True},
            idempotency_key=f"simulation:{campaign.id}:day:{day_number}:signal:{strategy}",
            correlation_id=campaign.id,
        )
    emit_event(
        db,
        "campaign_session_completed",
        aggregate_type="campaign_day",
        aggregate_id=str(day.id),
        payload={"campaign_id": campaign.id, "day": day_number},
        idempotency_key=f"simulation:{campaign.id}:day:{day_number}:complete",
        correlation_id=campaign.id,
    )
    db.commit()
    _quality_report(db, campaign.id, market_date)
    review = queue_campaign_day_review(db, day)
    rejected = day_number == 9
    submit_review(
        db,
        review,
        reviewer="simulation-reviewer",
        reviewer_role="reviewer",
        target_state="rejected" if rejected else "accepted",
        data_quality_verdict="pass",
        strategy_behavior_verdict="concern" if rejected else "pass",
        risk_engine_verdict="pass",
        execution_model_verdict="pass",
        incidents_reviewed=[],
        comments="Injected failed review day." if rejected else "Accelerated evidence accepted.",
        approval_decision="reject" if rejected else "accept",
    )
    return day


def run_distributed_simulation_phase(
    db: Session,
    broker: TaskBroker,
    *,
    campaign_name: str,
    start_day: int,
    end_day: int,
    total_days: int = 30,
    output_dir: Path = Path("../reports/distributed_simulation"),
    require_distributed: bool = False,
) -> dict[str, Any]:
    if not 1 <= start_day <= end_day <= total_days:
        raise ValueError("Simulation phase bounds are invalid")
    broker_health = broker.health()
    dialect = db.bind.dialect.name if db.bind is not None else "unknown"
    infrastructure_verified = dialect == "postgresql" and broker_health.get("backend") == "redis"
    if require_distributed and not infrastructure_verified:
        raise RuntimeError("Distributed simulation requires PostgreSQL and Redis")
    campaign = _campaign(db, campaign_name, total_days)
    worker = TaskWorker(broker, worker_id=f"simulation-worker-phase-{start_day}")
    with SessionLocal() as heartbeat_db:
        worker.heartbeat(heartbeat_db)
    task_results: list[str] = []
    for day_number in range(start_day, end_day + 1):
        task = enqueue_task(
            db,
            broker,
            "simulation_day",
            {
                "day": day_number,
                "symbols": SIMULATION_SYMBOLS,
                "strategies": SIMULATION_STRATEGIES,
            },
            f"simulation:{campaign.id}:task:{day_number}",
            correlation_id=campaign.id,
        )
        if day_number == 6:
            broker.push(task.id)
        refreshed = db.get(TaskRecord, task.id)
        for _ in range(3):
            worker.run_once(timeout_seconds=1)
            if refreshed is not None:
                db.refresh(refreshed)
                if refreshed.state in {"succeeded", "dead_letter"}:
                    break
        if refreshed is not None:
            task_results.append(refreshed.state)
        market_date = _trading_date(campaign.start_date, day_number)
        _complete_and_review_day(db, campaign, day_number, market_date)
        if day_number == 15:
            restart_incident = OperationalIncident(
                campaign_id=campaign.id,
                incident_type="unexpected_process_restart",
                state="open",
                severity="critical",
                evidence={"injected": True, "checkpoint": 15},
            )
            db.add(restart_incident)
            db.flush()
            emit_event(
                db,
                "incident_opened",
                aggregate_type="operational_incident",
                aggregate_id=restart_incident.id,
                payload={"severity": "critical", "injected": True},
                idempotency_key=f"simulation:{campaign.id}:incident:restart",
                correlation_id=campaign.id,
            )
            db.commit()
        if day_number == 16:
            open_incident = db.scalar(
                select(OperationalIncident).where(
                    OperationalIncident.campaign_id == campaign.id,
                    OperationalIncident.state == "open",
                )
            )
            if open_incident is not None:
                open_incident.state = "resolved"
                open_incident.owner = "simulation-operator"
                open_incident.root_cause = "Injected restart exercise"
                open_incident.remediation = "Services restarted and durable state recovered"
                open_incident.resolved_at = datetime.now(UTC)
                db.commit()
    worker.stop()
    with SessionLocal() as heartbeat_db:
        worker.heartbeat(heartbeat_db, "stopped")
    engine.dispose()
    with SessionLocal() as reconnect:
        database_reconnect = reconnect.scalar(text("SELECT 1")) == 1
    final = end_day == total_days
    qualification: dict[str, Any] | None = None
    reconciliation: dict[str, object] | None = None
    if final:
        reconciliation = PaperBroker(db).reconcile()
        snapshot = calculate_qualification(db, campaign.id, target_days=60)
        campaign.state = "completed"
        db.commit()
        qualification = {
            "counts": snapshot.counts,
            "qualifying": snapshot.qualifying,
            "remaining_qualifying_days": snapshot.remaining_qualifying_days,
            "failure_reasons": snapshot.failure_reasons,
        }
    report: dict[str, Any] = {
        "campaign_id": campaign.id,
        "campaign_name": campaign.name,
        "phase": {"start_day": start_day, "end_day": end_day, "total_days": total_days},
        "symbols": SIMULATION_SYMBOLS,
        "strategies": SIMULATION_STRATEGIES,
        "database": dialect,
        "broker": broker_health,
        "infrastructure_verified": infrastructure_verified,
        "execution_mode": "postgresql_redis" if infrastructure_verified else "local_emulation",
        "worker_restart_checkpoint": 15,
        "duplicate_task_delivery_day": 6,
        "duplicate_event_delivery_day": 5,
        "failed_review_day": 9,
        "database_client_reconnect": database_reconnect,
        "server_restart_verification": "external_harness_required",
        "task_states": task_results,
        "final_reconciliation": reconciliation,
        "final_qualification": qualification,
        "profitability_claimed": False,
        "paper_only": True,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{campaign.name}_phase_{start_day}_{end_day}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    return report

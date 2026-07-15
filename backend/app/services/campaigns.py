from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.core.config import Settings
from app.core.database import database_health_metadata
from app.data.providers import create_provider
from app.models import (
    CampaignDay,
    DataQualityReport,
    FeeProfile,
    ImportBatch,
    JobExecution,
    MarketRuleSet,
    OperationalIncident,
    Order,
    PaperAccount,
    PaperSession,
    ProviderCertification,
    RiskState,
    Transaction,
    ValidationCampaign,
    WorkerHeartbeat,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain
from app.services.backups import backup_database
from app.services.campaign_analytics import campaign_metrics
from app.services.events import emit_event
from app.services.governance import strategy_by_reference
from app.services.incidents import open_incident
from app.services.task_queue import create_broker

CAMPAIGN_STATES = {
    "configured",
    "awaiting_data",
    "ready",
    "active",
    "paused",
    "degraded",
    "reconciliation_required",
    "completed",
    "invalidated",
    "archived",
}
CONTROLLING_STATES = {"active", "paused", "degraded", "reconciliation_required"}
TRANSITIONS = {
    "configured": {"awaiting_data", "ready", "active", "invalidated", "archived"},
    "awaiting_data": {"ready", "invalidated", "archived"},
    "ready": {"active", "awaiting_data", "invalidated", "archived"},
    "active": {"paused", "degraded", "reconciliation_required", "completed", "invalidated"},
    "paused": {"active", "reconciliation_required", "completed", "invalidated"},
    "degraded": {"active", "paused", "reconciliation_required", "invalidated"},
    "reconciliation_required": {"active", "invalidated"},
    "completed": {"archived"},
    "invalidated": {"archived"},
    "archived": set(),
}
ACTIVE_ORDER_STATES = {"proposed", "awaiting_approval", "approved", "submitted", "partially_filled"}
DEFAULT_RECOVERY_DIR = Path(__file__).resolve().parents[3] / "reports" / "recovery"


def campaign_view(campaign: ValidationCampaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "name": campaign.name,
        "account_id": campaign.account_id,
        "start_date": campaign.start_date.isoformat(),
        "planned_end_date": campaign.planned_end_date.isoformat(),
        "approved_symbols": campaign.approved_symbols,
        "approved_strategies": campaign.approved_strategies,
        "starting_capital": str(campaign.starting_capital),
        "risk_profile": campaign.risk_profile,
        "data_source_policy": campaign.data_source_policy,
        "timestamp_trust_requirement": campaign.timestamp_trust_requirement,
        "fill_model": campaign.fill_model,
        "benchmark": campaign.benchmark,
        "operator_notes": campaign.operator_notes,
        "state": campaign.state,
        "active_rule_set_id": campaign.active_rule_set_id,
        "active_fee_profile_id": campaign.active_fee_profile_id,
        "evidence_class": campaign.evidence_class,
        "provider_certification_id": campaign.provider_certification_id,
        "daily_reviewer_assignments": campaign.daily_reviewer_assignments,
    }


def create_campaign(
    db: Session,
    *,
    name: str,
    start_date: date,
    planned_end_date: date,
    approved_symbols: list[str],
    approved_strategies: list[str],
    starting_capital: Decimal,
    risk_profile: dict[str, Any],
    data_source_policy: dict[str, Any],
    timestamp_trust_requirement: str,
    fill_model: str,
    benchmark: str,
    operator_notes: str,
    active_rule_set_id: str,
    active_fee_profile_id: str,
    account_id: int = 1,
    evidence_class: str = "synthetic",
    provider_certification_id: str | None = None,
    daily_reviewer_assignments: dict[str, str] | None = None,
) -> ValidationCampaign:
    if planned_end_date < start_date:
        raise ValueError("Campaign end date precedes its start date")
    if not approved_symbols or not approved_strategies or starting_capital <= 0:
        raise ValueError("Campaign requires symbols, strategies, and positive capital")
    if fill_model not in {"pessimistic", "balanced", "optimistic"}:
        raise ValueError("Unknown fill model")
    if timestamp_trust_requirement not in {"operator_attested", "exchange_verified"}:
        raise ValueError("Campaign timestamp trust must be operator_attested or exchange_verified")
    if evidence_class not in {"synthetic", "imported", "real_market"}:
        raise ValueError("Unknown campaign evidence class")
    certification = (
        db.get(ProviderCertification, provider_certification_id)
        if provider_certification_id
        else None
    )
    operator_attested_allowed = bool(data_source_policy.get("allow_operator_attested"))
    if evidence_class == "real_market" and not (
        (certification is not None and certification.status == "passed")
        or operator_attested_allowed
    ):
        raise ValueError(
            "Real-market campaigns require a passing licensed provider or an explicit "
            "operator-attested-file policy"
        )
    if (
        evidence_class == "real_market"
        and int(data_source_policy.get("qualification_target_days", 60)) != 60
    ):
        raise ValueError("Real-market campaign qualification target must be 60 days")
    rule_set = db.get(MarketRuleSet, active_rule_set_id)
    fee_profile = db.get(FeeProfile, active_fee_profile_id)
    if rule_set is None or rule_set.verification_status == "deprecated":
        raise ValueError("Campaign requires a non-deprecated market rule set")
    if fee_profile is None:
        raise ValueError("Campaign requires a versioned fee profile")
    campaign = ValidationCampaign(
        name=name,
        account_id=account_id,
        start_date=start_date,
        planned_end_date=planned_end_date,
        approved_symbols=sorted({symbol.upper() for symbol in approved_symbols}),
        approved_strategies=approved_strategies,
        starting_capital=starting_capital,
        risk_profile=risk_profile,
        data_source_policy=data_source_policy,
        timestamp_trust_requirement=timestamp_trust_requirement,
        fill_model=fill_model,
        benchmark=benchmark,
        operator_notes=operator_notes,
        active_rule_set_id=active_rule_set_id,
        active_fee_profile_id=active_fee_profile_id,
        evidence_class=evidence_class,
        provider_certification_id=provider_certification_id,
        daily_reviewer_assignments=daily_reviewer_assignments or {},
    )
    db.add(campaign)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="campaign.configured",
        entity_type="validation_campaign",
        entity_id=campaign.id,
        new_state={**campaign_view(campaign), "paper_only": True},
    )
    db.commit()
    return campaign


def _governance_ready(db: Session, campaign: ValidationCampaign) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for reference in campaign.approved_strategies:
        strategy = strategy_by_reference(db, reference)
        if strategy is None or strategy.lifecycle_state != "paper_active":
            failures.append(reference)
    return not failures, failures


def transition_campaign(
    db: Session,
    campaign: ValidationCampaign,
    target_state: str,
    reason: str,
    actor: str = "operator",
) -> ValidationCampaign:
    if target_state not in CAMPAIGN_STATES or target_state not in TRANSITIONS[campaign.state]:
        raise ValueError(f"Invalid campaign transition {campaign.state} -> {target_state}")
    if target_state == "active":
        if campaign.evidence_class == "real_market" and campaign.state != "ready":
            raise ValueError("Real-market campaign must pass through ready before activation")
        controlling = db.scalar(
            select(ValidationCampaign).where(
                ValidationCampaign.account_id == campaign.account_id,
                ValidationCampaign.id != campaign.id,
                ValidationCampaign.state.in_(CONTROLLING_STATES),
            )
        )
        if controlling:
            raise ValueError(f"Paper account is controlled by campaign {controlling.id}")
        governed, failures = _governance_ready(db, campaign)
        if not governed:
            raise ValueError(f"Strategies have not passed governance: {failures}")
        rule_set = db.get(MarketRuleSet, campaign.active_rule_set_id)
        if rule_set is None or rule_set.verification_status == "deprecated":
            raise ValueError("Campaign rule-set version is unavailable or deprecated")
    previous = campaign.state
    campaign.state = target_state
    append_audit(
        db,
        actor=actor,
        event_type="campaign.state_changed",
        entity_type="validation_campaign",
        entity_id=campaign.id,
        previous_state={"state": previous},
        new_state={"state": target_state, "reason": reason},
        metadata={
            "rule_set_id": campaign.active_rule_set_id,
            "fee_profile_id": campaign.active_fee_profile_id,
            "strategies": campaign.approved_strategies,
        },
    )
    db.commit()
    return campaign


def change_campaign_rule_set(
    db: Session,
    campaign: ValidationCampaign,
    rule_set_id: str,
    operator_approval: str,
) -> ValidationCampaign:
    if campaign.state in CONTROLLING_STATES:
        raise ValueError("Rule-set version is locked for a controlling campaign")
    rule_set = db.get(MarketRuleSet, rule_set_id)
    if rule_set is None or rule_set.verification_status == "deprecated":
        raise ValueError("Replacement rule set is unavailable or deprecated")
    if len(operator_approval.strip()) < 12:
        raise ValueError("Rule-set change requires explicit operator approval")
    previous = campaign.active_rule_set_id
    campaign.active_rule_set_id = rule_set_id
    append_audit(
        db,
        actor="operator",
        event_type="campaign.rule_set_changed",
        entity_type="validation_campaign",
        entity_id=campaign.id,
        previous_state={"rule_set_id": previous},
        new_state={"rule_set_id": rule_set_id},
        metadata={"operator_approval": operator_approval.strip()},
    )
    db.commit()
    return campaign


def evaluate_campaign_readiness(
    db: Session,
    campaign: ValidationCampaign,
    settings: Settings,
    market_date: date,
    *,
    backup_dir: Path = DEFAULT_RECOVERY_DIR,
    operator_acknowledgement: str | None = None,
) -> dict[str, Any]:
    audit = audit_status(db)
    latest_job = db.scalar(select(JobExecution).order_by(JobExecution.started_at.desc()).limit(1))
    risk = db.get(RiskState, 1)
    rule_set = db.get(MarketRuleSet, campaign.active_rule_set_id)
    fee_profile = db.get(FeeProfile, campaign.active_fee_profile_id)
    governed, governance_failures = _governance_ready(db, campaign)
    imports = db.scalars(
        select(ImportBatch).where(
            ImportBatch.campaign_id == campaign.id,
            ImportBatch.market_date == market_date,
            ImportBatch.status == "activated",
        )
    ).all()
    import_kinds = {batch.import_kind for batch in imports}
    provenances = {
        "operator_attested" for batch in imports if batch.operator_attestation is not None
    }
    provider_detail: dict[str, Any]
    provider_ready = False
    try:
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        provider_health = provider.health_check()
        quote = provider.get_quote(campaign.approved_symbols[0])
        provenance = quote.timestamp_provenance.value
        approved_sources = set(campaign.data_source_policy.get("approved", []))
        provider_ready = bool(provider_health.get("healthy")) and provider.name in approved_sources
        if provider_ready:
            provenances.add(provenance)
        provider_detail = {
            "name": provider.name,
            "healthy": provider_health.get("healthy"),
            "approved": provider.name in approved_sources,
            "timestamp_provenance": provenance,
        }
    except Exception as exc:
        provider_detail = {"healthy": False, "error": str(exc)}
    trust_rank = {
        "unknown": 0,
        "receipt_only": 0,
        "provider_asserted": 1,
        "operator_attested": 2,
        "exchange_verified": 3,
    }
    required_rank = trust_rank[campaign.timestamp_trust_requirement]
    trust_ok = any(trust_rank.get(item, 0) >= required_rank for item in provenances)
    backup_candidates = [
        *backup_dir.glob("*.dump"),
        *backup_dir.glob("dse_autotrader_backup_*.db"),
    ]
    now = datetime.now(UTC)
    fresh_backups = [
        path
        for path in backup_candidates
        if now - datetime.fromtimestamp(path.stat().st_mtime, UTC) <= timedelta(hours=24)
    ]
    database_health = database_health_metadata(db)
    broker_health = create_broker(settings).health()
    weekly_days = set(rule_set.rules.get("weekly_trading_days", [])) if rule_set else set()
    calendar_ok = market_date.strftime("%A").lower() in {str(day).lower() for day in weekly_days}
    try:
        reconciliation = PaperBroker(db).reconcile()
    except Exception as exc:
        reconciliation = {"healthy": False, "error": str(exc)}
    unresolved_critical = db.scalars(
        select(OperationalIncident).where(
            OperationalIncident.campaign_id == campaign.id,
            OperationalIncident.severity == "critical",
            OperationalIncident.state.not_in(("resolved",)),
        )
    ).all()
    worker = db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.state == "active").limit(1))
    reviewer = campaign.daily_reviewer_assignments.get(
        market_date.isoformat(), campaign.daily_reviewer_assignments.get("default")
    )
    required_kinds = set(
        campaign.data_source_policy.get("required_daily_kinds", ["quote", "ohlcv", "dsex"])
    )
    checks: dict[str, dict[str, Any]] = {
        "audit": {"passed": bool(audit.get("canonical_valid")), "detail": audit},
        "database": {
            "passed": bool(database_health.get("healthy"))
            and database_health.get("dialect") == "postgresql",
            "detail": database_health,
        },
        "redis": {
            "passed": bool(broker_health.get("healthy"))
            and broker_health.get("backend") == "redis",
            "detail": broker_health,
        },
        "backup": {
            "passed": bool(fresh_backups),
            "fresh_count": len(fresh_backups),
            "paths": [str(path) for path in fresh_backups],
            "maximum_age_hours": 24,
        },
        "provider_or_import": {
            "passed": required_kinds.issubset(import_kinds) or provider_ready,
            "activated_batches": len(imports),
            "required_kinds": sorted(required_kinds),
            "available_kinds": sorted(import_kinds),
            "provider": provider_detail,
        },
        "timestamp_trust": {"passed": trust_ok, "available": sorted(provenances)},
        "calendar": {"passed": calendar_ok, "market_date": market_date.isoformat()},
        "scheduler": {"passed": latest_job is not None and latest_job.status != "failed"},
        "worker": {"passed": worker is not None, "worker_id": worker.worker_id if worker else None},
        "emergency_stop": {
            "passed": risk is not None and risk.state == "healthy",
            "state": risk.state if risk else "missing",
        },
        "reconciliation": {"passed": bool(reconciliation["healthy"]), "detail": reconciliation},
        "campaign": {"passed": campaign.state == "active", "state": campaign.state},
        "critical_incidents": {
            "passed": not unresolved_critical,
            "count": len(unresolved_critical),
        },
        "reviewer_assignment": {"passed": bool(reviewer), "reviewer": reviewer},
        "operator_acknowledgement": {
            "passed": bool(operator_acknowledgement and len(operator_acknowledgement.strip()) >= 12)
            if campaign.evidence_class == "real_market"
            else True,
        },
        "governance": {"passed": governed, "failures": governance_failures},
        "rules_and_fees": {"passed": rule_set is not None and fee_profile is not None},
        "paper_safety": {
            "passed": settings.TRADING_MODE == "paper"
            and not settings.LIVE_TRADING_ENABLED
            and settings.BROKER_ADAPTER == "disabled"
        },
    }
    return {
        "ready": all(bool(check["passed"]) for check in checks.values()),
        "market_date": market_date.isoformat(),
        "checks": checks,
    }


def start_campaign_day(
    db: Session,
    campaign: ValidationCampaign,
    settings: Settings,
    market_date: date,
    *,
    backup_dir: Path = DEFAULT_RECOVERY_DIR,
    readiness_override: dict[str, Any] | None = None,
    operator_acknowledgement: str | None = None,
) -> CampaignDay:
    if campaign.state != "active":
        raise ValueError("Campaign must be active before a daily session can start")
    if db.scalar(
        select(CampaignDay).where(
            CampaignDay.campaign_id == campaign.id, CampaignDay.market_date == market_date
        )
    ):
        raise ValueError("Campaign day already exists")
    readiness = readiness_override or evaluate_campaign_readiness(
        db,
        campaign,
        settings,
        market_date,
        backup_dir=backup_dir,
        operator_acknowledgement=operator_acknowledgement,
    )
    if not readiness.get("ready"):
        campaign.state = "degraded"
        append_audit(
            db,
            actor="daily_workflow",
            event_type="campaign.premarket_blocked",
            entity_type="validation_campaign",
            entity_id=campaign.id,
            new_state={"state": "degraded", "readiness": readiness},
        )
        db.commit()
        raise ValueError("Pre-market readiness failed closed")
    now = datetime.now(UTC)
    session = PaperSession(
        name=f"{campaign.name}-{market_date.isoformat()}",
        account_id=campaign.account_id,
        state="running",
        starting_cash=campaign.starting_capital,
        approved_universe=campaign.approved_symbols,
        strategies=campaign.approved_strategies,
        risk_profile=campaign.risk_profile,
        fill_model=campaign.fill_model,
        started_at=now,
        heartbeat_at=now,
        campaign_id=campaign.id,
        market_rule_set_id=campaign.active_rule_set_id,
        fee_profile_id=campaign.active_fee_profile_id,
    )
    db.add(session)
    db.flush()
    day = CampaignDay(
        campaign_id=campaign.id,
        market_date=market_date,
        evidence_class=campaign.evidence_class,
        session_id=session.id,
        state="market_open",
        premarket_completed=True,
        summary={
            "premarket": readiness,
            "skipped_signals": [],
            "paper_only": True,
            "operator_acknowledgement": operator_acknowledgement,
            "synthetic_or_accelerated": False,
        },
        started_at=now,
    )
    db.add(day)
    db.flush()
    emit_event(
        db,
        "campaign_session_started",
        aggregate_type="campaign_day",
        aggregate_id=str(day.id),
        payload={
            "campaign_id": campaign.id,
            "session_id": session.id,
            "market_date": market_date.isoformat(),
        },
        idempotency_key=f"campaign-session-started:{day.id}",
        correlation_id=campaign.id,
    )
    append_audit(
        db,
        actor="daily_workflow",
        event_type="campaign.day_started",
        entity_type="campaign_day",
        entity_id=str(day.id),
        new_state={
            "campaign_id": campaign.id,
            "session_id": session.id,
            "market_date": market_date.isoformat(),
            "rule_set_id": campaign.active_rule_set_id,
            "fee_profile_id": campaign.active_fee_profile_id,
        },
    )
    db.commit()
    return day


def record_skipped_signal(
    db: Session, day: CampaignDay, symbol: str, strategy: str, reason: str
) -> None:
    summary = dict(day.summary)
    skipped = list(summary.get("skipped_signals", []))
    skipped.append({"symbol": symbol, "strategy": strategy, "reason": reason})
    summary["skipped_signals"] = skipped
    day.summary = summary
    append_audit(
        db,
        actor="strategy_runner",
        event_type="signal.skipped",
        entity_type="campaign_day",
        entity_id=str(day.id),
        metadata={"symbol": symbol, "strategy": strategy, "reason": reason},
    )
    db.commit()


def _daily_counts(db: Session, campaign_id: str, market_date: date) -> dict[str, Any]:
    start = datetime.combine(market_date, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    orders = db.scalars(
        select(Order).where(
            Order.campaign_id == campaign_id,
            Order.created_at >= start,
            Order.created_at < end,
        )
    ).all()
    transactions = db.scalars(
        select(Transaction).where(
            Transaction.campaign_id == campaign_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
        )
    ).all()
    return {
        "orders": len(orders),
        "rejected_trades": sum(order.status == "rejected" for order in orders),
        "partial_fills": sum(0 < order.filled_quantity < order.quantity for order in orders),
        "fills": sum(order.status == "filled" for order in orders),
        "fees": float(sum((tx.fees for tx in transactions), Decimal("0"))),
    }


def complete_campaign_day(
    db: Session,
    campaign: ValidationCampaign,
    day: CampaignDay,
    settings: Settings,
    *,
    evidence_dir: Path = Path("../reports/campaigns"),
    backup_dir: Path = Path("../data/backups"),
    backup_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if day.campaign_id != campaign.id or day.eod_completed:
        raise ValueError("Campaign day is mismatched or already completed")
    now = datetime.now(UTC)
    active_orders = db.scalars(
        select(Order).where(Order.campaign_id == campaign.id, Order.status.in_(ACTIVE_ORDER_STATES))
    ).all()
    for order in active_orders:
        order.status = "expired"
    reconciliation = PaperBroker(db).reconcile()
    if not reconciliation["healthy"]:
        campaign.state = "reconciliation_required"
        day.state = "reconciliation_required"
        open_incident(
            db,
            "reconciliation_mismatch",
            "critical",
            campaign_id=campaign.id,
            evidence=reconciliation,
        )
        raise ValueError("End-of-day reconciliation failed closed")
    account = db.get(PaperAccount, campaign.account_id)
    counts = _daily_counts(db, campaign.id, day.market_date)
    backup: dict[str, Any]
    try:
        backup = backup_override or backup_database(db, settings, backup_dir)
        if not backup.get("successful"):
            raise ValueError("Backup evidence is not successful")
    except Exception as exc:
        campaign.state = "degraded"
        day.state = "backup_failed"
        open_incident(
            db,
            "backup_failure",
            "critical",
            campaign_id=campaign.id,
            evidence={"error": str(exc)},
        )
        raise ValueError("End-of-day backup failed closed") from exc
    summary = {
        **day.summary,
        **counts,
        "expired_orders": len(active_orders),
        "reconciliation": reconciliation,
        "account_snapshot": {"cash": str(account.cash) if account else None},
        "audit_valid": verify_audit_chain(db),
        "backup": backup,
        "completed_at": now.isoformat(),
        "rule_set_id": campaign.active_rule_set_id,
        "fee_profile_id": campaign.active_fee_profile_id,
    }
    if not summary["audit_valid"]:
        campaign.state = "degraded"
        open_incident(db, "audit_failure", "critical", campaign_id=campaign.id)
        raise ValueError("End-of-day audit verification failed closed")
    output = evidence_dir / campaign.id / day.market_date.isoformat()
    output.mkdir(parents=True, exist_ok=True)
    evidence = output / "daily_evidence.json"
    evidence.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    session = db.get(PaperSession, day.session_id) if day.session_id else None
    if session:
        session.state = "completed"
        session.stopped_at = now
    day.summary = summary
    day.evidence_path = str(evidence)
    day.eod_completed = True
    day.state = "completed"
    day.completed_at = now
    emit_event(
        db,
        "campaign_session_completed",
        aggregate_type="campaign_day",
        aggregate_id=str(day.id),
        payload={
            "campaign_id": campaign.id,
            "session_id": day.session_id,
            "evidence_path": str(evidence),
            "backup_hash": backup["sha256"],
        },
        idempotency_key=f"campaign-session-completed:{day.id}",
        correlation_id=campaign.id,
        causation_id=f"campaign-session-started:{day.id}",
    )
    append_audit(
        db,
        actor="daily_workflow",
        event_type="campaign.day_completed",
        entity_type="campaign_day",
        entity_id=str(day.id),
        new_state={"evidence_path": str(evidence), "backup_hash": backup["sha256"]},
    )
    db.commit()
    from app.services.evidence_review import queue_campaign_day_review

    queue_campaign_day_review(db, day)
    return summary


def detect_missed_trading_days(
    db: Session, campaign: ValidationCampaign, through_date: date
) -> list[CampaignDay]:
    rule_set = db.get(MarketRuleSet, campaign.active_rule_set_id)
    if rule_set is None:
        raise ValueError("Campaign market rule set is unavailable")
    days = {str(item).lower() for item in rule_set.rules.get("weekly_trading_days", [])}
    holidays = {date.fromisoformat(str(item)) for item in rule_set.rules.get("holidays", [])}
    end = min(through_date, campaign.planned_end_date)
    existing = {
        row.market_date
        for row in db.scalars(select(CampaignDay).where(CampaignDay.campaign_id == campaign.id))
    }
    missed: list[CampaignDay] = []
    current = campaign.start_date
    while current <= end:
        if (
            current.strftime("%A").lower() in days
            and current not in holidays
            and current not in existing
        ):
            day = CampaignDay(
                campaign_id=campaign.id,
                market_date=current,
                state="missed",
                missed_reason="No daily session record for configured trading day",
                summary={"missed_trading_day": True},
            )
            db.add(day)
            db.flush()
            missed.append(day)
            open_incident(
                db,
                "missed_scheduler_job",
                "high",
                campaign_id=campaign.id,
                evidence={"market_date": current.isoformat()},
            )
        current += timedelta(days=1)
    db.commit()
    return missed


def recover_missed_eod(
    db: Session,
    campaign: ValidationCampaign,
    settings: Settings,
    *,
    as_of: date,
    evidence_dir: Path = Path("../reports/campaigns"),
    backup_dir: Path = Path("../data/backups"),
) -> list[int]:
    incomplete = db.scalars(
        select(CampaignDay).where(
            CampaignDay.campaign_id == campaign.id,
            CampaignDay.market_date < as_of,
            CampaignDay.eod_completed.is_(False),
            CampaignDay.premarket_completed.is_(True),
        )
    ).all()
    recovered: list[int] = []
    for day in incomplete:
        campaign.state = "reconciliation_required"
        day.state = "reconciliation_required"
        open_incident(
            db,
            "missed_eod",
            "critical",
            campaign_id=campaign.id,
            evidence={"market_date": day.market_date.isoformat()},
        )
        complete_campaign_day(
            db,
            campaign,
            day,
            settings,
            evidence_dir=evidence_dir,
            backup_dir=backup_dir,
        )
        recovered.append(day.id)
    if recovered:
        campaign.state = "paused"
        append_audit(
            db,
            actor="recovery_service",
            event_type="campaign.missed_eod_recovered",
            entity_type="validation_campaign",
            entity_id=campaign.id,
            new_state={"days": recovered, "state": "paused", "operator_resume_required": True},
        )
        db.commit()
    return recovered


def recover_campaigns_after_restart(db: Session, as_of: date) -> list[str]:
    affected: list[str] = []
    campaigns = db.scalars(
        select(ValidationCampaign).where(ValidationCampaign.state.in_(CONTROLLING_STATES))
    ).all()
    for campaign in campaigns:
        incomplete = db.scalar(
            select(CampaignDay).where(
                CampaignDay.campaign_id == campaign.id,
                CampaignDay.market_date < as_of,
                CampaignDay.premarket_completed.is_(True),
                CampaignDay.eod_completed.is_(False),
            )
        )
        if incomplete:
            campaign.state = "reconciliation_required"
            incomplete.state = "reconciliation_required"
            open_incident(
                db,
                "unexpected_process_restart",
                "high",
                campaign_id=campaign.id,
                evidence={"incomplete_day": incomplete.market_date.isoformat()},
            )
            affected.append(campaign.id)
    db.commit()
    return affected


def campaign_summary(db: Session, campaign: ValidationCampaign) -> dict[str, Any]:
    days = db.scalars(
        select(CampaignDay)
        .where(CampaignDay.campaign_id == campaign.id)
        .order_by(CampaignDay.market_date)
    ).all()
    equity = [float(campaign.starting_capital)]
    benchmark = [100.0]
    cumulative_effects: dict[str, float] = {
        "fees": 0.0,
        "slippage": 0.0,
        "turnover": 0.0,
        "rejected_trades": 0.0,
        "missed_trades": 0.0,
        "partial_fills": 0.0,
        "risk_interventions": 0.0,
        "data_quality_incidents": 0.0,
        "operational_downtime_minutes": 0.0,
    }
    daily: list[dict[str, Any]] = []
    for item in days:
        summary = item.summary
        snapshot = summary.get("account_snapshot", {})
        if snapshot.get("cash") is not None:
            equity.append(float(snapshot["cash"]))
        else:
            equity.append(equity[-1])
        benchmark.append(float(summary.get("benchmark_value", benchmark[-1])))
        for key in cumulative_effects:
            cumulative_effects[key] += float(summary.get(key, 0.0))
        daily.append(
            {
                "market_date": item.market_date.isoformat(),
                "state": item.state,
                "evidence_path": item.evidence_path,
                "summary": summary,
            }
        )
    metrics = campaign_metrics(equity, benchmark, effects=cumulative_effects)
    weekly: list[dict[str, Any]] = []
    for index in range(0, len(daily), 5):
        slice_days = daily[index : index + 5]
        weekly.append(
            {
                "week": index // 5 + 1,
                "start": slice_days[0]["market_date"],
                "end": slice_days[-1]["market_date"],
                "sessions": len(slice_days),
                "completed": sum(day["state"] == "completed" for day in slice_days),
            }
        )
    incidents = db.scalar(
        select(func.count())
        .select_from(OperationalIncident)
        .where(OperationalIncident.campaign_id == campaign.id)
    )
    quality_report = db.scalar(
        select(DataQualityReport)
        .where(DataQualityReport.campaign_id == campaign.id)
        .order_by(DataQualityReport.created_at.desc())
        .limit(1)
    )
    return {
        "campaign": campaign_view(campaign),
        "daily": daily,
        "weekly": weekly,
        "cumulative": metrics,
        "incident_count": int(incidents or 0),
        "data_quality_evidence": {
            "report_id": quality_report.id,
            "integrity_hash": quality_report.integrity_hash,
            "passed": quality_report.passed,
            "json_path": quality_report.json_path,
        }
        if quality_report
        else None,
        "strategy_results_visible": bool(quality_report and quality_report.passed),
        "profitability_claimed": False,
    }


def archive_campaign(db: Session, campaign: ValidationCampaign, reason: str) -> ValidationCampaign:
    return transition_campaign(db, campaign, "archived", reason)

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import require_api_key
from app.models import (
    CampaignDay,
    FeeProfile,
    ImportBatch,
    MarketRuleSet,
    OperationalIncident,
    StrategyRegistration,
    ValidationCampaign,
)
from app.schemas.campaigns import (
    CampaignCreate,
    FeeProfileCreate,
    IncidentCreate,
    IncidentTransition,
    RuleSetCreate,
    StrategyCreate,
    StrategyObservation,
    StrategyTransition,
)
from app.services.attested_imports import (
    activate_attested_import,
    import_template,
    preview_attested_import,
    rollback_attested_import,
)
from app.services.audit import audit_status
from app.services.campaigns import (
    archive_campaign,
    campaign_summary,
    campaign_view,
    complete_campaign_day,
    create_campaign,
    recover_missed_eod,
    start_campaign_day,
    transition_campaign,
)
from app.services.governance import (
    create_fee_profile,
    create_rule_set,
    evaluate_strategy_suspension,
    fee_sensitivity,
    promote_strategy,
    register_strategy,
    trade_cost_breakdown,
)
from app.services.incidents import open_incident, transition_incident
from app.services.observability import health_summary

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]


def _campaign(db: Session, campaign_id: str) -> ValidationCampaign:
    campaign = db.get(ValidationCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "Campaign not found")
    return campaign


@router.post("/campaigns", dependencies=[Depends(require_api_key)])
def configure_campaign(payload: CampaignCreate, db: Db) -> dict[str, Any]:
    try:
        return campaign_view(create_campaign(db, **payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/campaigns")
def list_campaigns(db: Db) -> list[dict[str, Any]]:
    return [
        campaign_view(item)
        for item in db.scalars(
            select(ValidationCampaign).order_by(ValidationCampaign.created_at.desc())
        )
    ]


@router.get("/campaigns/{campaign_id}/summary")
def get_campaign_summary(campaign_id: str, db: Db) -> dict[str, Any]:
    return campaign_summary(db, _campaign(db, campaign_id))


@router.post("/campaigns/{campaign_id}/state/{action}", dependencies=[Depends(require_api_key)])
def change_campaign_state(campaign_id: str, action: str, reason: str, db: Db) -> dict[str, Any]:
    target = {
        "activate": "active",
        "pause": "paused",
        "resume": "active",
        "degrade": "degraded",
        "require-reconciliation": "reconciliation_required",
        "complete": "completed",
        "fail": "failed",
    }.get(action)
    try:
        campaign = _campaign(db, campaign_id)
        if action == "archive":
            return campaign_view(archive_campaign(db, campaign, reason))
        if target is None:
            raise ValueError("Unknown campaign action")
        return campaign_view(transition_campaign(db, campaign, target, reason))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/campaigns/{campaign_id}/days/start", dependencies=[Depends(require_api_key)])
def start_daily_session(
    campaign_id: str,
    market_date: date,
    db: Db,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        day = start_campaign_day(db, _campaign(db, campaign_id), settings, market_date)
        return {"day_id": day.id, "state": day.state, "session_id": day.session_id}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/campaigns/{campaign_id}/days/eod", dependencies=[Depends(require_api_key)])
def run_daily_eod(
    campaign_id: str,
    market_date: date,
    db: Db,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    campaign = _campaign(db, campaign_id)
    day = db.scalar(
        select(CampaignDay).where(
            CampaignDay.campaign_id == campaign.id, CampaignDay.market_date == market_date
        )
    )
    if day is None:
        raise HTTPException(404, "Campaign day not found")
    try:
        return complete_campaign_day(db, campaign, day, settings)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/campaigns/{campaign_id}/recover-eod", dependencies=[Depends(require_api_key)])
def recover_daily_eod(
    campaign_id: str,
    as_of: date,
    db: Db,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    try:
        recovered = recover_missed_eod(db, _campaign(db, campaign_id), settings, as_of=as_of)
        return {"recovered_day_ids": recovered, "operator_resume_required": bool(recovered)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/data-imports/preview", dependencies=[Depends(require_api_key)])
async def preview_daily_import(
    db: Db,
    file: UploadFile = File(...),
    import_kind: str = Form(...),
    market_date: date = Form(...),
    operator_attestation: str = Form(...),
    campaign_id: str | None = Form(default=None),
) -> dict[str, Any]:
    try:
        return preview_attested_import(
            db,
            filename=file.filename or "upload.csv",
            raw=await file.read(),
            import_kind=import_kind,
            market_date=market_date,
            operator_attestation=operator_attestation,
            raw_dir=Path("../data/raw_imports"),
            campaign_id=campaign_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/data-imports/{batch_id}/activate", dependencies=[Depends(require_api_key)])
def activate_daily_import(batch_id: str, approval: str, db: Db) -> dict[str, Any]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import batch not found")
    try:
        activated = activate_attested_import(db, batch, approval)
        return {
            "batch_id": activated.id,
            "status": activated.status,
            "provenance": "operator_attested",
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/data-imports/{batch_id}/rollback", dependencies=[Depends(require_api_key)])
def rollback_daily_import(batch_id: str, reason: str, db: Db) -> dict[str, Any]:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "Import batch not found")
    try:
        rolled_back = rollback_attested_import(db, batch, reason)
        return {"batch_id": rolled_back.id, "status": rolled_back.status, "raw_retained": True}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/data-imports/templates/{import_kind}")
def download_import_template(import_kind: str) -> Response:
    try:
        return Response(
            import_template(import_kind),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{import_kind}_template.csv"'},
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/data-imports")
def list_daily_imports(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "source_name": item.source_name,
            "source_hash": item.source_hash,
            "status": item.status,
            "import_kind": item.import_kind,
            "market_date": item.market_date.isoformat() if item.market_date else None,
            "campaign_id": item.campaign_id,
            "timestamp_provenance": "operator_attested" if item.operator_attestation else "unknown",
            "exchange_verified": False,
        }
        for item in db.scalars(
            select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)
        )
    ]


@router.post("/market-rules", dependencies=[Depends(require_api_key)])
def add_market_rules(payload: RuleSetCreate, db: Db) -> dict[str, Any]:
    try:
        item = create_rule_set(db, **payload.model_dump())
        return {
            "id": item.id,
            "version": item.version,
            "status": item.verification_status,
            "hash": item.integrity_hash,
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/market-rules")
def list_market_rules(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "version": item.version,
            "status": item.verification_status,
            "hash": item.integrity_hash,
            "effective_date": item.effective_date.isoformat(),
        }
        for item in db.scalars(select(MarketRuleSet).order_by(MarketRuleSet.effective_date.desc()))
    ]


@router.post("/fee-profiles", dependencies=[Depends(require_api_key)])
def add_fee_profile(payload: FeeProfileCreate, db: Db) -> dict[str, Any]:
    item = create_fee_profile(db, **payload.model_dump())
    return {"id": item.id, "name": item.name, "version": item.version, "hash": item.integrity_hash}


@router.get("/fee-profiles")
def list_fee_profiles(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "version": item.version,
            "configuration": item.configuration,
            "effective_date": item.effective_date.isoformat(),
        }
        for item in db.scalars(select(FeeProfile).order_by(FeeProfile.effective_date.desc()))
    ]


@router.get("/fee-profiles/{profile_id}/cost")
def estimate_fee(profile_id: str, side: str, gross: Decimal, db: Db) -> dict[str, Any]:
    profile = db.get(FeeProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Fee profile not found")
    try:
        return {
            "breakdown": {
                key: str(value) for key, value in trade_cost_breakdown(profile, side, gross).items()
            },
            "sensitivity": fee_sensitivity(profile, side, gross),
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/strategies/registry", dependencies=[Depends(require_api_key)])
def add_strategy(payload: StrategyCreate, db: Db) -> dict[str, Any]:
    item = register_strategy(db, **payload.model_dump())
    return {
        "id": item.id,
        "strategy_id": item.strategy_id,
        "version": item.version,
        "state": item.lifecycle_state,
    }


@router.get("/strategies/registry")
def list_strategies(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "strategy_id": item.strategy_id,
            "version": item.version,
            "state": item.lifecycle_state,
            "code_hash": item.code_hash,
            "suspension_reason": item.suspension_reason,
        }
        for item in db.scalars(
            select(StrategyRegistration).order_by(StrategyRegistration.created_at.desc())
        )
    ]


@router.post(
    "/strategies/registry/{registration_id}/transition", dependencies=[Depends(require_api_key)]
)
def change_strategy(registration_id: str, payload: StrategyTransition, db: Db) -> dict[str, Any]:
    item = db.get(StrategyRegistration, registration_id)
    if item is None:
        raise HTTPException(404, "Strategy registration not found")
    try:
        item = promote_strategy(db, item, payload.target_state, payload.operator_approval)
        return {"id": item.id, "state": item.lifecycle_state}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post(
    "/strategies/registry/{registration_id}/evaluate", dependencies=[Depends(require_api_key)]
)
def evaluate_strategy(registration_id: str, payload: StrategyObservation, db: Db) -> dict[str, Any]:
    item = db.get(StrategyRegistration, registration_id)
    if item is None:
        raise HTTPException(404, "Strategy registration not found")
    reason = evaluate_strategy_suspension(db, item, payload.observations)
    return {"id": item.id, "state": item.lifecycle_state, "suspension_reason": reason}


@router.post("/incidents", dependencies=[Depends(require_api_key)])
def add_incident(payload: IncidentCreate, db: Db) -> dict[str, Any]:
    try:
        item = open_incident(db, **payload.model_dump())
        return {"id": item.id, "state": item.state, "severity": item.severity}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/incidents")
def list_incidents(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "campaign_id": item.campaign_id,
            "type": item.incident_type,
            "state": item.state,
            "severity": item.severity,
            "owner": item.owner,
            "opened_at": item.opened_at.isoformat(),
        }
        for item in db.scalars(
            select(OperationalIncident).order_by(OperationalIncident.opened_at.desc())
        )
    ]


@router.post("/incidents/{incident_id}/transition", dependencies=[Depends(require_api_key)])
def change_incident(incident_id: str, payload: IncidentTransition, db: Db) -> dict[str, Any]:
    item = db.get(OperationalIncident, incident_id)
    if item is None:
        raise HTTPException(404, "Incident not found")
    try:
        item = transition_incident(db, item, **payload.model_dump())
        return {"id": item.id, "state": item.state, "owner": item.owner}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/metrics")
def local_metrics(db: Db) -> dict[str, Any]:
    return health_summary(db)


@router.get("/operations/summary")
def operations_summary(db: Db) -> dict[str, Any]:
    campaign = db.scalar(
        select(ValidationCampaign)
        .where(
            ValidationCampaign.state.in_(
                ["active", "paused", "degraded", "reconciliation_required"]
            )
        )
        .order_by(ValidationCampaign.updated_at.desc())
    )
    day = (
        db.scalar(
            select(CampaignDay)
            .where(CampaignDay.campaign_id == campaign.id)
            .order_by(CampaignDay.market_date.desc())
            .limit(1)
        )
        if campaign
        else None
    )
    rule_set = db.get(MarketRuleSet, campaign.active_rule_set_id) if campaign else None
    fee_profile = db.get(FeeProfile, campaign.active_fee_profile_id) if campaign else None
    latest_import = db.scalar(select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(1))
    incidents = db.scalars(
        select(OperationalIncident)
        .where(OperationalIncident.state.in_(["open", "acknowledged", "mitigated"]))
        .order_by(OperationalIncident.opened_at.desc())
    ).all()
    report = campaign_summary(db, campaign) if campaign else None
    latest_backup = day.summary.get("backup") if day else None
    return {
        "current_campaign": campaign_view(campaign) if campaign else None,
        "current_session": {
            "day_id": day.id,
            "date": day.market_date.isoformat(),
            "state": day.state,
        }
        if day
        else None,
        "market_state": day.state if day else "no_active_session",
        "data_provenance": "operator_attested"
        if latest_import and latest_import.operator_attestation
        else "unknown",
        "timestamp_trust": campaign.timestamp_trust_requirement if campaign else "none",
        "rule_set_version": rule_set.version if rule_set else None,
        "rule_set_status": rule_set.verification_status if rule_set else None,
        "fee_profile": f"{fee_profile.name}@{fee_profile.version}" if fee_profile else None,
        "strategy_versions": campaign.approved_strategies if campaign else [],
        "drawdown": report["cumulative"]["maximum_drawdown"] if report else None,
        "backup_status": latest_backup,
        "audit": audit_status(db),
        "unresolved_incidents": [
            {
                "id": item.id,
                "type": item.incident_type,
                "severity": item.severity,
                "state": item.state,
            }
            for item in incidents
        ],
        "observability": health_summary(db),
        "paper_trading": True,
        "live_trading_disabled": True,
    }

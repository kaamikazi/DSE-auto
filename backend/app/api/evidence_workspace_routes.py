from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_api_key
from app.models import (
    AuthoritativeEvidence,
    EvidenceCollectionCase,
    EvidenceSourceProfile,
    ExtractedClaim,
    GovernanceItemApproval,
)
from app.services.evidence_workspace import (
    WORKSPACE_ATTESTATION,
    batch_intake,
    deterministic_extract,
    fee_decision_view,
    generate_scoped_approval_pack,
    initialize_default_cases,
    preview_market_dataset,
    preview_portfolio_statement,
    review_claim,
    rule_decision_view,
    transition_case,
    workspace_summary,
)

router = APIRouter(prefix="/evidence-workspace", tags=["evidence-workspace"])
Db = Annotated[Session, Depends(get_db)]


@router.get("/summary")
def summary(db: Db) -> dict[str, Any]:
    return workspace_summary(db)


@router.get("/cases")
def cases(db: Db) -> list[dict[str, Any]]:
    return [
        {
            "id": item.id,
            "title": item.title,
            "category": item.evidence_category,
            "requested_documents": item.requested_documents,
            "received_documents": item.received_evidence_ids,
            "missing_documents": item.missing_documents,
            "collector": item.responsible_collector,
            "reviewer": item.reviewer,
            "due_date": item.due_date,
            "review_date": item.review_date,
            "notes": item.notes,
            "state": item.state,
        }
        for item in db.scalars(
            select(EvidenceCollectionCase).order_by(EvidenceCollectionCase.title)
        )
    ]


@router.post("/cases/initialize", dependencies=[Depends(require_api_key)])
def initialize_cases(payload: dict[str, str], db: Db) -> dict[str, Any]:
    result = initialize_default_cases(
        db,
        collector=payload.get("collector", "operator"),
        reviewer=payload.get("reviewer") or None,
    )
    return {"cases": len(result), "automatic_approval": False}


@router.post("/cases/{case_id}/state/{target_state}", dependencies=[Depends(require_api_key)])
def change_case_state(
    case_id: str, target_state: str, payload: dict[str, str], db: Db
) -> dict[str, Any]:
    case = db.get(EvidenceCollectionCase, case_id)
    if case is None:
        raise HTTPException(404, "Evidence case not found")
    try:
        transition_case(
            db,
            case,
            target_state,
            actor=payload.get("actor", "operator"),
            notes=payload.get("notes", ""),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": case.id, "state": case.state}


@router.post("/inbox/batch", dependencies=[Depends(require_api_key)])
async def upload_batch(
    db: Db,
    files: Annotated[list[UploadFile], File()],
    case_id: Annotated[str, Form()],
    source_organization: Annotated[str, Form()],
    source_class: Annotated[str, Form()],
    source_description: Annotated[str, Form()],
    operator_attestation: Annotated[str, Form()],
    collected_by: Annotated[str, Form()] = "operator",
    document_date: Annotated[str | None, Form()] = None,
    effective_date: Annotated[str | None, Form()] = None,
    account_or_broker_label: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    case = db.get(EvidenceCollectionCase, case_id)
    if case is None:
        raise HTTPException(404, "Evidence case not found")
    uploads = [
        {
            "filename": item.filename or "evidence.bin",
            "raw": await item.read(),
            "declared_type": item.content_type,
        }
        for item in files
    ]
    try:
        return batch_intake(
            db,
            case=case,
            files=uploads,
            source_organization=source_organization,
            source_class=source_class,
            source_description=source_description,
            operator_attestation=operator_attestation,
            collected_by=collected_by,
            raw_dir=Path("../data/evidence_workspace/raw"),
            document_date=date.fromisoformat(document_date) if document_date else None,
            effective_date=date.fromisoformat(effective_date) if effective_date else None,
            account_or_broker_label=account_or_broker_label,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/evidence/{evidence_id}/extract", dependencies=[Depends(require_api_key)])
def extract(evidence_id: str, payload: dict[str, str], db: Db) -> dict[str, Any]:
    evidence = db.get(AuthoritativeEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "Evidence not found")
    case = (
        db.get(EvidenceCollectionCase, payload.get("case_id")) if payload.get("case_id") else None
    )
    profile = (
        db.get(EvidenceSourceProfile, str(evidence.extraction.get("source_profile_id")))
        if evidence.extraction.get("source_profile_id")
        else None
    )
    try:
        claims = deterministic_extract(db, evidence, case=case, source_profile=profile)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "evidence_id": evidence.id,
        "claims": len(claims),
        "verified": False,
        "automatic_approval": False,
    }


@router.get("/claims")
def claims(db: Db, status: str | None = None) -> list[dict[str, Any]]:
    query = select(ExtractedClaim).order_by(ExtractedClaim.created_at.desc())
    if status:
        query = query.where(ExtractedClaim.reviewer_status == status)
    return [
        {
            "id": item.id,
            "evidence_id": item.evidence_id,
            "claim_type": item.claim_type,
            "source_location": item.source_location,
            "original_value": item.original_value,
            "normalized_interpretation": item.normalized_interpretation,
            "confidence": item.confidence,
            "extraction_method": item.extraction_method,
            "reviewer_status": item.reviewer_status,
            "conflict_reasons": item.conflict_reasons,
        }
        for item in db.scalars(query)
    ]


@router.post("/claims/{claim_id}/review", dependencies=[Depends(require_api_key)])
def decide_claim(claim_id: str, payload: dict[str, Any], db: Db) -> dict[str, Any]:
    claim = db.get(ExtractedClaim, claim_id)
    if claim is None:
        raise HTTPException(404, "Claim not found")
    try:
        review_claim(
            db,
            claim,
            action=str(payload.get("action", "")),
            reviewer=str(payload.get("reviewer", "operator")),
            notes=str(payload.get("notes", "")),
            corrected_interpretation=dict(payload["corrected_interpretation"])
            if payload.get("corrected_interpretation")
            else None,
            supporting_evidence_ids=[
                str(item) for item in payload.get("supporting_evidence_ids", [])
            ],
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "id": claim.id,
        "status": claim.reviewer_status,
        "extraction_accuracy_only": True,
        "configuration_approval": False,
    }


@router.get("/decisions/rules")
def rule_decisions(db: Db) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(GovernanceItemApproval)
        .where(GovernanceItemApproval.approval_type == "rule")
        .order_by(GovernanceItemApproval.item_key)
    )
    return [rule_decision_view(db, row) for row in rows]


@router.get("/decisions/fees")
def fee_decisions(db: Db) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(GovernanceItemApproval)
        .where(GovernanceItemApproval.approval_type == "fee")
        .order_by(GovernanceItemApproval.item_key)
    )
    return [fee_decision_view(db, row) for row in rows]


@router.post("/portfolio-statements/preview", dependencies=[Depends(require_api_key)])
def portfolio_preview(payload: dict[str, str], db: Db) -> dict[str, Any]:
    evidence = db.get(AuthoritativeEvidence, payload.get("evidence_id"))
    if evidence is None:
        raise HTTPException(404, "Evidence not found")
    try:
        draft = preview_portfolio_statement(
            db,
            evidence,
            broker_label=payload["broker_label"],
            account_label=payload["account_label"],
            statement_date=date.fromisoformat(payload["statement_date"]),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": draft.id,
        "state": draft.state,
        "statement_hash": draft.statement_hash,
        "reconciliation": draft.reconciliation_summary,
        "discrepancies": draft.discrepancies,
        "activated": False,
    }


@router.post("/market-datasets/preview", dependencies=[Depends(require_api_key)])
def market_dataset_preview(payload: dict[str, str], db: Db) -> dict[str, Any]:
    evidence = db.get(AuthoritativeEvidence, payload.get("evidence_id"))
    if evidence is None:
        raise HTTPException(404, "Evidence not found")
    try:
        dataset = preview_market_dataset(
            db,
            evidence,
            name=payload["name"],
            timestamp_trust=payload.get("timestamp_trust", "operator_attested"),
            raw_dir=Path("../data/evidence_workspace/datasets/raw"),
            normalized_dir=Path("../data/evidence_workspace/datasets/normalized"),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": dataset.id,
        "status": dataset.status,
        "dataset_hash": dataset.dataset_hash,
        "quality": dataset.quality_report,
        "activated": False,
        "qualification_days": 0,
    }


@router.post("/approval-packs/{scope}", dependencies=[Depends(require_api_key)])
def generate_pack(scope: str, payload: dict[str, str], db: Db) -> dict[str, Any]:
    try:
        pack = generate_scoped_approval_pack(
            db,
            scope=scope,
            output_dir=Path("../reports/evidence_workspace/approval_packs"),
            generated_by=payload.get("generated_by", "operator"),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "id": pack.id,
        "scope": pack.scope,
        "hash": pack.pack_hash,
        "path": pack.output_path,
        "decision_implied": False,
    }


@router.get("/attestation")
def attestation() -> dict[str, str]:
    return {"text": WORKSPACE_ATTESTATION, "warning": "UPLOADED DOES NOT MEAN VERIFIED"}

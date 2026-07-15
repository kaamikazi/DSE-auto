from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CampaignDay, EvidenceReview
from app.services.audit import append_audit

REVIEW_STATES = {
    "pending_review",
    "reviewed",
    "accepted",
    "concerns_found",
    "rejected",
    "requires_rerun",
}
FINAL_REVIEW_STATES = {"accepted", "concerns_found", "rejected", "requires_rerun"}


def _evidence_hash(day: CampaignDay) -> str:
    if day.evidence_path:
        path = Path(day.evidence_path)
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(day.summary, sort_keys=True, default=str).encode()).hexdigest()


def queue_campaign_day_review(db: Session, day: CampaignDay) -> EvidenceReview:
    if not day.eod_completed or day.state != "completed":
        raise ValueError("Only completed campaign days can enter evidence review")
    existing = db.scalar(select(EvidenceReview).where(EvidenceReview.campaign_day_id == day.id))
    if existing is not None:
        return existing
    review = EvidenceReview(
        campaign_day_id=day.id,
        campaign_id=day.campaign_id,
        session_id=day.session_id,
        state="pending_review",
        evidence_pack_hash=_evidence_hash(day),
    )
    db.add(review)
    db.commit()
    return review


def submit_review(
    db: Session,
    review: EvidenceReview,
    *,
    reviewer: str,
    reviewer_role: str,
    target_state: str,
    data_quality_verdict: str,
    strategy_behavior_verdict: str,
    risk_engine_verdict: str,
    execution_model_verdict: str,
    incidents_reviewed: list[str],
    comments: str,
    approval_decision: str,
    review_checklist: dict[str, bool] | None = None,
    concerns: list[str] | None = None,
    linked_evidence_hashes: list[str] | None = None,
) -> EvidenceReview:
    if reviewer_role not in {"reviewer", "operator"}:
        raise PermissionError("Evidence decisions require reviewer or operator role")
    if target_state not in REVIEW_STATES - {"pending_review"}:
        raise ValueError("Invalid evidence review target state")
    if review.state in FINAL_REVIEW_STATES:
        raise ValueError("Final evidence reviews are immutable; create a rerun day")
    required = {
        data_quality_verdict,
        strategy_behavior_verdict,
        risk_engine_verdict,
        execution_model_verdict,
    }
    if not required <= {"pass", "concern", "fail", "not_applicable"}:
        raise ValueError("Review verdict must be pass, concern, fail, or not_applicable")
    checklist = review_checklist or {}
    if checklist and not all(checklist.values()):
        raise ValueError("Every mandatory daily-review checklist item must be confirmed")
    day = db.get(CampaignDay, review.campaign_day_id)
    real_market_review = bool(day and day.evidence_class == "real_market")
    if (
        target_state == "accepted"
        and real_market_review
        and (
            not checklist
            or not linked_evidence_hashes
            or review.evidence_pack_hash not in linked_evidence_hashes
        )
    ):
        raise ValueError("Accepted review requires a complete checklist and linked evidence hash")
    previous = review.state
    review.state = target_state
    review.reviewer = reviewer
    review.reviewer_role = reviewer_role
    review.reviewed_at = datetime.now(UTC)
    review.data_quality_verdict = data_quality_verdict
    review.strategy_behavior_verdict = strategy_behavior_verdict
    review.risk_engine_verdict = risk_engine_verdict
    review.execution_model_verdict = execution_model_verdict
    review.incidents_reviewed = incidents_reviewed
    review.comments = comments
    review.approval_decision = approval_decision
    append_audit(
        db,
        actor=reviewer,
        event_type="evidence.reviewed",
        entity_type="evidence_review",
        entity_id=review.id,
        previous_state={"state": previous},
        new_state={
            "state": target_state,
            "campaign_id": review.campaign_id,
            "session_id": review.session_id,
            "evidence_pack_hash": review.evidence_pack_hash,
            "approval_decision": approval_decision,
        },
        metadata={
            "reviewer_role": reviewer_role,
            "incidents_reviewed": incidents_reviewed,
            "review_checklist": checklist,
            "concerns": concerns or [],
            "linked_evidence_hashes": linked_evidence_hashes or [],
        },
    )
    db.commit()
    return review


def review_view(review: EvidenceReview) -> dict[str, Any]:
    return {
        "id": review.id,
        "campaign_day_id": review.campaign_day_id,
        "campaign_id": review.campaign_id,
        "session_id": review.session_id,
        "state": review.state,
        "reviewer": review.reviewer,
        "reviewer_role": review.reviewer_role,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "evidence_pack_hash": review.evidence_pack_hash,
        "data_quality_verdict": review.data_quality_verdict,
        "strategy_behavior_verdict": review.strategy_behavior_verdict,
        "risk_engine_verdict": review.risk_engine_verdict,
        "execution_model_verdict": review.execution_model_verdict,
        "incidents_reviewed": review.incidents_reviewed,
        "comments": review.comments,
        "approval_decision": review.approval_decision,
    }

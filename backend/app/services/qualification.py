from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CampaignDay,
    DataQualityReport,
    EvidenceReview,
    OperationalIncident,
    PaperQualification,
    ValidationCampaign,
)


def calculate_qualification(
    db: Session,
    campaign_id: str,
    *,
    target_days: int = 60,
    qualification_scope: str = "paper",
) -> PaperQualification:
    if qualification_scope not in {"paper", "real_market"}:
        raise ValueError("Unknown qualification scope")
    campaign = db.get(ValidationCampaign, campaign_id)
    if campaign is None and qualification_scope == "real_market":
        raise ValueError("Campaign not found")
    days = list(
        db.scalars(
            select(CampaignDay)
            .where(CampaignDay.campaign_id == campaign_id)
            .order_by(CampaignDay.market_date)
        )
    )
    reviews = {
        item.campaign_day_id: item
        for item in db.scalars(
            select(EvidenceReview).where(EvidenceReview.campaign_id == campaign_id)
        )
    }
    reports = list(
        db.scalars(select(DataQualityReport).where(DataQualityReport.campaign_id == campaign_id))
    )
    quality_dates = {
        day.market_date
        for day in days
        if any(
            report.passed and report.start_date <= day.market_date <= report.end_date
            for report in reports
        )
    }
    completed = [day for day in days if day.eod_completed and day.state == "completed"]
    reviewed = [day for day in completed if reviews.get(day.id) is not None]
    accepted = [
        day for day in completed if reviews.get(day.id) and reviews[day.id].state == "accepted"
    ]
    rejected = [
        day for day in completed if reviews.get(day.id) and reviews[day.id].state == "rejected"
    ]
    concerns = [
        day
        for day in completed
        if reviews.get(day.id) and reviews[day.id].state == "concerns_found"
    ]
    rerun = [
        day
        for day in completed
        if reviews.get(day.id) and reviews[day.id].state == "requires_rerun"
    ]
    audit_valid = [day for day in completed if bool(day.summary.get("audit_valid"))]
    reconciliation_valid = [
        day for day in completed if bool(day.summary.get("reconciliation", {}).get("healthy"))
    ]
    backup_valid = [
        day for day in completed if bool(day.summary.get("backup", {}).get("successful"))
    ]
    qualifying_days = [
        day
        for day in accepted
        if day in audit_valid
        and day in reconciliation_valid
        and day in backup_valid
        and day.market_date in quality_dates
    ]
    if qualification_scope == "real_market":
        licensed_provider = bool(campaign and campaign.provider_certification_id)
        attested_policy = bool(
            campaign and campaign.data_source_policy.get("allow_operator_attested")
        )
        real_market_eligible = bool(
            campaign
            and campaign.evidence_class == "real_market"
            and (licensed_provider or attested_policy)
        )
        qualifying_days = [
            day
            for day in qualifying_days
            if real_market_eligible
            and day.evidence_class == "real_market"
            and bool(day.summary.get("real_market_eligible"))
            and not bool(day.summary.get("synthetic_or_accelerated"))
            and day.summary.get("timestamp_provenance")
            in {"operator_attested", "exchange_verified"}
            and all(bool(value) for value in day.summary.get("mandatory_evidence", {}).values())
        ]
    unresolved_critical = list(
        db.scalars(
            select(OperationalIncident).where(
                OperationalIncident.campaign_id == campaign_id,
                OperationalIncident.severity == "critical",
                OperationalIncident.state.not_in(("resolved",)),
            )
        )
    )
    counts: dict[str, Any] = {
        "planned_trading_days": target_days,
        "completed_days": len(completed),
        "reviewed_days": len(reviewed),
        "accepted_days": len(accepted),
        "rejected_days": len(rejected),
        "concerns_found_days": len(concerns),
        "missing_days": max(target_days - len(completed), 0),
        "rerun_required_days": len(rerun),
        "audit_valid_days": len(audit_valid),
        "reconciliation_valid_days": len(reconciliation_valid),
        "backup_valid_days": len(backup_valid),
        "data_quality_passing_days": len(
            [day for day in completed if day.market_date in quality_dates]
        ),
        "qualifying_days": len(qualifying_days),
        "unresolved_critical_incidents": len(unresolved_critical),
        "qualification_scope": qualification_scope,
        "real_market_evidence_days": len(
            [day for day in completed if day.evidence_class == "real_market"]
        ),
    }
    failures: list[str] = []
    if len(reviewed) < len(completed):
        failures.append("completed_day_missing_review")
    if len(audit_valid) < len(completed):
        failures.append("audit_invalid_or_missing")
    if len(reconciliation_valid) < len(completed):
        failures.append("reconciliation_invalid_or_missing")
    if len(backup_valid) < len(completed):
        failures.append("backup_invalid_or_missing")
    if len(quality_dates) < len(completed):
        failures.append("data_quality_unacceptable_or_missing")
    if unresolved_critical:
        failures.append("unresolved_critical_incidents")
    if qualification_scope == "real_market" and (
        campaign is None or campaign.evidence_class != "real_market"
    ):
        failures.append("campaign_not_real_market")
    if (
        qualification_scope == "real_market"
        and campaign is not None
        and not (
            campaign.provider_certification_id
            or campaign.data_source_policy.get("allow_operator_attested")
        )
    ):
        failures.append("approved_real_market_source_missing")
    remaining = max(target_days - len(qualifying_days), 0)
    if remaining:
        failures.append("qualification_target_not_met")
    snapshot = db.scalar(
        select(PaperQualification).where(PaperQualification.campaign_id == campaign_id)
    )
    if snapshot is None:
        snapshot = PaperQualification(campaign_id=campaign_id)
        db.add(snapshot)
    snapshot.target_days = target_days
    snapshot.counts = counts
    snapshot.qualifying = not failures and remaining == 0
    snapshot.failure_reasons = failures
    snapshot.remaining_qualifying_days = remaining
    snapshot.calculated_at = datetime.now(UTC)
    db.commit()
    return snapshot

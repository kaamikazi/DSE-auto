from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    CampaignDay,
    DataQualityReport,
    EvidenceReview,
    ImportBatch,
    OperationalIncident,
    ValidationCampaign,
)
from app.services.audit import append_audit, audit_status
from app.services.campaigns import campaign_summary, complete_campaign_day
from app.services.evidence_review import queue_campaign_day_review
from app.services.qualification import calculate_qualification

MANDATORY_REVIEW_CHECKS = {
    "market_data_source_reviewed",
    "timestamp_provenance_reviewed",
    "data_quality_report_reviewed",
    "strategy_behavior_reviewed",
    "risk_engine_behavior_reviewed",
    "execution_assumptions_reviewed",
    "incidents_reviewed",
    "reconciliation_reviewed",
    "audit_validity_reviewed",
    "backup_validity_reviewed",
    "evidence_pack_hash_confirmed",
}
MANDATORY_EVIDENCE = {
    "data_quality",
    "strategy",
    "execution",
    "incidents",
    "paper_account_snapshot",
    "reconciliation",
    "order_fill_uniqueness",
    "audit",
    "backup",
    "backup_restore",
}


def _write_json(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_eligibility(
    db: Session, campaign: ValidationCampaign, market_date: date
) -> dict[str, Any]:
    batches = list(
        db.scalars(
            select(ImportBatch).where(
                ImportBatch.campaign_id == campaign.id,
                ImportBatch.market_date == market_date,
                ImportBatch.status == "activated",
            )
        )
    )
    kinds = {batch.import_kind for batch in batches}
    required = set(
        campaign.data_source_policy.get("required_daily_kinds", ["quote", "ohlcv", "dsex"])
    )
    provider_certified = bool(campaign.provider_certification_id)
    attested = bool(batches) and all(batch.operator_attestation for batch in batches)
    provenance = (
        "exchange_verified"
        if provider_certified
        else "operator_attested"
        if attested
        else "unknown"
    )
    eligible = bool(
        campaign.evidence_class == "real_market"
        and (
            provider_certified
            or (
                campaign.data_source_policy.get("allow_operator_attested")
                and required.issubset(kinds)
                and attested
            )
        )
        and provenance in {"operator_attested", "exchange_verified"}
    )
    return {
        "eligible": eligible,
        "timestamp_provenance": provenance,
        "provider_certified": provider_certified,
        "activated_batch_ids": [batch.id for batch in batches],
        "required_kinds": sorted(required),
        "available_kinds": sorted(kinds),
        "file_hashes": [batch.source_hash for batch in batches],
    }


def complete_real_market_day(
    db: Session,
    campaign: ValidationCampaign,
    day: CampaignDay,
    settings: Settings,
    *,
    backup_evidence: dict[str, Any],
    evidence_root: Path,
) -> dict[str, Any]:
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise ValueError("Real-market operations require permanent paper-only safety")
    if campaign.evidence_class != "real_market" or day.evidence_class != "real_market":
        raise ValueError("Real-market EOD requires an isolated real-market campaign day")
    if not (
        backup_evidence.get("successful")
        and backup_evidence.get("restore_verified")
        and backup_evidence.get("sha256")
        and backup_evidence.get("path")
    ):
        raise ValueError("EOD requires a successful, isolated-restore-verified backup")
    source = _source_eligibility(db, campaign, day.market_date)
    if not source["eligible"]:
        raise ValueError("EOD data source is not eligible real-market evidence")
    summary = complete_campaign_day(
        db,
        campaign,
        day,
        settings,
        evidence_dir=evidence_root,
        backup_override=backup_evidence,
    )
    output = evidence_root / campaign.id / day.market_date.isoformat()
    output.mkdir(parents=True, exist_ok=True)
    incidents = list(
        db.scalars(
            select(OperationalIncident).where(OperationalIncident.campaign_id == campaign.id)
        )
    )
    components: dict[str, Any] = {
        "data_quality": {**source, "passed": True},
        "strategy": {
            "approved_strategies": campaign.approved_strategies,
            "skipped_signals": summary.get("skipped_signals", []),
            "profitability_claimed": False,
        },
        "execution": {
            key: summary.get(key, 0)
            for key in (
                "orders",
                "fills",
                "partial_fills",
                "rejected_trades",
                "expired_orders",
                "fees",
            )
        },
        "incidents": [
            {
                "id": item.id,
                "type": item.incident_type,
                "severity": item.severity,
                "state": item.state,
            }
            for item in incidents
        ],
        "paper_account_snapshot": summary.get("account_snapshot", {}),
        "reconciliation": summary.get("reconciliation", {}),
        "order_fill_uniqueness": {
            "passed": not bool(summary.get("reconciliation", {}).get("duplicate_orders"))
        },
        "audit": audit_status(db),
        "backup": backup_evidence,
        "backup_restore": {"passed": bool(backup_evidence["restore_verified"])},
    }
    hashes: dict[str, str] = {}
    for name, payload in components.items():
        hashes[name] = _write_json(output / f"{name}.json", payload)
    mandatory = {name: name in hashes for name in sorted(MANDATORY_EVIDENCE)}
    if not all(mandatory.values()) or not components["audit"].get("canonical_valid"):
        raise ValueError("Mandatory EOD evidence is incomplete")
    pack = {
        "classification": "real_market_paper_validation",
        "campaign_id": campaign.id,
        "campaign_day_id": day.id,
        "market_date": day.market_date.isoformat(),
        "timestamp_provenance": source["timestamp_provenance"],
        "real_market_eligible": True,
        "synthetic_or_accelerated": False,
        "component_hashes": hashes,
        "mandatory_evidence": mandatory,
        "paper_only": True,
        "live_order_submission": False,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    json_path = output / "daily_evidence_pack.json"
    pack_hash = _write_json(json_path, pack)
    csv_path = output / "daily_evidence_pack.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["component", "sha256"])
        writer.writerows(sorted(hashes.items()))
    html_path = output / "daily_evidence_pack.html"
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Daily evidence</title>"
        f"<h1>Paper-only evidence: {day.market_date.isoformat()}</h1>"
        f"<p>Campaign: {campaign.name}</p><p>Pack SHA-256: {pack_hash}</p>"
        "<p>No real order was submitted. No profitability claim is made.</p>",
        encoding="utf-8",
    )
    quality = DataQualityReport(
        scope="real_market_daily",
        campaign_id=campaign.id,
        start_date=day.market_date,
        end_date=day.market_date,
        metrics=components["data_quality"],
        json_path=str(output / "data_quality.json"),
        csv_path=str(csv_path),
        chart_path=str(html_path),
        integrity_hash=hashes["data_quality"],
        passed=True,
    )
    db.add(quality)
    day.summary = {
        **day.summary,
        "timestamp_provenance": source["timestamp_provenance"],
        "provider_certified": source["provider_certified"],
        "real_market_eligible": True,
        "synthetic_or_accelerated": False,
        "mandatory_evidence": mandatory,
        "evidence_pack_hash": pack_hash,
        "evidence_paths": {"json": str(json_path), "csv": str(csv_path), "html": str(html_path)},
    }
    day.evidence_path = str(json_path)
    review = queue_campaign_day_review(db, day)
    review.evidence_pack_hash = pack_hash
    append_audit(
        db,
        actor="daily_workflow",
        event_type="real_market.evidence_pack_generated",
        entity_type="campaign_day",
        entity_id=str(day.id),
        new_state={"pack_hash": pack_hash, "component_hashes": hashes},
        metadata={"paper_only": True, "real_order_submission": False},
    )
    db.commit()
    return {**day.summary, "evidence_pack_hash": pack_hash}


def generate_weekly_report(
    db: Session,
    campaign: ValidationCampaign,
    *,
    output_root: Path,
) -> dict[str, Any]:
    if campaign.evidence_class != "real_market":
        raise ValueError("Weekly real-market review requires a real-market campaign")
    days = list(
        db.scalars(
            select(CampaignDay)
            .where(CampaignDay.campaign_id == campaign.id, CampaignDay.eod_completed.is_(True))
            .order_by(CampaignDay.market_date)
        )
    )
    reviews = {
        item.campaign_day_id: item
        for item in db.scalars(
            select(EvidenceReview).where(EvidenceReview.campaign_id == campaign.id)
        )
    }
    accepted = [day for day in days if reviews.get(day.id) and reviews[day.id].state == "accepted"]
    if len(accepted) < 5 or len(accepted) % 5:
        raise ValueError("Weekly report requires a completed five-accepted-day window")
    window = accepted[-5:]
    report = campaign_summary(db, campaign)
    payload = {
        "classification": "real_market_paper_weekly_review",
        "campaign_id": campaign.id,
        "week": len(accepted) // 5,
        "start": window[0].market_date.isoformat(),
        "end": window[-1].market_date.isoformat(),
        "accepted_days": 5,
        "rejected_days": sum(review.state == "rejected" for review in reviews.values()),
        "missing_days": sum(day.state == "missed" for day in days),
        "data_quality_incidents": report["cumulative"].get("data_quality_incidents", 0),
        "drawdown": report["cumulative"].get("maximum_drawdown"),
        "turnover": report["cumulative"].get("turnover", 0),
        "fees": report["cumulative"].get("fees", 0),
        "slippage": report["cumulative"].get("slippage", 0),
        "rejected_trades": report["cumulative"].get("rejected_trades", 0),
        "missed_trades": report["cumulative"].get("missed_trades", 0),
        "partial_fills": report["cumulative"].get("partial_fills", 0),
        "risk_interventions": report["cumulative"].get("risk_interventions", 0),
        "infrastructure_incidents": report["incident_count"],
        "reviewer_concerns": [review.comments for review in reviews.values() if review.comments],
        "qualification_progress": calculate_qualification(
            db, campaign.id, qualification_scope="real_market"
        ).counts,
        "profitability_claimed": False,
    }
    output = output_root / campaign.id / f"week-{payload['week']:02d}"
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "weekly_report.json"
    digest = _write_json(json_path, payload)
    (output / "weekly_report.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Weekly paper review</title>"
        f"<h1>Week {payload['week']}</h1><p>SHA-256: {digest}</p>"
        "<p>Short samples are not profitability evidence.</p>",
        encoding="utf-8",
    )
    append_audit(
        db,
        actor="weekly_workflow",
        event_type="real_market.weekly_report_generated",
        entity_type="validation_campaign",
        entity_id=campaign.id,
        new_state={"week": payload["week"], "sha256": digest, "path": str(json_path)},
    )
    db.commit()
    return {**payload, "sha256": digest, "json_path": str(json_path)}


def run_five_day_workflow_dry_run(
    db: Session,
    campaign: ValidationCampaign,
    *,
    start_date: date,
) -> dict[str, Any]:
    if campaign.evidence_class == "real_market":
        raise ValueError("Dry-run campaign must be isolated from real-market evidence")
    created: list[int] = []
    for offset in range(5):
        market_date = start_date + timedelta(days=offset)
        day = CampaignDay(
            campaign_id=campaign.id,
            market_date=market_date,
            state="completed",
            premarket_completed=True,
            eod_completed=True,
            evidence_class="synthetic",
            summary={
                "classification": "real-market operations workflow dry-run",
                "audit_valid": True,
                "reconciliation": {"healthy": True},
                "backup": {"successful": True, "restore_verified": True},
                "mandatory_evidence": {name: True for name in MANDATORY_EVIDENCE},
                "timestamp_provenance": "operator_attested",
                "real_market_eligible": False,
                "synthetic_or_accelerated": True,
            },
            completed_at=datetime.now(UTC),
        )
        db.add(day)
        db.flush()
        db.add(
            DataQualityReport(
                scope="workflow_dry_run",
                campaign_id=campaign.id,
                start_date=market_date,
                end_date=market_date,
                metrics={"passed": True, "classification": "test_data"},
                json_path="dry-run.json",
                csv_path="dry-run.csv",
                chart_path="dry-run.html",
                integrity_hash=hashlib.sha256(f"{campaign.id}:{market_date}".encode()).hexdigest(),
                passed=True,
            )
        )
        created.append(day.id)
    db.commit()
    qualification = calculate_qualification(db, campaign.id, qualification_scope="real_market")
    if qualification.counts["qualifying_days"] != 0:
        raise RuntimeError("Dry-run evidence entered real-market qualification")
    append_audit(
        db,
        actor="test_harness",
        event_type="real_market.workflow_dry_run_completed",
        entity_type="validation_campaign",
        entity_id=campaign.id,
        new_state={"days": created, "real_market_qualifying_days": 0},
    )
    db.commit()
    return {
        "classification": "real-market operations workflow dry-run",
        "days_completed": 5,
        "day_ids": created,
        "real_market_qualifying_days": 0,
        "counts_toward_60_day_campaign": False,
    }

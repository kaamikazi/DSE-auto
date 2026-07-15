from __future__ import annotations

import hashlib
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    CampaignDay,
    DataQualityReport,
    EvidenceReview,
    ImportBatch,
    OperationalIncident,
    Order,
    PaperAccount,
    ValidationCampaign,
)
from app.services.attested_imports import (
    ATTESTATION,
    activate_attested_import,
    preview_attested_import,
)
from app.services.audit import initialize_canonical_chain
from app.services.campaigns import evaluate_campaign_readiness
from app.services.evidence_review import submit_review
from app.services.portfolio import derive_portfolio
from app.services.portfolio_imports import (
    PORTFOLIO_ATTESTATION,
    activate_real_portfolio,
    preview_real_portfolio,
    reverse_import,
)
from app.services.qualification import calculate_qualification
from app.services.real_market_operations import (
    MANDATORY_EVIDENCE,
    MANDATORY_REVIEW_CHECKS,
    complete_real_market_day,
    generate_weekly_report,
    run_five_day_workflow_dry_run,
)


def _campaign(db: Session, *, evidence_class: str = "real_market") -> ValidationCampaign:
    item = ValidationCampaign(
        name=f"m10-{evidence_class}",
        start_date=date(2026, 7, 20),
        planned_end_date=date(2026, 10, 30),
        approved_symbols=["GP"],
        approved_strategies=["ma_crossover@1"],
        starting_capital=Decimal("1000000"),
        risk_profile={"max_drawdown": 0.1},
        data_source_policy={
            "allow_operator_attested": True,
            "qualification_target_days": 60,
            "required_daily_kinds": ["quote", "ohlcv", "dsex"],
            "approved": ["attested_csv"],
        },
        timestamp_trust_requirement="operator_attested",
        fill_model="pessimistic",
        benchmark="DSEX",
        state="active",
        active_rule_set_id="rules-v1",
        active_fee_profile_id="fees-v1",
        evidence_class=evidence_class,
        daily_reviewer_assignments={"default": "reviewer-one"},
    )
    db.add(item)
    db.commit()
    return item


def _activate_daily_inputs(
    db: Session, tmp_path: Path, campaign: ValidationCampaign, market_date: date
) -> None:
    values = {
        "quote": "symbol,timestamp,last_price,volume,source\nGP,{stamp},250,1000,reviewed_file\n",
        "ohlcv": "symbol,timestamp,open,high,low,close,volume,source\nGP,{stamp},248,252,247,250,1000,reviewed_file\n",
        "dsex": "timestamp,index_value,volume,source\n{stamp},5200,100000,reviewed_file\n",
    }
    stamp = f"{market_date.isoformat()}T14:30:00+06:00"
    for kind, template in values.items():
        raw = template.format(stamp=stamp).encode()
        preview = preview_attested_import(
            db,
            filename=f"{kind}-{market_date}.csv",
            raw=raw,
            import_kind=kind,
            market_date=market_date,
            operator_attestation=ATTESTATION,
            raw_dir=tmp_path,
            campaign_id=campaign.id,
        )
        batch = db.get(ImportBatch, preview["batch_id"])
        assert batch is not None
        activate_attested_import(db, batch, "Operator approves reviewed daily input")


def _quality(db: Session, campaign_id: str, market_date: date, suffix: str) -> None:
    db.add(
        DataQualityReport(
            scope="daily",
            campaign_id=campaign_id,
            start_date=market_date,
            end_date=market_date,
            metrics={"passed": True},
            json_path="quality.json",
            csv_path="quality.csv",
            chart_path="quality.html",
            integrity_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            passed=True,
        )
    )


def test_operator_attested_intake_is_immutable_and_never_exchange_verified(
    db: Session, tmp_path: Path
) -> None:
    campaign = _campaign(db)
    raw = b"symbol,timestamp,status,reason,source\nGP,2026-07-20T12:00:00+06:00,active,reviewed,manual\n"
    preview = preview_attested_import(
        db,
        filename="suspension.csv",
        raw=raw,
        import_kind="suspension",
        market_date=date(2026, 7, 20),
        operator_attestation=ATTESTATION,
        raw_dir=tmp_path,
        campaign_id=campaign.id,
    )
    assert preview["exchange_verified"] is False
    assert preview["valid_rows"][0]["timestamp_provenance"] == "operator_attested"
    assert Path(preview["raw_file_path"]).read_bytes() == raw
    batch = db.get(ImportBatch, preview["batch_id"])
    assert batch is not None
    activate_attested_import(db, batch, "Operator approves reviewed suspension")
    assert batch.status == "activated"
    with pytest.raises(ValueError, match="Duplicate batch"):
        preview_attested_import(
            db,
            filename="duplicate.csv",
            raw=raw,
            import_kind="suspension",
            market_date=date(2026, 7, 20),
            operator_attestation=ATTESTATION,
            raw_dir=tmp_path,
        )


def test_intake_rejects_missing_attestation_and_wrong_market_date(
    db: Session, tmp_path: Path
) -> None:
    raw = b"symbol,timestamp,last_price,source\nGP,2026-07-19T12:00:00+06:00,250,manual\n"
    with pytest.raises(ValueError, match="confirm exactly"):
        preview_attested_import(
            db,
            filename="bad.csv",
            raw=raw,
            import_kind="quote",
            market_date=date(2026, 7, 20),
            operator_attestation="",
            raw_dir=tmp_path,
        )
    result = preview_attested_import(
        db,
        filename="wrong-date.csv",
        raw=raw,
        import_kind="quote",
        market_date=date(2026, 7, 20),
        operator_attestation=ATTESTATION,
        raw_dir=tmp_path,
    )
    assert result["status"] == "rejected"
    assert "attested market date" in result["errors"][0]["error"]


def test_premarket_fails_closed_for_missing_controls_and_critical_incident(db: Session) -> None:
    campaign = _campaign(db)
    db.add(
        OperationalIncident(
            campaign_id=campaign.id,
            incident_type="database_failure",
            severity="critical",
            state="open",
        )
    )
    db.commit()
    result = evaluate_campaign_readiness(
        db,
        campaign,
        get_settings(),
        campaign.start_date,
        operator_acknowledgement="Operator acknowledges premarket responsibility",
    )
    assert result["ready"] is False
    assert result["checks"]["critical_incidents"]["passed"] is False
    assert result["checks"]["provider_or_import"]["passed"] is False


def test_eod_generates_complete_evidence_and_review_queue(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(
        db,
        tmp_path / "audit",
        "Test operator authorizes an isolated canonical chain",
    )
    campaign = _campaign(db)
    db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    db.commit()
    _activate_daily_inputs(db, tmp_path / "raw", campaign, campaign.start_date)
    day = CampaignDay(
        campaign_id=campaign.id,
        market_date=campaign.start_date,
        state="market_open",
        premarket_completed=True,
        evidence_class="real_market",
        summary={"paper_only": True, "synthetic_or_accelerated": False},
    )
    db.add(day)
    db.commit()
    result = complete_real_market_day(
        db,
        campaign,
        day,
        get_settings(),
        backup_evidence={
            "successful": True,
            "restore_verified": True,
            "path": "isolated.dump",
            "sha256": "a" * 64,
        },
        evidence_root=tmp_path / "evidence",
    )
    assert all(result["mandatory_evidence"].values())
    assert result["timestamp_provenance"] == "operator_attested"
    assert result["real_market_eligible"] is True
    assert result["synthetic_or_accelerated"] is False
    assert all(Path(path).is_file() for path in result["evidence_paths"].values())
    review = db.scalar(select(EvidenceReview).where(EvidenceReview.campaign_day_id == day.id))
    assert review is not None and review.evidence_pack_hash == result["evidence_pack_hash"]
    assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_review_rejection_rerun_and_strict_accepted_counting(db: Session) -> None:
    campaign = _campaign(db)
    decisions = ["accepted", "rejected", "requires_rerun"]
    for index, decision in enumerate(decisions):
        market_date = campaign.start_date + timedelta(days=index)
        day = CampaignDay(
            campaign_id=campaign.id,
            market_date=market_date,
            state="completed",
            premarket_completed=True,
            eod_completed=True,
            evidence_class="real_market",
            summary={
                "audit_valid": True,
                "reconciliation": {"healthy": True},
                "backup": {"successful": True},
                "mandatory_evidence": {name: True for name in MANDATORY_EVIDENCE},
                "timestamp_provenance": "operator_attested",
                "real_market_eligible": True,
                "synthetic_or_accelerated": False,
            },
        )
        db.add(day)
        db.flush()
        _quality(db, campaign.id, market_date, str(index))
        review = EvidenceReview(
            campaign_day_id=day.id,
            campaign_id=campaign.id,
            state="pending_review",
            evidence_pack_hash=hashlib.sha256(str(index).encode()).hexdigest(),
        )
        db.add(review)
        db.flush()
        submit_review(
            db,
            review,
            reviewer="reviewer-one",
            reviewer_role="reviewer",
            target_state=decision,
            data_quality_verdict="pass" if decision == "accepted" else "concern",
            strategy_behavior_verdict="pass" if decision == "accepted" else "concern",
            risk_engine_verdict="pass",
            execution_model_verdict="pass",
            incidents_reviewed=[],
            comments=decision,
            approval_decision=decision,
            review_checklist={name: True for name in MANDATORY_REVIEW_CHECKS},
            concerns=[] if decision == "accepted" else [decision],
            linked_evidence_hashes=[review.evidence_pack_hash],
        )
    result = calculate_qualification(db, campaign.id, qualification_scope="real_market")
    assert result.counts["accepted_days"] == 1
    assert result.counts["rejected_days"] == 1
    assert result.counts["rerun_required_days"] == 1
    assert result.counts["qualifying_days"] == 1
    assert result.remaining_qualifying_days == 59


def test_synthetic_dry_run_never_counts_toward_real_market(db: Session) -> None:
    campaign = _campaign(db, evidence_class="synthetic")
    result = run_five_day_workflow_dry_run(db, campaign, start_date=campaign.start_date)
    assert result["classification"] == "real-market operations workflow dry-run"
    assert result["days_completed"] == 5
    assert result["real_market_qualifying_days"] == 0
    assert result["counts_toward_60_day_campaign"] is False
    assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_weekly_report_requires_five_accepted_days(db: Session, tmp_path: Path) -> None:
    campaign = _campaign(db)
    for index in range(5):
        market_date = campaign.start_date + timedelta(days=index)
        day = CampaignDay(
            campaign_id=campaign.id,
            market_date=market_date,
            state="completed",
            premarket_completed=True,
            eod_completed=True,
            evidence_class="real_market",
            summary={"account_snapshot": {"cash": "1000000"}},
        )
        db.add(day)
        db.flush()
        db.add(
            EvidenceReview(
                campaign_day_id=day.id,
                campaign_id=campaign.id,
                state="accepted",
                reviewer="reviewer-one",
                evidence_pack_hash=hashlib.sha256(str(index).encode()).hexdigest(),
            )
        )
    db.commit()
    result = generate_weekly_report(db, campaign, output_root=tmp_path)
    assert result["accepted_days"] == 5
    assert result["profitability_claimed"] is False
    assert Path(result["json_path"]).is_file()


def test_reference_portfolio_attestation_cash_isolation_and_reversal(db: Session) -> None:
    db.add(PaperAccount(id=1, cash=Decimal("900000"), starting_cash=Decimal("900000")))
    db.commit()
    raw = (
        b"occurred_at,transaction_type,symbol,quantity,price,fees,taxes,broker,account_label,notes\n"
        b"2026-07-20T10:00:00+06:00,buy,GP,10,250,0,0,manual,reference,statement\n"
        b"2026-07-20T10:00:00+06:00,cash_balance,BDT,0,50000,0,0,manual,reference,statement\n"
    )
    preview = preview_real_portfolio(
        db,
        "reference.csv",
        raw,
        statement_date="2026-07-20",
        source_description="Reviewed broker statement",
        attestation=PORTFOLIO_ATTESTATION,
    )
    assert preview["activation_allowed"] is True
    batch = activate_real_portfolio(
        db,
        "reference.csv",
        raw,
        statement_date="2026-07-20",
        source_description="Reviewed broker statement",
        attestation=PORTFOLIO_ATTESTATION,
    )
    reference = derive_portfolio(db, account_label="reference")
    assert reference.holdings[0].quantity == Decimal("10")
    assert reference.cash == Decimal("50000")
    assert derive_portfolio(db, account_label="paper").holdings == []
    assert derive_portfolio(db, account_label="paper").cash == Decimal("900000")
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    reverse_import(db, batch)
    assert derive_portfolio(db, account_label="reference").holdings == []
    assert derive_portfolio(db, account_label="reference").cash == Decimal("0")


def test_reference_portfolio_rejects_credentials(db: Session) -> None:
    raw = (
        b"occurred_at,transaction_type,symbol,quantity,price,broker,account_label,password\n"
        b"2026-07-20T10:00:00+06:00,buy,GP,10,250,manual,reference,secret\n"
    )
    with pytest.raises(ValueError, match="Credential columns are forbidden"):
        preview_real_portfolio(
            db,
            "unsafe.csv",
            raw,
            statement_date="2026-07-20",
            source_description="Reviewed broker statement",
            attestation=PORTFOLIO_ATTESTATION,
        )

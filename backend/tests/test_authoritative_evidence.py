from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, PaperSession, StrategyRegistration, Transaction, ValidationCampaign
from app.services.audit import verify_audit_chain
from app.services.authoritative_evidence import (
    approve_dataset_for_research,
    approve_governance_item,
    calibrate_risk_limits,
    create_approval_matrix,
    create_research_dataset,
    intake_evidence_file,
    invite_reviewer,
    matrix_ready,
    promotion_readiness,
    reject_blanket_approval,
    review_evidence,
    run_ma_crossover_research,
    submit_manual_evidence,
    verify_evidence_file_integrity,
)
from app.services.governance import promote_strategy, register_strategy
from app.services.research_governance import (
    PARAMETERS,
    build_fee_verification_review,
    build_risk_limit_review,
    build_rule_verification_review,
    parameter_set_hash,
    strategy_code_hash,
)


def _file_evidence(db: Session, tmp_path: Path, raw: bytes = b"rule,value\ntimezone,Asia/Dhaka\n"):
    return intake_evidence_file(
        db,
        category="dse_trading_rules",
        title="Test rule evidence",
        source_organization="Synthetic test organization",
        source_type="deterministic_fixture",
        source_reference="fixture://rule-evidence",
        collected_by="test-operator",
        source_description="Deterministic fixture; not external evidence",
        operator_attestation="I attest this deterministic fixture is test-only.",
        filename="rules.csv",
        raw=raw,
        raw_dir=tmp_path / "raw",
        declared_type="text/csv",
        affected_fields=["timezone"],
        extracted_claim="Asia/Dhaka",
        extraction={"method": "test fixture"},
    )


def _verified_evidence(db: Session, tmp_path: Path):  # type: ignore[no-untyped-def]
    item = _file_evidence(db, tmp_path)
    return review_evidence(
        db,
        item,
        reviewer="synthetic-independent-reviewer",
        reviewer_is_operator=False,
        status="verified",
        confidence="high",
        notes="Synthetic test review only",
    )


def _registration(db: Session) -> StrategyRegistration:
    registration = register_strategy(
        db,
        strategy_id="ma_crossover",
        version="1.0.0",
        code_hash=strategy_code_hash(),
        parameters={**PARAMETERS, "parameter_set_hash": parameter_set_hash(PARAMETERS)},
        data_requirements={"symbols": ["GP", "ACI", "BRACBANK", "DSEX"]},
        minimum_sample_size=252,
        evidence={
            "walk_forward_report": "synthetic",
            "sensitivity_report": "synthetic",
            "promotion_authorized": False,
            "independent_risk_review": False,
        },
    )
    promote_strategy(db, registration, "research", "Research-only test registration")
    return registration


def _dataset_csv(*, missing_day: bool = False) -> bytes:
    header = "symbol,timestamp,open,high,low,close,volume,source_timestamp,source,corporate_action,corporate_action_factor"
    dates = ["2026-07-13", "2026-07-14", "2026-07-15"]
    if missing_day:
        dates.pop(1)
    rows = [header]
    for symbol_index, symbol in enumerate(("GP", "ACI", "BRACBANK", "DSEX")):
        for index, day in enumerate(dates):
            close = 100 + symbol_index * 10 + index
            factor = "2" if symbol == "GP" and index == 1 and not missing_day else "1"
            action = "split" if factor == "2" else ""
            rows.append(
                f"{symbol},{day}T14:30:00+06:00,{close},{close + 1},{close - 1},{close},100000,"
                f"{day}T14:30:00+06:00,fixture,{action},{factor}"
            )
    return ("\n".join(rows) + "\n").encode()


def test_evidence_duplicate_hash_and_integrity(db: Session, tmp_path: Path) -> None:
    item = _file_evidence(db, tmp_path)
    assert item.verification_status == "submitted"
    assert item.extraction["human_verified"] is False
    assert item.file_hash == hashlib.sha256(Path(item.raw_file_path or "").read_bytes()).hexdigest()
    assert verify_evidence_file_integrity(item)
    with pytest.raises(ValueError, match="Duplicate"):
        _file_evidence(db, tmp_path)
    Path(item.raw_file_path or "").write_bytes(b"tampered")
    assert verify_evidence_file_integrity(item) is False


def test_unsupported_or_executable_file_is_rejected(db: Session, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        intake_evidence_file(
            db,
            category="rules",
            title="bad",
            source_organization="test",
            source_type="file",
            source_reference="fixture://bad",
            collected_by="test",
            source_description="bad",
            operator_attestation="This is a test attestation.",
            filename="payload.exe",
            raw=b"MZpayload",
            raw_dir=tmp_path,
        )


def test_conflicting_and_expired_evidence_fail_closed(db: Session) -> None:
    first = submit_manual_evidence(
        db,
        category="tick_sizes",
        title="first",
        source_organization="A",
        source_type="manual",
        source_reference="fixture://a",
        collected_by="operator",
        extracted_claim="0.1",
        affected_fields=["tick_sizes"],
    )
    review_evidence(
        db,
        first,
        reviewer="reviewer-a",
        reviewer_is_operator=False,
        status="verified",
        confidence="high",
        notes="test",
    )
    second = submit_manual_evidence(
        db,
        category="tick_sizes",
        title="second",
        source_organization="B",
        source_type="manual",
        source_reference="fixture://b",
        collected_by="operator",
        extracted_claim="0.5",
        affected_fields=["tick_sizes"],
    )
    assert (
        review_evidence(
            db,
            second,
            reviewer="reviewer-b",
            reviewer_is_operator=False,
            status="verified",
            confidence="high",
            notes="test",
        ).verification_status
        == "conflicting"
    )
    expired = submit_manual_evidence(
        db,
        category="tax",
        title="expired",
        source_organization="C",
        source_type="manual",
        source_reference="fixture://c",
        collected_by="operator",
        extracted_claim="old",
        affected_fields=["tax"],
    )
    expired.review_date = date.today() - timedelta(days=1)
    assert (
        review_evidence(
            db,
            expired,
            reviewer="reviewer",
            reviewer_is_operator=False,
            status="under_review",
            confidence="low",
            notes="expired",
        ).verification_status
        == "expired"
    )


def test_rule_level_approval_and_blanket_rejection(db: Session, tmp_path: Path) -> None:
    rows = create_approval_matrix(
        db,
        approval_type="rule",
        draft_version="dse-paper-rules-v1-draft",
        items=build_rule_verification_review()["items"],
    )
    assert len(rows) == 16 and not matrix_ready(rows, 16)
    with pytest.raises(ValueError, match="Blanket"):
        reject_blanket_approval(rows)
    evidence = _verified_evidence(db, tmp_path)
    approved = approve_governance_item(
        db,
        rows[0],
        evidence_ids=[evidence.id],
        proposed_value={"value": "Asia/Dhaka"},
        effective_date=date.today(),
        operator_identity="operator",
        reviewer_identity="synthetic-independent-reviewer",
        reviewer_is_operator=False,
    )
    assert approved.approval_status == "approved"
    assert approved.audit_event_id and approved.decision_hash
    assert not matrix_ready(rows, 16)


def test_fee_level_approval_leaves_unresolved_profile_blocked(db: Session, tmp_path: Path) -> None:
    rows = create_approval_matrix(
        db,
        approval_type="fee",
        draft_version="1.0-draft",
        items=build_fee_verification_review()["items"],
    )
    assert len(rows) == 12
    evidence = _verified_evidence(db, tmp_path)
    approve_governance_item(
        db,
        rows[0],
        evidence_ids=[evidence.id],
        proposed_value={"value": "0.5", "unit": "% gross"},
        effective_date=date.today(),
        operator_identity="operator",
        reviewer_identity="synthetic-independent-reviewer",
        reviewer_is_operator=False,
    )
    assert rows[0].approval_status == "approved"
    assert all(row.approval_status == "unapproved" for row in rows[1:])
    assert matrix_ready(rows, 12) is False


def test_risk_calibration_never_approves(db: Session) -> None:
    registration = _registration(db)
    limits = build_risk_limit_review()["limits"]
    run = calibrate_risk_limits(
        db, strategy_registration_id=registration.id, proposed_limits=limits
    )
    assert len(run.report["limits"]) == 12
    assert run.report["auto_approved"] is False
    assert all(item["recommended_status"] == "review_required" for item in run.report["limits"])
    assert all(item["approved"] is False for item in run.report["limits"])


def test_reviewer_conflict_is_visible(db: Session) -> None:
    invitation = invite_reviewer(
        db,
        reviewer_identity="operator-reviewer",
        role="strategy_reviewer",
        invited_by="operator-reviewer",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        conflict_declaration="Operator and proposed reviewer are the same person.",
    )
    assert invitation.independence == "non_independent"
    assert invitation.access_scope == ["evidence:read", "review:decide", "configuration:no_write"]


def test_dataset_hash_provenance_corporate_actions_and_research(
    db: Session, tmp_path: Path
) -> None:
    evidence = _verified_evidence(db, tmp_path)
    raw = _dataset_csv()
    dataset = create_research_dataset(
        db,
        name="synthetic-complete",
        raw=raw,
        filename="research.csv",
        source_evidence=evidence,
        timestamp_trust="operator_attested",
        raw_dir=tmp_path / "dataset-raw",
        normalized_dir=tmp_path / "normalized",
    )
    assert dataset.source_hash == hashlib.sha256(raw).hexdigest()
    assert dataset.quality_report["corporate_actions_applied"] == 1
    assert dataset.quality_report["passed"] is True
    normalized = json.loads(Path(dataset.normalized_file_path).read_text())
    adjusted = next(
        row for row in normalized if row["symbol"] == "GP" and row["corporate_action"] == "split"
    )
    assert Decimal(adjusted["adjusted_close"]) == Decimal(adjusted["close"]) * 2
    approve_dataset_for_research(db, dataset, operator_identity="test-operator")
    result = run_ma_crossover_research(dataset)
    assert result["classification"] == "research_only"
    assert result["dataset_hash"] == dataset.dataset_hash
    assert result["promotion_authorized"] is False


def test_dataset_missing_day_detection(db: Session, tmp_path: Path) -> None:
    evidence = _verified_evidence(db, tmp_path)
    dataset = create_research_dataset(
        db,
        name="synthetic-missing",
        raw=_dataset_csv(missing_day=True),
        filename="missing.csv",
        source_evidence=evidence,
        timestamp_trust="operator_attested",
        raw_dir=tmp_path / "dataset-raw",
        normalized_dir=tmp_path / "normalized",
    )
    assert dataset.status == "quality_failed"
    assert dataset.quality_report["missing_days"]
    with pytest.raises(ValueError, match="quality"):
        approve_dataset_for_research(db, dataset, operator_identity="operator")


def test_strategy_readiness_fails_closed_without_activation(db: Session) -> None:
    registration = _registration(db)
    report = promotion_readiness(db, registration)
    assert report.status == "evidence_incomplete"
    assert "approved_rule_set" in report.missing_items
    assert "separate_operator_approval" in report.missing_items
    db.refresh(registration)
    assert registration.lifecycle_state == "research"
    assert db.scalar(select(func.count()).select_from(ValidationCampaign)) == 0
    assert db.scalar(select(func.count()).select_from(PaperSession)) == 0
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0
    assert verify_audit_chain(db)


def test_governance_dashboard_summary_is_read_only(client: TestClient) -> None:
    response = client.get("/api/v1/infrastructure/governance/pre-campaign")
    assert response.status_code == 200
    payload = response.json()
    assert payload["paper_trading"] is True
    assert payload["live_trading_enabled"] is False
    assert payload["strategy_promotion_readiness"] == "evidence_incomplete"
    assert payload["campaign"] == {
        "created": False,
        "active": False,
        "qualification": "0/60",
    }
    assert payload["proof_no_activation"]["orders"] == 0

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ApprovalPackRecord,
    AuthoritativeEvidence,
    EvidenceCollectionCase,
    Order,
    PaperSession,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import verify_audit_chain
from app.services.authoritative_evidence import create_approval_matrix
from app.services.evidence_workspace import (
    WORKSPACE_ATTESTATION,
    add_manual_claim,
    batch_intake,
    completeness_tracker,
    create_collection_case,
    create_source_profile,
    detect_claim_conflicts,
    deterministic_extract,
    fee_decision_view,
    generate_scoped_approval_pack,
    initialize_default_cases,
    preview_market_dataset,
    preview_portfolio_statement,
    reverse_portfolio_statement_draft,
    review_claim,
    rule_decision_view,
    transition_case,
    workspace_summary,
)
from app.services.research_governance import (
    build_fee_verification_review,
    build_rule_verification_review,
)


def _case(db: Session) -> EvidenceCollectionCase:
    return create_collection_case(
        db,
        title="Trading rules",
        category="dse_market_rules",
        requested_documents=["official rule publication"],
        collector="test-operator",
        reviewer="test-reviewer",
    )


def _upload(
    db: Session,
    tmp_path: Path,
    *,
    raw: bytes,
    filename: str,
    case: EvidenceCollectionCase | None = None,
) -> AuthoritativeEvidence:
    target = case or _case(db)
    result = batch_intake(
        db,
        case=target,
        files=[{"filename": filename, "raw": raw}],
        source_organization="Synthetic test source",
        source_class="official_exchange_publication",
        source_description="Deterministic test fixture, not external evidence",
        operator_attestation=WORKSPACE_ATTESTATION,
        collected_by="test-operator",
        raw_dir=tmp_path / "raw",
        document_date=date(2026, 7, 1),
        effective_date=date(2026, 7, 1),
    )
    assert result["automatic_approval"] is False
    return db.get(AuthoritativeEvidence, result["accepted"][0]["id"])  # type: ignore[return-value]


def _dataset_csv(*, duplicate: bool = False, outlier: bool = False) -> bytes:
    header = (
        "symbol,timestamp,open,high,low,close,volume,source_timestamp,source,"
        "corporate_action,corporate_action_factor"
    )
    rows = [header]
    for symbol_index, symbol in enumerate(("GP", "ACI", "BRACBANK", "DSEX")):
        for index, day in enumerate(("2026-07-13", "2026-07-14", "2026-07-15")):
            close = 100 + symbol_index * 10 + index
            if outlier and symbol == "GP" and index == 2:
                close = 10000
            row = (
                f"{symbol},{day}T14:30:00+06:00,{close},{close + 1},{close - 1},"
                f"{close},100000,{day}T14:30:00+06:00,fixture,,1"
            )
            rows.append(row)
            if duplicate and symbol == "GP" and index == 0:
                rows.append(row)
    return ("\n".join(rows) + "\n").encode()


def test_case_lifecycle_and_default_case_idempotency(db: Session) -> None:
    cases = initialize_default_cases(db, collector="operator", reviewer="reviewer")
    assert len(cases) == 16
    assert len(initialize_default_cases(db, collector="operator", reviewer="reviewer")) == 16
    assert db.scalar(select(func.count()).select_from(EvidenceCollectionCase)) == 16
    case = cases[0]
    transition_case(db, case, "awaiting_documents", actor="operator")
    transition_case(db, case, "documents_received", actor="operator")
    with pytest.raises(ValueError, match="Invalid"):
        transition_case(db, case, "completed", actor="operator")
    assert verify_audit_chain(db)


def test_batch_intake_preserves_hash_rejects_duplicate_and_never_verifies(
    db: Session, tmp_path: Path
) -> None:
    case = _case(db)
    evidence = _upload(
        db, tmp_path, raw=b"tick_size,value\nGP,0.1\n", filename="../rules.csv", case=case
    )
    assert evidence.verification_status == "submitted"
    assert evidence.original_filename == "rules.csv"
    assert Path(evidence.raw_file_path or "").exists()
    duplicate = batch_intake(
        db,
        case=case,
        files=[{"filename": "rules-again.csv", "raw": b"tick_size,value\nGP,0.1\n"}],
        source_organization="Synthetic test source",
        source_class="official_exchange_publication",
        source_description="Deterministic test fixture",
        operator_attestation=WORKSPACE_ATTESTATION,
        collected_by="operator",
        raw_dir=tmp_path / "raw",
    )
    assert not duplicate["accepted"]
    assert "Duplicate" in duplicate["errors"][0]["error"]
    assert case.state == "documents_received"


def test_deterministic_extraction_preserves_source_and_human_correction(
    db: Session, tmp_path: Path
) -> None:
    case = _case(db)
    evidence = _upload(
        db,
        tmp_path,
        raw=b"tick_size,settlement_cycle\n0.10,T+2\n",
        filename="rules.csv",
        case=case,
    )
    profile = create_source_profile(
        db,
        name="DSE fixture",
        source_class="official_exchange_publication",
        authority_scope=["dse_market_rules"],
    )
    claims = deterministic_extract(db, evidence, case=case, source_profile=profile)
    assert {item.claim_type for item in claims} == {"tick_sizes", "settlement"}
    assert all(item.source_location.startswith("row 2") for item in claims)
    assert all(item.reviewer_status == "pending" for item in claims)
    corrected = review_claim(
        db,
        claims[0],
        action="correct",
        reviewer="human-reviewer",
        notes="Corrected deterministic normalization only",
        corrected_interpretation={"value": "0.10"},
    )
    assert corrected.reviewer_status == "corrected"
    assert corrected.normalized_interpretation["authoritative_verification"] is False


def test_source_hierarchy_and_conflict_resolution_require_human_review(
    db: Session, tmp_path: Path
) -> None:
    evidence = _upload(db, tmp_path, raw=b"tick_size\n0.10\n", filename="first.csv")
    official = create_source_profile(
        db,
        name="Official",
        source_class="official_exchange_publication",
        authority_scope=["tick_sizes"],
    )
    informal = create_source_profile(
        db,
        name="Informal",
        source_class="informal_webpage",
        authority_scope=["tick_sizes"],
    )
    assert official.hierarchy_rank < informal.hierarchy_rank
    assert not official.auto_verified and not informal.auto_verified
    first = add_manual_claim(
        db,
        evidence,
        claim_type="tick_sizes",
        source_location="page 1",
        original_value="0.10",
        normalized_interpretation={"value": "0.10"},
    )
    first.source_profile_id = official.id
    review_claim(db, first, action="accept", reviewer="reviewer", notes="accuracy reviewed")
    second = add_manual_claim(
        db,
        evidence,
        claim_type="tick_sizes",
        source_location="page 2",
        original_value="0.50",
        normalized_interpretation={"value": "0.50"},
    )
    second.source_profile_id = informal.id
    review_claim(db, second, action="accept", reviewer="reviewer", notes="accuracy reviewed")
    conflicts = detect_claim_conflicts(db, "tick_sizes")
    assert len(conflicts) == 2
    assert "different_values" in first.conflict_reasons
    assert all(item.reviewer_status == "conflicting" for item in conflicts)


def test_rule_and_fee_assistants_are_non_approving(db: Session) -> None:
    rules = create_approval_matrix(
        db,
        approval_type="rule",
        draft_version="rules-draft",
        items=build_rule_verification_review()["items"],
    )
    fees = create_approval_matrix(
        db,
        approval_type="fee",
        draft_version="fees-draft",
        items=build_fee_verification_review()["items"],
    )
    rule = rule_decision_view(db, rules[0])
    fee = fee_decision_view(db, fees[0])
    assert rule["system_may_approve"] is False
    assert rule["approval_options"] == [
        "approve_this_item",
        "approve_conservative_fallback",
        "reject_this_item",
        "request_more_evidence",
    ]
    assert set(fee["cost_examples_bdt"]) == {"5000", "10000", "50000", "100000", "500000"}
    assert fee["automatic_approval"] is False
    assert all(row.approval_status == "unapproved" for row in [*rules, *fees])


def test_portfolio_statement_preview_duplicate_credentials_discrepancy_and_reverse(
    db: Session, tmp_path: Path
) -> None:
    first = _upload(
        db,
        tmp_path,
        raw=(
            b"record_type,symbol,quantity,average_acquisition_cost,cash_balance\n"
            b"holding,GP,10,250,\n"
            b"cash,,,,1000\n"
        ),
        filename="statement-1.csv",
    )
    draft = preview_portfolio_statement(
        db,
        first,
        broker_label="fixture-broker",
        account_label="reference",
        statement_date=date(2026, 7, 1),
    )
    assert draft.reconciliation_summary["imported_to_portfolio"] is False
    with pytest.raises(ValueError, match="Duplicate"):
        preview_portfolio_statement(
            db,
            first,
            broker_label="fixture-broker",
            account_label="reference",
            statement_date=date(2026, 7, 1),
        )
    second = _upload(
        db,
        tmp_path,
        raw=b"record_type,symbol,quantity,average_acquisition_cost\nholding,GP,12,250\n",
        filename="statement-2.csv",
    )
    changed = preview_portfolio_statement(
        db,
        second,
        broker_label="fixture-broker",
        account_label="reference",
        statement_date=date(2026, 7, 2),
    )
    assert changed.discrepancies[0]["difference"] == "2"
    assert reverse_portfolio_statement_draft(db, changed, actor="operator").state == "reversed"
    unsafe = _upload(
        db,
        tmp_path,
        raw=b"record_type,symbol,quantity,average_acquisition_cost,password\nholding,GP,1,1,nope\n",
        filename="unsafe.csv",
    )
    with pytest.raises(ValueError, match="Credential"):
        preview_portfolio_statement(
            db,
            unsafe,
            broker_label="fixture-broker",
            account_label="reference",
            statement_date=date(2026, 7, 3),
        )
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


@pytest.mark.parametrize(
    ("duplicate", "outlier", "expected_key"),
    [(True, False, "duplicate_report"), (False, True, "outlier_report")],
)
def test_market_dataset_quality_reports_never_activate(
    db: Session,
    tmp_path: Path,
    duplicate: bool,
    outlier: bool,
    expected_key: str,
) -> None:
    evidence = _upload(
        db,
        tmp_path,
        raw=_dataset_csv(duplicate=duplicate, outlier=outlier),
        filename=f"market-{duplicate}-{outlier}.csv",
    )
    dataset = preview_market_dataset(
        db,
        evidence,
        name=f"fixture-{duplicate}-{outlier}",
        timestamp_trust="operator_attested",
        raw_dir=tmp_path / "dataset-raw",
        normalized_dir=tmp_path / "normalized",
    )
    assert dataset.quality_report[expected_key]
    assert dataset.quality_report["automatically_activated"] is False
    assert dataset.quality_report["campaign_qualification_days"] == 0
    assert dataset.status != "approved_for_research"


def test_completeness_and_scoped_approval_pack_are_fail_closed(db: Session, tmp_path: Path) -> None:
    create_approval_matrix(
        db,
        approval_type="rule",
        draft_version="rules-draft",
        items=build_rule_verification_review()["items"],
    )
    create_approval_matrix(
        db,
        approval_type="fee",
        draft_version="fees-draft",
        items=build_fee_verification_review()["items"],
    )
    tracker = completeness_tracker(db)
    assert len(tracker["rules"]) == 16
    assert len(tracker["fees"]) == 12
    assert tracker["campaign_qualification"] == "0/60"
    pack = generate_scoped_approval_pack(
        db, scope="rules", output_dir=tmp_path / "packs", generated_by="operator"
    )
    payload = json.loads(Path(pack.output_path).read_text(encoding="utf-8"))
    assert payload["scope"] == "rules"
    assert payload["decision_implied"] is False
    assert payload["blanket_approval_allowed"] is False
    assert db.scalar(select(func.count()).select_from(ApprovalPackRecord)) == 1


def test_api_summary_and_batch_upload_are_review_only(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    initialized = client.post(
        "/api/v1/evidence-workspace/cases/initialize",
        headers=auth_headers,
        json={"collector": "operator", "reviewer": "reviewer"},
    )
    assert initialized.status_code == 200
    case_id = client.get("/api/v1/evidence-workspace/cases").json()[0]["id"]
    response = client.post(
        "/api/v1/evidence-workspace/inbox/batch",
        headers=auth_headers,
        data={
            "case_id": case_id,
            "source_organization": "Synthetic fixture",
            "source_class": "official_exchange_publication",
            "source_description": "Test fixture",
            "operator_attestation": WORKSPACE_ATTESTATION,
        },
        files=[("files", ("rules.csv", b"tick_size\n0.10\n", "text/csv"))],
    )
    assert response.status_code == 200
    assert response.json()["automatic_approval"] is False
    summary = client.get("/api/v1/evidence-workspace/summary").json()
    assert summary["warning"] == "UPLOADED DOES NOT MEAN VERIFIED"
    assert summary["proof_no_activation"] == {
        "campaigns": 0,
        "sessions": 0,
        "orders": 0,
        "transactions_fills": 0,
        "promoted_strategies": 0,
    }


def test_workspace_operations_create_no_activation_or_trading_effects(
    db: Session, tmp_path: Path
) -> None:
    initialize_default_cases(db, collector="operator", reviewer=None)
    summary = workspace_summary(db)
    assert summary["paper_trading"] is True
    assert summary["live_trading_enabled"] is False
    assert summary["qualification"] == "0/60"
    assert db.scalar(select(func.count()).select_from(ValidationCampaign)) == 0
    assert db.scalar(select(func.count()).select_from(PaperSession)) == 0
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(StrategyRegistration)
            .where(StrategyRegistration.lifecycle_state.in_(["paper_candidate", "paper_active"]))
        )
        == 0
    )
    assert verify_audit_chain(db)

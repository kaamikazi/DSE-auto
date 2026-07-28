from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    OperationalIncident,
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import audit_status, initialize_canonical_chain, verify_audit_chain
from app.services.research_strategy_registration import (
    APPROVED_CODE_HASH,
    APPROVED_DATASET_HASH,
    APPROVED_DATASET_ID,
    APPROVED_DATASET_NAME,
    APPROVED_PARAMETER_HASH,
    execution_readiness,
    register_research_strategy,
    verify_strategy_artifacts,
)

PROTECTED = (ValidationCampaign, PaperSession, Signal, Order, Transaction)


def _dataset(db: Session) -> ResearchDataset:
    dataset = ResearchDataset(
        id=APPROVED_DATASET_ID,
        name=APPROVED_DATASET_NAME,
        symbols=["GP", "ACI", "BRACBANK"],
        data_types=["daily_ohlcv"],
        source_evidence_ids=["evidence"],
        source_hash="a" * 64,
        dataset_hash=APPROVED_DATASET_HASH,
        timestamp_trust="unknown",
        raw_file_path="candidate.sqlite3",
        normalized_file_path="active.jsonl",
        quality_report={"classification": "RESEARCH DATASET ACTIVE"},
        status="research_dataset_active",
        approved_by="operator",
        approved_at=datetime.now(UTC),
        audit_event_ids=[],
    )
    db.add(dataset)
    db.commit()
    return dataset


def _provenance(db: Session) -> dict[str, str]:
    chain_id = audit_status(db)["canonical_chain_id"]
    return {
        "git_head": "a5e04272e7d75dcaf8836bce11b7e9d64b4a2daa",
        "database_fingerprint": "sha256:test-operational-database",
        "audit_chain_id": chain_id,
    }


def _counts(db: Session) -> dict[str, int]:
    return {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in PROTECTED
    }


def test_exact_strategy_hashes_recompute_from_sources() -> None:
    root = Path(__file__).resolve().parents[2]
    identity = verify_strategy_artifacts(root)
    assert identity["code_hash"] == APPROVED_CODE_HASH
    assert identity["parameter_hash"] == APPROVED_PARAMETER_HASH
    assert identity["strategy_version"] == "1.0.0"
    assert identity["parameter_serialization"] == '{"fast":20,"slow":50}'
    with pytest.raises(ValueError, match="code hash mismatch"):
        verify_strategy_artifacts(root, expected_code_hash="0" * 64)
    with pytest.raises(ValueError, match="parameter hash mismatch"):
        verify_strategy_artifacts(root, expected_parameter_hash="0" * 64)


def test_registration_is_research_only_idempotent_and_audited(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Research registration test chain")
    _dataset(db)
    before = _counts(db)
    identity = verify_strategy_artifacts(Path(__file__).resolve().parents[2])
    registered_at = "2026-07-28T18:00:00+00:00"
    registration, created = register_research_strategy(
        db,
        artifact_identity=identity,
        provenance=_provenance(db),
        authorization_sha256="b" * 64,
        legacy_review_hash="c" * 64,
        registered_at=registered_at,
    )
    assert created is True
    assert registration.lifecycle_state == "research"
    assert registration.evidence["parameter_hash"] == APPROVED_PARAMETER_HASH
    assert registration.evidence["campaign_eligibility"] is False
    assert registration.evidence["execution_eligibility"] == "research_only"
    assert registration.evidence["research_execution_authorized"] is False
    assert registration.evidence["promotion_status"] == "blocked"
    assert len(registration.evidence["audit_event_ids"]) == 5
    assert execution_readiness(registration)["status"] == (
        "ready_for_research_execution_authorization"
    )
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_id == registration.id)
            .order_by(AuditEvent.sequence)
        )
    )
    assert [event.event_type for event in events] == [
        "strategy.artifact_hashes_verified",
        "strategy.research_registration_authorized",
        "strategy.research_registration_created",
        "strategy.promotion_prohibited",
        "strategy.research_execution_pending_authorization",
    ]
    same, created_again = register_research_strategy(
        db,
        artifact_identity=identity,
        provenance=_provenance(db),
        authorization_sha256="b" * 64,
        legacy_review_hash="c" * 64,
        registered_at=registered_at,
    )
    assert created_again is False
    assert same.id == registration.id
    assert db.scalar(select(func.count()).select_from(StrategyRegistration)) == 1
    assert _counts(db) == before
    assert verify_audit_chain(db)


def test_conflicting_registration_fails_closed_and_creates_incident(
    db: Session, tmp_path: Path
) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Registration conflict test chain")
    _dataset(db)
    existing = StrategyRegistration(
        strategy_id="ma_crossover",
        version="1.0.0",
        lifecycle_state="research",
        code_hash="0" * 64,
        parameters={"fast": 1, "slow": 2},
        data_requirements={},
        evidence={},
        minimum_sample_size=252,
    )
    db.add(existing)
    db.commit()
    before = _counts(db)
    identity = verify_strategy_artifacts(Path(__file__).resolve().parents[2])
    with pytest.raises(ValueError, match="Conflicting strategy identity"):
        register_research_strategy(
            db,
            artifact_identity=identity,
            provenance=_provenance(db),
            authorization_sha256="d" * 64,
            legacy_review_hash="e" * 64,
            registered_at="2026-07-28T18:00:00+00:00",
        )
    incident = db.scalar(
        select(OperationalIncident).where(
            OperationalIncident.incident_type == "strategy_registration_identity_conflict"
        )
    )
    assert incident is not None
    assert incident.severity == "critical"
    assert existing.code_hash == "0" * 64
    assert db.scalar(select(func.count()).select_from(StrategyRegistration)) == 1
    assert _counts(db) == before
    assert verify_audit_chain(db)

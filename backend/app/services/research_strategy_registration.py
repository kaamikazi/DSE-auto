from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationalIncident, ResearchDataset, StrategyRegistration
from app.services.audit import append_audit, audit_status, verify_audit_chain
from app.services.research_governance import (
    MINIMUM_SAMPLE_SIZE,
    PARAMETERS,
    STRATEGY_ID,
    STRATEGY_VERSION,
)

APPROVED_CODE_HASH = "b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3"
APPROVED_PARAMETER_HASH = "51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e"
APPROVED_DATASET_NAME = "gp-aci-bracbank-research-f24a48cb729e8a65"
APPROVED_DATASET_ID = "ba5f2d99-6c66-4e37-ae31-d48c8ee47b15"
APPROVED_DATASET_HASH = "ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791"
IMPLEMENTATION_PATH = "backend/app/backtesting/engine.py"
PARAMETER_SOURCE = "backend/app/services/research_governance.py::PARAMETERS"
NO_REAL_MONEY = (
    "Research registration only; no paper-campaign, broker, live, production, "
    "or real-money authorization is granted."
)


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_strategy_artifacts(
    repository_root: Path,
    *,
    expected_code_hash: str = APPROVED_CODE_HASH,
    expected_parameter_hash: str = APPROVED_PARAMETER_HASH,
) -> dict[str, Any]:
    implementation = repository_root / IMPLEMENTATION_PATH
    parameter_file = repository_root / PARAMETER_SOURCE.split("::", 1)[0]
    if not implementation.is_file() or not parameter_file.is_file():
        raise ValueError("Strategy implementation or parameter source is missing")
    code_payload = (
        implementation.read_bytes()
        + b"\0"
        + STRATEGY_ID.encode()
        + b"\0"
        + STRATEGY_VERSION.encode()
    )
    code_hash = hashlib.sha256(code_payload).hexdigest()
    parameter_serialization = json.dumps(PARAMETERS, sort_keys=True, separators=(",", ":"))
    parameter_hash = hashlib.sha256(parameter_serialization.encode()).hexdigest()
    if code_hash != expected_code_hash:
        raise ValueError(f"Strategy code hash mismatch: {code_hash}")
    if parameter_hash != expected_parameter_hash:
        raise ValueError(f"Strategy parameter hash mismatch: {parameter_hash}")
    return {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "code_hash": code_hash,
        "parameter_hash": parameter_hash,
        "implementation_path": IMPLEMENTATION_PATH,
        "parameter_source": PARAMETER_SOURCE,
        "parameter_serialization": parameter_serialization,
        "deterministic": True,
    }


def inspect_sqlite_registration(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "classification": "historical_database_identity",
        "matches": [],
    }
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "strategy_registrations" in tables:
            columns = [
                str(row[1])
                for row in connection.execute("PRAGMA table_info(strategy_registrations)")
            ]
            result["matches"] = [
                dict(zip(columns, row, strict=True))
                for row in connection.execute(
                    "SELECT * FROM strategy_registrations WHERE strategy_id=? AND version=?",
                    (STRATEGY_ID, STRATEGY_VERSION),
                )
            ]
        else:
            result["classification"] = "historical_database_identity_no_registration_table"
        connection.close()
    except sqlite3.Error as exc:
        result.update(
            {
                "classification": "unverifiable",
                "error": type(exc).__name__,
            }
        )
    return result


def expected_registration_evidence(
    *,
    artifact_identity: dict[str, Any],
    dataset: ResearchDataset,
    provenance: dict[str, Any],
    authorization_sha256: str,
    legacy_review_hash: str,
    registered_at: str,
) -> dict[str, Any]:
    return {
        "parameter_hash": artifact_identity["parameter_hash"],
        "implementation_path": artifact_identity["implementation_path"],
        "parameter_source": artifact_identity["parameter_source"],
        "parameter_serialization": artifact_identity["parameter_serialization"],
        "deterministic": True,
        "registered_by": "operator",
        "reviewer_independence": "non_independent",
        "promotion_status": "blocked",
        "promotion_authorized": False,
        "campaign_eligibility": False,
        "execution_eligibility": "research_only",
        "research_execution_authorized": False,
        "approved_dataset_id": dataset.id,
        "approved_dataset_name": dataset.name,
        "approved_dataset_hash": dataset.dataset_hash,
        "registration_timestamp": registered_at,
        "git_head": provenance["git_head"],
        "database_fingerprint": provenance["database_fingerprint"],
        "canonical_audit_chain_id": provenance["audit_chain_id"],
        "authorization_sha256": authorization_sha256,
        "legacy_identity_review_hash": legacy_review_hash,
        "qualification": "0/60",
        "no_real_money_authorization": NO_REAL_MONEY,
        "audit_event_ids": [],
    }


def _registration_mismatches(
    registration: StrategyRegistration,
    *,
    expected_evidence: dict[str, Any],
) -> list[str]:
    checks = {
        "code_hash": (registration.code_hash, APPROVED_CODE_HASH),
        "parameter_hash": (
            canonical_hash(registration.parameters),
            APPROVED_PARAMETER_HASH,
        ),
        "lifecycle_state": (registration.lifecycle_state, "research"),
        "evidence.parameter_hash": (
            registration.evidence.get("parameter_hash"),
            APPROVED_PARAMETER_HASH,
        ),
        "evidence.dataset_id": (
            registration.evidence.get("approved_dataset_id"),
            APPROVED_DATASET_ID,
        ),
        "evidence.dataset_hash": (
            registration.evidence.get("approved_dataset_hash"),
            APPROVED_DATASET_HASH,
        ),
        "evidence.campaign_eligibility": (
            registration.evidence.get("campaign_eligibility"),
            False,
        ),
        "evidence.execution_eligibility": (
            registration.evidence.get("execution_eligibility"),
            "research_only",
        ),
        "evidence.promotion_status": (
            registration.evidence.get("promotion_status"),
            "blocked",
        ),
        "evidence.authorization_sha256": (
            registration.evidence.get("authorization_sha256"),
            expected_evidence["authorization_sha256"],
        ),
    }
    return [name for name, values in checks.items() if values[0] != values[1]]


def _record_conflict_incident(
    db: Session,
    *,
    existing: StrategyRegistration,
    expected_evidence: dict[str, Any],
    mismatches: list[str],
) -> OperationalIncident:
    incident = OperationalIncident(
        incident_type="strategy_registration_identity_conflict",
        state="open",
        severity="critical",
        owner="operator",
        evidence={
            "strategy": f"{STRATEGY_ID}@{STRATEGY_VERSION}",
            "registration_id": existing.id,
            "mismatches": mismatches,
            "existing_code_hash": existing.code_hash,
            "expected_code_hash": APPROVED_CODE_HASH,
            "expected_parameter_hash": APPROVED_PARAMETER_HASH,
            "authorization_sha256": expected_evidence["authorization_sha256"],
        },
        remediation="Stop and obtain explicit identity-conflict authorization; never overwrite.",
    )
    db.add(incident)
    db.flush()
    event = append_audit(
        db,
        actor="operator",
        event_type="strategy.registration_identity_conflict",
        entity_type="operational_incident",
        entity_id=incident.id,
        new_state=incident.evidence,
    )
    incident.linked_audit_events = [event.id]
    db.commit()
    return incident


def register_research_strategy(
    db: Session,
    *,
    artifact_identity: dict[str, Any],
    provenance: dict[str, Any],
    authorization_sha256: str,
    legacy_review_hash: str,
    registered_at: str,
) -> tuple[StrategyRegistration, bool]:
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain is invalid")
    chain = audit_status(db)
    if provenance["audit_chain_id"] != chain.get("canonical_chain_id"):
        raise ValueError("Canonical audit identity changed")
    dataset = db.scalar(
        select(ResearchDataset).where(ResearchDataset.name == APPROVED_DATASET_NAME)
    )
    if (
        dataset is None
        or dataset.id != APPROVED_DATASET_ID
        or dataset.dataset_hash != APPROVED_DATASET_HASH
        or dataset.status != "research_dataset_active"
    ):
        raise ValueError("Approved research dataset identity is unavailable or changed")
    if artifact_identity["code_hash"] != APPROVED_CODE_HASH:
        raise ValueError("Approved code identity changed")
    if artifact_identity["parameter_hash"] != APPROVED_PARAMETER_HASH:
        raise ValueError("Approved parameter identity changed")
    evidence = expected_registration_evidence(
        artifact_identity=artifact_identity,
        dataset=dataset,
        provenance=provenance,
        authorization_sha256=authorization_sha256,
        legacy_review_hash=legacy_review_hash,
        registered_at=registered_at,
    )
    existing = db.scalar(
        select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == STRATEGY_ID,
            StrategyRegistration.version == STRATEGY_VERSION,
        )
    )
    if existing is not None:
        mismatches = _registration_mismatches(existing, expected_evidence=evidence)
        if mismatches:
            incident = _record_conflict_incident(
                db,
                existing=existing,
                expected_evidence=evidence,
                mismatches=mismatches,
            )
            raise ValueError(f"Conflicting strategy identity; incident {incident.id}")
        return existing, False

    registration_id = str(uuid4())
    shared = {
        "strategy": f"{STRATEGY_ID}@{STRATEGY_VERSION}",
        "registration_id": registration_id,
        "code_hash": APPROVED_CODE_HASH,
        "parameter_hash": APPROVED_PARAMETER_HASH,
        "dataset_id": APPROVED_DATASET_ID,
        "dataset_hash": APPROVED_DATASET_HASH,
        "authorization_sha256": authorization_sha256,
        "qualification": "0/60",
    }
    event_ids: list[str] = []
    for event_type, state in (
        (
            "strategy.artifact_hashes_verified",
            {**shared, "hashes_match": True, "implementation_path": IMPLEMENTATION_PATH},
        ),
        (
            "strategy.research_registration_authorized",
            {**shared, "authorization_scope": "registration_only"},
        ),
    ):
        event = append_audit(
            db,
            actor="operator",
            event_type=event_type,
            entity_type="strategy_registration",
            entity_id=registration_id,
            new_state=state,
        )
        event_ids.append(event.id)

    registration = StrategyRegistration(
        id=registration_id,
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        lifecycle_state="research",
        code_hash=APPROVED_CODE_HASH,
        parameters=dict(PARAMETERS),
        data_requirements={
            "approved_symbols": ["GP", "ACI", "BRACBANK"],
            "dsex_allowed": False,
            "approved_dataset_id": APPROVED_DATASET_ID,
            "approved_dataset_hash": APPROVED_DATASET_HASH,
            "adjusted_and_unadjusted_explicit": True,
        },
        evidence={**evidence, "audit_event_ids": list(event_ids)},
        minimum_sample_size=MINIMUM_SAMPLE_SIZE,
        operator_approval=(
            "Operator authorized research registration only; strategy execution and "
            "promotion require separate authorization."
        ),
        suspension_reason="Promotion blocked; research execution not yet authorized.",
        created_at=datetime.fromisoformat(registered_at),
    )
    db.add(registration)
    db.flush()
    for event_type, state in (
        (
            "strategy.research_registration_created",
            {**shared, "lifecycle_state": "research", "deterministic": True},
        ),
        (
            "strategy.promotion_prohibited",
            {**shared, "promotion_status": "blocked", "campaign_eligibility": False},
        ),
        (
            "strategy.research_execution_pending_authorization",
            {
                **shared,
                "execution_eligibility": "research_only",
                "research_execution_authorized": False,
                "no_real_money_authorization": NO_REAL_MONEY,
            },
        ),
    ):
        event = append_audit(
            db,
            actor="operator",
            event_type=event_type,
            entity_type="strategy_registration",
            entity_id=registration_id,
            new_state=state,
        )
        event_ids.append(event.id)
    registration.evidence = {**registration.evidence, "audit_event_ids": event_ids}
    db.commit()
    return registration, True


def execution_readiness(registration: StrategyRegistration) -> dict[str, Any]:
    mismatches = _registration_mismatches(
        registration,
        expected_evidence={
            "authorization_sha256": registration.evidence.get("authorization_sha256")
        },
    )
    safe = (
        not mismatches
        and registration.lifecycle_state == "research"
        and registration.evidence.get("campaign_eligibility") is False
        and registration.evidence.get("research_execution_authorized") is False
        and len(registration.evidence.get("audit_event_ids", [])) == 5
    )
    return {
        "status": (
            "ready_for_research_execution_authorization"
            if safe
            else "blocked_identity_or_governance_mismatch"
        ),
        "registration_id": registration.id,
        "strategy": f"{registration.strategy_id}@{registration.version}",
        "lifecycle_state": registration.lifecycle_state,
        "campaign_eligibility": registration.evidence.get("campaign_eligibility"),
        "execution_eligibility": registration.evidence.get("execution_eligibility"),
        "research_execution_authorized": registration.evidence.get("research_execution_authorized"),
        "promotion_status": registration.evidence.get("promotion_status"),
        "mismatches": mismatches,
        "qualification": "0/60",
    }

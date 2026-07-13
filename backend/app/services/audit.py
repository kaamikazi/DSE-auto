from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditChain, AuditEvent, OperationalMetric

_AUDIT_LOCK = threading.RLock()
ZERO_HASH = "0" * 64


def _canonical(event: AuditEvent, *, include_generation: bool) -> str:
    payload: dict[str, Any] = {
        "actor": event.actor,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "previous_state": event.previous_state,
        "new_state": event.new_state,
        "metadata": event.event_metadata,
        "previous_hash": event.previous_hash,
    }
    if include_generation:
        payload.update({"chain_id": event.chain_id, "sequence": event.sequence})
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def append_audit(
    db: Session,
    *,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append under a process-wide lock and a database unique sequence constraint."""
    started = time.perf_counter()
    with _AUDIT_LOCK:
        chain = db.scalar(select(AuditChain).where(AuditChain.status == "active"))
        if chain:
            prior = db.scalar(
                select(AuditEvent)
                .where(AuditEvent.chain_id == chain.id)
                .order_by(AuditEvent.sequence.desc())
                .limit(1)
            )
            sequence = (prior.sequence or 0) + 1 if prior else 1
            previous_hash = prior.integrity_hash if prior else ZERO_HASH
            chain_id: str | None = chain.id
        else:
            prior = db.scalar(
                select(AuditEvent)
                .where(AuditEvent.chain_id.is_(None))
                .order_by(AuditEvent.timestamp.desc(), AuditEvent.id.desc())
                .limit(1)
            )
            sequence, chain_id = None, None
            previous_hash = prior.integrity_hash if prior else ZERO_HASH
        timestamp = datetime.now(UTC)
        if prior is not None:
            prior_timestamp = prior.timestamp
            if prior_timestamp.tzinfo is None:
                prior_timestamp = prior_timestamp.replace(tzinfo=UTC)
            if timestamp <= prior_timestamp:
                timestamp = prior_timestamp + timedelta(microseconds=1)
        event = AuditEvent(
            timestamp=timestamp,
            actor=actor,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            previous_state=previous_state,
            new_state=new_state,
            event_metadata=metadata or {},
            previous_hash=previous_hash,
            integrity_hash="pending",
            chain_id=chain_id,
            sequence=sequence,
        )
        event.integrity_hash = hashlib.sha256(
            _canonical(event, include_generation=chain_id is not None).encode()
        ).hexdigest()
        db.add(event)
        db.flush()
        db.add(
            OperationalMetric(
                metric_name="audit_write_latency",
                value=(time.perf_counter() - started) * 1000,
                unit="milliseconds",
                labels={"event_type": event_type},
            )
        )
        if chain_id is not None:
            # Canonical audit records are independently durable. The lock remains
            # held through commit, preventing another writer from selecting the
            # same predecessor even if the caller later crashes or rolls back.
            db.commit()
        return event


def _verify_events(events: list[AuditEvent], *, generated: bool) -> bool:
    previous = ZERO_HASH
    expected_sequence = 1
    for event in events:
        if event.previous_hash != previous:
            return False
        if generated and event.sequence != expected_sequence:
            return False
        digest = hashlib.sha256(
            _canonical(event, include_generation=generated).encode()
        ).hexdigest()
        if digest != event.integrity_hash:
            return False
        previous = event.integrity_hash
        expected_sequence += 1
    return True


def audit_status(db: Session) -> dict[str, Any]:
    chain = db.scalar(select(AuditChain).where(AuditChain.status == "active"))
    legacy = list(db.scalars(select(AuditEvent).where(AuditEvent.chain_id.is_(None))))
    legacy_successors: dict[str, int] = {}
    for event in legacy:
        legacy_successors[event.previous_hash] = legacy_successors.get(event.previous_hash, 0) + 1
    branches = [key for key, count in legacy_successors.items() if count > 1]
    if not chain:
        return {
            "canonical_initialized": False,
            "canonical_valid": False,
            "legacy_events": len(legacy),
            "legacy_branch_count": len(branches),
            "ready": False,
        }
    canonical = list(
        db.scalars(
            select(AuditEvent).where(AuditEvent.chain_id == chain.id).order_by(AuditEvent.sequence)
        )
    )
    valid = _verify_events(canonical, generated=True)
    return {
        "canonical_initialized": True,
        "canonical_chain_id": chain.id,
        "canonical_valid": valid,
        "canonical_events": len(canonical),
        "legacy_events": len(legacy),
        "legacy_branch_count": len(branches),
        "archive_path": chain.legacy_archive_path,
        "archive_hash": chain.legacy_archive_hash,
        "ready": valid,
    }


def verify_audit_chain(db: Session) -> bool:
    status = audit_status(db)
    if status["canonical_initialized"]:
        return bool(status["ready"])
    legacy = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.chain_id.is_(None))
            .order_by(AuditEvent.timestamp, AuditEvent.id)
        )
    )
    return not status["legacy_branch_count"] and _verify_events(legacy, generated=False)


def initialize_canonical_chain(
    db: Session,
    archive_dir: Path,
    operator_acknowledgement: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if len(operator_acknowledgement.strip()) < 12:
        raise ValueError("Operator acknowledgement must describe the recovery decision")
    if db.scalar(select(AuditChain).where(AuditChain.status == "active")):
        raise ValueError("A canonical audit chain already exists")
    legacy = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.chain_id.is_(None))
            .order_by(AuditEvent.timestamp, AuditEvent.id)
        )
    )
    payload = [
        {
            "id": event.id,
            "timestamp": event.timestamp.isoformat(),
            "actor": event.actor,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "previous_state": event.previous_state,
            "new_state": event.new_state,
            "metadata": event.event_metadata,
            "previous_hash": event.previous_hash,
            "integrity_hash": event.integrity_hash,
        }
        for event in legacy
    ]
    encoded = json.dumps(payload, sort_keys=True, indent=2, default=str).encode()
    archive_hash = hashlib.sha256(encoded).hexdigest()
    path = archive_dir / f"legacy_audit_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    result = {
        "dry_run": dry_run,
        "legacy_events": len(legacy),
        "archive_path": str(path),
        "archive_hash": archive_hash,
        "original_evidence_preserved": True,
    }
    if dry_run:
        return result
    archive_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    chain = AuditChain(
        status="active",
        genesis_reason="Historical branched chain archived; canonical generation initialized",
        operator_acknowledgement=operator_acknowledgement.strip(),
        legacy_archive_path=str(path),
        legacy_archive_hash=archive_hash,
    )
    db.add(chain)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="audit.canonical_chain_initialized",
        entity_type="audit_chain",
        entity_id=chain.id,
        new_state=result,
        metadata={"operator_acknowledgement": operator_acknowledgement.strip()},
    )
    db.commit()
    return {**result, "canonical_chain_id": chain.id}

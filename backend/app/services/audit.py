from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent


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
    prior = db.scalar(select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(1))
    previous_hash = prior.integrity_hash if prior else "0" * 64
    canonical = json.dumps(
        {
            "actor": actor,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "previous_state": previous_state,
            "new_state": new_state,
            "metadata": metadata or {},
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    event = AuditEvent(
        actor=actor,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        previous_state=previous_state,
        new_state=new_state,
        event_metadata=metadata or {},
        previous_hash=previous_hash,
        integrity_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    db.add(event)
    db.flush()
    return event


def verify_audit_chain(db: Session) -> bool:
    previous = "0" * 64
    for event in db.scalars(select(AuditEvent).order_by(AuditEvent.timestamp, AuditEvent.id)):
        if event.previous_hash != previous:
            return False
        canonical = json.dumps(
            {
                "actor": event.actor,
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "previous_state": event.previous_state,
                "new_state": event.new_state,
                "metadata": event.event_metadata,
                "previous_hash": event.previous_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if hashlib.sha256(canonical.encode()).hexdigest() != event.integrity_hash:
            return False
        previous = event.integrity_hash
    return True

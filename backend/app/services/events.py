from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import EventDelivery, OutboxEvent

EVENT_TYPES = frozenset(
    {
        "quote_received",
        "data_activated",
        "signal_generated",
        "risk_rejected",
        "proposal_created",
        "proposal_approved",
        "order_submitted",
        "partial_fill",
        "fill_completed",
        "reconciliation_completed",
        "incident_opened",
        "emergency_stop",
        "campaign_session_started",
        "campaign_session_completed",
    }
)
EventConsumer = Callable[[Session, OutboxEvent], dict[str, Any]]


def emit_event(
    db: Session,
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: str | None,
    payload: dict[str, Any],
    idempotency_key: str,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    audit_event_id: str | None = None,
    schema_version: int = 1,
) -> OutboxEvent:
    """Stage an event in the caller's business transaction.

    Delivery is deliberately at-least-once.  Consumers get exactly-once business
    effects only when they use the durable effect key recorded by ``deliver_event``.
    """

    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")
    if schema_version < 1:
        raise ValueError("Event schema version must be positive")
    existing = db.scalar(select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    event = OutboxEvent(
        event_type=event_type,
        schema_version=schema_version,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id or str(uuid4()),
        causation_id=causation_id,
        audit_event_id=audit_event_id,
    )
    db.add(event)
    db.flush()
    return event


def claim_event(
    db: Session,
    dispatcher_id: str,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> OutboxEvent | None:
    current = now or datetime.now(UTC)
    query = (
        select(OutboxEvent)
        .where(
            OutboxEvent.state.in_(("pending", "retry", "leased")),
            OutboxEvent.available_at <= current,
            or_(OutboxEvent.lease_expires_at.is_(None), OutboxEvent.lease_expires_at < current),
        )
        .order_by(OutboxEvent.created_at, OutboxEvent.id)
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    event = db.scalar(query)
    if event is None:
        return None
    event.state = "leased"
    event.lease_owner = dispatcher_id
    event.lease_expires_at = current + timedelta(seconds=lease_seconds)
    event.attempts += 1
    db.flush()
    return event


def deliver_event(
    db: Session,
    event: OutboxEvent,
    consumer_name: str,
    consumer: EventConsumer,
) -> EventDelivery:
    existing = db.scalar(
        select(EventDelivery).where(
            EventDelivery.event_id == event.id,
            EventDelivery.consumer == consumer_name,
        )
    )
    if existing is not None:
        return existing
    effect_key = f"{event.id}:{consumer_name}:v{event.schema_version}"
    result = consumer(db, event)
    delivery = EventDelivery(
        event_id=event.id,
        consumer=consumer_name,
        effect_key=effect_key,
        result=result,
    )
    db.add(delivery)
    db.flush()
    return delivery


def dispatch_once(
    db: Session,
    dispatcher_id: str,
    consumers: dict[str, EventConsumer],
    *,
    now: datetime | None = None,
) -> OutboxEvent | None:
    current = now or datetime.now(UTC)
    event = claim_event(db, dispatcher_id, now=current)
    if event is None:
        return None
    try:
        for name, consumer in consumers.items():
            deliver_event(db, event, name, consumer)
        event.state = "delivered"
        event.delivered_at = current
        event.lease_owner = None
        event.lease_expires_at = None
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(OutboxEvent, event.id)
        if failed is None:
            raise
        failed.last_error = f"{type(exc).__name__}: {exc}"
        failed.lease_owner = None
        failed.lease_expires_at = None
        if failed.attempts >= failed.max_attempts:
            failed.state = "dead_letter"
        else:
            failed.state = "retry"
            failed.available_at = current + timedelta(seconds=2 ** max(failed.attempts - 1, 0))
        db.commit()
    return event


def replay_event(db: Session, event_id: str) -> OutboxEvent:
    event = db.get(OutboxEvent, event_id)
    if event is None:
        raise ValueError("Outbox event not found")
    event.state = "pending"
    event.attempts = 0
    event.available_at = datetime.now(UTC)
    event.lease_owner = None
    event.lease_expires_at = None
    event.delivered_at = None
    event.last_error = None
    db.commit()
    return event

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal, is_retryable_database_error
from app.models import EventDelivery, OutboxEvent, TaskRecord, WorkerHeartbeat
from app.services.events import dispatch_once, emit_event, replay_event
from app.services.task_queue import (
    InMemoryBroker,
    TaskWorker,
    enqueue_task,
    recover_stale_workers,
)


class SerializationFailure(Exception):
    sqlstate = "40001"


def test_database_serialization_and_deadlock_errors_are_retryable() -> None:
    serialization = OperationalError("statement", {}, SerializationFailure())
    assert is_retryable_database_error(serialization)
    assert not is_retryable_database_error(ValueError("ordinary failure"))


def test_production_refuses_sqlite_and_weak_secrets() -> None:
    common: dict[str, Any] = {
        "APP_ENV": "production",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "SCHEDULER_MODE": "external",
        "SCHEDULER_ENABLED": False,
        "REVIEWER_API_SECRET_KEY": "reviewer-credential-with-adequate-entropy-987",
    }
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(**common, DATABASE_URL="sqlite:///unsafe.db")
    with pytest.raises(ValueError, match="weak operator"):
        Settings(
            **common,
            DATABASE_URL="postgresql+psycopg://paper@db/paper",
            API_SECRET_KEY="development-only-secret-change-me",
        )


def test_task_duplicate_delivery_has_one_business_effect(db: Session) -> None:
    broker = InMemoryBroker()
    calls: list[str] = []

    def handler(_: Session, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(str(payload["value"]))
        return {"handled": True}

    first = enqueue_task(db, broker, "test", {"value": "one"}, "duplicate-key")
    second = enqueue_task(db, broker, "test", {"value": "two"}, "duplicate-key")
    broker.push(first.id)  # Simulate Redis redelivery.
    worker = TaskWorker(broker, worker_id="worker-1", handlers={"test": handler})
    assert worker.run_once() is not None
    assert worker.run_once() is None
    assert first.id == second.id
    assert calls == ["one"]
    with SessionLocal() as check:
        assert check.get(TaskRecord, first.id).state == "succeeded"  # type: ignore[union-attr]


def test_worker_failure_reaches_dead_letter(db: Session) -> None:
    broker = InMemoryBroker()

    def fail(_: Session, __: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("injected worker crash")

    task = enqueue_task(db, broker, "fail", {}, "dead-letter-key", max_attempts=1)
    worker = TaskWorker(broker, worker_id="worker-crash", handlers={"fail": fail})
    result = worker.run_once()
    assert result is not None
    assert result.state == "dead_letter"
    assert "injected worker crash" in (result.last_error or "")
    assert task.id == result.id


def test_stale_worker_recovery_releases_lease(db: Session) -> None:
    old = datetime.now(UTC) - timedelta(minutes=10)
    db.add(
        WorkerHeartbeat(
            worker_id="stale-worker",
            process_id=42,
            state="running",
            queues=["test"],
            started_at=old,
            heartbeat_at=old,
        )
    )
    task = TaskRecord(
        task_name="test",
        queue="test",
        payload={},
        idempotency_key="stale-lease",
        state="leased",
        lease_owner="stale-worker",
        lease_expires_at=old,
    )
    db.add(task)
    db.commit()
    recovered = recover_stale_workers(db, stale_after_seconds=60)
    db.refresh(task)
    assert recovered == ["stale-worker"]
    assert task.state == "retry"
    assert task.lease_owner is None


def test_outbox_replay_and_consumer_idempotency(db: Session) -> None:
    event = emit_event(
        db,
        "quote_received",
        aggregate_type="quote",
        aggregate_id="GP",
        payload={"price": "25.00"},
        idempotency_key="quote:GP:1",
        correlation_id="correlation-1",
    )
    duplicate = emit_event(
        db,
        "quote_received",
        aggregate_type="quote",
        aggregate_id="GP",
        payload={"price": "99.00"},
        idempotency_key="quote:GP:1",
    )
    db.commit()
    effects: list[str] = []

    def consume(_: Session, item: OutboxEvent) -> dict[str, Any]:
        effects.append(item.id)
        return {"recorded": True}

    dispatch_once(db, "dispatcher-1", {"quality-metrics": consume})
    replay_event(db, event.id)
    dispatch_once(db, "dispatcher-2", {"quality-metrics": consume})
    assert duplicate.id == event.id
    assert effects == [event.id]
    assert db.scalar(select(EventDelivery).where(EventDelivery.event_id == event.id)) is not None
    assert db.get(OutboxEvent, event.id).state == "delivered"  # type: ignore[union-attr]

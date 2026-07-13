from __future__ import annotations

import importlib
import os
import socket
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models import Order, TaskRecord, WorkerHeartbeat

TaskHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]


class TaskBroker(Protocol):
    def push(self, task_id: str) -> None: ...

    def pop(self, timeout_seconds: int = 1) -> str | None: ...

    def health(self) -> dict[str, Any]: ...


class InMemoryBroker:
    """Deterministic test/development broker; production must use Redis."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()

    def push(self, task_id: str) -> None:
        self._items.append(task_id)

    def pop(self, timeout_seconds: int = 1) -> str | None:
        del timeout_seconds
        return self._items.popleft() if self._items else None

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "backend": "memory", "depth": len(self._items)}


class RedisBroker:
    def __init__(self, url: str, queue_name: str) -> None:
        redis_module = importlib.import_module("redis")
        self.client: Any = redis_module.Redis.from_url(url, decode_responses=True)
        self.queue_name = queue_name

    def push(self, task_id: str) -> None:
        self.client.lpush(self.queue_name, task_id)

    def pop(self, timeout_seconds: int = 1) -> str | None:
        item = self.client.brpop(self.queue_name, timeout=timeout_seconds)
        return str(item[1]) if item else None

    def health(self) -> dict[str, Any]:
        try:
            return {
                "healthy": bool(self.client.ping()),
                "backend": "redis",
                "depth": int(self.client.llen(self.queue_name)),
            }
        except Exception as exc:
            return {"healthy": False, "backend": "redis", "error": type(exc).__name__}


def create_broker(settings: Settings | None = None) -> TaskBroker:
    configured = settings or get_settings()
    if configured.REDIS_URL:
        return RedisBroker(configured.REDIS_URL, configured.TASK_QUEUE_NAME)
    if configured.APP_ENV == "production":
        raise RuntimeError("Production task queue requires Redis")
    return InMemoryBroker()


def enqueue_task(
    db: Session,
    broker: TaskBroker,
    task_name: str,
    payload: dict[str, Any],
    idempotency_key: str,
    *,
    correlation_id: str | None = None,
    max_attempts: int | None = None,
    commit: bool = True,
) -> TaskRecord:
    existing = db.scalar(select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    settings = get_settings()
    task = TaskRecord(
        task_name=task_name,
        queue=settings.TASK_QUEUE_NAME,
        payload=payload,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        max_attempts=max_attempts or settings.TASK_MAX_ATTEMPTS,
    )
    db.add(task)
    db.flush()
    if commit:
        db.commit()
    broker.push(task.id)
    return task


def claim_task(
    db: Session,
    task_id: str,
    worker_id: str,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> TaskRecord | None:
    current = now or datetime.now(UTC)
    query = select(TaskRecord).where(
        TaskRecord.id == task_id,
        TaskRecord.state.in_(("queued", "retry", "leased")),
        TaskRecord.available_at <= current,
        or_(TaskRecord.lease_expires_at.is_(None), TaskRecord.lease_expires_at < current),
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    task = db.scalar(query)
    if task is None:
        return None
    task.state = "leased"
    task.lease_owner = worker_id
    task.lease_expires_at = current + timedelta(seconds=lease_seconds)
    task.attempts += 1
    db.commit()
    return task


def recover_stale_workers(
    db: Session,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(UTC)
    stale_before = current - timedelta(seconds=stale_after_seconds)
    workers = db.scalars(
        select(WorkerHeartbeat).where(
            WorkerHeartbeat.state.in_(("starting", "running")),
            WorkerHeartbeat.heartbeat_at < stale_before,
        )
    ).all()
    worker_ids = [worker.worker_id for worker in workers]
    for worker in workers:
        worker.state = "stale"
    tasks = db.scalars(
        select(TaskRecord).where(
            TaskRecord.state == "leased",
            or_(TaskRecord.lease_owner.in_(worker_ids), TaskRecord.lease_expires_at < current),
        )
    ).all()
    for task in tasks:
        task.state = "retry"
        task.lease_owner = None
        task.lease_expires_at = None
        task.available_at = current
        task.last_error = "Recovered after stale worker lease"
    db.commit()
    return worker_ids


def requeue_ready_tasks(db: Session, broker: TaskBroker, *, now: datetime | None = None) -> int:
    current = now or datetime.now(UTC)
    tasks = db.scalars(
        select(TaskRecord).where(
            TaskRecord.state.in_(("queued", "retry")), TaskRecord.available_at <= current
        )
    ).all()
    for task in tasks:
        broker.push(task.id)
    return len(tasks)


def _legacy_job(name: str) -> TaskHandler:
    def run(_: Session, __: dict[str, Any]) -> dict[str, Any]:
        scheduler = importlib.import_module("app.services.scheduler")
        function = getattr(scheduler, name)
        function()
        return {"completed": True, "handler": name}

    return run


def _expire_proposals(db: Session, _: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    orders = db.scalars(
        select(Order).where(
            Order.status.in_(("proposed", "awaiting_approval")), Order.expires_at < now
        )
    ).all()
    for order in orders:
        order.status = "expired"
    db.commit()
    return {"expired": len(orders)}


def _audit_verify(db: Session, _: dict[str, Any]) -> dict[str, Any]:
    from app.services.audit import verify_audit_chain

    return {"valid": verify_audit_chain(db)}


DEFAULT_TASK_HANDLERS: dict[str, TaskHandler] = {
    "market_data_ingestion": _legacy_job("refresh_quotes_job"),
    "campaign_scans": _legacy_job("campaign_drift_detection_job"),
    "signal_generation": _legacy_job("scan_signals_job"),
    "proposal_expiry": _expire_proposals,
    "reconciliation": _legacy_job("reconciliation_job"),
    "eod_processing": _legacy_job("campaign_end_of_day_job"),
    "evidence_generation": _legacy_job("report_generation_job"),
    "backups": _legacy_job("end_of_day_snapshot_job"),
    "audit_verification": _audit_verify,
    "incident_notifications": _legacy_job("scan_news_job"),
}


class TaskWorker:
    def __init__(
        self,
        broker: TaskBroker,
        *,
        worker_id: str | None = None,
        handlers: dict[str, TaskHandler] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.broker = broker
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.handlers = handlers or DEFAULT_TASK_HANDLERS
        self.running = True

    def heartbeat(self, db: Session, state: str = "running") -> None:
        now = datetime.now(UTC)
        heartbeat = db.get(WorkerHeartbeat, self.worker_id)
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                worker_id=self.worker_id,
                process_id=os.getpid(),
                state=state,
                queues=[self.settings.TASK_QUEUE_NAME],
                started_at=now,
                heartbeat_at=now,
            )
            db.add(heartbeat)
        else:
            heartbeat.state = state
            heartbeat.heartbeat_at = now
            heartbeat.shutdown_at = now if state == "stopped" else None
        db.commit()

    def run_once(self, timeout_seconds: int = 1) -> TaskRecord | None:
        task_id = self.broker.pop(timeout_seconds)
        if task_id is None:
            return None
        with SessionLocal() as db:
            task = claim_task(
                db,
                task_id,
                self.worker_id,
                lease_seconds=self.settings.TASK_LEASE_SECONDS,
            )
            if task is None:
                return None
            handler = self.handlers.get(task.task_name)
            if handler is None:
                error: Exception = ValueError(f"Unknown task: {task.task_name}")
            else:
                try:
                    result = handler(db, task.payload)
                    task = db.get(TaskRecord, task.id)
                    if task is None:
                        raise RuntimeError("Task disappeared during execution")
                    task.result = result
                    task.state = "succeeded"
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.last_error = None
                    db.commit()
                    return task
                except Exception as exc:
                    error = exc
                    db.rollback()
            failed = db.get(TaskRecord, task_id)
            if failed is None:
                raise RuntimeError("Task disappeared during failure recovery")
            failed.last_error = f"{type(error).__name__}: {error}"
            failed.lease_owner = None
            failed.lease_expires_at = None
            if failed.attempts >= failed.max_attempts:
                failed.state = "dead_letter"
            else:
                failed.state = "retry"
                failed.available_at = datetime.now(UTC) + timedelta(
                    seconds=2 ** max(failed.attempts - 1, 0)
                )
            db.commit()
            if failed.state == "retry":
                self.broker.push(failed.id)
            return failed

    def run_forever(self) -> None:
        with SessionLocal() as db:
            recover_stale_workers(db, stale_after_seconds=self.settings.WORKER_STALE_AFTER_SECONDS)
            requeue_ready_tasks(db, self.broker)
            self.heartbeat(db)
        last_heartbeat = time.monotonic()
        while self.running:
            self.run_once()
            if time.monotonic() - last_heartbeat >= self.settings.WORKER_HEARTBEAT_SECONDS:
                with SessionLocal() as db:
                    self.heartbeat(db)
                last_heartbeat = time.monotonic()
        with SessionLocal() as db:
            self.heartbeat(db, "stopped")

    def stop(self) -> None:
        self.running = False

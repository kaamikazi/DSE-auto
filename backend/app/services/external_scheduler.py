from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models import TaskRecord, WorkerHeartbeat
from app.services.task_queue import TaskBroker, create_broker, enqueue_task


@dataclass(frozen=True)
class Schedule:
    task_name: str
    interval_seconds: int


SCHEDULES = (
    Schedule("market_data_ingestion", 60),
    Schedule("campaign_scans", 300),
    Schedule("signal_generation", 300),
    Schedule("proposal_expiry", 60),
    Schedule("reconciliation", 3600),
    Schedule("eod_processing", 3600),
    Schedule("evidence_generation", 3600),
    Schedule("backups", 86400),
    Schedule("audit_verification", 3600),
    Schedule("incident_notifications", 300),
)


class ExternalScheduler:
    def __init__(
        self, broker: TaskBroker | None = None, *, settings: Settings | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.broker = broker or create_broker(self.settings)
        self.scheduler_id = f"scheduler:{socket.gethostname()}:{os.getpid()}"
        self.running = True

    def heartbeat(self, state: str = "running") -> None:
        now = datetime.now(UTC)
        with SessionLocal() as db:
            record = db.get(WorkerHeartbeat, self.scheduler_id)
            if record is None:
                record = WorkerHeartbeat(
                    worker_id=self.scheduler_id,
                    process_id=os.getpid(),
                    state=state,
                    queues=["scheduler"],
                    started_at=now,
                    heartbeat_at=now,
                )
                db.add(record)
            else:
                record.state = state
                record.heartbeat_at = now
                record.shutdown_at = now if state == "stopped" else None
            db.commit()

    def tick(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        epoch = int(current.timestamp())
        created = 0
        with SessionLocal() as db:
            for schedule in SCHEDULES:
                slot = epoch // schedule.interval_seconds
                key = f"schedule:{schedule.task_name}:{slot}"
                before = db.scalar(select(TaskRecord).where(TaskRecord.idempotency_key == key))
                task = enqueue_task(
                    db,
                    self.broker,
                    schedule.task_name,
                    {"scheduled_at": current.isoformat(), "slot": slot},
                    key,
                )
                if before is None and task.id:
                    created += 1
        self.heartbeat()
        return created

    def run_forever(self) -> None:
        while self.running:
            self.tick()
            time.sleep(1)
        self.heartbeat("stopped")

    def stop(self) -> None:
        self.running = False

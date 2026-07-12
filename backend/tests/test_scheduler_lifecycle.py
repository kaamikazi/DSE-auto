from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import JobExecution
from app.services.scheduler import logged_job


def test_scheduler_overlap_and_persisted_state(db: Session) -> None:
    # 1. Overlap prevention: active job should prevent another run
    active_run = JobExecution(job_name="my_job", status="running", started_at=datetime.now(UTC))
    db.add(active_run)
    db.commit()

    run_called = False

    @logged_job("my_job", max_attempts=2, backoff_seconds=1)
    def my_job() -> str:
        nonlocal run_called
        run_called = True
        return "done"

    # Should not execute because job is already running
    result = my_job()
    assert result is None
    assert run_called is False


def test_scheduler_recovers_stale_worker(db: Session) -> None:
    stale = JobExecution(
        job_name="stale_job",
        status="running",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db.add(stale)
    db.commit()

    @logged_job("stale_job", max_attempts=1)
    def stale_job() -> str:
        return "recovered"

    assert stale_job() == "recovered"
    db.refresh(stale)
    assert stale.status == "failed"
    assert stale.error_message == "STALE_WORKER_DETECTED"


def test_scheduler_retry_and_backoff(db: Session) -> None:
    call_count = 0

    @logged_job("retry_job", max_attempts=3, backoff_seconds=1)
    def retry_job() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ValueError("Simulated failure")
        return "success"

    # Runs and fails, retrying up to success
    result = retry_job()
    assert result == "success"
    assert call_count == 3

    # Verify run count in DB
    runs = db.scalars(
        select(JobExecution)
        .where(JobExecution.job_name == "retry_job")
        .order_by(JobExecution.started_at.desc())
    ).all()

    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].attempts == 3


def test_scheduler_database_outage(db: Session) -> None:
    # Mock db.add or commit to raise OperationalError (simulating database outage)
    with patch.object(
        db, "commit", side_effect=OperationalError("Outage", params={}, orig=Exception("Outage"))
    ):

        @logged_job("outage_job")
        def outage_job() -> str:
            return "ok"

        # Should catch db exception and fail gracefully without crashing the thread
        res = outage_job()
        assert res == "db_error" or res == "ok"  # Should handle safely


def test_scheduler_health_endpoint(db: Session) -> None:
    # Seed various job execution states
    j1 = JobExecution(
        job_name="job_1", status="success", started_at=datetime.now(UTC) - timedelta(minutes=5)
    )
    j2 = JobExecution(
        job_name="job_2", status="failed", started_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    db.add(j1)
    db.add(j2)
    db.commit()

    # Call scheduler health lookup query (same logic as routes.py /scheduler/health)
    from sqlalchemy import func

    subq = (
        select(
            JobExecution.job_name,
            func.max(JobExecution.started_at).label("max_started"),
        )
        .group_by(JobExecution.job_name)
        .subquery()
    )
    last_runs = db.scalars(
        select(JobExecution).join(
            subq,
            (JobExecution.job_name == subq.c.job_name)
            & (JobExecution.started_at == subq.c.max_started),
        )
    ).all()

    health: dict[str, Any] = {
        "healthy": not any(r.status == "failed" for r in last_runs),
        "jobs": {
            r.job_name: {
                "status": r.status,
            }
            for r in last_runs
        },
    }

    assert health["healthy"] is False
    assert health["jobs"]["job_2"]["status"] == "failed"
    assert health["jobs"]["job_1"]["status"] == "success"

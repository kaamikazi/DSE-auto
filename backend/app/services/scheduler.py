from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.data.providers.factory import create_provider
from app.models import JobExecution
from app.services.audit import append_audit
from app.services.collection import CollectionService
from app.services.signals import moving_average_signal

logger = logging.getLogger(__name__)

# Core symbols list for scanning
ACTIVE_SYMBOLS = [
    "GP",
    "SQURPHARMA",
    "BRACBANK",
    "BATBC",
    "ACI",
    "RENATA",
    "CITYBANK",
    "BEXIMCO",
]


def logged_job(
    job_name: str, max_attempts: int = 3, backoff_seconds: int = 2
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 1. Prevent overlapping
            with SessionLocal() as db:
                one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
                active = db.scalar(
                    select(JobExecution)
                    .where(JobExecution.job_name == job_name)
                    .where(JobExecution.status == "running")
                    .where(JobExecution.started_at >= one_hour_ago)
                )
                if active:
                    logger.warning("Overlapping execution of job %s blocked.", job_name)
                    return None

                # Record start
                run = JobExecution(job_name=job_name, status="running", attempts=1)
                db.add(run)
                db.commit()
                run_id = run.id

            # 2. Execute with retries
            attempts = 1
            last_error = None
            while attempts <= max_attempts:
                try:
                    result = func(*args, **kwargs)
                    # Success
                    with SessionLocal() as db:
                        record = db.get(JobExecution, run_id)
                        if record:
                            record.status = "success"
                            record.finished_at = datetime.now(UTC)
                            record.attempts = attempts
                        append_audit(
                            db,
                            actor="scheduler",
                            event_type="job.success",
                            entity_type="job",
                            entity_id=job_name,
                            metadata={"attempts": attempts},
                        )
                        db.commit()
                    return result
                except Exception as exc:
                    last_error = str(exc)
                    logger.error("Job %s attempt %s failed: %s", job_name, attempts, exc)
                    attempts += 1
                    if attempts <= max_attempts:
                        time.sleep(backoff_seconds * (attempts - 1))

            # 3. Failure after max attempts
            with SessionLocal() as db:
                record = db.get(JobExecution, run_id)
                if record:
                    record.status = "failed"
                    record.finished_at = datetime.now(UTC)
                    record.attempts = max_attempts
                    record.error_message = last_error
                append_audit(
                    db,
                    actor="scheduler",
                    event_type="job.failed",
                    entity_type="job",
                    entity_id=job_name,
                    new_state={"error": last_error, "attempts": max_attempts},
                )
                db.commit()
            return None

        return wrapper

    return decorator


# Job definitions wrapper
@logged_job("refresh_quotes")
def refresh_quotes_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        CollectionService(db, provider).current_quote_refresh(ACTIVE_SYMBOLS)


@logged_job("update_dsex")
def update_dsex_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        CollectionService(db, provider).daily_market_summary()


@logged_job("scan_signals")
def scan_signals_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        end = date.today()
        start = date(end.year - 1, end.month, min(end.day, 28))
        for symbol in ACTIVE_SYMBOLS:
            try:
                bars = provider.get_history(symbol, start, end)
                quote = provider.get_quote(symbol)
                moving_average_signal(db, symbol, bars, quote)
            except Exception as exc:
                logger.error("Signal generation failed for %s: %s", symbol, exc)


@logged_job("scan_news")
def scan_news_job() -> None:
    settings = get_settings()
    provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
    for symbol in ACTIVE_SYMBOLS:
        try:
            provider.get_price_sensitive_news(symbol)
        except Exception as exc:
            logger.error("News scan failed for %s: %s", symbol, exc)


@logged_job("reconciliation")
def reconciliation_job() -> None:
    with SessionLocal() as db:
        from app.brokers.paper import PaperBroker

        PaperBroker(db).reconcile()


@logged_job("historical_backfill")
def historical_backfill_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        end = date.today()
        start = date(end.year - 1, end.month, min(end.day, 28))
        collector = CollectionService(db, provider)
        for symbol in ACTIVE_SYMBOLS:
            try:
                collector.historical_backfill(symbol, start, end)
            except Exception as exc:
                logger.error("Historical backfill failed for %s: %s", symbol, exc)


@logged_job("report_generation")
def report_generation_job() -> None:
    # Generates a quick overview file in reports
    settings = get_settings()
    with SessionLocal() as db:
        from app.services.portfolio import derive_portfolio

        view = derive_portfolio(db)
        report_path = (
            settings.CSV_DATA_DIR.parent / "reports" / "daily_snapshot.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(view.to_json() if hasattr(view, "to_json") else view.model_dump_json())


# Background scheduler global instance
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler()

    # Schedule tasks
    _scheduler.add_job(refresh_quotes_job, "interval", minutes=1, id="refresh_quotes")
    _scheduler.add_job(update_dsex_job, "interval", minutes=5, id="update_dsex")
    _scheduler.add_job(scan_signals_job, "interval", minutes=5, id="scan_signals")
    _scheduler.add_job(scan_news_job, "interval", minutes=15, id="scan_news")

    # Daily snapshots
    _scheduler.add_job(reconciliation_job, "cron", hour=17, minute=0, id="reconciliation")
    _scheduler.add_job(
        report_generation_job, "cron", hour=17, minute=30, id="report_generation"
    )
    _scheduler.add_job(
        historical_backfill_job, "cron", hour=18, minute=0, id="historical_backfill"
    )

    _scheduler.start()
    logger.info("APScheduler started background jobs successfully.")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown()
    _scheduler = None
    logger.info("APScheduler background jobs shut down.")

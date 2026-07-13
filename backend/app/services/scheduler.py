from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.data.providers.factory import create_provider
from app.models import CampaignDay, JobExecution, OperationalMetric, ValidationCampaign
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


def _active_campaign(db: Session) -> ValidationCampaign | None:
    return db.scalar(
        select(ValidationCampaign).where(ValidationCampaign.state == "active").limit(1)
    )


def logged_job(
    job_name: str, max_attempts: int = 3, backoff_seconds: int = 2
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 1. Prevent overlapping
            with SessionLocal() as db:
                now = datetime.now(UTC)
                stale_before = now - timedelta(seconds=get_settings().SCHEDULER_STALE_AFTER_SECONDS)
                stale_runs = db.scalars(
                    select(JobExecution)
                    .where(JobExecution.job_name == job_name)
                    .where(JobExecution.status == "running")
                    .where(JobExecution.started_at < stale_before)
                ).all()
                for stale in stale_runs:
                    stale.status = "failed"
                    stale.finished_at = now
                    stale.error_message = "STALE_WORKER_DETECTED"
                if stale_runs:
                    append_audit(
                        db,
                        actor="scheduler",
                        event_type="job.stale_worker_recovered",
                        entity_type="job",
                        entity_id=job_name,
                        metadata={"run_ids": [run.id for run in stale_runs]},
                    )
                    db.commit()
                active = db.scalar(
                    select(JobExecution)
                    .where(JobExecution.job_name == job_name)
                    .where(JobExecution.status == "running")
                    .where(JobExecution.started_at >= stale_before)
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
                            started_at = (
                                record.started_at
                                if record.started_at.tzinfo
                                else record.started_at.replace(tzinfo=UTC)
                            )
                            db.add(
                                OperationalMetric(
                                    metric_name="scheduler_job_runtime",
                                    value=(record.finished_at - started_at).total_seconds(),
                                    unit="seconds",
                                    labels={"job_name": job_name, "status": "success"},
                                )
                            )
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
        campaign = _active_campaign(db)
        symbols = campaign.approved_symbols if campaign else ACTIVE_SYMBOLS
        CollectionService(db, provider, campaign.id if campaign else None).current_quote_refresh(
            symbols
        )


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
        campaign = _active_campaign(db)
        end = date.today()
        start = date(end.year - 1, end.month, min(end.day, 28))
        symbols = campaign.approved_symbols if campaign else ACTIVE_SYMBOLS
        for symbol in symbols:
            try:
                bars = provider.get_history(symbol, start, end)
                quote = provider.get_quote(symbol)
                moving_average_signal(db, symbol, bars, quote, campaign.id if campaign else None)
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
        campaign = _active_campaign(db)
        end = date.today()
        start = date(end.year - 1, end.month, min(end.day, 28))
        collector = CollectionService(db, provider, campaign.id if campaign else None)
        symbols = campaign.approved_symbols if campaign else ACTIVE_SYMBOLS
        for symbol in symbols:
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
        report_path = settings.CSV_DATA_DIR.parent / "reports" / "daily_snapshot.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            view.to_json() if hasattr(view, "to_json") else view.model_dump_json()
        )


@logged_job("end_of_day_snapshot")
def end_of_day_snapshot_job() -> None:
    """Persist the paper account state independently of report rendering."""
    settings = get_settings()
    with SessionLocal() as db:
        from app.services.portfolio import derive_portfolio

        view = derive_portfolio(db)
        snapshot_path = settings.CSV_DATA_DIR.parent / "reports" / "end_of_day_snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(view.model_dump_json(), encoding="utf-8")
        append_audit(
            db,
            actor="scheduler",
            event_type="paper.end_of_day_snapshot",
            entity_type="portfolio",
            new_state={"path": str(snapshot_path), "captured_at": datetime.now(UTC).isoformat()},
        )
        db.commit()


@logged_job("campaign_premarket")
def campaign_premarket_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        from app.services.campaigns import start_campaign_day

        campaign = _active_campaign(db)
        if campaign is None:
            return
        existing = db.scalar(
            select(CampaignDay).where(
                CampaignDay.campaign_id == campaign.id,
                CampaignDay.market_date == date.today(),
            )
        )
        if existing is None:
            start_campaign_day(db, campaign, settings, date.today())


@logged_job("campaign_end_of_day")
def campaign_end_of_day_job() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        from app.services.campaigns import complete_campaign_day

        campaign = _active_campaign(db)
        if campaign is None:
            return
        day = db.scalar(
            select(CampaignDay).where(
                CampaignDay.campaign_id == campaign.id,
                CampaignDay.market_date == date.today(),
                CampaignDay.eod_completed.is_(False),
            )
        )
        if day:
            complete_campaign_day(db, campaign, day, settings)


@logged_job("campaign_drift_detection")
def campaign_drift_detection_job() -> None:
    with SessionLocal() as db:
        from app.services.campaigns import (
            detect_missed_trading_days,
            recover_campaigns_after_restart,
        )

        campaign = _active_campaign(db)
        if campaign:
            detect_missed_trading_days(db, campaign, date.today() - timedelta(days=1))
        recover_campaigns_after_restart(db, date.today())


# Background scheduler global instance
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )

    # Schedule tasks
    _scheduler.add_job(refresh_quotes_job, "interval", minutes=1, id="refresh_quotes")
    _scheduler.add_job(update_dsex_job, "interval", minutes=5, id="update_dsex")
    _scheduler.add_job(scan_signals_job, "interval", minutes=5, id="scan_signals")
    _scheduler.add_job(scan_news_job, "interval", minutes=15, id="scan_news")
    _scheduler.add_job(
        campaign_drift_detection_job,
        "interval",
        minutes=30,
        id="campaign_drift_detection",
    )

    # Daily snapshots
    _scheduler.add_job(reconciliation_job, "cron", hour=17, minute=0, id="reconciliation")
    _scheduler.add_job(campaign_premarket_job, "cron", hour=3, minute=45, id="campaign_premarket")
    _scheduler.add_job(
        campaign_end_of_day_job, "cron", hour=11, minute=10, id="campaign_end_of_day"
    )
    _scheduler.add_job(
        end_of_day_snapshot_job, "cron", hour=17, minute=15, id="end_of_day_snapshot"
    )
    _scheduler.add_job(report_generation_job, "cron", hour=17, minute=30, id="report_generation")
    _scheduler.add_job(historical_backfill_job, "cron", hour=18, minute=0, id="historical_backfill")

    _scheduler.start()
    logger.info("APScheduler started background jobs successfully.")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown()
    _scheduler = None
    logger.info("APScheduler background jobs shut down.")

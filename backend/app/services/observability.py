from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import (
    JobExecution,
    MarketBar,
    OperationalIncident,
    OperationalMetric,
    Order,
)


def record_metric(
    db: Session,
    name: str,
    value: float,
    unit: str,
    *,
    campaign_id: str | None = None,
    labels: dict[str, Any] | None = None,
) -> OperationalMetric:
    metric = OperationalMetric(
        campaign_id=campaign_id,
        metric_name=name,
        value=value,
        unit=unit,
        labels=labels or {},
    )
    db.add(metric)
    db.commit()
    return metric


def scheduler_lag_seconds(db: Session, now: datetime | None = None) -> float | None:
    last = db.scalar(select(JobExecution).order_by(JobExecution.started_at.desc()).limit(1))
    if last is None:
        return None
    current = now or datetime.now(UTC)
    started = last.started_at if last.started_at.tzinfo else last.started_at.replace(tzinfo=UTC)
    return max(0.0, (current - started).total_seconds())


def health_summary(db: Session) -> dict[str, Any]:
    database_healthy = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database_healthy = False
    unresolved = db.scalar(
        select(func.count())
        .select_from(OperationalIncident)
        .where(OperationalIncident.state.in_(["open", "acknowledged", "mitigated"]))
    )
    queue_depth = db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.status.in_(
                ["proposed", "awaiting_approval", "approved", "submitted", "partially_filled"]
            )
        )
    )
    failures = db.scalar(
        select(func.count()).select_from(JobExecution).where(JobExecution.status == "failed")
    )
    latest_metrics = db.scalars(
        select(OperationalMetric).order_by(OperationalMetric.recorded_at.desc()).limit(100)
    ).all()
    by_name: dict[str, dict[str, Any]] = {}
    for metric in latest_metrics:
        by_name.setdefault(
            metric.metric_name,
            {
                "value": metric.value,
                "unit": metric.unit,
                "recorded_at": metric.recorded_at.isoformat(),
                "labels": metric.labels,
            },
        )
    now = datetime.now(UTC)
    latest_bar = db.scalar(select(MarketBar).order_by(MarketBar.received_at.desc()).limit(1))
    latest_strategy = db.scalar(
        select(JobExecution)
        .where(JobExecution.job_name == "scan_signals")
        .order_by(JobExecution.started_at.desc())
        .limit(1)
    )
    completed_orders = db.scalars(
        select(Order)
        .where(Order.status.in_(["filled", "rejected", "cancelled", "expired"]))
        .order_by(Order.updated_at.desc())
        .limit(100)
    ).all()

    def aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    quote_age = (
        max(0.0, (now - aware(latest_bar.timestamp)).total_seconds()) if latest_bar else None
    )
    ingestion_latency = (
        max(0.0, (aware(latest_bar.received_at) - aware(latest_bar.timestamp)).total_seconds())
        if latest_bar
        else None
    )
    strategy_runtime = (
        (aware(latest_strategy.finished_at) - aware(latest_strategy.started_at)).total_seconds()
        if latest_strategy and latest_strategy.finished_at
        else None
    )
    order_latency = (
        sum(
            (aware(item.updated_at) - aware(item.created_at)).total_seconds()
            for item in completed_orders
        )
        / len(completed_orders)
        if completed_orders
        else None
    )
    return {
        "database_healthy": database_healthy,
        "scheduler_lag_seconds": scheduler_lag_seconds(db),
        "queue_depth": int(queue_depth or 0),
        "failure_count": int(failures or 0),
        "unresolved_incidents": int(unresolved or 0),
        "quote_age_seconds": quote_age,
        "ingestion_latency_seconds": ingestion_latency,
        "strategy_runtime_seconds": strategy_runtime,
        "order_processing_latency_seconds": order_latency,
        "audit_write_latency_ms": by_name.get("audit_write_latency", {}).get("value"),
        "metrics": by_name,
        "contains_portfolio_details": False,
        "contains_secrets": False,
    }

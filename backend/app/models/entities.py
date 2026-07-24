from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class MarketBar(Base):
    __tablename__ = "market_bars"
    __table_args__ = (UniqueConstraint("symbol", "timestamp", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    volume: Mapped[int | None] = mapped_column(Integer)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(24, 4))
    source: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    quality_status: Mapped[str] = mapped_column(String(32), default="valid")
    timestamp_provenance: Mapped[str] = mapped_column(String(32), default="unknown")
    import_batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transaction_type: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    taxes: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=0)
    broker: Mapped[str | None] = mapped_column(String(100))
    account_label: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    source_record: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)


class RiskState(Base):
    __tablename__ = "risk_state"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(32), default="healthy")
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(32), default="limit")
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    strategy_id: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_token: Mapped[str | None] = mapped_column(String(32), index=True)
    approval_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)


class JobExecution(Base):
    __tablename__ = "job_executions"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    strategy_id: Mapped[str] = mapped_column(String(100))
    strategy_version: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    signal_type: Mapped[str] = mapped_column(String(32))
    strength_score: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    quantity_suggestion: Mapped[int | None] = mapped_column(Integer)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text)
    source_data_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_quality_status: Mapped[str] = mapped_column(String(32))
    risk_preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    previous_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    new_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64))
    integrity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    chain_id: Mapped[str | None] = mapped_column(String(36), index=True)
    sequence: Mapped[int | None] = mapped_column(Integer)


class AuditChain(Base):
    __tablename__ = "audit_chains"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    genesis_reason: Mapped[str] = mapped_column(Text)
    operator_acknowledgement: Mapped[str] = mapped_column(Text)
    legacy_archive_path: Mapped[str] = mapped_column(Text)
    legacy_archive_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    as_of: Mapped[date] = mapped_column(Date, default=date.today)


class PaperSession(Base):
    __tablename__ = "paper_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    state: Mapped[str] = mapped_column(String(32), default="configured", index=True)
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    approved_universe: Mapped[list[str]] = mapped_column(JSON, default=list)
    strategies: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fill_model: Mapped[str] = mapped_column(String(16), default="pessimistic")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    market_rule_set_id: Mapped[str | None] = mapped_column(String(36))
    fee_profile_id: Mapped[str | None] = mapped_column(String(36))


class PaperSessionRun(Base):
    __tablename__ = "paper_session_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    run_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_name: Mapped[str] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="previewed")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    transaction_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_kind: Mapped[str] = mapped_column(String(32), default="transactions")
    market_date: Mapped[date | None] = mapped_column(Date)
    operator_attestation: Mapped[str | None] = mapped_column(Text)
    raw_file_path: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)


class ValidationCampaign(Base):
    __tablename__ = "validation_campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    account_id: Mapped[int] = mapped_column(Integer, default=1, index=True)
    start_date: Mapped[date] = mapped_column(Date)
    planned_end_date: Mapped[date] = mapped_column(Date)
    approved_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    approved_strategies: Mapped[list[str]] = mapped_column(JSON, default=list)
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    risk_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    data_source_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp_trust_requirement: Mapped[str] = mapped_column(String(32))
    fill_model: Mapped[str] = mapped_column(String(16))
    benchmark: Mapped[str] = mapped_column(String(32))
    operator_notes: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="configured", index=True)
    active_rule_set_id: Mapped[str] = mapped_column(String(36))
    active_fee_profile_id: Mapped[str] = mapped_column(String(36))
    evidence_class: Mapped[str] = mapped_column(String(32), default="synthetic", index=True)
    provider_certification_id: Mapped[str | None] = mapped_column(String(36), index=True)
    daily_reviewer_assignments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CampaignDay(Base):
    __tablename__ = "campaign_days"
    __table_args__ = (UniqueConstraint("campaign_id", "market_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    market_date: Mapped[date] = mapped_column(Date, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(32), default="planned")
    premarket_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    eod_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    missed_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_class: Mapped[str] = mapped_column(String(32), default="synthetic", index=True)


class ProviderCertification(Base):
    __tablename__ = "provider_certifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    provider_id: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    integrity_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    certified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketRuleSet(Base):
    __tablename__ = "market_rule_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    version: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    source_reference: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(32))
    operator_approval: Mapped[str] = mapped_column(Text)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    change_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    integrity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FeeProfile(Base):
    __tablename__ = "fee_profiles"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(32))
    effective_date: Mapped[date] = mapped_column(Date)
    broker: Mapped[str | None] = mapped_column(String(100))
    account_label: Mapped[str | None] = mapped_column(String(100))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    integrity_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyRegistration(Base):
    __tablename__ = "strategy_registrations"
    __table_args__ = (UniqueConstraint("strategy_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    strategy_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(32))
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    data_requirements: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    minimum_sample_size: Mapped[int] = mapped_column(Integer)
    operator_approval: Mapped[str | None] = mapped_column(Text)
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperationalIncident(Base):
    __tablename__ = "operational_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    incident_type: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), default="open", index=True)
    severity: Mapped[str] = mapped_column(String(16))
    owner: Mapped[str | None] = mapped_column(String(100))
    root_cause: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    remediation: Mapped[str | None] = mapped_column(Text)
    linked_audit_events: Mapped[list[str]] = mapped_column(JSON, default=list)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationalMetric(Base):
    __tablename__ = "operational_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    metric_name: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class TaskRecord(Base):
    __tablename__ = "task_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_name: Mapped[str] = mapped_column(String(100), index=True)
    queue: Mapped[str] = mapped_column(String(100), default="dse-paper-tasks", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    process_id: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="starting", index=True)
    queues: Mapped[list[str]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    shutdown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    aggregate_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[str | None] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(100), index=True)
    causation_id: Mapped[str | None] = mapped_column(String(100), index=True)
    audit_event_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "consumer"),
        UniqueConstraint("consumer", "effect_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox_events.id"), index=True)
    consumer: Mapped[str] = mapped_column(String(100))
    effect_key: Mapped[str] = mapped_column(String(255))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataQualityObservation(Base):
    __tablename__ = "data_quality_observations"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    market_date: Mapped[date] = mapped_column(Date, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    timestamp_trust: Mapped[str] = mapped_column(String(32))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"
    __table_args__ = (UniqueConstraint("scope", "campaign_id", "start_date", "end_date"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(32), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    json_path: Mapped[str] = mapped_column(Text)
    csv_path: Mapped[str] = mapped_column(Text)
    chart_path: Mapped[str] = mapped_column(Text)
    integrity_hash: Mapped[str] = mapped_column(String(64), index=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceReview(Base):
    __tablename__ = "evidence_reviews"
    __table_args__ = (UniqueConstraint("campaign_day_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    campaign_day_id: Mapped[int] = mapped_column(ForeignKey("campaign_days.id"), index=True)
    campaign_id: Mapped[str] = mapped_column(String(36), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending_review", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(100))
    reviewer_role: Mapped[str | None] = mapped_column(String(32))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_pack_hash: Mapped[str] = mapped_column(String(64))
    data_quality_verdict: Mapped[str | None] = mapped_column(String(32))
    strategy_behavior_verdict: Mapped[str | None] = mapped_column(String(32))
    risk_engine_verdict: Mapped[str | None] = mapped_column(String(32))
    execution_model_verdict: Mapped[str | None] = mapped_column(String(32))
    incidents_reviewed: Mapped[list[str]] = mapped_column(JSON, default=list)
    comments: Mapped[str] = mapped_column(Text, default="")
    approval_decision: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperQualification(Base):
    __tablename__ = "paper_qualifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    campaign_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    target_days: Mapped[int] = mapped_column(Integer, default=60)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    qualifying: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    remaining_qualifying_days: Mapped[int] = mapped_column(Integer, default=60)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskValidationRun(Base):
    __tablename__ = "risk_validation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    campaign_id: Mapped[str | None] = mapped_column(String(36), index=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    integrity_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DisasterRecoveryRun(Base):
    __tablename__ = "disaster_recovery_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(32), index=True)
    recovery_point_seconds: Mapped[float] = mapped_column(Float)
    recovery_time_seconds: Mapped[float] = mapped_column(Float)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_path: Mapped[str] = mapped_column(Text)
    integrity_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DatabaseMigrationRun(Base):
    __tablename__ = "database_migration_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_url_redacted: Mapped[str] = mapped_column(Text)
    destination_url_redacted: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    record_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    table_hashes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AuthoritativeEvidence(Base):
    __tablename__ = "authoritative_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    category: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source_organization: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(64))
    source_reference: Mapped[str] = mapped_column(Text)
    document_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    review_date: Mapped[date | None] = mapped_column(Date, index=True)
    collected_by: Mapped[str] = mapped_column(String(100))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewer_independence: Mapped[str] = mapped_column(String(32), default="unreviewed")
    confidence: Mapped[str] = mapped_column(String(32), default="unknown")
    verification_status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    extracted_claim: Mapped[str] = mapped_column(Text, default="")
    affected_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    file_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    raw_file_path: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    media_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(Integer)
    source_description: Mapped[str] = mapped_column(Text, default="")
    operator_attestation: Mapped[str] = mapped_column(Text, default="")
    extraction: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    audit_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GovernanceItemApproval(Base):
    __tablename__ = "governance_item_approvals"
    __table_args__ = (UniqueConstraint("approval_type", "draft_version", "item_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    approval_type: Mapped[str] = mapped_column(String(32), index=True)
    draft_version: Mapped[str] = mapped_column(String(64), index=True)
    item_key: Mapped[str] = mapped_column(String(100), index=True)
    current_draft: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    proposed_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflicts: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    verification_status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    approval_status: Mapped[str] = mapped_column(String(32), default="unapproved", index=True)
    effective_date: Mapped[date | None] = mapped_column(Date)
    operator_identity: Mapped[str | None] = mapped_column(String(100))
    reviewer_identity: Mapped[str | None] = mapped_column(String(100))
    reviewer_independence: Mapped[str] = mapped_column(String(32), default="unreviewed")
    conservative_fallback: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    audit_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewerInvitation(Base):
    __tablename__ = "reviewer_invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    reviewer_identity: Mapped[str] = mapped_column(String(100), index=True)
    role: Mapped[str] = mapped_column(String(64))
    access_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), default="invited", index=True)
    conflict_declaration: Mapped[str] = mapped_column(Text, default="")
    independence: Mapped[str] = mapped_column(String(32), default="unassessed")
    invited_by: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    audit_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    invitation_id: Mapped[str] = mapped_column(ForeignKey("reviewer_invitations.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(100), index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="assigned", index=True)
    decision: Mapped[str | None] = mapped_column(String(32))
    comments: Mapped[str] = mapped_column(Text, default="")
    independence: Mapped[str] = mapped_column(String(32), default="unassessed")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audit_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchDataset(Base):
    __tablename__ = "research_datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    data_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dataset_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    timestamp_trust: Mapped[str] = mapped_column(String(32))
    raw_file_path: Mapped[str] = mapped_column(Text)
    normalized_file_path: Mapped[str] = mapped_column(Text)
    quality_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audit_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskCalibrationRun(Base):
    __tablename__ = "risk_calibration_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    strategy_registration_id: Mapped[str] = mapped_column(String(36), index=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    integrity_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="recommendations_only", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StrategyReadinessReport(Base):
    __tablename__ = "strategy_readiness_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    strategy_registration_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    checks: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    missing_items: Mapped[list[str]] = mapped_column(JSON, default=list)
    report_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceCollectionCase(Base):
    __tablename__ = "evidence_collection_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    title: Mapped[str] = mapped_column(String(255))
    evidence_category: Mapped[str] = mapped_column(String(64), index=True)
    requested_documents: Mapped[list[str]] = mapped_column(JSON, default=list)
    received_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_documents: Mapped[list[str]] = mapped_column(JSON, default=list)
    responsible_collector: Mapped[str] = mapped_column(String(100))
    reviewer: Mapped[str | None] = mapped_column(String(100))
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    review_date: Mapped[date | None] = mapped_column(Date, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(32), default="planned", index=True)
    audit_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvidenceSourceProfile(Base):
    __tablename__ = "evidence_source_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_class: Mapped[str] = mapped_column(String(64), index=True)
    hierarchy_rank: Mapped[int] = mapped_column(Integer)
    authority_scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    applicable_from: Mapped[date | None] = mapped_column(Date)
    applicable_to: Mapped[date | None] = mapped_column(Date)
    account_applicability: Mapped[list[str]] = mapped_column(JSON, default=list)
    authenticity_review: Mapped[str] = mapped_column(String(32), default="pending")
    confidence: Mapped[str] = mapped_column(String(32), default="unknown")
    conflicts: Mapped[list[str]] = mapped_column(JSON, default=list)
    auto_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractedClaim(Base):
    __tablename__ = "extracted_claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    evidence_id: Mapped[str] = mapped_column(ForeignKey("authoritative_evidence.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_collection_cases.id"), index=True
    )
    source_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_source_profiles.id"), index=True
    )
    claim_type: Mapped[str] = mapped_column(String(100), index=True)
    source_location: Mapped[str] = mapped_column(String(255))
    original_value: Mapped[str] = mapped_column(Text)
    normalized_interpretation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(32), default="unknown")
    extraction_method: Mapped[str] = mapped_column(String(64))
    reviewer_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(100))
    reviewer_notes: Mapped[str] = mapped_column(Text, default="")
    effective_date: Mapped[date | None] = mapped_column(Date)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflict_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    audit_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortfolioStatementDraft(Base):
    __tablename__ = "portfolio_statement_drafts"
    __table_args__ = (UniqueConstraint("account_label", "statement_date", "statement_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    evidence_id: Mapped[str] = mapped_column(ForeignKey("authoritative_evidence.id"), index=True)
    broker_label: Mapped[str] = mapped_column(String(100))
    account_label: Mapped[str] = mapped_column(String(100), index=True)
    statement_date: Mapped[date] = mapped_column(Date, index=True)
    statement_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    parsed_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reconciliation_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    discrepancies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(32), default="previewed", index=True)
    audit_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalPackRecord(Base):
    __tablename__ = "approval_pack_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    scope: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pack_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    output_path: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="generated", index=True)
    audit_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

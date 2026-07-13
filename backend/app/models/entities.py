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

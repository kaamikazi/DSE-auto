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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


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


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(24, 4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    as_of: Mapped[date] = mapped_column(Date, default=date.today)

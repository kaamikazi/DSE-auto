from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Order, PaperAccount
from app.risk.engine import RiskEngine
from app.risk.kill_switch import get_state
from app.schemas.trading import OrderProposalCreate, RiskDecision
from app.services.audit import append_audit


def propose_order(
    db: Session, payload: OrderProposalCreate, engine: RiskEngine, max_data_age_seconds: int
) -> tuple[Order, RiskDecision]:
    existing = db.scalar(select(Order).where(Order.idempotency_key == payload.idempotency_key))
    if existing:
        raise ValueError(f"Duplicate order idempotency key; existing order {existing.id}")
    account = db.get(PaperAccount, 1)
    portfolio_value = account.cash if account else Decimal("0")
    state = get_state(db)
    age = (datetime.now(UTC) - payload.data_timestamp).total_seconds()
    open_orders = (
        db.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status.in_(["awaiting_approval", "approved", "submitted", "partially_filled"])
            )
        )
        or 0
    )
    decision = engine.evaluate(
        payload,
        kill_switch_state=state.state,
        portfolio_value=portfolio_value,
        open_orders=open_orders,
        data_age_seconds=age,
        max_data_age_seconds=max_data_age_seconds,
    )
    order = Order(
        idempotency_key=payload.idempotency_key,
        symbol=payload.symbol,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        stop_price=payload.stop_price,
        strategy_id=payload.strategy_id,
        expires_at=payload.expires_at,
        status="awaiting_approval" if decision.approved else "risk_rejected",
    )
    db.add(order)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Duplicate order submission blocked") from exc
    append_audit(
        db,
        actor="system",
        event_type="risk.decision",
        entity_type="order",
        entity_id=order.id,
        new_state=decision.model_dump(mode="json"),
    )
    append_audit(
        db,
        actor="system",
        event_type="order.proposed",
        entity_type="order",
        entity_id=order.id,
        new_state={"status": order.status, "symbol": order.symbol, "quantity": order.quantity},
    )
    db.commit()
    return order, decision


def approve_order(
    db: Session,
    order: Order,
    payload: OrderProposalCreate,
    engine: RiskEngine,
    max_data_age_seconds: int,
) -> RiskDecision:
    if order.status != "awaiting_approval":
        raise ValueError(f"Order in {order.status} is not awaiting approval")
    if order.expires_at and order.expires_at < datetime.now(UTC):
        order.status = "expired"
        db.commit()
        raise ValueError("Proposal expired")
    state = get_state(db)
    account = db.get(PaperAccount, 1)
    decision = engine.evaluate(
        payload,
        kill_switch_state=state.state,
        portfolio_value=account.cash if account else Decimal("0"),
        data_age_seconds=(datetime.now(UTC) - payload.data_timestamp).total_seconds(),
        max_data_age_seconds=max_data_age_seconds,
    )
    order.status = "approved" if decision.approved else "risk_rejected"
    append_audit(
        db,
        actor="user",
        event_type="order.approval_revalidated",
        entity_type="order",
        entity_id=order.id,
        new_state=decision.model_dump(mode="json"),
    )
    db.commit()
    return decision

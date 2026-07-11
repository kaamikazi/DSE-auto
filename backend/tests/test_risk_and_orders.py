from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.brokers import PaperBroker
from app.models import Order, PaperAccount, RiskState
from app.risk import RiskEngine
from app.schemas.trading import OrderProposalCreate
from app.services.orders import approve_order, propose_order


def proposal(**changes) -> OrderProposalCreate:  # type: ignore[no-untyped-def]
    values = {
        "idempotency_key": "unique-key-123",
        "symbol": "GP",
        "side": "buy",
        "quantity": 100,
        "limit_price": Decimal("100"),
        "current_price": Decimal("100"),
        "data_timestamp": datetime.now(UTC),
        "average_daily_volume": 100_000,
    }
    values.update(changes)
    return OrderProposalCreate(**values)


def seed(db) -> None:  # type: ignore[no-untyped-def]
    db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    db.add(RiskState(id=1, state="healthy", reason="test"))
    db.commit()


def test_risk_rejects_large_position() -> None:
    decision = RiskEngine().evaluate(
        proposal(quantity=3000), kill_switch_state="healthy", portfolio_value=Decimal("1000000")
    )
    assert decision.rejected
    assert (
        "MAX_TRADE_VALUE" in decision.reason_codes
        or "MAX_POSITION_PERCENT" in decision.reason_codes
    )


def test_emergency_stop_rejects_every_order() -> None:
    decision = RiskEngine().evaluate(
        proposal(), kill_switch_state="emergency_stop", portfolio_value=Decimal("1000000")
    )
    assert "KILL_SWITCH_NOT_HEALTHY" in decision.reason_codes


def test_stale_approval_fails_closed(db) -> None:  # type: ignore[no-untyped-def]
    seed(db)
    original = proposal()
    order, decision = propose_order(db, original, RiskEngine(), 30)
    assert decision.approved and order.status == "awaiting_approval"
    stale = original.model_copy(
        update={"data_timestamp": datetime.now(UTC) - timedelta(minutes=10)}
    )
    decision = approve_order(db, order, stale, RiskEngine(), 30)
    assert decision.rejected and order.status == "risk_rejected"


def test_duplicate_order_prevented(db) -> None:  # type: ignore[no-untyped-def]
    seed(db)
    payload = proposal()
    propose_order(db, payload, RiskEngine(), 30)
    try:
        propose_order(db, payload, RiskEngine(), 30)
        raise AssertionError("duplicate should fail")
    except ValueError as exc:
        assert "Duplicate" in str(exc)


def test_paper_partial_fill_and_cancel(db) -> None:  # type: ignore[no-untyped-def]
    seed(db)
    order = Order(
        idempotency_key="paper-fill-1",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=100,
        limit_price=Decimal("101"),
        status="approved",
    )
    db.add(order)
    db.commit()
    broker = PaperBroker(db, participation_rate=Decimal("0.1"))
    broker.submit_order(order, Decimal("100"), 500)
    assert order.status == "partially_filled" and order.filled_quantity == 50
    broker.cancel_order(order)
    assert order.status == "cancelled"


def test_paper_rejects_insufficient_cash(db) -> None:  # type: ignore[no-untyped-def]
    seed(db)
    account = db.get(PaperAccount, 1)
    account.cash = Decimal("1")
    order = Order(
        idempotency_key="paper-fill-2",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=10,
        limit_price=Decimal("101"),
        status="approved",
    )
    db.add(order)
    db.commit()
    PaperBroker(db).submit_order(order, Decimal("100"), 10000)
    assert order.status == "rejected"


def test_paper_rejects_sell_without_shares(db) -> None:  # type: ignore[no-untyped-def]
    seed(db)
    order = Order(
        idempotency_key="paper-fill-3",
        symbol="GP",
        side="sell",
        order_type="limit",
        quantity=10,
        limit_price=Decimal("99"),
        status="approved",
    )
    db.add(order)
    db.commit()
    PaperBroker(db).submit_order(order, Decimal("100"), 10000)
    assert order.status == "rejected"

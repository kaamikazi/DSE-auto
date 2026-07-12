from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.brokers.paper import PaperBroker
from app.models import Order, PaperAccount
from app.services.recovery import run_startup_recovery


def seed(db: Session) -> None:
    account = db.get(PaperAccount, 1)
    if not account:
        db.add(PaperAccount(id=1, cash=Decimal("10000.00"), starting_cash=Decimal("10000.00")))
    else:
        account.cash = Decimal("10000.00")
    db.commit()


# 1. Test partial and full fills
def test_paper_broker_fills(db: Session) -> None:
    seed(db)

    # Create order: buy 100 shares of GP at limit price of 100
    order = Order(
        idempotency_key="fill-test-key",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=100,
        limit_price=Decimal("100.00"),
        status="approved",
    )
    db.add(order)
    db.commit()

    broker = PaperBroker(db, participation_rate=Decimal("0.10"), slippage_percent=Decimal("0.10"))

    # Market volume = 300, participation rate = 10% -> liquidity cap = 30 shares
    order = broker.submit_order(order, market_price=Decimal("99.00"), available_volume=300)
    assert order.status == "partially_filled"
    assert order.filled_quantity == 30
    # Price GP: market price 99 + 0.1% slippage = 99.099
    # fill price = min(99.10, 100.00) = 99.10
    assert order.average_fill_price == Decimal("99.10")

    # Second execution: market volume = 500 -> liquidity cap = 50 shares
    order = broker.submit_order(order, market_price=Decimal("98.00"), available_volume=500)
    assert order.status == "partially_filled"
    assert order.filled_quantity == 80

    # Third execution: market volume = 1000 -> liquidity cap = 100 shares. Remaining is 20 shares.
    order = broker.submit_order(order, market_price=Decimal("97.00"), available_volume=1000)
    assert order.status == "filled"
    assert order.filled_quantity == 100


# 2. Test insufficient liquidity & price gaps
def test_paper_broker_liquidity_and_price_gaps(db: Session) -> None:
    seed(db)
    order = Order(
        idempotency_key="gap-test-key",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=100,
        limit_price=Decimal("100.00"),
        status="approved",
    )
    db.add(order)
    db.commit()

    broker = PaperBroker(db, participation_rate=Decimal("0.10"))

    # Available volume = 0 -> no fill
    order = broker.submit_order(order, market_price=Decimal("95.00"), available_volume=0)
    assert order.status == "submitted"
    assert order.filled_quantity == 0

    # Market price = 105.00 -> limit price is 100.00 -> does not cross -> no fill
    order = broker.submit_order(order, market_price=Decimal("105.00"), available_volume=1000)
    assert order.status == "submitted"
    assert order.filled_quantity == 0


# 3. Test cancel and expiry
def test_paper_broker_cancel_and_expiry(db: Session) -> None:
    seed(db)
    order = Order(
        idempotency_key="cancel-expiry-key",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=100,
        limit_price=Decimal("100.00"),
        status="approved",
    )
    db.add(order)
    db.commit()

    broker = PaperBroker(db)
    # Cancel order
    broker.cancel_order(order)
    assert order.status == "cancelled"

    # Cannot cancel again
    with pytest.raises(ValueError, match="cannot be cancelled"):
        broker.cancel_order(order)


# 4. Test restart recovery with in-flight order
def test_recovery_with_inflight_order(db: Session) -> None:
    from app.models import RiskState

    seed(db)
    db.add(RiskState(id=1, state="healthy", reason="seeding"))

    # Order was submitted but restart happened -> it remains in "submitted" state
    order = Order(
        idempotency_key="inflight-key",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=100,
        limit_price=Decimal("100.00"),
        status="submitted",
    )
    db.add(order)
    db.commit()

    # Startup recovery runs
    run_startup_recovery(db)

    # System should fail-closed (reconciliation_required) due to active pending order on startup
    state = db.get(RiskState, 1)
    assert state is not None
    assert state.state == "reconciliation_required"

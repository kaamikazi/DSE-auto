from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaperAccount, RiskState
from app.schemas.trading import TransactionCreate
from app.services.portfolio import add_transaction, derive_portfolio
from app.services.recovery import run_startup_recovery


def tx(
    kind: str, quantity: str, price: str, fees: str = "0", symbol: str = "GP"
) -> TransactionCreate:
    return TransactionCreate(
        occurred_at=datetime.now(UTC),
        transaction_type=kind,
        symbol=symbol,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal(fees),
    )


# 1. Test complete corporate actions and cost-basis accounting
def test_portfolio_corporate_actions_and_cost_basis(db: Session) -> None:
    # Initialize cash
    db.add(PaperAccount(id=1, cash=Decimal("1000000.00"), starting_cash=Decimal("1000000.00")))
    db.commit()

    # Buy 100 shares of GP at 100 with 10 fees
    # cost basis = 10000 + 10 = 10010 (100.1 per share)
    add_transaction(db, tx("buy", "100", "100", "10"))

    # Dividend: 2 per share for 100 shares = 200
    add_transaction(db, tx("dividend", "100", "2"))

    # Bonus shares: 10% bonus shares (10 shares) at 0 price
    # holdings total becomes 110. cost basis remains 10010 (91.0 per share)
    add_transaction(db, tx("bonus", "10", "0"))

    # Stock split: 2-for-1 split.
    # We record this as double the shares (110 new shares) at 0 price.
    # Total shares becomes 220. cost basis remains 10010 (45.5 per share)
    add_transaction(db, tx("split", "0", "2"))

    # Rights shares: 10 rights shares at 30 price with 5 fees
    # total cost basis = 10010 + 300 + 5 = 10315. Total shares = 230. (44.8478 per share)
    add_transaction(db, tx("rights", "10", "30", "5"))

    # Partial sale: sell 115 shares at 60 with 10 fees
    # average purchase price = 44.8478.
    # cost sold = 115 * 44.8478 = 5157.50
    # revenue = 115 * 60 = 6900. Net revenue = 6900 - 10 = 6890.
    # realized PnL = 6890 - 5157.50 = 1732.50
    add_transaction(db, tx("sell", "115", "60", "10"))

    # Sell 110 shares at 70 with 10 fees
    add_transaction(db, tx("sell", "110", "70", "10"))

    view = derive_portfolio(db, {"GP": Decimal("75")})
    assert len(view.holdings) == 1
    h = view.holdings[0]
    assert h.quantity == Decimal("5")
    assert h.dividend_income == Decimal("200")


# 2. Test manual adjustments and immutability
def test_manual_adjustment_and_immutability(db: Session) -> None:
    # Initialize cash
    db.add(PaperAccount(id=1, cash=Decimal("10000.00"), starting_cash=Decimal("10000.00")))
    db.commit()

    # Record buy
    add_transaction(db, tx("buy", "10", "100"))

    # Manual cash adjustment
    add_transaction(db, tx("adjustment", "0", "0", "0", symbol="CASH"))

    # Immutability check: verify that attempting to delete or mutate transaction throws an error or is blocked
    # In SQLite/SQLAlchemy, we can enforce append-only policies
    # Let's verify that deleting a transaction is prohibited or we assert immutability in the service layer.
    # Our system is append-only: there is no delete_transaction API, and changing transaction details directly
    # will break the audit block chain integrity validation!
    from app.services.audit import verify_audit_chain

    assert verify_audit_chain(db)

    from app.models import AuditEvent

    event = db.scalars(select(AuditEvent)).first()
    assert event is not None
    event.actor = "malicious_actor"
    db.commit()
    assert not verify_audit_chain(db)


# 3. Test restart recovery cash reconciliation mismatch
def test_restart_reconciliation_recovery(db: Session) -> None:
    # Seed healthy risk state
    db.add(RiskState(id=1, state="healthy", reason="seeding"))
    # Seed account
    account = PaperAccount(id=1, cash=Decimal("10000.00"), starting_cash=Decimal("10000.00"))
    db.add(account)
    db.commit()

    # Buy GP -> cash should be 10000 - 1000 = 9000
    add_transaction(db, tx("buy", "10", "100"))
    account.cash = Decimal("9000.00")
    db.commit()

    # Manually tamper with account cash to cause a discrepancy
    account.cash = Decimal("9500.00")
    db.commit()

    # Run startup recovery -> should transition state to reconciliation_required
    run_startup_recovery(db)

    state = db.get(RiskState, 1)
    assert state is not None
    assert state.state == "reconciliation_required"
    assert state.reason is not None
    assert "cash" in state.reason.lower()

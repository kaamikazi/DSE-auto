from datetime import UTC, datetime
from decimal import Decimal

from app.models import PaperAccount
from app.schemas.trading import TransactionCreate
from app.services.portfolio import add_transaction, derive_portfolio


def tx(kind: str, quantity: str, price: str, fees: str = "0") -> TransactionCreate:
    return TransactionCreate(
        occurred_at=datetime.now(UTC),
        transaction_type=kind,
        symbol="GP",
        quantity=Decimal(quantity),
        price=Decimal(price),
        fees=Decimal(fees),
    )  # type: ignore[arg-type]


def test_buy_partial_sell_and_dividend_accounting(db) -> None:  # type: ignore[no-untyped-def]
    db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    db.commit()
    add_transaction(db, tx("buy", "100", "100", "10"))
    add_transaction(db, tx("sell", "40", "120", "5"))
    add_transaction(db, tx("dividend", "60", "2"))
    view = derive_portfolio(db, {"GP": Decimal("130")})
    holding = view.holdings[0]
    assert holding.quantity == 60
    assert holding.realized_pnl == Decimal("791")
    assert holding.dividend_income == Decimal("120")
    assert holding.unrealized_pnl == Decimal("1794")


def test_original_source_record_preserved(db) -> None:  # type: ignore[no-untyped-def]
    payload = tx("buy", "5", "100")
    record = {"raw": "original"}
    created = add_transaction(db, payload, record)
    assert created.source_record == record

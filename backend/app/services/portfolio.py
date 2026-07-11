from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaperAccount, Transaction
from app.schemas.trading import HoldingView, PortfolioView, TransactionCreate
from app.services.audit import append_audit

ZERO = Decimal("0")


def add_transaction(
    db: Session, payload: TransactionCreate, source_record: dict[str, object] | None = None
) -> Transaction:
    transaction = Transaction(
        **payload.model_dump(), source_record=source_record or payload.model_dump(mode="json")
    )
    db.add(transaction)
    db.flush()
    append_audit(
        db,
        actor="user",
        event_type="portfolio.transaction_added",
        entity_type="transaction",
        entity_id=transaction.id,
        new_state=payload.model_dump(mode="json"),
    )
    db.commit()
    return transaction


def derive_portfolio(db: Session, prices: dict[str, Decimal] | None = None) -> PortfolioView:
    prices = prices or {}
    state: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "quantity": ZERO,
            "cost": ZERO,
            "realized": ZERO,
            "dividend": ZERO,
        }
    )
    transactions = db.scalars(
        select(Transaction).order_by(Transaction.occurred_at, Transaction.created_at)
    ).all()
    for tx in transactions:
        item = state[tx.symbol]
        quantity, price, fees, taxes = tx.quantity, tx.price, tx.fees, tx.taxes
        if tx.transaction_type in {"buy", "rights"}:
            item["quantity"] += quantity
            item["cost"] += quantity * price + fees + taxes
        elif tx.transaction_type == "sell":
            if quantity > item["quantity"]:
                raise ValueError(f"Portfolio history sells more {tx.symbol} than held")
            average = item["cost"] / item["quantity"] if item["quantity"] else ZERO
            item["realized"] += quantity * price - fees - taxes - quantity * average
            item["quantity"] -= quantity
            item["cost"] -= quantity * average
        elif tx.transaction_type == "dividend":
            item["dividend"] += price if quantity == 0 else quantity * price
        elif tx.transaction_type == "bonus":
            item["quantity"] += quantity
        elif tx.transaction_type == "split" and price > 0:
            item["quantity"] *= price
        elif tx.transaction_type == "fee":
            item["realized"] -= fees or price
        elif tx.transaction_type == "tax":
            item["realized"] -= taxes or price
        elif tx.transaction_type == "adjustment":
            item["quantity"] += quantity

    holdings: list[HoldingView] = []
    total_value = ZERO
    have_all_prices = True
    for symbol, item in sorted(state.items()):
        if item["quantity"] <= 0:
            continue
        current = prices.get(symbol)
        value = current * item["quantity"] if current is not None else None
        pnl = value - item["cost"] if value is not None else None
        if value is None:
            have_all_prices = False
        else:
            total_value += value
        holdings.append(
            HoldingView(
                symbol=symbol,
                quantity=item["quantity"],
                average_purchase_price=item["cost"] / item["quantity"]
                if item["quantity"]
                else ZERO,
                cost_basis=item["cost"],
                current_price=current,
                market_value=value,
                unrealized_pnl=pnl,
                unrealized_pnl_percent=(pnl / item["cost"] * 100)
                if pnl is not None and item["cost"]
                else None,
                realized_pnl=item["realized"],
                dividend_income=item["dividend"],
            )
        )
    if total_value:
        for holding in holdings:
            if holding.market_value is not None:
                holding.allocation_percent = holding.market_value / total_value * 100
    account = db.get(PaperAccount, 1)
    cash = account.cash if account else ZERO
    total_cost = sum((item.cost_basis for item in holdings), ZERO)
    return PortfolioView(
        holdings=holdings,
        total_cost=total_cost,
        total_market_value=total_value if have_all_prices else None,
        total_unrealized_pnl=total_value - total_cost if have_all_prices else None,
        total_realized_pnl=sum((item.realized_pnl for item in holdings), ZERO),
        dividend_income=sum((item.dividend_income for item in holdings), ZERO),
        cash=cash,
    )

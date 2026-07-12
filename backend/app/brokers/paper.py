from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.base import BrokerAdapter
from app.brokers.dse_execution import DSEExecutionContext, DSEExecutionRules
from app.models import Order, PaperAccount, Transaction
from app.services.audit import append_audit

FILLABLE = {"approved", "submitted", "partially_filled"}


class PaperBroker(BrokerAdapter):
    def __init__(
        self,
        db: Session,
        *,
        participation_rate: Decimal = Decimal("0.10"),
        slippage_percent: Decimal = Decimal("0.10"),
        fill_model: str = "pessimistic",
        rules: DSEExecutionRules | None = None,
    ) -> None:
        self.db = db
        self.participation_rate = participation_rate
        self.slippage_percent = slippage_percent
        self.fill_model = fill_model
        self.rules = rules

    def _account(self) -> PaperAccount:
        account = self.db.get(PaperAccount, 1)
        if account is None:
            raise RuntimeError("Paper account is not initialized")
        return account

    def submit_order(
        self,
        order: Order,
        market_price: Decimal,
        available_volume: int,
        context: DSEExecutionContext | None = None,
    ) -> Order:
        if order.status not in FILLABLE:
            raise ValueError(f"Order in {order.status} cannot be submitted")
        if order.order_type == "market":
            raise ValueError("Market orders are disabled")
        if order.limit_price is None:
            raise ValueError("Paper execution requires a limit price")
        if context is not None and self.rules is not None:
            if context.suspended or context.no_trade:
                order.status = "submitted"
                append_audit(
                    self.db,
                    actor="paper_broker",
                    event_type="order.skipped",
                    entity_type="order",
                    entity_id=order.id,
                    metadata={"reason": "suspended_or_no_trade", "fill_model": self.fill_model},
                )
                self.db.commit()
                return order
            lower, upper = self.rules.band(context.previous_close)
            if order.side == "buy" and market_price >= upper:
                raise ValueError("Buy blocked at configured limit-up band")
            if order.side == "sell" and market_price <= lower:
                raise ValueError("Sell blocked at configured limit-down band")
            if context.available_volume < int(self.rules.config["minimum_liquidity"]):
                raise ValueError("Minimum liquidity threshold not met")
            if (
                order.side == "sell"
                and context.settled_quantity is not None
                and context.settled_quantity < order.quantity - order.filled_quantity
            ):
                raise ValueError("Insufficient settled shares")
            available_volume = self.rules.fillable_volume(context, self.fill_model)
        crosses = (
            order.limit_price >= market_price
            if order.side == "buy"
            else order.limit_price <= market_price
        )
        order.status = "submitted"
        if not crosses or available_volume <= 0:
            append_audit(
                self.db,
                actor="paper_broker",
                event_type="order.submitted_no_fill",
                entity_type="order",
                entity_id=order.id,
                new_state={"status": order.status},
                metadata={"fill_model": self.fill_model},
            )
            self.db.commit()
            return order
        remaining = order.quantity - order.filled_quantity
        liquidity_cap = int(Decimal(available_volume) * self.participation_rate)
        fill_quantity = min(remaining, max(0, liquidity_cap))
        if fill_quantity == 0:
            self.db.commit()
            return order
        slippage = market_price * self.slippage_percent / 100
        simulated_price = (
            market_price + slippage if order.side == "buy" else market_price - slippage
        )
        fill_price = (
            min(simulated_price, order.limit_price)
            if order.side == "buy"
            else max(simulated_price, order.limit_price)
        )
        fill_price = fill_price.quantize(Decimal("0.01"))
        account = self._account()
        gross = fill_price * fill_quantity
        fee = (gross * Decimal("0.004")).quantize(Decimal("0.01"))
        if order.side == "buy" and account.cash < gross + fee:
            order.status = "rejected"
            append_audit(
                self.db,
                actor="paper_broker",
                event_type="order.rejected",
                entity_type="order",
                entity_id=order.id,
                new_state={"status": order.status},
                metadata={"reason": "insufficient_cash"},
            )
            self.db.commit()
            return order
        if order.side == "sell":
            held = self._held_quantity(order.symbol)
            if held < fill_quantity:
                order.status = "rejected"
                append_audit(
                    self.db,
                    actor="paper_broker",
                    event_type="order.rejected",
                    entity_type="order",
                    entity_id=order.id,
                    new_state={"status": order.status},
                    metadata={"reason": "insufficient_shares"},
                )
                self.db.commit()
                return order
        account.cash += -(gross + fee) if order.side == "buy" else gross - fee
        old_filled = order.filled_quantity
        total_cost = (
            order.average_fill_price or Decimal("0")
        ) * old_filled + fill_price * fill_quantity
        order.filled_quantity += fill_quantity
        order.average_fill_price = (total_cost / order.filled_quantity).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        order.status = "filled" if order.filled_quantity == order.quantity else "partially_filled"
        self.db.add(
            Transaction(
                occurred_at=order.updated_at,
                transaction_type=order.side,
                symbol=order.symbol,
                quantity=Decimal(fill_quantity),
                price=fill_price,
                fees=fee,
                taxes=Decimal("0"),
                broker="paper",
                account_label="paper",
                notes=f"Paper fill for {order.id}",
                source_record={"order_id": order.id, "simulated": True},
            )
        )
        append_audit(
            self.db,
            actor="paper_broker",
            event_type="order.fill",
            entity_type="order",
            entity_id=order.id,
            new_state={
                "status": order.status,
                "filled_quantity": order.filled_quantity,
                "average_fill_price": str(order.average_fill_price),
            },
            metadata={"fill_model": self.fill_model},
        )
        self.db.commit()
        return order

    def _held_quantity(self, symbol: str) -> int:
        total = Decimal("0")
        for tx in self.db.scalars(select(Transaction).where(Transaction.symbol == symbol)):
            if tx.transaction_type in {"buy", "rights", "bonus"}:
                total += tx.quantity
            elif tx.transaction_type == "sell":
                total -= tx.quantity
        return int(total)

    def cancel_order(self, order: Order) -> Order:
        if order.status in {"filled", "cancelled", "expired", "rejected"}:
            raise ValueError(f"Order in {order.status} cannot be cancelled")
        order.status = "cancelled"
        append_audit(
            self.db,
            actor="user",
            event_type="order.cancelled",
            entity_type="order",
            entity_id=order.id,
            new_state={"status": order.status},
        )
        self.db.commit()
        return order

    def replace_order(self, order: Order, quantity: int, limit_price: Decimal) -> Order:
        if order.filled_quantity or order.status not in {
            "proposed",
            "awaiting_approval",
            "approved",
            "submitted",
        }:
            raise ValueError("Only unfilled active paper orders may be replaced")
        order.quantity, order.limit_price, order.status = quantity, limit_price, "awaiting_approval"
        self.db.commit()
        return order

    def reconcile(self) -> dict[str, object]:
        account = self._account()
        duplicate_count = self.db.scalar(
            select(Order.idempotency_key)
            .group_by(Order.idempotency_key)
            .having(__import__("sqlalchemy").func.count() > 1)
        )
        derived_cash = account.starting_cash
        transactions = self.db.scalars(select(Transaction)).all()
        for t in transactions:
            qty, prc, fee, tax = t.quantity, t.price, t.fees, t.taxes
            if t.transaction_type in {"buy", "rights"}:
                derived_cash -= qty * prc + fee + tax
            elif t.transaction_type == "sell":
                derived_cash += qty * prc - fee - tax
            elif t.transaction_type == "dividend":
                derived_cash += prc if qty == 0 else qty * prc
            elif t.transaction_type == "fee":
                derived_cash -= fee or prc
            elif t.transaction_type == "tax":
                derived_cash -= tax or prc
            elif t.transaction_type == "adjustment":
                derived_cash += prc
        cash_reconciled = abs(account.cash - derived_cash) <= Decimal("0.01")
        return {
            "healthy": duplicate_count is None and account.cash >= 0 and cash_reconciled,
            "cash": str(account.cash),
            "derived_cash": str(derived_cash),
            "duplicate_orders": duplicate_count is not None,
            "cash_reconciled": cash_reconciled,
        }

    def reset_account(self, starting_cash: Decimal) -> None:
        account = self._account()
        account.cash = account.starting_cash = starting_cash
        append_audit(
            self.db,
            actor="user",
            event_type="paper_account.reset",
            entity_type="paper_account",
            entity_id="1",
        )
        self.db.commit()

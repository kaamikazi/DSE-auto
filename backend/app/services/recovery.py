from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models import Order, PaperAccount, Transaction
from app.risk.kill_switch import get_state, set_state
from app.services.audit import append_audit

logger = logging.getLogger(__name__)


def run_startup_recovery(db) -> None:  # type: ignore[no-untyped-def]
    """Scans for active orders during restart and reconciles paper trading state."""
    from app.core.config import get_settings
    from app.services.paper_sessions import recover_stale_sessions

    recover_stale_sessions(db, get_settings().PAPER_SESSION_STALE_AFTER_SECONDS)
    logger.info("Executing startup diagnostics and state reconciliation...")

    # 1. Clean up stale/expired proposals
    now = datetime.now(UTC)
    expired_count = 0
    active_orders = db.scalars(
        select(Order).where(
            Order.status.in_(
                ["proposed", "awaiting_approval", "approved", "submitted", "partially_filled"]
            )
        )
    ).all()

    for order in active_orders:
        # Expire any proposal that has an expiration date in the past
        if order.expires_at and order.expires_at < now:
            order.status = "expired"
            expired_count += 1
        elif order.status in ["proposed", "awaiting_approval"]:
            # Expire pending approvals to prevent stale orders executing after restart
            order.status = "expired"
            expired_count += 1

    if expired_count > 0:
        logger.info("Expired %s pending proposals/orders on restart.", expired_count)
        append_audit(
            db,
            actor="recovery_service",
            event_type="recovery.expired_proposals",
            entity_type="order",
            new_state={"expired_count": expired_count},
        )
        db.commit()

    # 2. Re-check active submitted/partially filled orders
    open_count = db.scalar(
        select(Order).where(Order.status.in_(["submitted", "partially_filled"])).limit(1)
    )

    # 3. Portfolio reconciliation check
    account = db.get(PaperAccount, 1)
    if account:
        try:
            derived_cash = account.starting_cash
            transactions = db.scalars(select(Transaction)).all()
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
            if abs(account.cash - derived_cash) > Decimal("0.01"):
                raise ValueError(
                    f"Stored cash {account.cash} and derived cash {derived_cash} mismatch"
                )
        except Exception as exc:
            logger.error("Portfolio reconciliation check failed: %s", exc)
            set_state(
                db,
                "reconciliation_required",
                f"Startup cash reconciliation mismatch: {exc}",
                "recovery_service",
            )
            return

    # If any orders are left in submitted/partially_filled, fail closed into reconciliation_required
    if open_count is not None:
        logger.warning(
            "Active submitted/partially-filled orders detected during restart. Transitioning to reconciliation state."
        )
        set_state(
            db,
            "reconciliation_required",
            "Active submitted/partially-filled orders found during restart",
            "recovery_service",
        )
        return

    # If everything is reconciled and current state was reconciliation_required, restore to healthy
    state = get_state(db)
    if state.state == "reconciliation_required":
        set_state(
            db,
            "healthy",
            "Startup checks passed, auto-resuming from reconciliation state",
            "recovery_service",
        )
    logger.info("Startup state recovery passed successfully.")

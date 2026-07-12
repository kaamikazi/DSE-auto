from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import JobExecution, Order, RiskState, Signal

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token, self.chat_id = token, chat_id

    async def send(self, message: str) -> dict[str, object]:
        if not self.token or not self.chat_id:
            logger.info("Telegram unavailable; console alert: %s", message)
            return {"delivered": False, "fallback": "console"}
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url, json={"chat_id": self.chat_id, "text": message}
            )
            response.raise_for_status()
        return {"delivered": True, "fallback": None}


def validate_chat(expected_chat_id: str | None, actual_chat_id: str) -> bool:
    return bool(expected_chat_id) and expected_chat_id == actual_chat_id


def send_telegram_alert(message: str) -> None:
    """Sends a Telegram alert either asynchronously or synchronously depending on the execution thread."""
    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.info("Telegram notifier not configured; console alert: %s", message)
        return

    try:
        loop = asyncio.get_running_loop()
        notifier = TelegramNotifier(
            settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID
        )
        loop.create_task(notifier.send(message))
    except RuntimeError:
        # Fallback to sync HTTP POST if no event loop is running
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            with httpx.Client(timeout=5) as client:
                client.post(
                    url,
                    json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": message},
                )
        except Exception as exc:
            logger.error("Failed to send synchronous Telegram alert: %s", exc)


# Bot update polling global task reference
_bot_task: asyncio.Task[None] | None = None


def start_bot_polling() -> None:
    global _bot_task
    if _bot_task is not None:
        return
    settings = get_settings()
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        _bot_task = asyncio.create_task(run_bot_polling())
        logger.info("Telegram Bot Update loop activated.")


def stop_bot_polling() -> None:
    global _bot_task
    if _bot_task is None:
        return
    _bot_task.cancel()
    _bot_task = None
    logger.info("Telegram Bot Update loop deactivated.")


async def run_bot_polling() -> None:
    settings = get_settings()
    token = settings.TELEGRAM_BOT_TOKEN
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    offset = 0

    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    url, params={"offset": offset, "timeout": 10}
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok"):
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            await handle_update(update)
                elif response.status_code == 409:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in Telegram updater loop: %s", exc)
            await asyncio.sleep(5)


async def handle_update(update: dict[str, Any]) -> None:
    message = update.get("message")
    if not message:
        return
    text = (message.get("text") or "").strip()
    from_chat = message.get("chat", {})
    chat_id_val = from_chat.get("id")
    if not text or chat_id_val is None:
        return

    settings = get_settings()
    # 1. Verification & Access Control
    if not validate_chat(settings.TELEGRAM_CHAT_ID, str(chat_id_val)):
        logger.warning("Access violation attempt from Chat ID: %s", chat_id_val)
        with SessionLocal() as db:
            from app.services.audit import append_audit

            append_audit(
                db,
                actor=f"unauthorized_chat_{chat_id_val}",
                event_type="security.unauthorized_telegram_access",
                entity_type="telegram",
                new_state={"text": text},
            )
            db.commit()
        return

    # 2. Command parser
    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    reply = "Unknown command. Send /help to list available commands."

    if cmd == "/help":
        reply = (
            "🤖 DSE AutoTrader Help Surface:\n"
            "/status - Live system checks, scheduler status\n"
            "/portfolio - Balance, asset valuation\n"
            "/signals - Last generated trading signals\n"
            "/orders - Last active orders\n"
            "/risk - Daily limit limits\n"
            "/pnl - Realized & Unrealized totals\n"
            "/pause - Pauses trading activity\n"
            "/resume - Runs database checks and resumes\n"
            "/emergency_stop - Instantly emergency stops trading\n"
            "/approve <token> - Approves order for execution\n"
            "/reject <token> - Rejects/cancels order proposal"
        )
    elif cmd == "/status":
        reply = await get_status_cmd()
    elif cmd == "/portfolio":
        reply = await get_portfolio_cmd()
    elif cmd == "/signals":
        reply = await get_signals_cmd()
    elif cmd == "/orders":
        reply = await get_orders_cmd()
    elif cmd == "/risk":
        reply = await get_risk_cmd()
    elif cmd == "/pnl":
        reply = await get_pnl_cmd()
    elif cmd == "/pause":
        reply = await set_risk_state_cmd("trading_paused", "Paused via Telegram Bot")
    elif cmd == "/resume":
        reply = await resume_cmd()
    elif cmd == "/emergency_stop":
        reply = await set_risk_state_cmd(
            "emergency_stop", "Emergency stopped via Telegram Bot"
        )
    elif cmd in ("/approve", "/reject"):
        if not args:
            reply = f"Error: Token parameter required. Usage: {cmd} <token>"
        else:
            token = args[0].upper()
            if cmd == "/approve":
                reply = await approve_token_cmd(token)
            else:
                reply = await reject_token_cmd(token)

    # 3. Send response
    notifier = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    await notifier.send(reply)


# CMD Command Helper Implementations
async def get_status_cmd() -> str:
    with SessionLocal() as db:
        state = db.get(RiskState, 1)
        status_str = state.state.upper() if state else "UNKNOWN"
        reason_str = state.reason if state else ""

        from app.services.audit import verify_audit_chain

        chain_valid = verify_audit_chain(db)

        runs = db.scalars(
            select(JobExecution).order_by(JobExecution.started_at.desc()).limit(5)
        ).all()
        runs_str = ""
        for r in runs:
            fin = r.finished_at.strftime("%H:%M:%S") if r.finished_at else "running"
            runs_str += f"\n- {r.job_name}: {r.status.upper()} ({fin})"
        if not runs_str:
            runs_str = "\n- No scheduler jobs run yet."

        return (
            f"🤖 DSE AutoTrader Operations Status:\n"
            f"● Risk Engine: {status_str} ({reason_str})\n"
            f"● Audit Integrity: {'VERIFIED' if chain_valid else 'CHECK REQUIRED'}\n"
            f"● Scheduler runs:{runs_str}"
        )


async def get_portfolio_cmd() -> str:
    settings = get_settings()
    with SessionLocal() as db:
        from app.data.providers.factory import create_provider
        from app.models import Transaction
        from app.services.portfolio import derive_portfolio

        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        symbols = sorted(set(db.scalars(select(Transaction.symbol)).all()))
        prices = {}
        for s in symbols:
            with suppress(Exception):
                prices[s] = provider.get_quote(s).last_price
        view = derive_portfolio(db, prices)
        holdings_str = ""
        for h in view.holdings:
            val_str = f"৳{h.market_value:,.2f}" if h.market_value else "N/A"
            pnl_str = f"৳{h.unrealized_pnl:,.2f}" if h.unrealized_pnl else "N/A"
            holdings_str += f"\n- {h.symbol}: {h.quantity} @ ৳{h.average_purchase_price:,.2f} (Val: {val_str}, P&L: {pnl_str})"
        if not holdings_str:
            holdings_str = "\n- No holdings in portfolio."

        mval = f"৳{view.total_market_value:,.2f}" if view.total_market_value else "N/A"
        return (
            f"💼 Asset Valuation & Balance:\n"
            f"● Cash Balance: ৳{view.cash:,.2f}\n"
            f"● Total Cost: ৳{view.total_cost:,.2f}\n"
            f"● Market Value: {mval}\n"
            f"● Holdings:{holdings_str}"
        )


async def get_signals_cmd() -> str:
    with SessionLocal() as db:
        signals = db.scalars(
            select(Signal).order_by(Signal.timestamp.desc()).limit(5)
        ).all()
        signals_str = ""
        for s in signals:
            signals_str += f"\n- {s.symbol} ({s.strategy_id}): {s.signal_type.upper()} strength={s.strength_score:.1f} @ {s.entry_price or 'N/A'}"
        if not signals_str:
            signals_str = "\n- No signals generated yet."
        return f"📈 Recent Signals:{signals_str}"


async def get_orders_cmd() -> str:
    with SessionLocal() as db:
        orders = db.scalars(
            select(Order).order_by(Order.created_at.desc()).limit(5)
        ).all()
        orders_str = ""
        for o in orders:
            price = f" @ ৳{o.limit_price:.2f}" if o.limit_price else ""
            tok = f" [Token: {o.approval_token}]" if o.status == "awaiting_approval" else ""
            orders_str += f"\n- {o.symbol} {o.side.upper()} {o.quantity}{price} -> {o.status.upper()}{tok}"
        if not orders_str:
            orders_str = "\n- No orders found."
        return f"🛒 Recent Orders:{orders_str}"


async def get_risk_cmd() -> str:
    with SessionLocal() as db:
        state = db.get(RiskState, 1)
        status_str = state.state.upper() if state else "UNKNOWN"
        from app.risk.engine import RiskEngine

        limits = RiskEngine().limits

        from sqlalchemy import func

        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        orders_today = (
            db.scalar(
                select(func.count()).select_from(Order).where(Order.created_at >= today)
            )
            or 0
        )
        return (
            f"🛡️ Pre-Trade Risk Limits:\n"
            f"● Risk State: {status_str}\n"
            f"● Count Today: {orders_today} / {limits.max_orders_per_day}\n"
            f"● Max Position Conc: {limits.max_position_percent}%\n"
            f"● Max Order Value: ৳{limits.max_trade_value:,.2f}"
        )


async def get_pnl_cmd() -> str:
    settings = get_settings()
    with SessionLocal() as db:
        from app.data.providers.factory import create_provider
        from app.services.portfolio import derive_portfolio

        provider = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        from app.models import Transaction

        symbols = sorted(set(db.scalars(select(Transaction.symbol)).all()))
        prices = {}
        for s in symbols:
            with suppress(Exception):
                prices[s] = provider.get_quote(s).last_price
        view = derive_portfolio(db, prices)
        unreal = (
            f"৳{view.total_unrealized_pnl:,.2f}"
            if view.total_unrealized_pnl is not None
            else "N/A"
        )
        return (
            f"💰 Derived Profit & Loss:\n"
            f"● Realized P&L: ৳{view.total_realized_pnl:,.2f}\n"
            f"● Unrealized P&L: {unreal}\n"
            f"● Dividend Income: ৳{view.dividend_income:,.2f}"
        )


async def set_risk_state_cmd(new_state: str, reason: str) -> str:
    with SessionLocal() as db:
        from app.risk.kill_switch import set_state

        set_state(db, new_state, reason, actor="telegram_bot")
        return f"🚨 Risk state transitioned to: {new_state.upper()}"


async def resume_cmd() -> str:
    with SessionLocal() as db:
        from app.brokers.paper import PaperBroker
        from app.risk.kill_switch import set_state
        from app.services.audit import verify_audit_chain

        recon = PaperBroker(db).reconcile()
        valid = verify_audit_chain(db)
        if not recon["healthy"] or not valid:
            set_state(
                db,
                "reconciliation_required",
                "Reconciliation failed on bot resume request",
                actor="telegram_bot",
            )
            return f"❌ Resume blocked. Reconciliation/Audit check failed: {recon}"
        set_state(
            db,
            "healthy",
            "Resumed trading following Telegram command",
            actor="telegram_bot",
        )
        return "✅ System checks passed. Risk engine is now HEALTHY."


async def approve_token_cmd(token: str) -> str:
    settings = get_settings()
    with SessionLocal() as db:
        order = db.scalar(
            select(Order)
            .where(Order.approval_token == token)
            .where(Order.status == "awaiting_approval")
        )
        if not order:
            return "❌ Error: Invalid token or order proposal has already been processed."

        if order.approval_token_expires_at:
            expires_at = (
                order.approval_token_expires_at.replace(tzinfo=None)
                if order.approval_token_expires_at.tzinfo
                else order.approval_token_expires_at
            )
            if expires_at < datetime.now(UTC).replace(tzinfo=None):
                order.status = "expired"
                db.commit()
                return "❌ Error: Order token has expired."

        # Revalidate Quote and Risk immediately before paper submit
        from app.data.providers.factory import create_provider
        from app.schemas.trading import OrderProposalCreate
        from app.services.data_validation import compare_quotes

        primary = create_provider(settings.DATA_PRIMARY_PROVIDER, settings.CSV_DATA_DIR)
        secondary = create_provider(settings.DATA_SECONDARY_PROVIDER, settings.CSV_DATA_DIR)

        try:
            quote = primary.get_quote(order.symbol)
            try:
                sec_quote = secondary.get_quote(order.symbol)
            except Exception:
                sec_quote = None

            comp = compare_quotes(
                quote,
                sec_quote,
                max_disagreement_percent=Decimal(
                    str(settings.DATA_MAX_PROVIDER_DISAGREEMENT_PERCENT)
                ),
                max_staleness_seconds=settings.DATA_MAX_STALENESS_SECONDS,
                now=datetime.now(UTC),
            )

            # Fail-closed on unsafe timestamp or staleness
            if not comp.safe_for_orders:
                order.status = "risk_rejected"
                db.commit()
                return f"❌ Revalidation failed: Quote comparison returned unsafe: {comp.reason_codes}"

            payload = OrderProposalCreate(
                idempotency_key=order.idempotency_key,
                symbol=order.symbol,
                side=order.side,
                order_type=order.order_type,
                quantity=order.quantity,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                current_price=quote.last_price,
                strategy_id=order.strategy_id,
                expires_at=order.expires_at,
                data_timestamp=quote.market_timestamp,
                data_quality_status="valid",
                provider_disagreement_percent=comp.disagreement_percent,
                bid=quote.bid,
                ask=quote.ask,
                average_daily_volume=quote.volume,
            )

            from app.risk.engine import RiskEngine
            from app.services.orders import approve_order

            decision = approve_order(
                db,
                order,
                payload,
                RiskEngine(),
                max_data_age_seconds=settings.DATA_MAX_STALENESS_SECONDS,
            )
            if order.status != "approved":
                return f"❌ Pre-trade risk validation failed: {', '.join(decision.reasons)}"

            # Submit to Paper Broker for realistic simulated execution
            from app.brokers.paper import PaperBroker

            broker = PaperBroker(db)
            order = broker.submit_order(order, quote.last_price, quote.volume or 10000)

            # Clear tokens after execution
            order.approval_token = None
            order.approval_token_expires_at = None
            db.commit()

            fill_info = (
                f"filled={order.filled_quantity} shares @ ৳{order.average_fill_price or 0:.2f}"
                if order.status in ("filled", "partially_filled")
                else "submitted"
            )
            return (
                f"✅ Order execution success:\n"
                f"● ID: {order.id}\n"
                f"● Action: {order.side.upper()} {order.quantity} {order.symbol}\n"
                f"● Result: {order.status.upper()} ({fill_info})"
            )
        except Exception as exc:
            logger.error("Telegram approval revalidation failed: %s", exc)
            return f"❌ Approval execution error: {exc}"


async def reject_token_cmd(token: str) -> str:
    with SessionLocal() as db:
        order = db.scalar(
            select(Order)
            .where(Order.approval_token == token)
            .where(Order.status == "awaiting_approval")
        )
        if not order:
            return "❌ Error: Invalid token or order has already been processed."

        order.status = "rejected"
        order.approval_token = None
        order.approval_token_expires_at = None
        db.commit()

        from app.services.audit import append_audit

        append_audit(
            db,
            actor="telegram_bot",
            event_type="order.rejected",
            entity_type="order",
            entity_id=order.id,
            new_state={"status": order.status},
        )
        db.commit()
        return f"🚫 Order proposal {order.id} rejected and cancelled."

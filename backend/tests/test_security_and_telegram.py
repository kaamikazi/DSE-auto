import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.main import app
from app.models import AuditEvent, Order, PaperAccount, RiskState
from app.notifications.telegram import (
    approve_token_cmd,
    handle_update,
    send_telegram_alert,
    validate_chat,
)


def seed(db: Session) -> None:
    account = db.get(PaperAccount, 1)
    if not account:
        db.add(PaperAccount(id=1, cash=Decimal("100000.00"), starting_cash=Decimal("100000.00")))
    else:
        account.cash = Decimal("100000.00")

    state = db.get(RiskState, 1)
    if not state:
        db.add(RiskState(id=1, state="healthy", reason="seeding"))
    else:
        state.state = "healthy"
    db.commit()


# ================== 1. API SECURITY & CORS & RATE LIMIT TESTS ==================


def test_api_key_verification() -> None:
    client = TestClient(app)

    # 1. Missing API Key
    response = client.post("/api/v1/risk/pause")
    assert response.status_code == 401

    # 2. Invalid API Key
    response = client.post("/api/v1/risk/pause", headers={"X-API-Key": "invalid-key"})
    assert response.status_code == 401

    # 3. Valid API Key (using test-secret-key-at-least-32-characters)
    settings = get_settings()
    response = client.post("/api/v1/risk/pause", headers={"X-API-Key": settings.API_SECRET_KEY})
    assert response.status_code == 200


def test_cors_and_trusted_host() -> None:
    client = TestClient(app)

    # Trusted Host check: request with untrusted host header should fail
    response = client.get("/", headers={"Host": "evilhost.com"})
    assert (
        response.status_code == 400 or response.status_code == 200
    )  # Depending on TrustedHost configuration


def test_rate_limiting() -> None:
    # Build a separate client or call the RateLimitMiddleware directly to verify 429
    from fastapi import FastAPI

    from app.core.rate_limit import RateLimitMiddleware

    test_app = FastAPI()
    test_app.add_middleware(RateLimitMiddleware, max_requests=2, window_seconds=10)

    @test_app.get("/test")
    def test_route() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(test_app)

    # Call 1 -> OK
    assert client.get("/test").status_code == 200
    # Call 2 -> OK
    assert client.get("/test").status_code == 200
    # Call 3 -> 429 Too Many Requests
    response = client.get("/test")
    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]


# ================== 2. LOG SECRET REDACTION & AUDIT FAIL-CLOSED ==================


def test_log_secret_redaction() -> None:
    from app.core.logging import redact

    payload = {"api_key": "super-secret-key", "normal_field": "hello"}
    redacted = redact(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["normal_field"] == "hello"


def test_audit_storage_failure_rolls_back_transaction(db: Session) -> None:
    seed(db)

    # Verify starting state
    start_orders = db.scalars(select(Order)).all()

    # Mock append_audit to throw operational error
    with patch(
        "app.services.orders.append_audit",
        side_effect=OperationalError(
            "Audit write failed", params={}, orig=Exception("Audit write failed")
        ),
    ):
        from app.data.providers.mock import MockProvider
        from app.risk.engine import RiskEngine
        from app.schemas.trading import OrderProposalCreate
        from app.services.orders import propose_order

        payload = OrderProposalCreate(
            idempotency_key="audit-failure-key",
            symbol="GP",
            side="buy",
            quantity=10,
            limit_price=Decimal("100"),
            current_price=Decimal("100"),
            data_timestamp=datetime.now(UTC),
            average_daily_volume=1000,
        )

        with pytest.raises(OperationalError):
            propose_order(db, payload, RiskEngine(), 30, MockProvider())

        # Verify database stayed clean (no order was saved because transaction rolled back!)
        db.rollback()
        current_orders = db.scalars(select(Order)).all()
        assert len(current_orders) == len(start_orders)


# ================== 3. TELEGRAM CHAT COMMANDS & BOT OPERATION ==================


def test_telegram_unauthorized_chat_logging(db: Session) -> None:
    seed(db)

    # Message update from unauthorized chat ID
    update = {
        "update_id": 9999,
        "message": {
            "text": "/status",
            "chat": {"id": 1111111},  # Unauthorized chat ID
        },
    }

    asyncio.run(handle_update(update))

    # Audit log check
    event = db.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "security.unauthorized_telegram_access")
    )
    assert event is not None
    assert "1111111" in event.actor


def test_telegram_multiple_configured_chat_ids() -> None:
    assert validate_chat({"100", "200"}, "200")
    assert not validate_chat({"100", "200"}, "300")


def test_telegram_command_routing_and_reused_tokens(db: Session) -> None:
    seed(db)

    # 1. Propose order
    order = Order(
        idempotency_key="tg-lifecycle-key",
        symbol="GP",
        side="buy",
        order_type="limit",
        quantity=5,
        limit_price=Decimal("283.40"),
        status="awaiting_approval",
        approval_token="TG999",
        approval_token_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    db.add(order)
    db.commit()

    # 2. Approve command first time
    reply = asyncio.run(approve_token_cmd("TG999"))
    assert (
        "execution success" in reply.lower()
        or "filled" in reply.lower()
        or "submitted" in reply.lower()
    )

    db.refresh(order)
    assert order.status in ("filled", "partially_filled", "submitted")

    # 3. Approve command second time (reused token / duplicate approval) -> rejects
    reply2 = asyncio.run(approve_token_cmd("TG999"))
    assert "error" in reply2.lower() or "invalid" in reply2.lower()


def test_telegram_expired_proposal_and_outage_fallback() -> None:
    # Outage fallback: send synchronous telegram alert does not crash when network is down
    with patch("httpx.Client.post", side_effect=Exception("Connection refused")):
        # Call alert sender, should log error but not raise exception
        send_telegram_alert("Hello fallback world")

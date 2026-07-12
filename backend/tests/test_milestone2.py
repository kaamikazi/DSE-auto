import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.providers.base import DataProviderError, MarketDataProvider
from app.data.providers.reliable import ReliableDataProvider
from app.models import AuditEvent, JobExecution, Order, PaperAccount, RiskState
from app.notifications.telegram import (
    approve_token_cmd,
    handle_update,
)
from app.risk.engine import RiskEngine
from app.risk.kill_switch import get_state
from app.schemas.market import QualityStatus, Quote
from app.schemas.trading import OrderProposalCreate
from app.services.data_validation import compare_quotes
from app.services.orders import approve_order
from app.services.recovery import run_startup_recovery
from app.services.scheduler import logged_job


# 1. Scheduler Overlap & Failures Test
def test_scheduler_overlap_and_failures(db: Session) -> None:
    call_count = 0

    @logged_job("test_job", max_attempts=2, backoff_seconds=1)
    def my_test_job(fail: bool = False) -> str:
        nonlocal call_count
        call_count += 1
        if fail:
            raise ValueError("Test error simulated")
        return "success"

    # Clean test runs
    my_test_job(fail=False)
    assert call_count == 1

    # Verify status in database
    run = db.scalar(select(JobExecution).where(JobExecution.job_name == "test_job"))
    assert run is not None
    assert run.status == "success"
    assert run.attempts == 1

    # Overlap test: mock an active running job
    active_run = JobExecution(job_name="test_job", status="running", started_at=datetime.now(UTC))
    db.add(active_run)
    db.commit()

    # Re-run should block overlap and not call the function
    my_test_job(fail=False)
    assert call_count == 1  # call_count remains 1

    # Failure with retry test
    db.delete(active_run)
    db.commit()

    my_test_job(fail=True)
    # Attempts is 2 because max_attempts=2, call_count should increment by 2
    assert call_count == 3
    failed_run = db.scalars(
        select(JobExecution)
        .where(JobExecution.job_name == "test_job")
        .order_by(JobExecution.started_at.desc())
    ).first()
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.attempts == 2
    assert failed_run.error_message is not None
    assert "Test error simulated" in failed_run.error_message


# 2. Restart Recovery Test
def test_startup_recovery(db: Session) -> None:
    # Set up paper cash and mismatched state
    account = db.get(PaperAccount, 1)
    if not account:
        account = PaperAccount(id=1, cash=Decimal("100000.00"), starting_cash=Decimal("100000.00"))
        db.add(account)
    else:
        account.cash = Decimal("100000.00")
    db.commit()

    # Create active order
    order = Order(
        idempotency_key="recovery-key-1",
        symbol="GP",
        side="buy",
        quantity=10,
        limit_price=Decimal("100.00"),
        status="submitted",
    )
    db.add(order)
    db.commit()

    # Run recovery, it should find submitted order and mark system degraded / reconciliation required
    run_startup_recovery(db)
    state = get_state(db)
    assert state.state == "reconciliation_required"

    # Make database reconciled and run recovery again
    order.status = "expired"
    db.commit()
    run_startup_recovery(db)
    state = get_state(db)
    assert state.state == "healthy"


# 3. Provider Failover and Circuit Breaker
def test_provider_failover_and_circuit_breaker() -> None:
    primary = MagicMock(spec=MarketDataProvider)
    secondary = MagicMock(spec=MarketDataProvider)
    primary.name = "primary_mock"
    secondary.name = "secondary_mock"

    # Set primary to fail
    primary.get_symbols.side_effect = Exception("Network down")
    secondary.get_symbols.return_value = ["GP", "BEXIMCO"]

    # Wrap with reliable provider
    reliable = ReliableDataProvider(primary, secondary, failure_threshold=2, cooldown_seconds=60)

    # Initial call should failover to secondary transparently
    symbols = reliable.get_symbols()
    assert symbols == ["GP", "BEXIMCO"]
    assert reliable.primary_breaker.failure_count == 1
    assert reliable.primary_breaker.state == "closed"

    # Second failure should trigger circuit breaker OPEN status
    with pytest.raises(DataProviderError):
        # Trigger second failure on primary and simulate secondary failure too
        secondary.get_symbols.side_effect = Exception("Fallback fail")
        reliable.get_symbols()

    assert reliable.primary_breaker.state == "open"
    assert reliable.primary_breaker.failure_count == 2

    # A subsequent call should skip primary altogether and try secondary directly
    primary.get_symbols.reset_mock()
    secondary.get_symbols.side_effect = None
    secondary.get_symbols.return_value = ["SUCCESS"]
    
    res = reliable.get_symbols()
    assert res == ["SUCCESS"]
    # Primary get_symbols should not have been called because circuit was open
    primary.get_symbols.assert_not_called()


# 4. Telegram Authorization Block
def test_telegram_unauthorized_chat(db: Session) -> None:
    # Mock update payload from unauthorized chat ID
    update = {
        "update_id": 1000,
        "message": {
            "text": "/status",
            "chat": {"id": 9999999},  # unauthorized chat ID
        },
    }


    # Handle update
    asyncio.run(handle_update(update))

    # Verify a security event was logged to audit chain
    event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "security.unauthorized_telegram_access"
        )
    )
    assert event is not None
    assert "9999999" in event.actor


# 5. Telegram Token Expiration and Command Handling
def test_telegram_expired_approval_token(db: Session) -> None:
    # Create order proposal with expired token
    order = Order(
        idempotency_key="tg-expired-1",
        symbol="GP",
        side="buy",
        quantity=5,
        limit_price=Decimal("200.00"),
        status="awaiting_approval",
        approval_token="EXP123",
        approval_token_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(order)
    db.commit()


    # Call bot command handler
    reply = asyncio.run(approve_token_cmd("EXP123"))
    assert "expired" in reply

    # Verify order transitioned to expired status
    db.refresh(order)
    assert order.status == "expired"


# 6. Revalidation and Stale Quote Timestamp Rejections
def test_revalidation_with_stale_timestamp(db: Session) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    
    # 1. Stale primary quote
    primary_quote = Quote(
        symbol="GP",
        last_price=Decimal("150.00"),
        bid=Decimal("149.00"),
        ask=Decimal("151.00"),
        volume=10000,
        market_timestamp=now - timedelta(seconds=100),  # > 30s staleness
        received_at=now,
        source="mock",
        quality_status=QualityStatus.VALID,
    )
    secondary_quote = primary_quote.model_copy(update={"market_timestamp": now, "source": "csv"})

    comp = compare_quotes(
        primary_quote,
        secondary_quote,
        max_disagreement_percent=Decimal("1.0"),
        max_staleness_seconds=settings.DATA_MAX_STALENESS_SECONDS,
        now=now,
    )
    assert comp.safe_for_orders is False
    assert "STALE_PRIMARY_DATA" in comp.reason_codes

    # 2. Risk check failure for stale timestamp
    db.add(RiskState(id=1, state="healthy", reason="Healthy test state"))
    db.commit()

    order = Order(
        idempotency_key="stale-test-1",
        symbol="GP",
        side="buy",
        quantity=10,
        limit_price=Decimal("150.00"),
        status="awaiting_approval",
    )
    payload = OrderProposalCreate(
        idempotency_key=order.idempotency_key,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        limit_price=order.limit_price,
        current_price=primary_quote.last_price,
        data_timestamp=primary_quote.market_timestamp,  # stale timestamp
        data_quality_status="unsafe",
        provider_disagreement_percent=Decimal("0.0"),
    )

    decision = approve_order(
        db, order, payload, RiskEngine(), max_data_age_seconds=settings.DATA_MAX_STALENESS_SECONDS
    )
    assert decision.approved is False
    assert "STALE_OR_UNSAFE_DATA" in decision.reason_codes

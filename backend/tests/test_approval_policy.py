from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.data.providers.bdshare_provider import BDShareProvider
from app.data.providers.csv_provider import CSVProvider
from app.data.providers.mock import MockProvider
from app.data.providers.reliable import ReliableDataProvider
from app.risk.engine import RiskEngine
from app.schemas.trading import OrderProposalCreate
from app.services.orders import approve_order, propose_order


def seed(db: Session) -> None:
    from app.models import PaperAccount, RiskState

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


def proposal(symbol: str = "GP", idempotency_key: str = "policy-test-key-1") -> OrderProposalCreate:
    return OrderProposalCreate(
        idempotency_key=idempotency_key,
        symbol=symbol,
        side="buy",
        quantity=10,
        limit_price=Decimal("150.00"),
        current_price=Decimal("150.00"),
        data_timestamp=datetime.now(UTC),
        average_daily_volume=100000,
    )


# 1. Mock data cannot approve orders outside explicitly enabled test mode
def test_mock_data_approval_disabled_outside_test_mode(db: Session) -> None:
    seed(db)
    payload = proposal(idempotency_key="mock-outside-test")

    # We patch settings to simulate production environment (APP_ENV != "test" and ALLOW_MOCK_APPROVALS = False)
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(get_settings(), "APP_ENV", "production")

        provider = MockProvider()
        # Verify capability suitability is False
        assert provider.get_capabilities().suitable_for_order_approval is False

        # Try to propose order -> should fail with UNSAFE_PROVIDER
        order, decision = propose_order(db, payload, RiskEngine(), 30, provider)
        assert decision.approved is False
        assert "UNSAFE_PROVIDER" in decision.reason_codes
        assert order.status == "risk_rejected"


# 2. CSV historical data cannot be mistaken for a current quote (fails approval)
def test_csv_data_cannot_approve_order(db: Session) -> None:
    seed(db)
    payload = proposal(idempotency_key="csv-approve-test")

    provider = CSVProvider(root=get_settings().CSV_DATA_DIR)
    assert provider.get_capabilities().suitable_for_order_approval is False

    order, decision = propose_order(db, payload, RiskEngine(), 30, provider)
    assert decision.approved is False
    assert "UNSAFE_PROVIDER" in decision.reason_codes
    assert order.status == "risk_rejected"


# 3. A quote without a trustworthy timestamp cannot approve an order
def test_quote_without_trustworthy_timestamp_cannot_approve_order(db: Session) -> None:
    seed(db)
    payload = proposal(idempotency_key="bdshare-approve-test")

    provider = BDShareProvider()
    assert provider.get_capabilities().suitable_for_order_approval is False

    order, decision = propose_order(db, payload, RiskEngine(), 30, provider)
    assert decision.approved is False
    assert "UNSAFE_PROVIDER" in decision.reason_codes
    assert order.status == "risk_rejected"


# 4. Failover cannot silently downgrade from an approval-safe provider to an approval-unsafe provider
def test_failover_downgrade_rejection(db: Session) -> None:
    seed(db)
    payload = proposal(idempotency_key="failover-downgrade-test")

    # Primary is safe in tests, secondary is unsafe (e.g. bdshare)
    primary = MockProvider()
    secondary = BDShareProvider()

    reliable = ReliableDataProvider(primary, secondary)

    # Initial state: primary active (safe)
    assert reliable.get_capabilities().suitable_for_order_approval is True

    # Propose proposal with active safe primary -> approved
    order, decision = propose_order(db, payload, RiskEngine(), 30, reliable)
    assert decision.approved is True
    assert order.status == "awaiting_approval"

    # Trigger primary failure -> primary circuit opens, failover to unsafe secondary
    import time

    reliable.primary_breaker.state = "open"
    reliable.primary_breaker.last_failure_time = time.time()
    assert reliable.primary_breaker.state == "open"

    # Capabilities should now evaluate secondary (unsafe)
    assert reliable.get_capabilities().suitable_for_order_approval is False

    # Try to approve the order now -> should fail revalidation because active provider is unsafe
    approve_payload = payload.model_copy(update={"idempotency_key": "failover-downgrade-test"})
    reval_decision = approve_order(db, order, approve_payload, RiskEngine(), 30, reliable)
    assert reval_decision.approved is False
    assert "UNSAFE_PROVIDER" in reval_decision.reason_codes
    assert order.status == "risk_rejected"

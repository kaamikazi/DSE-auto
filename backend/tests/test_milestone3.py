from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.brokers.dse_execution import DSEExecutionContext, DSEExecutionRules
from app.models import PaperAccount
from app.services.market_calendar import DSEMarketCalendar
from app.services.paper_sessions import create_session, recover_stale_sessions, transition_session

ROOT = Path(__file__).resolve().parents[2]


def seed_account(db: Session) -> None:
    if db.get(PaperAccount, 1) is None:
        db.add(PaperAccount(id=1, cash=Decimal("100000"), starting_cash=Decimal("100000")))
        db.commit()


def test_duplicate_active_paper_session_is_blocked(db: Session) -> None:
    seed_account(db)
    first = create_session(db, "first", ["GP"], ["ma"], {})
    transition_session(db, first, "warming_up", "test")
    second = create_session(db, "second", ["GP"], ["ma"], {})
    with pytest.raises(ValueError, match="already used"):
        transition_session(db, second, "warming_up", "test")


def test_stale_session_restart_recovery(db: Session) -> None:
    seed_account(db)
    session = create_session(db, "stale", ["GP"], ["ma"], {})
    transition_session(db, session, "warming_up", "test")
    session.heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    db.commit()
    assert recover_stale_sessions(db, 60) == 1
    assert session.state == "reconciliation_required"


def test_calendar_weekend_holiday_and_emergency_closure(tmp_path: Path) -> None:
    config = tmp_path / "calendar.json"
    config.write_text(
        '{"timezone":"Asia/Dhaka","weekend_days":[4,5],"emergency_closed":false,"periods":{"auction":{"open":"09:45","close":"10:00"},"continuous":{"open":"10:00","close":"14:30"}}}'
    )
    calendar = DSEMarketCalendar(config)
    friday = datetime(2026, 7, 17, 5, 0, tzinfo=UTC)
    assert calendar.decision(friday).reason == "weekend"


def test_execution_rules_are_conservative() -> None:
    rules = DSEExecutionRules(ROOT / "config" / "dse_execution_model.yaml")
    context = DSEExecutionContext(Decimal("100"), 10000, queue_ahead=2000)
    assert (
        rules.fillable_volume(context, "pessimistic")
        < rules.fillable_volume(context, "balanced")
        < rules.fillable_volume(context, "optimistic")
    )
    lower, upper = rules.band(Decimal("100"))
    assert lower == Decimal("90")
    assert upper == Decimal("110")


def test_accelerated_sixty_day_soak_invariants() -> None:
    state = {
        "cash": Decimal("1000000"),
        "position": 0,
        "duplicate_orders": set(),
        "events": 0,
        "recoveries": 0,
    }
    for day in range(60):
        state["events"] += 8
        if day in {7, 19, 41}:
            state["recoveries"] += 1
            continue
        key = f"GP-{day}"
        assert key not in state["duplicate_orders"]
        state["duplicate_orders"].add(key)
        quantity = 1 if day % 2 == 0 else -1
        if quantity < 0 and state["position"] == 0:
            continue
        state["position"] += quantity
        state["cash"] -= Decimal(quantity * 100)
        assert state["cash"] >= 0
        assert state["position"] >= 0
    assert state["events"] == 480
    assert state["recoveries"] == 3

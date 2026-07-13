from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import RiskValidationRun
from app.risk.engine import RiskEngine, RiskLimits
from app.schemas.trading import OrderProposalCreate
from app.services.audit import append_audit


def _proposal(**changes: Any) -> OrderProposalCreate:
    values: dict[str, Any] = {
        "idempotency_key": "risk-validation-base",
        "symbol": "GP",
        "side": "buy",
        "quantity": 100,
        "limit_price": Decimal("100"),
        "current_price": Decimal("100"),
        "data_timestamp": datetime.now(UTC),
        "data_quality_status": "valid",
        "provider_disagreement_percent": Decimal("0"),
        "average_daily_volume": 100_000,
    }
    values.update(changes)
    return OrderProposalCreate(**values)


def validate_risk_controls(db: Session, *, campaign_id: str | None = None) -> RiskValidationRun:
    engine = RiskEngine(
        RiskLimits(
            max_trade_value=Decimal("250000"),
            max_position_percent=Decimal("20"),
            min_average_daily_volume=10_000,
        )
    )
    scenarios: dict[str, tuple[OrderProposalCreate, dict[str, Any], str]] = {
        "position_limits": (
            _proposal(quantity=3_000),
            {},
            "MAX_TRADE_VALUE",
        ),
        "concentration_limits": (
            _proposal(),
            {"current_position_value": Decimal("195000")},
            "MAX_POSITION_PERCENT",
        ),
        "daily_loss_limits": (
            _proposal(),
            {"daily_loss_percent": Decimal("3")},
            "DAILY_LOSS_LIMIT",
        ),
        "campaign_drawdown_limits": (
            _proposal(),
            {"campaign_drawdown_percent": Decimal("10")},
            "CAMPAIGN_DRAWDOWN_LIMIT",
        ),
        "liquidity_limits": (
            _proposal(average_daily_volume=100),
            {},
            "INSUFFICIENT_LIQUIDITY",
        ),
        "stale_data_blocking": (
            _proposal(data_quality_status="unsafe"),
            {},
            "STALE_OR_UNSAFE_DATA",
        ),
        "provider_disagreement_blocking": (
            _proposal(provider_disagreement_percent=Decimal("2")),
            {},
            "PROVIDER_CONFLICT",
        ),
        "repeated_loss_cooldown": (
            _proposal(),
            {"consecutive_losses": 3},
            "REPEATED_LOSS_COOLDOWN",
        ),
        "strategy_suspension": (
            _proposal(),
            {"strategy_suspended": True},
            "STRATEGY_SUSPENDED",
        ),
        "emergency_stop": (
            _proposal(),
            {"kill_switch_state": "emergency_stop"},
            "KILL_SWITCH_NOT_HEALTHY",
        ),
        "restart_recovery": (
            _proposal(),
            {"restart_reconciled": False},
            "RESTART_RECONCILIATION_REQUIRED",
        ),
        "reconciliation_mismatch": (
            _proposal(),
            {"reconciliation_healthy": False},
            "RECONCILIATION_MISMATCH",
        ),
    }
    results: dict[str, Any] = {}
    prevented_exposure = Decimal("0")
    rejected_orders = 0
    for name, (proposal, kwargs, expected_code) in scenarios.items():
        defaults: dict[str, Any] = {
            "kill_switch_state": "healthy",
            "portfolio_value": Decimal("1000000"),
        }
        defaults.update(kwargs)
        decision = engine.evaluate(proposal, **defaults)
        effective = expected_code in decision.reason_codes and decision.rejected
        value = proposal.quantity * (proposal.limit_price or proposal.current_price)
        if effective:
            prevented_exposure += value
            rejected_orders += 1
        results[name] = {
            "effective": effective,
            "expected_code": expected_code,
            "reason_codes": decision.reason_codes,
            "prevented_exposure": str(value if effective else Decimal("0")),
        }
    report: dict[str, Any] = {
        "campaign_id": campaign_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "controls": results,
        "triggers": sum(bool(item["reason_codes"]) for item in results.values()),
        "prevented_exposure": str(prevented_exposure),
        "rejected_orders": rejected_orders,
        "false_positive_candidates": [],
        "missed_risk_candidates": [name for name, item in results.items() if not item["effective"]],
        "operator_overrides": [],
        "all_controls_effective": all(item["effective"] for item in results.values()),
    }
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run = RiskValidationRun(campaign_id=campaign_id, report=report, integrity_hash=digest)
    db.add(run)
    db.commit()
    return run


def record_operator_override(
    db: Session,
    *,
    actor: str,
    control: str,
    reason: str,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Record an override request without bypassing a risk rejection."""

    audit = append_audit(
        db,
        actor=actor,
        event_type="risk.override_recorded",
        entity_type="risk_control",
        entity_id=entity_id,
        new_state={"control": control, "reason": reason, "effect": "record_only"},
    )
    db.commit()
    return {"recorded": True, "audit_event_id": audit.id, "risk_bypassed": False}

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FeeProfile, MarketRuleSet, StrategyRegistration
from app.services.audit import append_audit

RULE_STATUSES = {"assumed", "partially_verified", "verified", "deprecated"}
STRATEGY_STATES = {
    "draft",
    "research",
    "paper_candidate",
    "paper_active",
    "suspended",
    "rejected",
    "archived",
}
REQUIRED_RULES = {
    "market_timezone",
    "weekly_trading_days",
    "trading_sessions",
    "auction_periods",
    "holidays",
    "tick_sizes",
    "price_bands",
    "settlement_assumptions",
    "short_selling_policy",
    "leverage_policy",
    "minimum_order_quantity",
    "order_expiry_rules",
    "transaction_fee_assumptions",
    "tax_assumptions",
    "liquidity_thresholds",
}
PROMOTION_EVIDENCE = {
    "backtest_report",
    "walk_forward_report",
    "sensitivity_report",
    "risk_review",
    "sample_size",
}


def integrity_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_rule_set(
    db: Session,
    *,
    version: str,
    effective_date: date,
    source_reference: str,
    verification_status: str,
    operator_approval: str,
    rules: dict[str, Any],
    change_history: list[dict[str, Any]] | None = None,
) -> MarketRuleSet:
    if verification_status not in RULE_STATUSES:
        raise ValueError("Unknown rule-set verification status")
    missing = sorted(REQUIRED_RULES - rules.keys())
    if missing:
        raise ValueError(f"Rule set is incomplete: {missing}")
    if len(operator_approval.strip()) < 12:
        raise ValueError("Operator approval must explain the rule-set decision")
    payload = {
        "version": version,
        "effective_date": effective_date.isoformat(),
        "source_reference": source_reference,
        "verification_status": verification_status,
        "rules": rules,
        "change_history": change_history or [],
    }
    rule_set = MarketRuleSet(
        version=version,
        effective_date=effective_date,
        source_reference=source_reference,
        verification_status=verification_status,
        operator_approval=operator_approval.strip(),
        rules=rules,
        change_history=change_history or [],
        integrity_hash=integrity_hash(payload),
    )
    db.add(rule_set)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="market_rules.version_created",
        entity_type="market_rule_set",
        entity_id=rule_set.id,
        new_state={
            "version": version,
            "status": verification_status,
            "hash": rule_set.integrity_hash,
        },
    )
    db.commit()
    return rule_set


def create_fee_profile(
    db: Session,
    *,
    name: str,
    version: str,
    effective_date: date,
    configuration: dict[str, Any],
    broker: str | None = None,
    account_label: str | None = None,
) -> FeeProfile:
    normalized = conservative_fee_configuration(configuration)
    payload = {
        "name": name,
        "version": version,
        "effective_date": effective_date.isoformat(),
        "broker": broker,
        "account_label": account_label,
        "configuration": normalized,
    }
    profile = FeeProfile(
        name=name,
        version=version,
        effective_date=effective_date,
        broker=broker,
        account_label=account_label,
        configuration=normalized,
        integrity_hash=integrity_hash(payload),
    )
    db.add(profile)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="fee_profile.version_created",
        entity_type="fee_profile",
        entity_id=profile.id,
        new_state={"name": name, "version": version, "hash": profile.integrity_hash},
    )
    db.commit()
    return profile


def conservative_fee_configuration(configuration: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "brokerage_buy_percent": 0.5,
        "brokerage_sell_percent": 0.5,
        "minimum_brokerage": 10.0,
        "exchange_fee_percent": 0.02,
        "regulatory_fee_percent": 0.01,
        "tax_buy_percent": 0.0,
        "tax_sell_percent": 0.1,
        "settlement_charge": 5.0,
        "account_fee": 0.0,
        "flat_buy_charge": 0.0,
        "flat_sell_charge": 0.0,
        "assumption": "conservative_when_unknown",
    }
    defaults.update(configuration)
    return defaults


def trade_cost_breakdown(profile: FeeProfile, side: str, gross: Decimal) -> dict[str, Decimal]:
    if side not in {"buy", "sell"} or gross < 0:
        raise ValueError("Trade cost requires buy/sell and non-negative gross value")
    config = conservative_fee_configuration(profile.configuration)

    def percent(key: str) -> Decimal:
        return gross * Decimal(str(config[key])) / Decimal("100")

    brokerage = max(percent(f"brokerage_{side}_percent"), Decimal(str(config["minimum_brokerage"])))
    values = {
        "brokerage": brokerage,
        "exchange_fee": percent("exchange_fee_percent"),
        "regulatory_fee": percent("regulatory_fee_percent"),
        "tax": percent(f"tax_{side}_percent"),
        "settlement_charge": Decimal(str(config["settlement_charge"])),
        "account_fee": Decimal(str(config["account_fee"])),
        "flat_charge": Decimal(str(config[f"flat_{side}_charge"])),
    }
    values["total"] = sum(values.values(), Decimal("0"))
    return {
        key: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for key, value in values.items()
    }


def fee_sensitivity(
    profile: FeeProfile,
    side: str,
    gross: Decimal,
    multipliers: tuple[Decimal, ...] = (Decimal("0.75"), Decimal("1"), Decimal("1.25")),
) -> list[dict[str, str]]:
    base = trade_cost_breakdown(profile, side, gross)["total"]
    return [
        {
            "multiplier": str(multiplier),
            "estimated_total": str((base * multiplier).quantize(Decimal("0.01"))),
        }
        for multiplier in multipliers
    ]


def register_strategy(
    db: Session,
    *,
    strategy_id: str,
    version: str,
    code_hash: str,
    parameters: dict[str, Any],
    data_requirements: dict[str, Any],
    minimum_sample_size: int,
    evidence: dict[str, Any] | None = None,
) -> StrategyRegistration:
    if len(code_hash) != 64 or minimum_sample_size <= 0:
        raise ValueError("Strategy requires a SHA-256 code hash and positive sample size")
    registration = StrategyRegistration(
        strategy_id=strategy_id,
        version=version,
        lifecycle_state="draft",
        code_hash=code_hash,
        parameters=parameters,
        data_requirements=data_requirements,
        evidence=evidence or {},
        minimum_sample_size=minimum_sample_size,
    )
    db.add(registration)
    db.flush()
    append_audit(
        db,
        actor="operator",
        event_type="strategy.registered",
        entity_type="strategy",
        entity_id=registration.id,
        new_state={"strategy_id": strategy_id, "version": version, "code_hash": code_hash},
    )
    db.commit()
    return registration


def promote_strategy(
    db: Session,
    registration: StrategyRegistration,
    target_state: str,
    operator_approval: str,
) -> StrategyRegistration:
    if target_state not in STRATEGY_STATES:
        raise ValueError("Unknown strategy lifecycle state")
    if target_state in {"paper_candidate", "paper_active"}:
        missing = sorted(PROMOTION_EVIDENCE - registration.evidence.keys())
        if missing:
            raise ValueError(f"Strategy promotion evidence missing: {missing}")
        if int(registration.evidence["sample_size"]) < registration.minimum_sample_size:
            raise ValueError("Strategy sample size is below the governance minimum")
        if len(operator_approval.strip()) < 12:
            raise ValueError("Operator approval is required; promotion is never automatic")
    allowed = {
        "draft": {"research", "rejected", "archived"},
        "research": {"paper_candidate", "rejected", "archived"},
        "paper_candidate": {"paper_active", "suspended", "rejected", "archived"},
        "paper_active": {"suspended", "archived"},
        "suspended": {"research", "rejected", "archived"},
        "rejected": {"archived"},
        "archived": set(),
    }
    if target_state not in allowed[registration.lifecycle_state]:
        raise ValueError(
            f"Invalid strategy transition {registration.lifecycle_state} -> {target_state}"
        )
    previous = registration.lifecycle_state
    registration.lifecycle_state = target_state
    registration.operator_approval = operator_approval.strip() or registration.operator_approval
    append_audit(
        db,
        actor="operator",
        event_type="strategy.lifecycle_changed",
        entity_type="strategy",
        entity_id=registration.id,
        previous_state={"state": previous},
        new_state={"state": target_state},
    )
    db.commit()
    return registration


def evaluate_strategy_suspension(
    db: Session,
    registration: StrategyRegistration,
    observations: dict[str, Any],
) -> str | None:
    expected_hash = str(observations.get("implementation_hash", registration.code_hash))
    triggers = [
        (
            float(observations.get("drawdown", 0)) > float(observations.get("max_drawdown", 0.15)),
            "excessive_drawdown",
        ),
        (int(observations.get("data_failures", 0)) >= 3, "repeated_data_failures"),
        (
            float(observations.get("turnover", 0)) > float(observations.get("max_turnover", 5.0)),
            "abnormal_turnover",
        ),
        (int(observations.get("risk_rejections", 0)) >= 3, "repeated_risk_rejection"),
        (bool(observations.get("behavior_divergence", False)), "behavior_divergence"),
        (bool(observations.get("insufficient_liquidity", False)), "insufficient_liquidity"),
        (expected_hash != registration.code_hash, "implementation_hash_change"),
    ]
    reason = next((name for triggered, name in triggers if triggered), None)
    if reason and registration.lifecycle_state in {"paper_candidate", "paper_active"}:
        registration.lifecycle_state = "suspended"
        registration.suspension_reason = reason
        append_audit(
            db,
            actor="governance",
            event_type="strategy.auto_suspended",
            entity_type="strategy",
            entity_id=registration.id,
            new_state={"state": "suspended", "reason": reason},
        )
        db.commit()
    return reason


def strategy_by_reference(db: Session, reference: str) -> StrategyRegistration | None:
    strategy_id, separator, version = reference.partition("@")
    if not separator:
        return None
    return db.scalar(
        select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == strategy_id,
            StrategyRegistration.version == version,
        )
    )

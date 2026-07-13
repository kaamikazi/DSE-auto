from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    start_date: date
    planned_end_date: date
    approved_symbols: list[str] = Field(min_length=1)
    approved_strategies: list[str] = Field(min_length=1)
    starting_capital: Decimal = Field(gt=0)
    risk_profile: dict[str, Any]
    data_source_policy: dict[str, Any]
    timestamp_trust_requirement: Literal["operator_attested", "exchange_verified"]
    fill_model: Literal["pessimistic", "balanced", "optimistic"] = "pessimistic"
    benchmark: str = "DSEX"
    operator_notes: str = ""
    active_rule_set_id: str
    active_fee_profile_id: str
    account_id: int = 1


class RuleSetCreate(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    effective_date: date
    source_reference: str = Field(min_length=1)
    verification_status: Literal["assumed", "partially_verified", "verified", "deprecated"]
    operator_approval: str = Field(min_length=12)
    rules: dict[str, Any]
    change_history: list[dict[str, Any]] = Field(default_factory=list)


class FeeProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    effective_date: date
    broker: str | None = None
    account_label: str | None = None
    configuration: dict[str, Any] = Field(default_factory=dict)


class StrategyCreate(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=32)
    code_hash: str = Field(min_length=64, max_length=64)
    parameters: dict[str, Any]
    data_requirements: dict[str, Any]
    minimum_sample_size: int = Field(gt=0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class StrategyTransition(BaseModel):
    target_state: Literal[
        "research",
        "paper_candidate",
        "paper_active",
        "suspended",
        "rejected",
        "archived",
    ]
    operator_approval: str = ""


class StrategyObservation(BaseModel):
    observations: dict[str, Any]


class IncidentCreate(BaseModel):
    incident_type: str
    severity: Literal["low", "medium", "high", "critical"]
    campaign_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    owner: str | None = None


class IncidentTransition(BaseModel):
    target_state: Literal["acknowledged", "mitigated", "resolved", "accepted_risk", "open"]
    owner: str | None = None
    root_cause: str | None = None
    remediation: str | None = None

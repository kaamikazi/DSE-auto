from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _ReadModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class SafetyStatus(_ReadModel):
    trading_mode: str
    live_trading_enabled: bool
    broker_adapter: str
    database_role: str
    audit_valid: bool


class DatasetSummary(_ReadModel):
    registry_id: str
    version: str
    dataset_hash: str
    symbols: list[str]
    coverage: dict[str, Any]
    row_count: int
    adjustment_grain: str
    activation_status: str
    lineage_status: str


class StrategySummary(_ReadModel):
    registration_id: str
    name: str
    version: str
    code_hash: str
    parameter_hash: str
    research_verdict: str
    execution_permission: bool
    promotion_permission: bool


class ResearchRunSummary(_ReadModel):
    run_id: str
    strategy_identity: dict[str, Any]
    dataset_identities: dict[str, Any]
    timing_contract: dict[str, Any]
    costs: dict[str, Any]
    benchmark: dict[str, Any]
    principal_metrics: dict[str, Any]
    verdict: dict[str, str]
    artifact_locations: list[str]

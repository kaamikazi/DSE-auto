from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, PaperSession, StrategyRegistration, Transaction, ValidationCampaign
from app.services.attested_imports import ATTESTATION
from app.services.governance import promote_strategy, register_strategy
from app.services.research_governance import (
    PARAMETERS,
    build_fee_verification_review,
    build_ma_crossover_evidence,
    build_risk_limit_review,
    build_rule_verification_review,
    parameter_set_hash,
    strategy_code_hash,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "config" / "operator_attested_templates"


def test_ma_crossover_hashes_and_replay_are_deterministic() -> None:
    first = build_ma_crossover_evidence()
    second = build_ma_crossover_evidence()
    assert first["code_hash"] == strategy_code_hash()
    assert first["parameter_set_hash"] == parameter_set_hash(PARAMETERS)
    assert first["deterministic_replay"]["passed"] is True
    assert (
        first["deterministic_replay"]["result_hash"]
        == second["deterministic_replay"]["result_hash"]
    )
    assert first["look_ahead_bias"]["status"] == "controlled_in_implementation"
    assert first["survivorship_bias"]["status"] == "not_resolved"
    assert first["risk_review"]["independent"] is False
    assert first["promotion_authorized"] is False
    assert first["synthetic_data_limitation"]


def test_research_registration_cannot_cross_promotion_boundary(db: Session) -> None:
    evidence = build_ma_crossover_evidence()
    registration = register_strategy(
        db,
        strategy_id="ma_crossover",
        version="1.0.0",
        code_hash=str(evidence["code_hash"]),
        parameters={**PARAMETERS, "parameter_set_hash": evidence["parameter_set_hash"]},
        data_requirements=evidence["required_data"],
        minimum_sample_size=252,
        evidence={
            "backtest_report": "research.json",
            "walk_forward_report": "research.json",
            "sensitivity_report": "research.json",
            "risk_review": "non-independent research review",
            "sample_size": 523,
            "promotion_authorized": False,
        },
    )
    promote_strategy(db, registration, "research", "Research registration only")
    with pytest.raises(ValueError, match="explicitly not authorized"):
        promote_strategy(db, registration, "paper_candidate", "Generic text cannot cross boundary")
    db.refresh(registration)
    assert registration.lifecycle_state == "research"
    assert db.scalar(select(func.count()).select_from(StrategyRegistration)) == 1
    assert db.scalar(select(func.count()).select_from(ValidationCampaign)) == 0
    assert db.scalar(select(func.count()).select_from(PaperSession)) == 0
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


def test_all_review_tables_are_unapproved_and_inactive() -> None:
    rules = build_rule_verification_review()
    fees = build_fee_verification_review()
    risk = build_risk_limit_review()
    assert rules["active"] is False
    assert fees["active"] is False
    assert risk["activation_authorized"] is False
    assert len(rules["items"]) == 16
    assert len(fees["items"]) == 12
    assert all(item["approved"] is False for item in rules["items"])
    assert all(item["approved"] is False for item in fees["items"])
    assert all(item["approved"] is False for item in risk["limits"])
    assert all("failure_behavior" in item and "scenario" in item for item in risk["limits"])


def test_decisions_remain_separate_and_are_not_blanket_authorization() -> None:
    approval = json.loads(
        (ROOT / "reports" / "governance" / "pre_campaign_approval_pack.json").read_text(
            encoding="utf-8"
        )
    )
    event_ids = approval["decision_audit_event_ids"]
    assert len(event_ids) == 11
    assert len(set(event_ids)) == 11
    assert approval["blanket_authorization"] is False


def test_operator_attested_templates_are_research_only() -> None:
    manifest = json.loads((TEMPLATES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["attestation"] == ATTESTATION
    assert manifest["timestamp_trust"] == "operator_attested"
    assert manifest["campaign_qualification_allowed"] is False
    assert manifest["campaign_created"] is False
    with (TEMPLATES / "equity_ohlcv.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["symbol"] for row in rows] == ["GP", "ACI", "BRACBANK"]
    assert list(rows[0]) == [
        "symbol",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
    ]
    with (TEMPLATES / "equity_quotes.csv").open(newline="", encoding="utf-8") as handle:
        quote_rows = list(csv.DictReader(handle))
    assert [row["symbol"] for row in quote_rows] == ["GP", "ACI", "BRACBANK"]
    with (TEMPLATES / "dsex_daily.csv").open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle))[0]["timestamp"].startswith("YYYY-MM-DD")

from __future__ import annotations

import inspect
from pathlib import Path

from app.models import StrategyRegistration
from app.services.strategy_research_archival import (
    MOMENTUM_MEASUREMENTS,
    PORTFOLIO_SELECTIONS,
    REBALANCE_FREQUENCIES,
    REJECTION_REASONS,
    WEIGHTING_METHODS,
    archive_registration_evidence,
    assert_archived_state,
    bounded_experiment_matrix,
    canonical_hash,
    data_requirement_report,
    new_strategy_specification,
    survivorship_requirements,
)


def _registration() -> StrategyRegistration:
    return StrategyRegistration(
        id="4faf2623-f458-4d96-93d0-e70e8af8f7f6",
        strategy_id="ma_crossover",
        version="1.0.0",
        lifecycle_state="research",
        code_hash="b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3",
        parameters={"fast": 20, "slow": 50},
        data_requirements={"symbols": ["GP", "ACI", "BRACBANK"]},
        evidence={"promotion_status": "blocked", "campaign_eligibility": False},
        minimum_sample_size=100,
    )


def _contract(registration: StrategyRegistration) -> dict[str, object]:
    return {
        "registration_id": registration.id,
        "code_hash": registration.code_hash,
        "parameter_hash": canonical_hash(registration.parameters),
        "mutable": False,
    }


def test_rejected_strategy_archival_preserves_lifecycle_and_identity() -> None:
    registration = _registration()
    identity = (
        registration.id,
        registration.strategy_id,
        registration.version,
        registration.code_hash,
        canonical_hash(registration.parameters),
    )
    archive_registration_evidence(
        registration,
        _contract(registration),
        decision_sha256="d" * 64,
        authorization_sha256="a" * 64,
    )
    assert_archived_state(registration)
    assert registration.lifecycle_state == "research"
    assert identity == (
        registration.id,
        registration.strategy_id,
        registration.version,
        registration.code_hash,
        canonical_hash(registration.parameters),
    )


def test_archival_blocks_promotion_campaign_execution_and_real_money() -> None:
    registration = _registration()
    evidence = archive_registration_evidence(
        registration,
        _contract(registration),
        decision_sha256="d" * 64,
        authorization_sha256="a" * 64,
    )
    assert evidence["research_verdict"] == "rejected"
    assert evidence["research_role"] == "archived_rejected_benchmark"
    assert evidence["promotion_authorized"] is False
    assert evidence["campaign_eligibility"] is False
    assert evidence["execution_authorization"] is False
    assert evidence["real_money_eligibility"] is False
    assert evidence["qualification"] == "0/60"
    assert len(evidence["rejection_reasons"]) == len(REJECTION_REASONS) == 12


def test_bounded_experiment_matrix_is_complete_and_predeclared() -> None:
    matrix = bounded_experiment_matrix()
    expected = (
        len(MOMENTUM_MEASUREMENTS)
        * len(REBALANCE_FREQUENCIES)
        * len(PORTFOLIO_SELECTIONS)
        * len(WEIGHTING_METHODS)
    )
    assert matrix["configuration_count"] == expected == 72
    assert len(matrix["rows"]) == expected
    assert len({row["experiment_id"] for row in matrix["rows"]}) == expected
    assert matrix["configuration_selection"] == "none"
    assert matrix["optimization_authorized"] is False


def test_matrix_contains_no_performance_driven_selection() -> None:
    matrix = bounded_experiment_matrix()
    prohibited = {"return", "sharpe", "score", "rank", "selected", "winner"}
    for row in matrix["rows"]:
        assert row["performance_observed"] is False
        assert row["selection_status"] == "predeclared_not_evaluated"
        assert not prohibited.intersection(row)


def test_survivorship_controls_fail_closed_without_forward_information() -> None:
    requirements = survivorship_requirements()
    assert requirements["dated_symbol_eligibility"] == "required"
    assert requirements["adjustment_grain"] == "known and consistent"
    assert "held" in requirements["row_dispositions"]
    assert requirements["forward_information_permitted"] is False
    assert "not official listing dates" in requirements["listing_date_boundary"]


def test_new_strategy_remains_design_only_and_unregistered() -> None:
    spec = new_strategy_specification()
    assert spec["lifecycle"] == "design"
    assert spec["registration"] == "absent"
    assert spec["implementation_status"] == "not_implemented"
    assert spec["execution_authorization"] is False
    assert spec["promotion_eligibility"] is False
    assert spec["campaign_eligibility"] is False
    assert spec["qualification_contribution"] == 0
    assert spec["combined_authorization_permitted"] is False
    assert spec["separate_future_authorizations"] == [
        "implementation",
        "registration",
        "execution",
    ]


def test_five_symbols_are_engineering_only() -> None:
    report = data_requirement_report(5)
    assert report["minimum_research_approved_symbols"] == 10
    assert report["current_five_symbol_use"] == "engineering_dry_run_only"
    assert report["research_conclusion_permitted"] is False
    assert report["activation_authorized"] is False


def test_service_has_no_execution_or_registration_path() -> None:
    import app.services.strategy_research_archival as module

    source = inspect.getsource(module)
    assert "run_portfolio(" not in source
    assert "BacktestRequest" not in source
    assert "db.add(StrategyRegistration" not in source


def test_runner_guards_no_operational_side_effects_and_audit() -> None:
    path = Path(__file__).parents[2] / "scripts" / "archive_rejected_strategy.py"
    text = path.read_text(encoding="utf-8")
    assert "before != after" in text
    assert "verify_audit_chain" in text
    assert "cross_sectional_registration_count" in text
    assert "assert_archived_state" in text
    assert "run_portfolio(" not in text
    assert "BacktestRequest" not in text

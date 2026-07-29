from __future__ import annotations

import inspect

from sqlalchemy import func, select

from app.models import (
    AuditChain,
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    Transaction,
    ValidationCampaign,
)
from app.services import final_batch_review
from app.services.audit import append_audit, verify_audit_chain
from app.services.final_batch_review import (
    SECONDARY_SYMBOLS,
    TARGET_SYMBOLS,
    approval_decisions,
    classify_corporate_action,
    lifecycle_evidence,
    portfolio_diversity,
    propose_source_hierarchy,
)


def _source(name: str, adjustment: str, score: float) -> dict[str, object]:
    return {
        "source": name,
        "adjustment_status": adjustment,
        "coverage_start": "2020-01-01",
        "coverage_end": "2024-12-31",
        "rows": 1000,
        "quality_score": score,
        "conflict_burden": 2,
        "invalid_row_burden": 0,
        "license_status": "registered research-use note",
        "human_approval": "",
    }


def test_exact_twelve_symbol_scope_and_secondary_preservation() -> None:
    assert len(TARGET_SYMBOLS) == 12
    assert len(set(TARGET_SYMBOLS)) == 12
    assert len(SECONDARY_SYMBOLS) == 13
    assert set(TARGET_SYMBOLS).isdisjoint(SECONDARY_SYMBOLS)


def test_source_hierarchy_has_all_roles_and_no_automatic_approval() -> None:
    adjusted_short = _source("adjusted-short", "adjusted", 95)
    adjusted_short["rows"] = 1
    hierarchy = propose_source_hierarchy(
        [
            adjusted_short,
            _source("adjusted-long", "adjusted", 90),
            _source("unadjusted", "unadjusted", 89),
            _source("validation", "unknown", 70),
        ]
    )
    assert {row["role"] for row in hierarchy} == {
        "primary_adjusted_source",
        "primary_unadjusted_source",
        "secondary_validation_source",
        "fallback_source",
        "rejected_source",
        "unresolved_source",
    }
    assert all(row["human_approval"] == "" for row in hierarchy)
    primary = next(row for row in hierarchy if row["role"] == "primary_adjusted_source")
    assert primary["source"] == "adjusted-long"


def test_source_hierarchy_does_not_promote_short_files_to_primary() -> None:
    adjusted_short = _source("adjusted-short", "adjusted", 100)
    adjusted_short["rows"] = 251
    hierarchy = propose_source_hierarchy([adjusted_short])
    primary = next(row for row in hierarchy if row["role"] == "primary_adjusted_source")
    assert primary["source"] is None
    assert primary["human_approval"] == ""


def test_lifecycle_evidence_keeps_observation_bounds_separate() -> None:
    evidence = lifecycle_evidence("2020-01-01", "2024-12-31")
    assert evidence["observed_first_valid_date"] == "2020-01-01"
    assert evidence["official_listing_evidence"] is None
    assert evidence["official_delisting_evidence"] is None
    assert evidence["suspension_evidence"] is None
    assert evidence["lifecycle_status"] == "lifecycle_evidence_pending"


def test_corporate_action_rows_remain_unapproved_and_classified() -> None:
    assert (
        classify_corporate_action("possible_suspension_resumption", None, None)
        == "suspension_resumption_candidate"
    )
    assert classify_corporate_action("probable_split", "10", "20") == "adjustment_divergence"
    assert classify_corporate_action("unresolved", None, None) == "insufficient_evidence"


def test_portfolio_diversity_uses_no_strategy_performance() -> None:
    symbols = []
    for index, _symbol in enumerate(TARGET_SYMBOLS):
        symbols.append(
            {
                "sector": "sector_a" if index < 3 else f"sector_{index}",
                "observed_coverage": {
                    "first_valid_date": "2020-01-01",
                    "last_valid_date": "2024-12-31",
                },
                "provisional_source_hierarchy": [
                    {"role": "primary_adjusted_source", "source": "source_a"}
                ],
                "candidate_tiers": {
                    "tier_1_cross_source_confirmed": 10,
                    "tier_2_single_source_high_quality": 20,
                },
                "corporate_action_held_rows": 1,
                "liquidity_data_availability": 0.9,
            }
        )
    result = portfolio_diversity(symbols)
    assert result["maximum_sector_weight_percent"] == 25.0
    assert result["equal_symbol_weight_percent"] == 8.3333
    assert result["performance_data_used"] is False


def test_approval_pack_defaults_each_inclusion_to_rejected() -> None:
    decisions = approval_decisions()
    assert len(decisions) == 12 * 8
    inclusion = [row for row in decisions if row["decision"] == "inclusion permission"]
    assert len(inclusion) == 12
    assert all(row["default"] == "REJECTED / NOT GRANTED" for row in inclusion)
    assert all(row["reviewer_decision"] == row["operator_decision"] == "" for row in decisions)


def test_review_module_has_no_strategy_execution_path() -> None:
    source = inspect.getsource(final_batch_review)
    assert "run_backtest" not in source
    assert "run_symbol" not in source
    assert "activate_dataset(" not in source
    assert "create_campaign(" not in source
    assert "create_order(" not in source


def test_no_dataset_or_trading_side_effects_and_audit_valid(db) -> None:  # type: ignore[no-untyped-def]
    chain = AuditChain(
        status="active",
        genesis_reason="test",
        operator_acknowledgement="test",
        legacy_archive_path="test.json",
        legacy_archive_hash="0" * 64,
    )
    db.add(chain)
    db.commit()
    protected = (ResearchDataset, ValidationCampaign, PaperSession, Signal, Order, Transaction)
    before = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    append_audit(
        db,
        actor="test",
        event_type="research.final_batch_review_prepared",
        entity_type="research_batch",
        new_state={"activation": False, "strategy_execution": False},
    )
    after = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before == after
    assert verify_audit_chain(db)

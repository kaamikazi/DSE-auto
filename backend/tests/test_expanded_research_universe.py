from __future__ import annotations

from datetime import date
from typing import Any

import pytest
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
from app.services.audit import append_audit, verify_audit_chain
from app.services.expanded_research_universe import (
    ACTIVE_RESEARCH_UNIVERSE,
    CANDIDATE_POOL,
    CONTINUITY_ANCHORS,
    EXPANSION_CANDIDATE_COUNT,
    FROZEN_STRATEGY_IDENTITIES,
    MAX_PER_SECTOR,
    CandidateSpec,
    expanded_research_plan,
    freeze_independent_universe_candidate,
    select_universe,
    survivorship_intervals,
)


def _profiles() -> list[dict[str, object]]:
    return [
        {
            "symbol": spec.symbol,
            "sector": spec.sector,
            "selection_eligible": True,
            "quality_score": 100 - index / 10,
            "proposed_activation_decision": "rejected_not_granted",
        }
        for index, spec in enumerate(CANDIDATE_POOL)
    ]


def _corrected_methodology_fixture(specs: list[CandidateSpec]) -> dict[str, Any]:
    summaries = []
    candidates = []
    lifecycle = []
    for spec in specs:
        conflicts = 1
        summaries.append(
            {
                "symbol": spec.symbol,
                "raw_rows": 10000,
                "logical_rows": 10000,
                "duplicate_groups": 1,
                "exact_duplicates_collapsed": 1,
                "tier_1_cross_source_confirmed": 0,
                "tier_2_single_source_high_quality": 3,
                "tier_3_research_only": 1,
                "held_genuine_conflict": conflicts,
                "lifecycle_holds": 0,
                "rejected_duplicate_conflict": 0,
                "rejected_invalid": 0,
                "validation_comparison_dates": 1,
                "validation_independence": "distinct_registered_files_independence_not_proven",
            }
        )
        candidates.extend(
            [
                {
                    "symbol": spec.symbol,
                    "status": "tier_2_single_source_high_quality",
                    "adjustment_status": "adjusted",
                    "source_names": ["registered-public-adjusted"],
                },
                {
                    "symbol": spec.symbol,
                    "status": "tier_2_single_source_high_quality",
                    "adjustment_status": "unadjusted",
                    "source_names": ["registered-public-unadjusted"],
                },
                {
                    "symbol": spec.symbol,
                    "status": "tier_2_single_source_high_quality",
                    "adjustment_status": "adjusted",
                    "source_names": ["registered-public-adjusted"],
                },
                {
                    "symbol": spec.symbol,
                    "status": "tier_3_research_only",
                    "adjustment_status": "unknown",
                    "source_names": ["registered-public-historical"],
                },
            ]
        )
        lifecycle.append(
            {
                "symbol": spec.symbol,
                "lifecycle_status": "lifecycle_evidence_pending",
                "conservative_research_window": {
                    "start": "2012-10-01",
                    "end": "2026-01-22",
                    "basis": "accepted known-adjustment observations; not a listing-date claim",
                },
            }
        )
    return {
        "scope": [spec.symbol for spec in specs],
        "symbol_summary": summaries,
        "candidates": candidates,
        "lifecycle_evidence": lifecycle,
        "conflict_approval_records": [
            {"symbol": spec.symbol, "date": "2021-01-03"} for spec in specs
        ],
        "totals": {"genuine_conflicts": len(specs)},
        "source_hierarchy": [],
    }


def test_selection_is_quality_and_sector_based_not_performance_based() -> None:
    profiles = _profiles()
    selected, sectors = select_universe(profiles)
    assert len(selected) == 25
    assert max(sectors.values()) <= MAX_PER_SECTOR
    assert set(CONTINUITY_ANCHORS).issubset({str(row["symbol"]) for row in selected})
    assert set(sectors) == {spec.sector for spec in CANDIDATE_POOL}
    assert all("return" not in key and "pnl" not in key for row in profiles for key in row)
    assert [row["symbol"] for row in selected] == [
        row["symbol"] for row in select_universe(profiles)[0]
    ]


def test_listing_and_delisting_dates_bound_eligibility() -> None:
    spec = CandidateSpec("TEST", "test", listing_date="2020-02-01", delisting_date="2020-11-30")
    intervals = survivorship_intervals(spec, "2020-01-01", "2020-12-31")
    assert intervals == [
        {
            "from": "2020-02-01",
            "to": "2020-11-30",
            "basis": "verified_listing_lifecycle_bounds",
            "approval": "eligible_if_dataset_is_separately_approved",
        }
    ]


def test_suspension_period_is_removed_not_silently_ignored() -> None:
    spec = CandidateSpec(
        "TEST",
        "test",
        listing_date="2020-01-01",
        suspension_periods=(("2020-03-01", "2020-03-31"),),
    )
    intervals = survivorship_intervals(spec, "2020-01-01", "2020-04-30")
    assert intervals[0]["to"] == "2020-02-29"
    assert intervals[1]["from"] == "2020-04-01"
    assert all(
        not date.fromisoformat(str(row["from"]))
        <= date(2020, 3, 15)
        <= date.fromisoformat(str(row["to"]))
        for row in intervals
    )


def test_unknown_lifecycle_is_provisional_and_rejected() -> None:
    intervals = survivorship_intervals(CandidateSpec("TEST", "test"), "2020-01-01", "2020-12-31")
    assert intervals[0]["basis"] == "first_to_last_valid_observation_proxy"
    assert intervals[0]["approval"].startswith("rejected_pending")


def test_every_approval_defaults_to_rejected() -> None:
    selected, _ = select_universe(_profiles())
    assert all(row["proposed_activation_decision"] == "rejected_not_granted" for row in selected)


def test_dsex_is_not_in_equity_pool_or_strategy_plan() -> None:
    assert all(spec.symbol not in {"DSEX", "00DSEX"} for spec in CANDIDATE_POOL)
    selected, sectors = select_universe(_profiles())
    plan = expanded_research_plan([str(row["symbol"]) for row in selected], sorted(sectors))
    assert "DSEX" not in plan["universe"]
    assert plan["status"] == "prepared_not_authorized_not_executed"
    assert plan["execution_authorized"] is False


def test_no_activation_execution_or_trading_side_effects(db) -> None:  # type: ignore[no-untyped-def]
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
    plan = expanded_research_plan(["GP"], ["telecommunication"])
    append_audit(
        db,
        actor="test",
        event_type="research.expanded_universe_candidate_prepared",
        entity_type="strategy",
        new_state={"strategy_execution": False, "activation": False},
    )
    after = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before == after
    assert plan["execution_authorized"] is False
    assert plan["campaign_authorized"] is False
    assert verify_audit_chain(db)


def test_independent_expansion_is_quality_only_frozen_and_inactive(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    specs = [spec for spec in CANDIDATE_POOL if spec.symbol not in ACTIVE_RESEARCH_UNIVERSE]
    methodology = _corrected_methodology_fixture(specs)
    monkeypatch.setattr("app.services.expanded_research_universe.MIN_EXPANSION_TIER_2_ROWS", 2)
    monkeypatch.setattr(
        "app.services.expanded_research_universe.MIN_EXPANSION_ADJUSTED_TIER_2_ROWS", 1
    )

    frozen = freeze_independent_universe_candidate(methodology, specs=specs)

    assert frozen["candidate_symbols_inspected"] == len(specs)
    assert len(frozen["recommended_symbols"]) == EXPANSION_CANDIDATE_COUNT
    assert frozen["expected_achievable_final_universe_size"] == 25
    assert frozen["active_research_universe_unchanged"] == list(ACTIVE_RESEARCH_UNIVERSE)
    assert frozen["frozen_strategy_identities_unchanged"] == list(FROZEN_STRATEGY_IDENTITIES)
    assert frozen["strategy_calculations_performed"] is False
    assert frozen["strategy_execution"] is False
    assert frozen["activation"] is False
    assert frozen["selection_policy"]["performance_fields_read"] is False
    assert all(not row["active"] for row in frozen["candidates"])
    assert max(frozen["sector_counts_if_approved"].values()) <= MAX_PER_SECTOR
    assert set(frozen["no_mutation_assertions"].values()) == {0}


def test_independent_expansion_retains_conflicts_lifecycle_and_lineage(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    specs = [spec for spec in CANDIDATE_POOL if spec.symbol not in ACTIVE_RESEARCH_UNIVERSE]
    methodology = _corrected_methodology_fixture(specs)
    monkeypatch.setattr("app.services.expanded_research_universe.MIN_EXPANSION_TIER_2_ROWS", 2)
    monkeypatch.setattr(
        "app.services.expanded_research_universe.MIN_EXPANSION_ADJUSTED_TIER_2_ROWS", 1
    )

    frozen = freeze_independent_universe_candidate(methodology, specs=specs)
    city = next(row for row in frozen["candidates"] if row["symbol"] == "CITYBANK")

    assert city["tier_1_rows"] == 0
    assert city["source_independence"].endswith("independence_not_proven")
    assert city["tier_2_adjusted_rows"] == 2
    assert city["complete_lineage"] is True
    assert city["known_adjustment_grain"] is True
    assert city["lifecycle_status"] == "lifecycle_evidence_pending"
    assert any(
        row["review_type"] == "genuine_conflict_resolution" and row["symbol"] == "CITYBANK"
        for row in frozen["human_review_queue"]
    )
    assert any(row["review_type"] == "sector_evidence" for row in frozen["human_review_queue"])


def test_independent_expansion_is_deterministic_and_scope_checked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    specs = [spec for spec in CANDIDATE_POOL if spec.symbol not in ACTIVE_RESEARCH_UNIVERSE]
    methodology = _corrected_methodology_fixture(specs)
    monkeypatch.setattr("app.services.expanded_research_universe.MIN_EXPANSION_TIER_2_ROWS", 2)
    monkeypatch.setattr(
        "app.services.expanded_research_universe.MIN_EXPANSION_ADJUSTED_TIER_2_ROWS", 1
    )

    first = freeze_independent_universe_candidate(methodology, specs=specs)
    second = freeze_independent_universe_candidate(methodology, specs=list(reversed(specs)))

    assert first["recommended_symbols"] == second["recommended_symbols"]
    methodology["scope"] = methodology["scope"][:-1]
    with pytest.raises(ValueError, match="scope"):
        freeze_independent_universe_candidate(methodology, specs=specs)

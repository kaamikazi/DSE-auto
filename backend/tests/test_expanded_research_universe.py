from __future__ import annotations

from datetime import date

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
    CANDIDATE_POOL,
    CONTINUITY_ANCHORS,
    MAX_PER_SECTOR,
    CandidateSpec,
    expanded_research_plan,
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

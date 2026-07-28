from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from scripts.build_target_subset_approval import _artifact
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import NormalizedDailyBar, Order, Transaction, ValidationCampaign
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.canonical_research_candidate import CanonicalCandidateBuilder, DatasetSource
from app.services.target_subset_approval import (
    COVERAGE_UNADJUSTED,
    approval_decisions,
    build_conflict_approval_records,
    build_dsex_forensics,
    classify_invalid_dsex_row,
    conclude_dsex_volume_semantics,
    final_source_hierarchies,
    research_readiness,
    source_role_decision,
    source_role_recommendations,
    validate_pack_invariants,
)

FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def _source(name: str, dataset_id: str, adjustment: str) -> DatasetSource:
    return DatasetSource(
        dataset_id=dataset_id,
        source_hash=f"hash-{dataset_id}",
        source_name=name,
        source_path=f"{dataset_id}.csv",
        adjustment_status=adjustment,
        source_trust="third_party_research",
        timestamp_trust="unknown",
        license_note="human review",
        logical_name=name,
    )


def _row(symbol: str, *, close: str = "5000", volume: str = "100") -> dict[str, str]:
    return {
        "symbol": symbol,
        "date": "2024-01-01",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


def test_source_role_proposal_is_pending_and_explains_consequence() -> None:
    rows = source_role_recommendations(
        [
            {
                "symbol": "GP",
                "source_name": COVERAGE_UNADJUSTED,
                "adjustment_status": "unadjusted",
                "observed_start": "2012-10-01",
                "observed_end": "2026-01-22",
                "valid_row_count": 10,
                "invalid_row_count": 0,
                "duplicate_count": 0,
                "missing_day_count": 2,
                "eligible_conflict_count": 1,
                "source_quality_score": 70.52,
                "license_status": "internal research only",
                "timestamp_provenance": "unknown",
            }
        ],
        symbol="GP",
    )
    assert rows[0]["recommended_role"] == "primary_unadjusted_validation_source"
    assert rows[0]["approval_field"] == "PENDING - HUMAN DECISION REQUIRED"
    assert rows[0]["consequences_of_selecting"]
    assert rows[0]["conservative_alternative"]
    decision = source_role_decision("GP")
    assert decision["recommended_adjusted_source"]
    assert decision["recommended_unadjusted_source"]
    assert decision["validation_sources"]
    assert decision["excluded_sources"]
    assert decision["approval_field"] == "PENDING - HUMAN DECISION REQUIRED"
    assert decision["automatic_approval"] is False


def test_six_conflicts_remain_separate_and_undecided() -> None:
    unexplained = []
    for index, symbol in enumerate(("ACI", "BRACBANK", "BRACBANK", "DSEX", "DSEX"), start=1):
        unexplained.append(
            {
                "symbol": symbol,
                "date": f"2021-01-{index:02d}",
                "source_a": "a",
                "source_b": "b",
                "source_a_values": {"close": "1", "volume": "10"},
                "source_b_values": {"close": "2", "volume": "20"},
                "percentage_difference": {"close": "0.5", "volume": "0.5"},
                "adjustment_status": "unadjusted",
                "nearby_source_a": [],
                "nearby_source_b": [],
                "possible_corporate_action_evidence": "none",
                "possible_source_error": "unverified",
            }
        )
    rounding = [
        {
            "symbol": "GP",
            "date": "2021-04-26",
            "source_a": "a",
            "source_b": "b",
            "values_a": {"close": "340.5", "volume": "56120"},
            "values_b": {"close": "340.2", "volume": "66064"},
            "max_price_relative": "0.00088",
            "volume_relative": "0.1505",
        }
    ]
    rows = build_conflict_approval_records(unexplained, rounding, {"a": 70, "b": 65})
    assert len(rows) == 6
    assert len({row["approval_record_id"] for row in rows}) == 6
    assert all(row["recommendation"] == "hold_for_review" for row in rows)
    assert all(row["reviewer_decision"] == row["operator_decision"] == "" for row in rows)


def test_dsex_clusters_never_auto_map(tmp_path: Path) -> None:
    builder = CanonicalCandidateBuilder(
        tmp_path / "candidate.db", tmp_path, tolerance=Decimal("0.001")
    )
    builder.ingest_rows(
        _source(COVERAGE_UNADJUSTED, "coverage-u", "unadjusted"),
        [("u:1", _row("00DSEX"))],
        FIELDS,
    )
    builder.ingest_rows(
        _source("literal source", "literal", "unknown"),
        [("l:1", _row("DSEX"))],
        FIELDS,
    )
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    result = build_dsex_forensics(builder.db)
    assert result["population"] == 1
    assert result["automatic_mapping"] is False
    assert result["clusters"][0]["approval_status"] == "under_review"
    assert result["clusters"][0]["proposed_mapping_groups"]
    builder.close()


def test_dsex_invalid_classification_is_explicit() -> None:
    assert classify_invalid_dsex_row(["zero_price", "open_outside_range"]) == "zero_index_value"
    assert classify_invalid_dsex_row(["open_outside_range"]) == "open_close_outside_range"
    assert classify_invalid_dsex_row(["high_below_low"]) == "high_low_violation"
    assert classify_invalid_dsex_row([]) == "unresolved"


def test_volume_semantics_excludes_non_comparable_index_volume() -> None:
    result = conclude_dsex_volume_semantics(
        ratio_rows=231, stable_ratio_share=1.0, official_index_has_volume=False
    )
    assert result["outcome"] == "field_not_comparable"
    assert result["confirmed_conversion"] is False
    assert result["automatic_rescale"] is False
    assert result["include_dsex_volume_in_research"] is False


def test_approval_pack_defaults_to_rejection_and_valid_readiness() -> None:
    decisions = approval_decisions()
    hierarchies = final_source_hierarchies()
    payload = {
        "approval_decisions": decisions,
        "conflict_approval_records": [{"id": index} for index in range(6)],
        "readiness": research_readiness(),
        "activation_permission": "REJECTED / NOT GRANTED",
        "qualification": "0/60",
        "source_hierarchies": hierarchies,
    }
    validate_pack_invariants(payload)
    assert len(decisions) == 15
    assert decisions[-1]["recommended_action"] == "REJECTED / NOT GRANTED"
    assert all(row["activation"] is False for row in payload["readiness"])
    assert {row["status"] for row in payload["readiness"]} <= {
        "not_ready",
        "human_decision_required",
        "ready_for_research_activation_review",
        "rejected",
    }
    assert all(row["active"] is False for row in hierarchies)


def test_report_artifact_has_bounded_data_and_query_provenance() -> None:
    artifact = _artifact(
        {
            "provenance": {"generated_at": "2026-07-28T00:00:00+00:00"},
            "approval_decisions": approval_decisions(),
            "dsex_forensics": {
                "classification_counts": {
                    "unresolved": 5295,
                    "alternate_dsex_label": 1051,
                    "duplicate_alias": 240,
                }
            },
            "readiness": research_readiness(),
        }
    )
    assert artifact["surface"] == "report"
    assert artifact["snapshot"]["status"] == "ready"
    assert len(artifact["snapshot"]["datasets"]["decisions"]) == 15
    source = artifact["sources"][0]
    assert source["query"]["sql"].startswith("SELECT normalized_symbol")
    assert source["query"]["tables_used"] == ["canonical_candidate.observations"]
    assert artifact["manifest"]["charts"][0]["sourceId"] == source["id"]


def test_pack_preparation_has_no_activation_side_effects(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Target subset approval test chain")
    before = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
    approval_decisions()
    final_source_hierarchies()
    research_readiness()
    after = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
    assert before == after
    assert verify_audit_chain(db)

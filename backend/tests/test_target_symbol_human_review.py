from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import NormalizedDailyBar, Order, Transaction, ValidationCampaign
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.canonical_research_candidate import CanonicalCandidateBuilder, DatasetSource
from app.services.target_symbol_human_review import (
    analyze_volume_ratios,
    build_calendar_review,
    build_corporate_action_review,
    build_review_samples,
    build_source_hierarchy_review,
    build_unexplained_conflict_review,
    classify_dsex_alias,
    classify_rounding_conflict,
    provisional_policies,
    readiness_statuses,
)

FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def _source(name: str, dataset_id: str, adjustment: str = "unadjusted") -> DatasetSource:
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


def _row(symbol: str = "ACI", close: str = "103", volume: str = "1000") -> dict[str, str]:
    return {
        "symbol": symbol,
        "date": "2024-01-01",
        "open": "100",
        "high": "105" if Decimal(close) <= 105 else close,
        "low": "99",
        "close": close,
        "volume": volume,
    }


def _builder(tmp_path: Path) -> CanonicalCandidateBuilder:
    builder = CanonicalCandidateBuilder(
        tmp_path / "candidate.db", tmp_path, tolerance=Decimal("0.001")
    )
    builder.ingest_rows(_source("source a", "a"), [("a:1", _row())], FIELDS)
    builder.ingest_rows(_source("source b", "b"), [("b:1", _row(close="104"))], FIELDS)
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    return builder


def test_dsex_alias_classification_requires_cross_source_evidence() -> None:
    assert (
        classify_dsex_alias(
            raw_symbol="00DSEX",
            instrument_class="index",
            mapping_reason="known_index_alias_candidate",
            exact_literal_overlap=True,
            same_source_literal_overlap=False,
            value_scale_consistent=True,
        )
        == "alternate_dsex_label"
    )
    assert (
        classify_dsex_alias(
            raw_symbol="00DSEX",
            instrument_class="index",
            mapping_reason="known_index_alias_candidate",
            exact_literal_overlap=False,
            same_source_literal_overlap=False,
            value_scale_consistent=True,
        )
        == "unresolved"
    )


def test_dsex_equity_and_other_index_collisions_are_not_merged() -> None:
    assert (
        classify_dsex_alias(
            raw_symbol="DSEX",
            instrument_class="equity",
            mapping_reason="exact",
            exact_literal_overlap=True,
            same_source_literal_overlap=True,
            value_scale_consistent=True,
        )
        == "equity_symbol_collision"
    )
    assert (
        classify_dsex_alias(
            raw_symbol="DS30",
            instrument_class="index",
            mapping_reason="index",
            exact_literal_overlap=False,
            same_source_literal_overlap=False,
            value_scale_consistent=True,
        )
        == "another_index"
    )
    assert (
        classify_dsex_alias(
            raw_symbol="00DSEX",
            instrument_class="index",
            mapping_reason="known_index_alias_candidate",
            exact_literal_overlap=True,
            same_source_literal_overlap=True,
            value_scale_consistent=True,
        )
        == "duplicate_alias"
    )


def test_volume_ratio_analysis_never_auto_rescales() -> None:
    result = analyze_volume_ratios(
        [Decimal("100"), Decimal("100"), Decimal("99.9"), Decimal("100.1")]
    )
    assert result["candidate_conversion_factor"] == "100"
    assert result["confidence"] == "medium"
    assert result["classification"] == "unresolved_mismatch"
    assert result["automatic_rescale"] is False


def test_rounding_classification_keeps_material_volume_conflict() -> None:
    assert (
        classify_rounding_conflict(
            max_price_relative=Decimal("0.0008"),
            max_price_absolute=Decimal("0.3"),
            volume_relative=Decimal("0.15"),
            adjustment_status="unadjusted",
            source_precision_differs=True,
        )
        == "material_discrepancy"
    )
    assert (
        classify_rounding_conflict(
            max_price_relative=Decimal("0.00001"),
            max_price_absolute=Decimal("0.01"),
            volume_relative=Decimal("0.01"),
            adjustment_status="unadjusted",
            source_precision_differs=True,
        )
        == "source_precision_loss"
    )


def test_unexplained_conflict_is_preserved_for_human_review(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    result = build_unexplained_conflict_review(
        builder.db,
        source_urls={"hash-a": "https://example.invalid/a", "hash-b": "https://example.invalid/b"},
    )
    assert result["target_scope_count"] == 1
    assert result["rows"][0]["recommended_decision"] == "hold_for_review"
    assert result["rows"][0]["human_review_status"] == "under_review"
    assert result["rows"][0]["raw_source_a"]["source_row_id"] == "a:1"
    builder.close()


def test_target_source_hierarchy_is_recommendation_only(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    rows = build_source_hierarchy_review(
        builder.db,
        source_scores={"source a": 75.0, "source b": 65.0},
        source_catalog={
            "source a": {
                "license_note": "reviewed metadata only",
                "source_trust": "third_party_research",
                "timestamp_trust": "unknown",
            }
        },
    )
    aci = [row for row in rows if row["symbol"] == "ACI"]
    assert len(aci) == 2
    assert all(row["recommendation_is_final"] is False for row in aci)
    assert all(row["human_approval"] == "pending" for row in aci)
    builder.close()


def test_review_sampling_includes_raw_held_evidence(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    subset = {
        "candidate_rows": [],
        "held_rows": [
            {
                "symbol": "ACI",
                "trading_date": "2024-01-01",
                "adjustment_status": "unadjusted",
                "reason": "unresolved_eligible_source_conflict",
            }
        ],
    }
    samples = build_review_samples(
        builder.db,
        subset=subset,
        unexplained_rows=[],
        corporate_rows=[],
        source_urls={"hash-a": "https://example.invalid/a"},
    )
    held = [row for row in samples if row["sample_type"] == "held_row"]
    assert held
    assert held[0]["raw_evidence"][0]["source_hash"] in {"hash-a", "hash-b"}
    assert held[0]["raw_evidence"][0]["source_row_id"] in {"a:1", "b:1"}
    volume = [row for row in samples if row["sample_type"] == "largest_volume_disagreement"]
    assert volume
    assert volume[0]["raw_source_a"]["source_hash"] == "hash-a"
    builder.close()


def test_corporate_action_review_includes_raw_file_lineage(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    rows = build_corporate_action_review(
        builder.db,
        [
            {
                "normalized_symbol": "ACI",
                "trading_date": "2024-01-01",
                "source_dataset_id": "a",
                "evidence": json.dumps(["a:1", "a:1"]),
                "revised_classification": "insufficient_evidence",
            }
        ],
        [],
        source_urls={"hash-a": "https://example.invalid/a"},
    )
    assert rows[0]["source_file_hash"] == "hash-a"
    assert rows[0]["source_url"] == "https://example.invalid/a"
    assert rows[0]["automatic_approval"] is False
    builder.close()


def test_calendar_and_sampling_preserve_long_gap_boundaries(tmp_path: Path) -> None:
    builder = CanonicalCandidateBuilder(
        tmp_path / "calendar.db", tmp_path, tolerance=Decimal("0.001")
    )
    first = _row()
    second = {**_row(), "date": "2024-01-15"}
    builder.ingest_rows(_source("source a", "a"), [("a:1", first), ("a:2", second)], FIELDS)
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    calendar = {row["symbol"]: row for row in build_calendar_review(builder.db)}
    assert calendar["ACI"]["long_gap_count"] == 1
    assert calendar["ACI"]["single_source_gap_count"] == 9
    candidates = [
        {
            "symbol": "ACI",
            "trading_date": day,
            "adjustment_status": "unadjusted",
            "lineage": [],
        }
        for day in ("2024-01-01", "2024-01-15")
    ]
    samples = build_review_samples(
        builder.db,
        subset={"candidate_rows": candidates, "held_rows": []},
        unexplained_rows=[],
        corporate_rows=[],
        source_urls={"hash-a": "https://example.invalid/a"},
    )
    boundaries = [row for row in samples if row["sample_type"] == "long_gap_boundary"]
    assert boundaries
    assert boundaries[0]["raw_boundary_evidence"][0]["source_row_id"] == "a:1"
    builder.close()


def test_readiness_classification_is_not_activation() -> None:
    rows = readiness_statuses(
        dsex_mapping_rows=6586,
        conflicts_by_symbol={"ACI": 1, "BRACBANK": 2},
        source_approvals_by_symbol={symbol: 1 for symbol in ("ACI", "BRACBANK", "DSEX", "GP")},
    )
    statuses = {row["symbol"]: row["status"] for row in rows}
    assert statuses == {
        "ACI": "conflict_review_required",
        "BRACBANK": "conflict_review_required",
        "DSEX": "mapping_review_required",
        "GP": "source_approval_required",
    }
    assert all(row["activation"] is False for row in rows)


def test_policy_preparation_has_no_activation_or_trading_side_effects(
    db: Session, tmp_path: Path
) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Isolated target review test chain")
    before = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
    policies = provisional_policies()
    after = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
    assert all(policy["active"] is False for policy in policies)
    assert (
        before
        == after
        == {
            "normalized_daily_bars": 0,
            "validation_campaigns": 0,
            "orders": 0,
            "transactions": 0,
        }
    )
    assert verify_audit_chain(db)

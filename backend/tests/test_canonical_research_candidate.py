from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    GovernedDataset,
    NormalizedDailyBar,
    Order,
    PaperSession,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.canonical_research_candidate import (
    CanonicalCandidateBuilder,
    DatasetSource,
    calendar_analysis,
    classify_ohlcv,
    compare_values,
    corporate_action_classification,
    duplicate_classification,
    normalize_symbol,
    parse_observation,
    source_quality_score,
)

FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume"]
MAPPING = {name: name for name in FIELDS}


def _source(name: str, *, adjustment: str = "unadjusted") -> DatasetSource:
    return DatasetSource(
        dataset_id=name,
        source_hash=f"hash-{name}",
        source_name=name,
        source_path=f"{name}.csv",
        adjustment_status=adjustment,
        source_trust="third_party_research",
        timestamp_trust="unknown",
        license_note="CC BY 4.0",
        logical_name=name,
    )


def _row(
    symbol: str = "GP",
    trading_date: str = "2024-01-01",
    open_: str = "100",
    high: str = "105",
    low: str = "99",
    close: str = "103",
    volume: str = "1000",
) -> dict[str, str]:
    return {
        "symbol": symbol,
        "date": trading_date,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _observation(source: DatasetSource, row_id: str, row: dict[str, str]):  # type: ignore[no-untyped-def]
    return parse_observation(source, row_id, row, MAPPING)[0]


def test_ohlcv_invalid_categories_are_explicit_and_fail_closed() -> None:
    categories, warnings, normalized = classify_ohlcv(
        raw_date="2024-02-31",
        symbol="",
        raw_open="bad",
        raw_high="1",
        raw_low="2",
        raw_close="0",
        raw_volume="-1",
    )
    assert {
        "impossible_date",
        "symbol_missing",
        "non_numeric_value",
        "high_below_low",
        "close_outside_range",
        "negative_volume",
        "zero_price",
        "suspected_source_corruption",
    } <= set(categories)
    assert warnings == ()
    assert normalized["date"] is None


def test_duplicates_distinguish_exact_volume_conflict_and_adjustment() -> None:
    source = _source("one")
    first = _observation(source, "1", _row())
    exact = _observation(source, "2", _row())
    volume_conflict = _observation(source, "3", _row(volume="1001"))
    adjusted = _observation(_source("one", adjustment="adjusted"), "4", _row())
    assert duplicate_classification([first, exact])[0] == "duplicate_exact"
    assert duplicate_classification([first, volume_conflict])[0] == "same_price_different_volume"
    assert duplicate_classification([first, adjusted])[0] == "adjusted_unadjusted_duplicate"


def test_comparison_records_exact_tolerance_and_conflict_without_averaging() -> None:
    source_a, source_b = _source("a"), _source("b")
    left = _observation(source_a, "1", _row())
    exact = compare_values(left, _observation(source_b, "1", _row()), Decimal("0.001"))
    close = compare_values(
        left,
        _observation(source_b, "2", _row(close="103.05")),
        Decimal("0.001"),
    )
    conflict = compare_values(
        left,
        _observation(source_b, "3", _row(close="120", high="120")),
        Decimal("0.001"),
    )
    assert exact["exact_match"] is True
    assert close["tolerance_result"] == "within_tolerance"
    assert conflict["tolerance_result"] == "outside_tolerance"
    assert conflict["absolute_differences"]["close"] == "17"


def test_symbol_calendar_and_action_candidates_remain_reviewable() -> None:
    index = normalize_symbol("00DSEX", "fixture")
    ambiguous = normalize_symbol("ABC LTD", "fixture")
    calendar = calendar_analysis(["2024-01-01", "2024-01-12"])
    assert index.normalized_symbol == "DSEX" and index.approval_status == "under_review"
    assert ambiguous.approval_status == "under_review"
    assert calendar["authoritative"] is False
    assert calendar["long_gaps"] == [{"after": "2024-01-01", "before": "2024-01-12", "days": 11}]
    assert calendar["unexpected_weekend_rows"] == ["2024-01-12"]
    assert corporate_action_classification(Decimal("100"), Decimal("50")) == "probable_split"
    assert (
        corporate_action_classification(Decimal("100"), Decimal("70"), gap_days=15)
        == "possible_suspension_resumption"
    )
    assert (
        corporate_action_classification(
            Decimal("100"),
            Decimal("100"),
            adjusted_close=Decimal("100"),
            unadjusted_close=Decimal("110"),
        )
        == "probable_bonus_share_adjustment"
    )


def test_candidate_builder_preserves_lineage_and_excludes_conflicts(tmp_path: Path) -> None:
    builder = CanonicalCandidateBuilder(
        tmp_path / "candidate.sqlite3", tmp_path, tolerance=Decimal("0.001")
    )
    source_a, source_b = _source("a"), _source("b")
    builder.ingest_rows(source_a, [("a:1", _row()), ("a:2", _row())], FIELDS)
    builder.ingest_rows(
        source_b,
        [
            ("b:1", _row()),
            ("b:2", _row(symbol="ACI", close="210", high="210")),
        ],
        FIELDS,
    )
    builder.ingest_rows(
        source_a,
        [("a:3", _row(symbol="ACI", close="200", high="200"))],
        FIELDS,
    )
    builder.materialize_symbol_mappings()
    assert builder.analyze_duplicates()["duplicate_exact"] == 1
    assert builder.reconcile_sources()["exact_match"] == 1
    counts = builder.build_canonical_candidates()
    assert counts["accepted_candidate"] == 1
    assert counts["rejected_conflicting"] == 1
    row = builder.db.execute(
        "SELECT lineage,review_status,quality_status FROM canonical_candidates"
    ).fetchone()
    lineage = json.loads(row[0])
    assert len(lineage) == 3
    assert {item["source_row_identifier"] for item in lineage} == {"a:1", "a:2", "b:1"}
    assert row[1:] == ("pending_human_approval", "accepted_candidate")
    builder.close()


def test_quality_score_is_transparent_and_does_not_establish_truth() -> None:
    result = source_quality_score(
        schema_complete=True,
        duplicate_rate=0,
        invalid_rate=0,
        conflict_rate=0,
        date_coverage_rate=1,
        symbol_coverage_rate=None,
        adjustment_status="unknown",
        license_note="no explicit redistribution license",
        timestamp_trust="unknown",
        reproducible=True,
        agreement_rate=None,
    )
    assert sum(result["weights"].values()) == 100
    assert result["truth_established"] is False
    assert result["use"] == "review_priority_only"


def test_builder_has_no_activation_or_trading_side_effects(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(
        db,
        tmp_path / "audit",
        "Operator authorizes isolated test-chain initialization",
    )
    builder = CanonicalCandidateBuilder(
        tmp_path / "candidate.sqlite3", tmp_path, tolerance=Decimal("0.001")
    )
    builder.ingest_rows(_source("isolated"), [("1", _row())], FIELDS)
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    builder.close()

    for model in (
        GovernedDataset,
        NormalizedDailyBar,
        ValidationCampaign,
        PaperSession,
        Order,
        Transaction,
    ):
        assert db.scalar(select(func.count()).select_from(model)) == 0
    assert verify_audit_chain(db)

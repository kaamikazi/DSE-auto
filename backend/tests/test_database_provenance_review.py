from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database_identity import (
    OPERATIONAL_SQLITE_PATH,
    database_role_violation,
    resolve_database_url,
)
from app.models import NormalizedDailyBar, Order, Transaction, ValidationCampaign
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.canonical_research_candidate import CanonicalCandidateBuilder, DatasetSource
from app.services.operational_provenance_audit import (
    classify_historical_record,
    infer_database_role,
    reconcile_count_claim,
)
from app.services.report_provenance import (
    PROVENANCE_FIELDS,
    build_report_provenance,
    provenance_status,
)
from app.services.target_research_review import (
    build_target_subset,
    compare_eligible_values,
    conservative_action_label,
)

FIELDS = ["symbol", "date", "open", "high", "low", "close", "volume"]


def _source(name: str, dataset_id: str) -> DatasetSource:
    return DatasetSource(
        dataset_id=dataset_id,
        source_hash=f"hash-{dataset_id}",
        source_name=name,
        source_path=f"{dataset_id}.csv",
        adjustment_status="unadjusted",
        source_trust="third_party_research",
        timestamp_trust="unknown",
        license_note="research only",
        logical_name=name,
    )


def _row(close: str = "103") -> dict[str, str]:
    return {
        "symbol": "GP",
        "date": "2024-01-01",
        "open": "100",
        "high": "105" if Decimal(close) <= 105 else close,
        "low": "99",
        "close": close,
        "volume": "1000",
    }


def test_database_role_identification_and_cwd_independent_resolution(tmp_path: Path) -> None:
    assert infer_database_role(OPERATIONAL_SQLITE_PATH) == "operational"
    assert infer_database_role(tmp_path / "tests" / "one.db") == "test"
    assert infer_database_role(tmp_path / "recovery" / "restored.db") == "recovery"
    assert (
        infer_database_role(tmp_path / "reports" / "distributed_campaign" / "day3.dump")
        == "simulation"
    )
    assert infer_database_role(tmp_path / "pre_collection_0011.db") == "recovery"
    assert resolve_database_url("sqlite:///./data/dse_autotrader.db").endswith(
        "/backend/data/dse_autotrader.db"
    )


def test_test_or_simulation_refuses_operational_database_without_override(tmp_path: Path) -> None:
    operational_url = f"sqlite:///{OPERATIONAL_SQLITE_PATH.as_posix()}"
    assert database_role_violation(
        app_env="test",
        database_role="test",
        database_url=operational_url,
        allow_override=False,
    )
    assert (
        database_role_violation(
            app_env="development",
            database_role="simulation",
            database_url=operational_url,
            allow_override=False,
        )
        is not None
    )
    assert (
        database_role_violation(
            app_env="test",
            database_role="postgres_verification",
            database_url="postgresql+psycopg://user:redacted@localhost/dse_autotrader",
            allow_override=False,
        )
        is not None
    )
    safe = Settings(
        APP_ENV="test",
        DATABASE_ROLE="test",
        DATABASE_URL=f"sqlite:///{(tmp_path / 'isolated.db').as_posix()}",
    )
    assert safe.DATABASE_ROLE == "test"
    allowed = Settings(
        APP_ENV="test",
        DATABASE_ROLE="test",
        DATABASE_URL=operational_url,
        ALLOW_DATABASE_ROLE_OVERRIDE=True,
    )
    assert allowed.ALLOW_DATABASE_ROLE_OVERRIDE is True


def test_per_process_test_database_is_not_repository_shared() -> None:
    configured = Settings()
    assert configured.DATABASE_ROLE == "test"
    assert str(os.getpid()) in configured.DATABASE_URL
    assert "backend/tests/test.db" not in configured.DATABASE_URL.replace("\\", "/")


def test_complete_report_provenance_and_legacy_classification(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(
        db, tmp_path / "audit", "Operator approves isolated provenance test chain"
    )
    settings = Settings()
    provenance = build_report_provenance(
        db,
        database_role=settings.DATABASE_ROLE,
        environment=settings.APP_ENV,
        database_url=settings.DATABASE_URL,
        dataset_ids=["dataset-b", "dataset-a"],
    )
    assert set(PROVENANCE_FIELDS).issubset(provenance)
    assert provenance["dataset_ids"] == ["dataset-a", "dataset-b"]
    assert provenance_status({"provenance": provenance})["status"] == "verified_provenance"
    assert provenance_status({})["status"] == "legacy_unverified"
    assert verify_audit_chain(db)


def test_conflicting_count_claim_requires_database_provenance() -> None:
    result = reconcile_count_claim(
        claimed={"campaigns": 0, "orders": 0},
        observed={"campaigns": 3, "orders": 5},
        report_database_fingerprint=None,
        operational_database_fingerprint="sha256:operational",
    )
    assert result["status"] == "legacy_unverified_scope_conflict"
    assert result["differences"]["campaigns"] == {"claimed": 0, "observed": 3}


def test_historical_record_classification_uses_preserved_evidence() -> None:
    assert (
        classify_historical_record(record_type="campaign", evidence_class="synthetic")
        == "synthetic_simulation"
    )
    assert (
        classify_historical_record(
            record_type="session", session_name="imported-validation-20260712T225705"
        )
        == "imported_data_validation"
    )
    assert (
        classify_historical_record(record_type="transaction", source_record={"seed": True})
        == "synthetic_simulation"
    )
    assert classify_historical_record(record_type="order") == "unknown"


def test_adjusted_unadjusted_comparisons_are_ineligible() -> None:
    left = {
        "adjustment_status": "adjusted",
        "mapping_approval_status": "not_required_format_only",
        "open": "100",
        "high": "105",
        "low": "99",
        "close": "103",
        "volume": "1000",
    }
    right = {**left, "adjustment_status": "unadjusted"}
    decision = compare_eligible_values(left, right)
    assert decision.eligible is False
    assert decision.classification == "adjusted_unadjusted_ineligible"


def test_weak_corporate_action_signal_is_not_promoted_to_probable_event() -> None:
    assert (
        conservative_action_label(
            official_evidence=False,
            adjustment_factor_discontinuity=False,
            gap_days=1,
            source_scale_mismatch=False,
            mapping_uncertain=False,
        )
        == "insufficient_evidence"
    )
    assert (
        conservative_action_label(
            official_evidence=False,
            adjustment_factor_discontinuity=True,
            gap_days=1,
            source_scale_mismatch=False,
            mapping_uncertain=False,
        )
        == "adjustment_divergence"
    )


def test_target_policy_preserves_lineage_and_holds_conflict(tmp_path: Path) -> None:
    candidate_db = tmp_path / "candidate.db"
    builder = CanonicalCandidateBuilder(candidate_db, tmp_path, tolerance=Decimal("0.001"))
    primary_name = "End-of-Day Financial Dataset with Coverage Metadata / unadjusted"
    secondary_name = "Dhaka Stock Exchange DSE 2021 yearly CSV"
    builder.ingest_rows(_source(primary_name, "primary"), [("primary:1", _row())], FIELDS)
    builder.ingest_rows(_source(secondary_name, "secondary"), [("secondary:1", _row())], FIELDS)
    builder.ingest_rows(
        _source("conflict source", "conflict"),
        [("conflict:1", _row(close="120"))],
        FIELDS,
    )
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    result = build_target_subset(
        builder.db,
        source_scores={primary_name: 75.0, secondary_name: 65.0, "conflict source": 60.0},
        source_urls={
            "hash-primary": "https://example.invalid/primary",
            "hash-secondary": "https://example.invalid/secondary",
        },
    )
    assert result["candidate_rows"] == []
    assert result["held_counts"] == {"unresolved_eligible_source_conflict": 1}
    builder.close()


def test_target_policy_selects_complete_two_source_lineage(tmp_path: Path) -> None:
    candidate_db = tmp_path / "candidate.db"
    builder = CanonicalCandidateBuilder(candidate_db, tmp_path, tolerance=Decimal("0.001"))
    primary_name = "End-of-Day Financial Dataset with Coverage Metadata / unadjusted"
    secondary_name = "Dhaka Stock Exchange DSE 2021 yearly CSV"
    builder.ingest_rows(_source(primary_name, "primary"), [("primary:1", _row())], FIELDS)
    builder.ingest_rows(_source(secondary_name, "secondary"), [("secondary:1", _row())], FIELDS)
    builder.materialize_symbol_mappings()
    builder.analyze_duplicates()
    builder.reconcile_sources()
    builder.detect_corporate_actions()
    builder.build_canonical_candidates()
    result = build_target_subset(
        builder.db,
        source_scores={primary_name: 75.0, secondary_name: 65.0},
        source_urls={
            "hash-primary": "https://example.invalid/primary",
            "hash-secondary": "https://example.invalid/secondary",
        },
    )
    assert len(result["candidate_rows"]) == 1
    row = result["candidate_rows"][0]
    assert row["active"] is False
    assert row["review_status"] == "pending_human_approval"
    assert len(row["lineage"]) == 2
    assert {item["source_dataset_id"] for item in row["lineage"]} == {
        "primary",
        "secondary",
    }
    builder.close()


def test_provenance_work_has_no_activation_or_trading_side_effects(
    db: Session, tmp_path: Path
) -> None:
    initialize_canonical_chain(
        db, tmp_path / "audit", "Operator approves isolated side-effect test chain"
    )
    settings = Settings()
    before = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
    build_report_provenance(
        db,
        database_role=settings.DATABASE_ROLE,
        environment=settings.APP_ENV,
        database_url=settings.DATABASE_URL,
    )
    after = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (NormalizedDailyBar, ValidationCampaign, Order, Transaction)
    }
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

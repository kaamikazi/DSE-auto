from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    GovernanceItemApproval,
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    Transaction,
    ValidationCampaign,
)
from app.services import pilot_research_extension
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.pilot_research_extension import (
    ACTIVE_STATUS,
    ALLOWED_DISPOSITION,
    BLOCKED_SYMBOLS,
    FROZEN_CANDIDATE_REVIEW_SHA256,
    INDEPENDENT_CONFLICTS,
    INDEPENDENT_EXPECTED_ADJUSTED_COUNTS,
    INDEPENDENT_EXPECTED_EXCLUSIONS,
    INDEPENDENT_EXPECTED_TOTAL_COUNTS,
    INDEPENDENT_EXTENSION_SYMBOLS,
    INDEPENDENT_RECONCILED_ROWS,
    OBSERVED_WINDOW,
    build_extension_rows,
    canonical_hash,
    create_independent_extension_dataset_record,
    decision_specs,
    independent_extension_decision_specs,
    record_decision,
    verify_independent_execution_grain,
    write_jsonl,
)


def _disposition(
    symbol: str,
    day: str,
    status: str,
    *,
    suffix: str = "1",
    adjustment: str = "adjusted",
) -> dict[str, object]:
    row_id = f"{symbol}:{day}:{adjustment}:{suffix}"
    return {
        "logical_row_id": canonical_hash(row_id),
        "symbol": symbol,
        "date": day,
        "adjustment_status": adjustment,
        "status": status,
        "final_disposition": status,
        "diagnostic_reason_codes": [],
        "source_row_ids": [row_id],
        "source_hashes": ["a" * 64],
        "source_names": [
            "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / adjusted"
        ],
        "active": False,
    }


def _observation(disposition: dict[str, object]) -> dict[str, object]:
    row_id = str(disposition["source_row_ids"][0])  # type: ignore[index]
    return {
        "source_dataset_id": "dataset-mendeley",
        "source_hash": "a" * 64,
        "source_name": disposition["source_names"][0],  # type: ignore[index]
        "source_row_id": row_id,
        "normalized_symbol": disposition["symbol"],
        "trading_date": disposition["date"],
        "open": "10",
        "high": "12",
        "low": "9",
        "close": "11",
        "volume": "100",
        "adjustment_status": disposition["adjustment_status"],
        "accepted_for_candidate": 1,
        "mapping_confidence": "high",
        "mapping_approval_status": "not_required_format_only",
    }


def _fixture() -> tuple[
    list[dict[str, object]], dict[str, dict[str, object]], dict[str, dict[str, int]]
]:
    rows = [
        _disposition("BATBC", "2024-01-01", ALLOWED_DISPOSITION),
        _disposition("BATBC", "2024-01-02", "tier_3_research_only"),
        _disposition("BATBC", "2024-01-03", "rejected_invalid"),
        _disposition("BATBC", "2024-01-04", "rejected_duplicate_conflict"),
        _disposition("SQURPHARMA", "2024-01-01", ALLOWED_DISPOSITION),
        _disposition("SQURPHARMA", "2024-01-02", "tier_3_research_only"),
        _disposition("SQURPHARMA", "2024-01-03", "held_lifecycle"),
        _disposition("SQURPHARMA", "2024-01-04", "rejected_invalid"),
        _disposition("SQURPHARMA", "2024-01-05", "rejected_duplicate_conflict"),
        _disposition("IDLC", "2024-01-01", ALLOWED_DISPOSITION),
    ]
    observations = {
        str(row["source_row_ids"][0]): _observation(row)  # type: ignore[index]
        for row in rows
        if row["symbol"] in {"BATBC", "SQURPHARMA"}
        and row["final_disposition"] == ALLOWED_DISPOSITION
    }
    exclusions = {
        "BATBC": {
            "tier_3_research_only": 1,
            "rejected_invalid": 1,
            "rejected_duplicate_conflict": 1,
        },
        "SQURPHARMA": {
            "tier_3_research_only": 1,
            "held_lifecycle": 1,
            "rejected_invalid": 1,
            "rejected_duplicate_conflict": 1,
        },
    }
    return rows, observations, exclusions


def _build() -> tuple[list[dict[str, object]], dict[str, object]]:
    rows, observations, exclusions = _fixture()
    return build_extension_rows(
        rows,  # type: ignore[arg-type]
        observations,  # type: ignore[arg-type]
        activation_timestamp="2026-07-29T00:00:00+00:00",
        human_decision_ids={"BATBC": "decision-b", "SQURPHARMA": "decision-s"},
        audit_event_ids={"BATBC": "audit-b", "SQURPHARMA": "audit-s"},
        expected_active_counts={"BATBC": 1, "SQURPHARMA": 1},
        expected_exclusions=exclusions,
        expected_reconciled_rows=len(rows),
    )


def test_batbc_and_squrpharma_activate_t2_only() -> None:
    active, summary = _build()
    assert [row["symbol"] for row in active] == ["BATBC", "SQURPHARMA"]
    assert all(row["final_disposition"] == ALLOWED_DISPOSITION for row in active)
    assert summary["active_by_symbol"] == {"BATBC": 1, "SQURPHARMA": 1}
    assert all(row["selected_source"].startswith("Dhaka Stock Exchange") for row in active)
    assert all(row["source_independence_claimed"] is False for row in active)


def test_t3_lifecycle_invalid_duplicate_and_blocked_symbols_are_excluded() -> None:
    active, summary = _build()
    assert not ({row["symbol"] for row in active} & set(BLOCKED_SYMBOLS))
    excluded = summary["excluded_by_symbol_and_disposition"]
    assert excluded["BATBC"]["tier_3_research_only"] == 1
    assert excluded["SQURPHARMA"]["held_lifecycle"] == 1
    assert excluded["SQURPHARMA"]["rejected_invalid"] == 1
    assert excluded["SQURPHARMA"]["rejected_duplicate_conflict"] == 1


def test_observed_window_is_enforced_without_listing_date_claim() -> None:
    rows, observations, exclusions = _fixture()
    rows[0]["date"] = "2012-09-30"
    source_id = str(rows[0]["source_row_ids"][0])  # type: ignore[index]
    observations[source_id]["trading_date"] = "2012-09-30"
    with pytest.raises(ValueError, match="outside approved observed window"):
        build_extension_rows(
            rows,  # type: ignore[arg-type]
            observations,  # type: ignore[arg-type]
            activation_timestamp="test",
            human_decision_ids={},
            audit_event_ids={},
            expected_active_counts={"BATBC": 1, "SQURPHARMA": 1},
            expected_exclusions=exclusions,
            expected_reconciled_rows=len(rows),
        )
    active, _ = _build()
    assert OBSERVED_WINDOW == {"start": "2012-10-01", "end": "2026-01-22"}
    assert all(
        row["observed_research_window"]["official_listing_date_claim"] is False for row in active
    )


def test_lineage_completeness_and_ohlc_invariants_fail_closed() -> None:
    rows, observations, exclusions = _fixture()
    source_id = str(rows[0]["source_row_ids"][0])  # type: ignore[index]
    observations[source_id]["source_hash"] = ""
    with pytest.raises(ValueError, match="Incomplete lineage"):
        build_extension_rows(
            rows,  # type: ignore[arg-type]
            observations,  # type: ignore[arg-type]
            activation_timestamp="test",
            human_decision_ids={},
            audit_event_ids={},
            expected_active_counts={"BATBC": 1, "SQURPHARMA": 1},
            expected_exclusions=exclusions,
            expected_reconciled_rows=len(rows),
        )


def test_existing_builder_supports_a_strict_custom_symbol_scope() -> None:
    symbols = ("IDLC", "LANKABAFIN", "POWERGRID", "RENATA", "BERGERPBL")
    rows: list[dict[str, object]] = []
    observations: dict[str, dict[str, object]] = {}
    exclusions: dict[str, dict[str, int]] = {}
    for symbol in symbols:
        active = _disposition(symbol, "2024-01-01", ALLOWED_DISPOSITION)
        held = _disposition(symbol, "2024-01-02", "held_genuine_conflict")
        rows.extend((active, held))
        observations[str(active["source_row_ids"][0])] = _observation(active)  # type: ignore[index]
        exclusions[symbol] = {"held_genuine_conflict": 1}

    active, summary = build_extension_rows(
        rows,  # type: ignore[arg-type]
        observations,  # type: ignore[arg-type]
        activation_timestamp="2026-08-05T00:00:00+00:00",
        human_decision_ids={"scope": "decision"},
        audit_event_ids={"scope": "audit"},
        expected_active_counts={symbol: 1 for symbol in symbols},
        expected_exclusions=exclusions,
        expected_reconciled_rows=len(rows),
        target_symbols=symbols,
        blocked_symbols=(),
        observed_windows={symbol: dict(OBSERVED_WINDOW) for symbol in symbols},
        transformation_version="five-symbol-t2-test-v1",
    )

    assert [row["symbol"] for row in active] == sorted(symbols)
    assert all(row["transformation_version"] == "five-symbol-t2-test-v1" for row in active)
    assert summary["active_rows"] == 5
    assert all(
        values == {"held_genuine_conflict": 1}
        for values in summary["excluded_by_symbol_and_disposition"].values()
    )


def test_custom_scope_rejects_incomplete_windows() -> None:
    rows, observations, exclusions = _fixture()
    with pytest.raises(ValueError, match="Observed windows must cover exactly"):
        build_extension_rows(
            rows,  # type: ignore[arg-type]
            observations,  # type: ignore[arg-type]
            activation_timestamp="test",
            human_decision_ids={},
            audit_event_ids={},
            expected_active_counts={"BATBC": 1, "SQURPHARMA": 1},
            expected_exclusions=exclusions,
            expected_reconciled_rows=len(rows),
            observed_windows={"BATBC": dict(OBSERVED_WINDOW)},
        )


def test_dataset_jsonl_hash_integrity(tmp_path: Path) -> None:
    rows, _ = _build()
    path = tmp_path / "extension.jsonl"
    digest = write_jsonl(path, rows)  # type: ignore[arg-type]
    assert len(digest) == 64
    assert canonical_hash(json.loads(path.read_text().splitlines()[0]))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_decisions_are_separate_and_strategy_remains_prohibited() -> None:
    specs = decision_specs("b" * 64, version="extension-v1", dataset_hash="c" * 64)
    assert len(specs) == 11
    assert len({spec["event"] for spec in specs}) == 11
    assert {spec["key"] for spec in specs} >= {
        "BATBC.t2_activation",
        "BATBC.t3_rejection",
        "SQURPHARMA.t2_activation",
        "SQURPHARMA.t3_rejection",
        "lifecycle_pending",
        "invalid_duplicate_exclusions",
        "dataset_activation",
        "strategy_execution",
    }
    lifecycle = next(spec for spec in specs if spec["key"] == "lifecycle_pending")
    assert lifecycle["value"]["official_listing_date_claim"] is False
    assert specs[-1]["status"] == "prohibited"
    assert specs[-1]["value"]["execution"] is False


def test_decision_records_have_independent_audit_events(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Pilot extension decision test")
    specs = decision_specs("d" * 64, version="extension-v1", dataset_hash="e" * 64)
    event_ids = []
    for spec in specs:
        approval = record_decision(
            db,
            spec=spec,
            draft_version="pilot-extension-test-v1",
            operator_identity="test-operator",
        )
        event_ids.append(approval.audit_event_id)
    assert len(event_ids) == len(set(event_ids)) == 11
    assert db.scalar(select(func.count()).select_from(GovernanceItemApproval)) == 11
    assert verify_audit_chain(db)


def test_decision_can_bind_to_a_specific_review_artifact(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Scoped extension decision test")
    review_hash = "f" * 64
    approval = record_decision(
        db,
        spec={
            "key": "IDLC.conflict.2021-04-26",
            "status": "held_genuine_conflict",
            "event": "research_subset.conflicts_held",
            "value": {"symbol": "IDLC", "date": "2021-04-26", "active": False},
        },
        draft_version="five-symbol-t2-test-v1",
        operator_identity="test-operator",
        approval_type="five_symbol_t2_extension",
        evidence_ids=(review_hash,),
    )

    assert approval.approval_type == "five_symbol_t2_extension"
    assert approval.evidence_ids == [review_hash]
    assert verify_audit_chain(db)


def test_service_has_no_strategy_or_trading_execution_path(db: Session) -> None:
    protected = (ValidationCampaign, PaperSession, Signal, Order, Transaction)
    before = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    source = inspect.getsource(pilot_research_extension)
    for forbidden in (
        "run_ma_crossover_research(",
        "run_backtest(",
        "create_campaign(",
        "create_order(",
        "create_fill(",
    ):
        assert forbidden not in source
    after = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before == after
    assert ACTIVE_STATUS == "RESEARCH DATASET ACTIVE"


def test_independent_extension_frozen_counts_reconcile_exactly() -> None:
    assert len(INDEPENDENT_EXTENSION_SYMBOLS) == 15
    assert set(INDEPENDENT_EXPECTED_ADJUSTED_COUNTS) == set(INDEPENDENT_EXTENSION_SYMBOLS)
    assert set(INDEPENDENT_EXPECTED_TOTAL_COUNTS) == set(INDEPENDENT_EXTENSION_SYMBOLS)
    assert set(INDEPENDENT_EXPECTED_EXCLUSIONS) == set(INDEPENDENT_EXTENSION_SYMBOLS)
    assert sum(INDEPENDENT_EXPECTED_ADJUSTED_COUNTS.values()) == 46896
    assert sum(INDEPENDENT_EXPECTED_TOTAL_COUNTS.values()) == 93774
    assert (
        sum(
            INDEPENDENT_EXPECTED_TOTAL_COUNTS[symbol]
            + sum(INDEPENDENT_EXPECTED_EXCLUSIONS[symbol].values())
            for symbol in INDEPENDENT_EXTENSION_SYMBOLS
        )
        == INDEPENDENT_RECONCILED_ROWS
    )
    assert len(INDEPENDENT_CONFLICTS) == 11
    assert len({symbol for symbol, _, _ in INDEPENDENT_CONFLICTS}) == 10
    assert len(FROZEN_CANDIDATE_REVIEW_SHA256) == 64


def test_independent_execution_grain_keeps_unadjusted_rows_out_of_execution() -> None:
    rows = [
        {
            "symbol": symbol,
            "date": "2024-01-01",
            "adjustment_status": grain,
            "final_disposition": ALLOWED_DISPOSITION,
        }
        for symbol in INDEPENDENT_EXTENSION_SYMBOLS
        for grain in ("adjusted", "unadjusted")
    ]
    expected_adjusted = {symbol: 1 for symbol in INDEPENDENT_EXTENSION_SYMBOLS}
    expected_total = {symbol: 2 for symbol in INDEPENDENT_EXTENSION_SYMBOLS}

    result = verify_independent_execution_grain(
        rows,
        expected_adjusted_counts=expected_adjusted,
        expected_total_counts=expected_total,
    )

    assert result["approved_dataset_rows"] == 30
    assert result["adjusted_execution_rows"] == 15
    assert result["unadjusted_non_execution_research_rows"] == 15
    assert result["execution_grain"] == "adjusted"
    assert result["unadjusted_execution_eligible"] is False


def test_independent_execution_grain_rejects_duplicates_and_sector_fields() -> None:
    rows = [
        {
            "symbol": "HEIDELBCEM",
            "date": "2024-01-01",
            "adjustment_status": "adjusted",
            "final_disposition": ALLOWED_DISPOSITION,
        }
    ]
    expected = {"HEIDELBCEM": 1}
    with pytest.raises(ValueError, match="Duplicate symbol/date/adjustment"):
        verify_independent_execution_grain(
            [*rows, *rows],
            expected_adjusted_counts={"HEIDELBCEM": 2},
            expected_total_counts={"HEIDELBCEM": 2},
        )
    rows[0]["sector"] = "cement"
    with pytest.raises(ValueError, match="sector metadata"):
        verify_independent_execution_grain(
            rows,
            expected_adjusted_counts=expected,
            expected_total_counts=expected,
        )


def test_independent_decisions_reuse_audit_types_and_prohibit_execution() -> None:
    specs = independent_extension_decision_specs(
        "a" * 64,
        version="fifteen-symbol-extension-test",
        dataset_hash="b" * 64,
    )
    assert {spec["key"] for spec in specs} == {
        "conflicts_held",
        "lifecycle_pending",
        "sector_evidence_pending",
        "row_exclusions",
        "dataset_activation",
        "strategy_execution_prohibited",
    }
    assert {spec["event"] for spec in specs} <= {
        "research_subset.conflicts_held",
        "research_extension.lifecycle_pending",
        "research_extension.invalid_duplicate_excluded",
        "research_extension.dataset_activated",
        "research_extension.strategy_execution_prohibited",
    }
    conflict = next(spec for spec in specs if spec["key"] == "conflicts_held")
    assert conflict["value"]["logical_observation_count"] == 11
    assert conflict["value"]["select_neither_source"] is True
    assert conflict["value"]["averaged"] is False
    assert specs[-1]["status"] == "prohibited"
    assert specs[-1]["value"]["execution"] is False


def test_independent_dataset_registration_preserves_existing_identities(
    db: Session, tmp_path: Path
) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Independent extension test")
    existing_symbols = (
        ("GP", "ACI", "BRACBANK"),
        ("BATBC", "SQURPHARMA"),
        ("IDLC", "LANKABAFIN", "POWERGRID", "RENATA", "BERGERPBL"),
    )
    preserved = []
    for index, symbols in enumerate(existing_symbols):
        dataset = ResearchDataset(
            name=f"preserved-{index}",
            symbols=list(symbols),
            data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
            source_evidence_ids=[],
            source_hash=f"{index + 1:064x}",
            dataset_hash=f"{index + 11:064x}",
            timestamp_trust="unknown",
            raw_file_path="raw",
            normalized_file_path="normalized",
            quality_report={},
            status="research_dataset_active",
        )
        db.add(dataset)
        preserved.append(dataset)
    approval = GovernanceItemApproval(
        approval_type="independent_universe_extension",
        draft_version="independent-test",
        item_key="dataset_activation",
        proposed_value={},
        approval_status="authorized_research_only",
        decision_hash="f" * 64,
        audit_event_id="audit-activation",
    )
    db.add(approval)
    db.commit()
    path = tmp_path / "independent.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    dataset_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    summary = {
        "active_by_symbol": dict(INDEPENDENT_EXPECTED_TOTAL_COUNTS),
        "adjusted_execution_by_symbol": dict(INDEPENDENT_EXPECTED_ADJUSTED_COUNTS),
        "active_rows": sum(INDEPENDENT_EXPECTED_TOTAL_COUNTS.values()),
        "observed_windows": {
            symbol: dict(OBSERVED_WINDOW) for symbol in INDEPENDENT_EXTENSION_SYMBOLS
        },
    }

    created = create_independent_extension_dataset_record(
        db,
        version="independent-fifteen-test",
        dataset_path=path,
        dataset_hash=dataset_hash,
        source_bundle_hash="e" * 64,
        source_file_hashes=("d" * 64,),
        candidate_db_path=tmp_path / "candidate.sqlite3",
        candidate_db_hash="c" * 64,
        frozen_review_sha256=FROZEN_CANDIDATE_REVIEW_SHA256,
        authorization_sha256="b" * 64,
        summary=summary,
        decisions={"dataset_activation": approval},
        preserved_datasets=preserved,
        git_head="a" * 40,
        audit_chain_id="test-chain",
        operator_identity="test-operator",
    )

    assert created.status == "research_dataset_active"
    assert created.symbols == list(INDEPENDENT_EXTENSION_SYMBOLS)
    assert created.quality_report["combined_active_universe_count"] == 25
    assert created.quality_report["strategy_approved"] is False
    assert [item.dataset_hash for item in preserved] == [f"{index + 11:064x}" for index in range(3)]

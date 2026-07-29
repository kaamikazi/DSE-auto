from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path

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
from app.services import pilot_conflict_methodology
from app.services.audit import verify_audit_chain
from app.services.pilot_conflict_methodology import (
    FINAL_DISPOSITIONS,
    PILOT_SYMBOLS,
    TIER_3_REASON_CODES,
    build_pilot_methodology_audit,
    classify_corporate_action_candidate,
    classify_existing_conflict,
    classify_final_disposition,
    collapse_duplicate_group,
    comparison_eligibility,
)


def _observation(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 1,
        "source_dataset_id": "source-a",
        "source_hash": "hash-a",
        "source_name": "source-a",
        "source_row_id": "a:1",
        "normalized_symbol": "IDLC",
        "trading_date": "2021-01-03",
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10",
        "volume": "100",
        "adjustment_status": "unadjusted",
        "accepted_for_candidate": 1,
        "value_fingerprint": "fingerprint-a",
        "mapping_confidence": "high",
        "mapping_approval_status": "not_required_format_only",
    }
    row.update(overrides)
    return row


def test_exact_pilot_scope() -> None:
    assert PILOT_SYMBOLS == ("IDLC", "LANKABAFIN", "BATBC", "SQURPHARMA", "POWERGRID")


def test_adjusted_unadjusted_comparison_is_excluded() -> None:
    result = comparison_eligibility(
        _observation(adjustment_status="adjusted"),
        _observation(source_dataset_id="source-b", source_hash="hash-b"),
    )
    assert result["eligible"] is False
    assert "adjustment_grain_mismatch" in result["reason_codes"]


def test_unknown_and_known_grains_are_excluded() -> None:
    result = comparison_eligibility(
        _observation(adjustment_status="unknown"),
        _observation(source_dataset_id="source-b", source_hash="hash-b"),
    )
    assert result["eligible"] is False
    assert "unknown_adjustment_grain" in result["reason_codes"]


def test_same_source_and_duplicate_logical_dataset_are_excluded() -> None:
    same = comparison_eligibility(_observation(), _observation(source_row_id="a:2"))
    assert same["eligible"] is False
    assert {"same_source_dataset", "duplicate_logical_dataset"} <= set(same["reason_codes"])


def test_unit_semantics_exclude_volume_but_allow_same_grain_ohlc() -> None:
    result = comparison_eligibility(
        _observation(),
        _observation(source_dataset_id="source-b", source_hash="hash-b"),
    )
    assert result["eligible"] is True
    assert result["eligible_fields"] == ["open", "high", "low", "close"]
    assert result["excluded_fields"] == {"volume": "volume_unit_not_registered"}


def test_exact_duplicates_collapse_with_complete_lineage() -> None:
    result = collapse_duplicate_group([_observation(), _observation(id=2, source_row_id="a:2")])
    assert result["duplicate_type"] == "exact_duplicate"
    assert result["collapsed_row_count"] == 1
    assert result["source_row_ids"] == ["a:1", "a:2"]
    assert result["selected_representative_row"] == "a:1"


def test_conflicting_same_source_duplicates_remain_separate() -> None:
    result = collapse_duplicate_group(
        [
            _observation(),
            _observation(id=2, source_row_id="a:2", value_fingerprint="different"),
        ]
    )
    assert result["duplicate_type"] == "conflicting_same_source_duplicate"
    assert result["collapsed_row_count"] == 0
    assert len(result["representative_row_ids"]) == 2


def test_genuine_same_grain_price_disagreement_is_preserved() -> None:
    row = {
        "adjustment_a": "unadjusted",
        "adjustment_b": "unadjusted",
        "source_a": "a",
        "source_b": "b",
        "percentage_difference": {
            "open": "0",
            "high": "0",
            "low": "0.02",
            "close": "0",
            "volume": "0.5",
        },
    }
    result = classify_existing_conflict(row)
    assert result["primary_root_cause"] == "genuine_same_grain_source_disagreement"
    assert result["corrected_disposition"] == "genuine_conflict"


def test_volume_only_disagreement_is_not_a_price_conflict() -> None:
    row = {
        "adjustment_a": "unadjusted",
        "adjustment_b": "unadjusted",
        "source_a": "a",
        "source_b": "b",
        "percentage_difference": {
            "open": "0",
            "high": "0",
            "low": "0",
            "close": "0",
            "volume": "0.5",
        },
    }
    assert classify_existing_conflict(row)["primary_root_cause"] == "unverified_volume_unit_only"


def test_large_move_alone_is_not_a_supported_corporate_action() -> None:
    cause, status = classify_corporate_action_candidate(
        gap_days=1,
        adjustment_factor_changed=False,
        conflicting_duplicate=False,
        registered_evidence=False,
    )
    assert cause == "ordinary_price_movement"
    assert status == "insufficient_evidence"


def test_lifecycle_and_adjustment_signals_remain_conservative() -> None:
    assert (
        classify_corporate_action_candidate(
            gap_days=11,
            adjustment_factor_changed=False,
            conflicting_duplicate=False,
            registered_evidence=False,
        )[1]
        == "suspension_candidate"
    )
    assert (
        classify_corporate_action_candidate(
            gap_days=1,
            adjustment_factor_changed=True,
            conflicting_duplicate=False,
            registered_evidence=False,
        )[1]
        == "adjustment_divergence"
    )


def _create_pilot_database(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE observations (
          id INTEGER, source_dataset_id TEXT, source_hash TEXT, source_name TEXT,
          source_row_id TEXT, normalized_symbol TEXT, trading_date TEXT,
          open TEXT, high TEXT, low TEXT, close TEXT, volume TEXT,
          adjustment_status TEXT, accepted_for_candidate INTEGER,
          value_fingerprint TEXT, mapping_confidence TEXT, mapping_approval_status TEXT
        );
        CREATE TABLE corporate_action_candidates (
          normalized_symbol TEXT, trading_date TEXT, source_dataset_id TEXT,
          candidate_type TEXT, previous_close TEXT, current_close TEXT,
          adjusted_close TEXT, unadjusted_close TEXT, volume_change TEXT,
          evidence TEXT, review_status TEXT
        );
        """
    )
    fields = list(_observation())
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(PILOT_SYMBOLS, start=1):
        base = _observation(
            id=index * 10,
            normalized_symbol=symbol,
            source_row_id=f"a:{symbol}:1",
            value_fingerprint=f"same:{symbol}",
        )
        rows.append(base)
        rows.append(
            _observation(
                **{
                    **base,
                    "id": index * 10 + 1,
                    "source_dataset_id": "source-b",
                    "source_hash": "hash-b",
                    "source_name": "source-b",
                    "source_row_id": f"b:{symbol}:1",
                }
            )
        )
    rows.append(
        _observation(
            id=100,
            source_row_id="a:IDLC:duplicate",
            value_fingerprint="same:IDLC",
        )
    )
    rows.append(
        _observation(
            id=101,
            source_row_id="invalid:IDLC",
            accepted_for_candidate=0,
            value_fingerprint="invalid",
        )
    )
    placeholders = ",".join("?" for _ in fields)
    db.executemany(
        f"INSERT INTO observations ({','.join(fields)}) VALUES ({placeholders})",  # noqa: S608
        [[row[field] for field in fields] for row in rows],
    )
    db.commit()
    db.close()


def test_candidate_rebuild_and_review_queue_are_inactive_and_manageable(tmp_path: Path) -> None:
    database = tmp_path / "pilot.sqlite3"
    conflict = tmp_path / "conflicts.json"
    quality = tmp_path / "quality.json"
    _create_pilot_database(database)
    conflict.write_text("[]", encoding="utf-8")
    quality.write_text(json.dumps([{"logical_name": "source-a", "score": 80}]), encoding="utf-8")
    result = build_pilot_methodology_audit(database, conflict, quality)
    assert result["totals"]["genuine_conflicts"] == 0
    assert result["totals"]["review_queue_manageable"] is True
    assert result["totals"]["human_review_queue"] == 5
    assert result["totals"]["exact_duplicates_collapsed"] == 1
    assert result["totals"]["reconciliation_equation"]["balanced"] is True
    assert result["totals"]["raw_source_rows"] != result["totals"]["comparison_pairs"]
    assert all(
        sum(row["final_disposition_counts"].values()) == row["logical_rows"]
        for row in result["symbol_summary"]
    )
    assert all(row["final_disposition"] in FINAL_DISPOSITIONS for row in result["candidates"])
    assert all(
        row["diagnostic_reason_codes"]
        for row in result["candidates"]
        if row["final_disposition"] == "tier_3_research_only"
    )
    assert all(row["active"] is False for row in result["candidates"])
    assert all(
        row["lifecycle_status"] == "lifecycle_evidence_pending"
        for row in result["lifecycle_evidence"]
    )


def test_service_has_no_activation_or_strategy_execution_path() -> None:
    source = inspect.getsource(pilot_conflict_methodology)
    for forbidden in (
        "activate_dataset(",
        "run_backtest(",
        "create_campaign(",
        "create_order(",
        "create_fill(",
    ):
        assert forbidden not in source


def test_methodology_does_not_mutate_operational_models(db) -> None:  # type: ignore[no-untyped-def]
    db.add(
        AuditChain(
            status="active",
            genesis_reason="test",
            operator_acknowledgement="test",
            legacy_archive_path="test.json",
            legacy_archive_hash="0" * 64,
        )
    )
    db.commit()
    protected = (ResearchDataset, ValidationCampaign, PaperSession, Signal, Order, Transaction)
    before = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    comparison_eligibility(
        _observation(),
        _observation(source_dataset_id="source-b", source_hash="hash-b"),
    )
    after = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before == after
    assert verify_audit_chain(db)


def test_duplicate_group_id_is_deterministic() -> None:
    result = collapse_duplicate_group([_observation()])
    expected = hashlib.sha256(
        json.dumps(
            {
                "adjustment_status": "unadjusted",
                "date": "2021-01-03",
                "source_dataset_id": "source-a",
                "symbol": "IDLC",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert result["duplicate_group_id"] == expected


def _disposition(**overrides: object) -> tuple[str, list[str]]:
    values: dict[str, object] = {
        "accepted": True,
        "complete_lineage": True,
        "valid_ohlc": True,
        "known_adjustment": True,
        "high_confidence_mapping": True,
        "independent_agreement": False,
        "high_quality_source": False,
        "unresolved_conflict": False,
        "lifecycle_hold": False,
        "corporate_action_hold": False,
        "duplicate_conflict": False,
        "tier_3_reasons": ("provenance_weaker",),
    }
    values.update(overrides)
    return classify_final_disposition(**values)  # type: ignore[arg-type]


def test_machine_testable_tier_one_and_tier_two_rules() -> None:
    assert _disposition(independent_agreement=True)[0] == "tier_1_cross_source_confirmed"
    assert _disposition(high_quality_source=True)[0] == "tier_2_single_source_high_quality"
    assert _disposition(known_adjustment=False)[0] == "tier_3_research_only"


def test_tier_three_reason_is_required_and_activation_is_rejected(tmp_path: Path) -> None:
    assert _disposition(tier_3_reasons=())[1] == ["other"]
    assert set(TIER_3_REASON_CODES) >= {"other", "adjustment_documentation_incomplete"}
    database = tmp_path / "pilot.sqlite3"
    conflict = tmp_path / "conflicts.json"
    quality = tmp_path / "quality.json"
    _create_pilot_database(database)
    conflict.write_text("[]", encoding="utf-8")
    quality.write_text(json.dumps([{"logical_name": "source-a", "score": 80}]), encoding="utf-8")
    result = build_pilot_methodology_audit(database, conflict, quality)
    policy = result["proposed_activation_policy"]
    assert policy["status"] == "REJECTED / NOT GRANTED"
    assert policy["active"] is False
    assert "tier_3_research_only" in policy["ineligible_by_default"]


def test_holds_and_rejections_are_mutually_exclusive() -> None:
    cases = {
        "held_genuine_conflict": {"unresolved_conflict": True},
        "rejected_duplicate_conflict": {"duplicate_conflict": True},
        "held_lifecycle": {"lifecycle_hold": True},
        "held_corporate_action": {"corporate_action_hold": True},
        "held_mapping": {"high_confidence_mapping": False},
        "rejected_invalid": {"accepted": False},
        "rejected_other": {"complete_lineage": False},
    }
    for expected, overrides in cases.items():
        assert _disposition(**overrides)[0] == expected


def test_nine_independent_approval_records_and_priority_readiness(tmp_path: Path) -> None:
    database = tmp_path / "pilot.sqlite3"
    conflict = tmp_path / "conflicts.json"
    quality = tmp_path / "quality.json"
    _create_pilot_database(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE observations SET close='11', value_fingerprint='changed' "
            "WHERE source_dataset_id='source-b' AND normalized_symbol IN "
            "('IDLC','LANKABAFIN','BATBC','POWERGRID')"
        )
        db.commit()
    conflict.write_text("[]", encoding="utf-8")
    quality.write_text(
        json.dumps(
            [
                {"logical_name": "source-a", "score": 80},
                {"logical_name": "source-b", "score": 80},
            ]
        ),
        encoding="utf-8",
    )
    result = build_pilot_methodology_audit(database, conflict, quality)
    approvals = result["human_review_queue"]
    assert len(approvals) == 9
    assert len({row["approval_record_id"] for row in approvals}) == 9
    assert len(result["conflict_approval_records"]) == 4
    assert len(result["lifecycle_approval_records"]) == 5
    assert all(row["reviewer_decision"] == "" for row in approvals)
    assert all(row["operator_decision"] == "" for row in approvals)
    priority = result["symbol_readiness"][:2]
    assert [row["symbol"] for row in priority] == ["BATBC", "SQURPHARMA"]
    assert all(row["status"] == "human_decision_required" for row in priority)

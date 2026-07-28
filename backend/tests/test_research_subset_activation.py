from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import GovernanceItemApproval
from app.services.audit import initialize_canonical_chain, verify_audit_chain
from app.services.research_subset_activation import (
    ACTIVE_STATUS,
    ACTIVE_SYMBOLS,
    build_active_rows,
    decision_specs,
    record_decision,
    write_jsonl,
)


def _candidate(
    symbol: str, date: str, tier: str = "tier_2_single_high_quality_source"
) -> dict[str, object]:
    source = "coverage-metadata adjusted"
    return {
        "symbol": symbol,
        "trading_date": date,
        "open": "10",
        "high": "12",
        "low": "9",
        "close": "11",
        "volume": "100",
        "adjustment_status": "adjusted",
        "selected_source": source,
        "quality_status": tier,
        "lineage": [
            {
                "source_name": source,
                "source_dataset_id": "dataset-1",
                "source_file_hash": "a" * 64,
                "source_row_identifier": f"{symbol}:{date}",
                "source_url": "https://example.test/source",
                "original_raw_values": {"symbol": symbol, "date": date},
                "transformation_version": "canonical-v1",
                "transformation_reason": "identity",
            }
        ],
    }


def _ledger(candidate: dict[str, object], status: str) -> dict[str, object]:
    return {
        "population": "canonical_candidate",
        "symbol": candidate["symbol"],
        "date": candidate["trading_date"],
        "adjustment_status": candidate["adjustment_status"],
        "status": status,
        "source": candidate["selected_source"],
    }


def test_active_rows_have_complete_lineage_and_exclude_held_rows(tmp_path: Path) -> None:
    gp = _candidate("GP", "2024-01-01", "tier_1_cross_source_confirmed")
    aci = _candidate("ACI", "2024-01-02")
    aci_conflict = _candidate("ACI", "2021-04-26")
    brac = _candidate("BRACBANK", "2024-01-02")
    dsex = _candidate("DSEX", "2024-01-02")
    candidates = [gp, aci, aci_conflict, brac, dsex]
    ledger = [
        _ledger(gp, "approvable_after_human_decision"),
        _ledger(aci, "approvable_after_human_decision"),
        _ledger(aci_conflict, "held_for_conflict"),
        _ledger(brac, "approvable_after_human_decision"),
        _ledger(dsex, "held_for_mapping"),
        {
            "population": "invalid_observation",
            "symbol": "GP",
            "date": "bad",
            "adjustment_status": "unknown",
            "status": "rejected_invalid",
            "source": "bad",
        },
    ]
    expected = {
        "approvable_after_human_decision": 3,
        "held_for_conflict": 1,
        "held_for_mapping": 1,
        "rejected_invalid": 1,
    }
    ids = {symbol: f"decision-{symbol}" for symbol in ACTIVE_SYMBOLS}
    audits = {symbol: f"audit-{symbol}" for symbol in ACTIVE_SYMBOLS} | {
        "corporate_actions": "audit-corporate",
        "calendar": "audit-calendar",
        "conflicts": "audit-conflicts",
    }
    rows, summary = build_active_rows(
        candidates,
        ledger,
        activation_timestamp="2026-07-28T00:00:00+00:00",
        approval_decision_ids=ids,
        audit_event_ids=audits,
        expected_status_counts=expected,
    )
    assert [row["symbol"] for row in rows] == ["ACI", "BRACBANK", "GP"]
    assert all(row["symbol"] != "DSEX" and row["date"] != "2021-04-26" for row in rows)
    assert rows[0]["quality_tier"] == "tier_2_single_source_high_quality"
    assert rows[2]["quality_tier"] == "tier_1_cross_source_confirmed"
    assert all(row["raw_hashes"] and row["source_row_ids"] and row["audit_linkage"] for row in rows)
    path = tmp_path / "active.jsonl"
    digest = write_jsonl(path, rows)
    assert len(digest) == 64
    assert len(path.read_text(encoding="utf-8").splitlines()) == summary["active_rows"] == 3
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["symbol"] == "ACI"


def test_forbidden_approvable_dsex_fails_closed() -> None:
    dsex = _candidate("DSEX", "2024-01-02")
    with pytest.raises(ValueError, match="Forbidden symbol"):
        build_active_rows(
            [dsex],
            [_ledger(dsex, "approvable_after_human_decision")],
            activation_timestamp="2026-07-28T00:00:00+00:00",
            approval_decision_ids={symbol: symbol for symbol in ACTIVE_SYMBOLS},
            audit_event_ids={
                **{symbol: symbol for symbol in ACTIVE_SYMBOLS},
                "corporate_actions": "c",
                "calendar": "k",
                "conflicts": "x",
            },
            expected_status_counts={"approvable_after_human_decision": 1},
        )


def test_nine_decisions_are_independent_and_strategy_is_prohibited() -> None:
    specs = decision_specs("b" * 64)
    assert len(specs) == 9
    assert len({spec["event"] for spec in specs}) == 9
    assert specs[3]["key"] == "DSEX" and specs[3]["status"] == "rejected"
    assert specs[6]["value"]["reviewer"] == ""
    assert specs[-1]["status"] == "prohibited"
    assert specs[7]["value"]["label"] == ACTIVE_STATUS


def test_decision_records_separate_canonical_audit_events(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path / "audit", "Research subset decision test chain")
    specs = decision_specs("c" * 64)
    event_ids = []
    for spec in specs:
        approval = record_decision(
            db,
            spec=spec,
            draft_version="test-research-subset-v1",
            operator_identity="test-operator",
        )
        event_ids.append(approval.audit_event_id)
    assert len(set(event_ids)) == 9
    assert db.scalar(select(func.count()).select_from(GovernanceItemApproval)) == 9
    conflict = db.scalar(
        select(GovernanceItemApproval).where(GovernanceItemApproval.item_key == "conflicts")
    )
    assert conflict is not None
    assert conflict.reviewer_identity is None
    assert conflict.proposed_value["operator_decision"] == "hold_for_review"
    assert verify_audit_chain(db)

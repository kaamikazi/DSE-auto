from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GovernanceItemApproval, ResearchDataset, StrategyRegistration
from app.services.audit import append_audit, verify_audit_chain
from app.services.research_governance import (
    MINIMUM_SAMPLE_SIZE,
    PARAMETERS,
    STRATEGY_ID,
    STRATEGY_VERSION,
    parameter_set_hash,
    strategy_code_hash,
)

ACTIVE_SYMBOLS = ("GP", "ACI", "BRACBANK")
REJECTED_SYMBOLS = ("DSEX",)
ACTIVE_STATUS = "RESEARCH DATASET ACTIVE"
TRANSFORMATION_VERSION = "target-research-subset-activation-v1"
EXPECTED_PACK_HASH = "28f44bd78a9e57e0e00ecf56046f5411e18f48508585ef4cbae0ad3e52207235"
ALLOWED_TIERS = {
    "tier_1_cross_source_confirmed": "tier_1_cross_source_confirmed",
    "tier_2_single_high_quality_source": "tier_2_single_source_high_quality",
    "tier_3_low_confidence_research_only": "tier_3_research_only",
}
EXPECTED_STATUS_COUNTS = {
    "approvable_after_human_decision": 18103,
    "held_for_mapping": 240,
    "held_for_conflict": 8,
    "held_for_calendar": 68,
    "held_for_corporate_action": 710,
    "rejected_invalid": 712,
}


def canonical_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_approval_pack(pack_dir: Path) -> dict[str, Any]:
    loaded = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Approval-pack manifest must be a JSON object")
    manifest: dict[str, Any] = loaded
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    actual_manifest_hash = canonical_hash(unsigned)
    if (
        manifest.get("manifest_hash") != EXPECTED_PACK_HASH
        or actual_manifest_hash != EXPECTED_PACK_HASH
    ):
        raise ValueError("Approval-pack manifest hash does not match the authorized evidence")
    for item in manifest["files"]:
        path = pack_dir / item["filename"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Approval-pack file verification failed: {item['filename']}")
    return manifest


def _ledger_key(row: dict[str, Any]) -> tuple[str, str, str, str | None]:
    return (
        str(row["symbol"]),
        str(row["date"]),
        str(row["adjustment_status"]),
        str(row["source"]) if row.get("source") is not None else None,
    )


def build_active_rows(
    candidate_rows: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    *,
    activation_timestamp: str,
    approval_decision_ids: dict[str, str],
    audit_event_ids: dict[str, str],
    expected_status_counts: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected = expected_status_counts or EXPECTED_STATUS_COUNTS
    counts = Counter(str(row["status"]) for row in ledger)
    if dict(counts) != expected:
        raise ValueError(f"Evidence status counts drifted: {dict(counts)}")
    statuses = {
        _ledger_key(row): str(row["status"])
        for row in ledger
        if row["population"] == "canonical_candidate"
    }
    if len(statuses) != sum(row["population"] == "canonical_candidate" for row in ledger):
        raise ValueError("Canonical-candidate ledger contains duplicate decision keys")

    active: list[dict[str, Any]] = []
    for candidate in candidate_rows:
        key = (
            str(candidate["symbol"]),
            str(candidate["trading_date"]),
            str(candidate["adjustment_status"]),
            str(candidate["selected_source"]),
        )
        status = statuses.get(key)
        if status is None:
            raise ValueError(f"Candidate lacks an approval-ledger decision: {key}")
        if status != "approvable_after_human_decision":
            continue
        symbol = str(candidate["symbol"])
        if symbol not in ACTIVE_SYMBOLS:
            raise ValueError(f"Forbidden symbol reached active selection: {symbol}")
        quality_tier = ALLOWED_TIERS.get(str(candidate["quality_status"]))
        if quality_tier is None:
            raise ValueError("Unsupported quality tier reached active selection")
        lineage = list(candidate["lineage"])
        contributing_sources = sorted(
            {str(item.get("source_name") or item["source_dataset_id"]) for item in lineage}
        )
        row = {
            "symbol": symbol,
            "date": str(candidate["trading_date"]),
            "open": str(candidate["open"]),
            "high": str(candidate["high"]),
            "low": str(candidate["low"]),
            "close": str(candidate["close"]),
            "volume": str(candidate["volume"]),
            "adjustment_status": str(candidate["adjustment_status"]),
            "selected_source": str(candidate["selected_source"]),
            "contributing_sources": contributing_sources,
            "source_row_ids": [str(item["source_row_identifier"]) for item in lineage],
            "raw_hashes": sorted({str(item["source_file_hash"]) for item in lineage}),
            "source_lineage": lineage,
            "transformation_version": TRANSFORMATION_VERSION,
            "quality_tier": quality_tier,
            "approval_decision_id": approval_decision_ids[symbol],
            "activation_timestamp": activation_timestamp,
            "audit_linkage": [
                audit_event_ids[symbol],
                audit_event_ids["corporate_actions"],
                audit_event_ids["calendar"],
                audit_event_ids["conflicts"],
            ],
        }
        active.append(row)
    active.sort(key=lambda row: (row["symbol"], row["date"], row["adjustment_status"]))
    if len(active) != expected["approvable_after_human_decision"]:
        raise ValueError(
            f"Expected {expected['approvable_after_human_decision']:,} active rows, "
            f"found {len(active):,}"
        )

    by_symbol = Counter(str(row["symbol"]) for row in active)
    by_tier = Counter(str(row["quality_tier"]) for row in active)
    coverage: dict[str, dict[str, Any]] = {}
    for symbol in ACTIVE_SYMBOLS:
        rows = [row for row in active if row["symbol"] == symbol]
        dates = [str(row["date"]) for row in rows]
        coverage[symbol] = {
            "rows": len(rows),
            "start": min(dates),
            "end": max(dates),
            "adjusted_rows": sum(row["adjustment_status"] == "adjusted" for row in rows),
            "unadjusted_rows": sum(row["adjustment_status"] == "unadjusted" for row in rows),
        }
    return active, {
        "active_rows": len(active),
        "active_by_symbol": dict(by_symbol),
        "active_by_tier": dict(by_tier),
        "coverage": coverage,
        "excluded_status_counts": {
            key: value
            for key, value in expected.items()
            if key != "approvable_after_human_decision"
        },
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
            handle.write("\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decision_specs(authorization_sha256: str) -> list[dict[str, Any]]:
    common = {"authorization_sha256": authorization_sha256, "qualification": "0/60"}
    return [
        {
            "key": "GP",
            "status": "approved_research_only",
            "event": "research_subset.gp_approved",
            "value": {**common, "symbol": "GP", "exclusions": "all held and rejected rows"},
        },
        {
            "key": "ACI",
            "status": "conditionally_approved",
            "event": "research_subset.aci_conditionally_approved",
            "value": {
                **common,
                "symbol": "ACI",
                "excluded_dates": ["2021-04-26"],
                "exclusions": "all held and rejected rows",
            },
        },
        {
            "key": "BRACBANK",
            "status": "conditionally_approved",
            "event": "research_subset.bracbank_conditionally_approved",
            "value": {
                **common,
                "symbol": "BRACBANK",
                "excluded_dates": ["2021-04-26", "2023-04-16"],
                "exclusions": "all held and rejected rows",
            },
        },
        {
            "key": "DSEX",
            "status": "rejected",
            "event": "research_subset.dsex_rejected",
            "value": {**common, "symbol": "DSEX", "benchmark_allowed": False, "active": False},
        },
        {
            "key": "corporate_actions",
            "status": "rejected_insufficient_evidence",
            "event": "research_subset.corporate_actions_rejected",
            "value": {
                **common,
                "verified": 0,
                "insufficient_evidence": 938,
                "likely_but_unconfirmed": 12,
                "affected_rows_excluded": True,
            },
        },
        {
            "key": "calendar",
            "status": "not_approved",
            "event": "research_subset.calendar_not_approved",
            "value": {
                **common,
                "authoritative": False,
                "inference_allowed": False,
                "source_present_dates_only": True,
            },
        },
        {
            "key": "conflicts",
            "status": "hold_for_review",
            "event": "research_subset.conflicts_held",
            "value": {
                **common,
                "records": 6,
                "reviewer": "",
                "operator_decision": "hold_for_review",
                "automatic_resolution": False,
            },
        },
        {
            "key": "subset_activation",
            "status": "authorized_research_only",
            "event": "research_subset.activation_authorized",
            "value": {
                **common,
                "symbols": list(ACTIVE_SYMBOLS),
                "label": ACTIVE_STATUS,
                "dsex": "excluded",
            },
        },
        {
            "key": "strategy_execution",
            "status": "prohibited",
            "event": "research_subset.strategy_execution_prohibited",
            "value": {
                **common,
                "strategy": "ma_crossover@1.0.0",
                "promotion": False,
                "execution": False,
                "separate_authorization_required": True,
            },
        },
    ]


def record_decision(
    db: Session,
    *,
    spec: dict[str, Any],
    draft_version: str,
    operator_identity: str,
) -> GovernanceItemApproval:
    if db.scalar(
        select(GovernanceItemApproval).where(
            GovernanceItemApproval.approval_type == "research_subset",
            GovernanceItemApproval.draft_version == draft_version,
            GovernanceItemApproval.item_key == spec["key"],
        )
    ):
        raise ValueError(f"Decision already exists: {spec['key']}")
    proposed = dict(spec["value"])
    decision_hash = canonical_hash(
        {
            "draft_version": draft_version,
            "key": spec["key"],
            "status": spec["status"],
            "proposed": proposed,
        }
    )
    approval = GovernanceItemApproval(
        approval_type="research_subset",
        draft_version=draft_version,
        item_key=spec["key"],
        current_draft={"active": False},
        proposed_value=proposed,
        evidence_ids=[EXPECTED_PACK_HASH],
        conflicts=[]
        if spec["key"] != "conflicts"
        else [f"target-conflict-{index:02d}" for index in range(1, 7)],
        missing_evidence=[],
        verification_status="operator_decided",
        approval_status=spec["status"],
        operator_identity=operator_identity,
        reviewer_identity=None,
        reviewer_independence="not_provided",
        conservative_fallback={"active": False, "effect": "exclude"},
        decision_hash=decision_hash,
        reviewed_at=datetime.now(UTC),
    )
    db.add(approval)
    db.flush()
    event = append_audit(
        db,
        actor=operator_identity,
        event_type=spec["event"],
        entity_type="governance_item_approval",
        entity_id=approval.id,
        new_state={
            "decision_hash": decision_hash,
            "approval_status": spec["status"],
            "decision": proposed,
            "research_only": True,
            "activation_scope": list(ACTIVE_SYMBOLS) if spec["key"] == "subset_activation" else [],
        },
    )
    approval.audit_event_id = event.id
    db.commit()
    return approval


def create_research_dataset_record(
    db: Session,
    *,
    version: str,
    dataset_path: Path,
    dataset_hash: str,
    candidate_db_path: Path,
    candidate_db_hash: str,
    summary: dict[str, Any],
    activation_approval: GovernanceItemApproval,
    operator_identity: str,
) -> ResearchDataset:
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain is invalid")
    if db.scalar(select(ResearchDataset).where(ResearchDataset.name == version)):
        raise ValueError("Research dataset version already exists")
    dataset = ResearchDataset(
        name=version,
        symbols=list(ACTIVE_SYMBOLS),
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=[EXPECTED_PACK_HASH],
        source_hash=candidate_db_hash,
        dataset_hash=dataset_hash,
        timestamp_trust="unknown",
        raw_file_path=str(candidate_db_path),
        normalized_file_path=str(dataset_path),
        quality_report={
            **summary,
            "classification": ACTIVE_STATUS,
            "research_only": True,
            "qualification": "0/60",
            "exchange_verified": False,
            "campaign_approved": False,
            "strategy_approved": False,
            "paper_trading_approved": False,
            "production_approved": False,
            "real_money_approved": False,
            "source_hierarchy": "coverage-metadata primary; AmarStock/DSE Stocks validation; historical unknown-adjustment fallback only",
            "calendar_policy": "source-present dates only; no inferred trading days, holidays, or gaps",
        },
        status="research_dataset_active",
        approved_by=operator_identity,
        approved_at=datetime.now(UTC),
        audit_event_ids=[activation_approval.audit_event_id],
    )
    db.add(dataset)
    db.commit()
    return dataset


def build_execution_plan(
    *, dataset: ResearchDataset, summary: dict[str, Any], excluded_dates: dict[str, list[str]]
) -> dict[str, Any]:
    return {
        "plan_status": "prepared_not_authorized_not_executed",
        "strategy": f"{STRATEGY_ID}@{STRATEGY_VERSION}",
        "strategy_promotion": False,
        "separate_execution_authorization_required": True,
        "dataset_version": dataset.name,
        "dataset_hash": dataset.dataset_hash,
        "coverage": summary["coverage"],
        "excluded_dates": excluded_dates,
        "excluded_status_counts": summary["excluded_status_counts"],
        "unresolved_conflicts": 6,
        "corporate_actions": {
            "verified": 0,
            "insufficient_evidence": 938,
            "likely_but_unconfirmed": 12,
        },
        "source_hierarchy": dataset.quality_report["source_hierarchy"],
        "code_hash": strategy_code_hash(),
        "parameter_set": PARAMETERS,
        "parameter_hash": parameter_set_hash(),
        "fee_assumptions": {
            "approved": False,
            "planned_sensitivity_percent": ["0", "0.4", "0.75", "1.0"],
        },
        "slippage_assumptions": {
            "approved": False,
            "planned_sensitivity_percent": ["0", "0.1", "0.25", "0.5"],
        },
        "benchmark_limitation": "DSEX rejected; no DSEX benchmark is permitted",
        "comparison_design": "per-symbol buy-and-hold only; no synthetic DSEX replacement",
        "walk_forward_design": "chronological expanding-train/forward-test windows with no look-ahead",
        "sensitivity_design": {
            "fast": [10, 20, 30],
            "slow": [40, 50, 75],
            "require_fast_less_than_slow": True,
        },
        "sample_size_assessment": {
            symbol: {
                "rows": item["rows"],
                "minimum_ordered_bars": MINIMUM_SAMPLE_SIZE,
                "meets_raw_row_minimum": item["rows"] >= MINIMUM_SAMPLE_SIZE,
            }
            for symbol, item in summary["coverage"].items()
        },
        "qualification": "0/60",
    }


def assert_strategy_not_promoted(db: Session) -> None:
    registration = db.scalar(
        select(StrategyRegistration).where(
            StrategyRegistration.strategy_id == STRATEGY_ID,
            StrategyRegistration.version == STRATEGY_VERSION,
        )
    )
    if registration is not None and registration.lifecycle_state not in {"draft", "research"}:
        raise ValueError("ma_crossover@1.0.0 is promoted; activation must fail closed")

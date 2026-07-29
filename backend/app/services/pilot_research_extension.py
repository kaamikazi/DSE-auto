from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GovernanceItemApproval, ResearchDataset, StrategyRegistration
from app.services.audit import append_audit, verify_audit_chain
from app.services.research_governance import STRATEGY_ID, STRATEGY_VERSION

TARGET_SYMBOLS = ("BATBC", "SQURPHARMA")
BLOCKED_SYMBOLS = ("IDLC", "LANKABAFIN", "POWERGRID")
FIVE_SYMBOL_UNIVERSE = ("GP", "ACI", "BRACBANK", "BATBC", "SQURPHARMA")
ACTIVE_STATUS = "RESEARCH DATASET ACTIVE"
ALLOWED_DISPOSITION = "tier_2_single_source_high_quality"
TRANSFORMATION_VERSION = "batbc-squrpharma-t2-extension-v1"
PACK_MANIFEST_HASH = "bcc52fbd94bc65a54ac3c42a6240b40d4c664dfc9db809db88449cc253b4efd1"
OBSERVED_WINDOW = {"start": "2012-10-01", "end": "2026-01-22"}
EXPECTED_ACTIVE_COUNTS = {"BATBC": 6255, "SQURPHARMA": 6317}
EXPECTED_EXCLUSIONS = {
    "BATBC": {
        "tier_3_research_only": 6262,
        "held_lifecycle": 0,
        "rejected_invalid": 15,
        "rejected_duplicate_conflict": 14,
    },
    "SQURPHARMA": {
        "tier_3_research_only": 6328,
        "held_lifecycle": 1,
        "rejected_invalid": 15,
        "rejected_duplicate_conflict": 14,
    },
}
INELIGIBLE_DISPOSITIONS = {
    "tier_3_research_only",
    "held_genuine_conflict",
    "held_lifecycle",
    "held_corporate_action",
    "held_mapping",
    "rejected_invalid",
    "rejected_duplicate_conflict",
    "rejected_other",
}
MENDELEY_PRIMARY_PREFIX = (
    "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata /"
)


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def verify_reconciliation_pack(pack_dir: Path) -> dict[str, Any]:
    manifest_path = pack_dir / "manifest.json"
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("Reconciliation manifest must be a JSON object")
    manifest: dict[str, Any] = loaded
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    actual = canonical_hash(unsigned)
    if manifest.get("manifest_hash") != PACK_MANIFEST_HASH or actual != PACK_MANIFEST_HASH:
        raise ValueError("Reconciliation manifest hash mismatch")
    listed = {str(item["name"]) for item in manifest["files"]}
    actual_files = {
        path.name for path in pack_dir.iterdir() if path.is_file() and path.name != "manifest.json"
    }
    if listed != actual_files:
        raise ValueError("Reconciliation pack file inventory mismatch")
    for item in manifest["files"]:
        path = pack_dir / str(item["name"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Reconciliation artifact hash mismatch: {path.name}")
    return manifest


def _valid_ohlc(row: Mapping[str, Any]) -> bool:
    try:
        open_, high, low, close = (
            Decimal(str(row[field])) for field in ("open", "high", "low", "close")
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    return low >= 0 and min(open_, close) >= low and max(open_, close) <= high


def _complete_source_lineage(row: Mapping[str, Any]) -> bool:
    return all(
        str(row.get(field) or "").strip()
        for field in (
            "source_dataset_id",
            "source_hash",
            "source_name",
            "source_row_id",
            "normalized_symbol",
            "trading_date",
            "adjustment_status",
        )
    )


def _lineage_record(row: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "source_dataset_id": str(row["source_dataset_id"]),
        "source_file_hash": str(row["source_hash"]),
        "source_name": str(row["source_name"]),
        "source_row_id": str(row["source_row_id"]),
        "mapping_confidence": str(row["mapping_confidence"]),
        "mapping_approval_status": str(row["mapping_approval_status"]),
        "adjustment_status": str(row["adjustment_status"]),
        "raw_ohlcv": {
            field: None if row.get(field) is None else str(row[field])
            for field in ("open", "high", "low", "close", "volume")
        },
    }


def build_extension_rows(
    dispositions: Sequence[dict[str, Any]],
    observations_by_row_id: Mapping[str, dict[str, Any]],
    *,
    activation_timestamp: str,
    human_decision_ids: Mapping[str, str],
    audit_event_ids: Mapping[str, str],
    expected_active_counts: Mapping[str, int] = EXPECTED_ACTIVE_COUNTS,
    expected_exclusions: Mapping[str, Mapping[str, int]] = EXPECTED_EXCLUSIONS,
    expected_reconciled_rows: int = 58662,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(dispositions) != expected_reconciled_rows:
        raise ValueError(
            f"Expected {expected_reconciled_rows:,} reconciled rows, found {len(dispositions):,}"
        )
    logical_ids = [str(row["logical_row_id"]) for row in dispositions]
    if len(logical_ids) != len(set(logical_ids)):
        raise ValueError("Reconciliation contains duplicate logical-row IDs")
    if any(row["status"] != row["final_disposition"] for row in dispositions):
        raise ValueError("Status and final disposition disagree")
    if any(bool(row.get("active")) for row in dispositions):
        raise ValueError("Reconciliation pack unexpectedly contains active rows")

    target_rows = [row for row in dispositions if str(row["symbol"]) in TARGET_SYMBOLS]
    blocked_active = [
        row
        for row in dispositions
        if str(row["symbol"]) in BLOCKED_SYMBOLS
        and row["final_disposition"] == ALLOWED_DISPOSITION
        and bool(row.get("active"))
    ]
    if blocked_active:
        raise ValueError("A blocked pilot symbol is active")
    actual_exclusions = {
        symbol: dict(
            Counter(
                str(row["final_disposition"])
                for row in target_rows
                if row["symbol"] == symbol and row["final_disposition"] != ALLOWED_DISPOSITION
            )
        )
        for symbol in TARGET_SYMBOLS
    }
    for symbol, expected in expected_exclusions.items():
        for disposition, count in expected.items():
            if actual_exclusions[symbol].get(disposition, 0) != count:
                raise ValueError(
                    f"{symbol} exclusion drift for {disposition}: "
                    f"{actual_exclusions[symbol].get(disposition, 0)} != {count}"
                )

    selected = [row for row in target_rows if row["final_disposition"] == ALLOWED_DISPOSITION]
    active: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for candidate in selected:
        symbol = str(candidate["symbol"])
        trading_date = str(candidate["date"])
        adjustment = str(candidate["adjustment_status"])
        key = (symbol, trading_date, adjustment)
        if key in seen_keys:
            raise ValueError(f"Duplicate active symbol/date/grain key: {key}")
        seen_keys.add(key)
        if not OBSERVED_WINDOW["start"] <= trading_date <= OBSERVED_WINDOW["end"]:
            raise ValueError(f"Row falls outside approved observed window: {key}")
        if adjustment not in {"adjusted", "unadjusted"}:
            raise ValueError(f"Unknown adjustment grain reached activation: {key}")
        source_ids = [str(value) for value in candidate["source_row_ids"]]
        if not source_ids:
            raise ValueError(f"Logical row has no lineage: {candidate['logical_row_id']}")
        try:
            source_rows = [observations_by_row_id[row_id] for row_id in source_ids]
        except KeyError as exc:
            raise ValueError(f"Source lineage row is missing: {exc.args[0]}") from exc
        for source in source_rows:
            if not _complete_source_lineage(source):
                raise ValueError(f"Incomplete lineage: {source.get('source_row_id')}")
            if (
                str(source["normalized_symbol"]) != symbol
                or str(source["trading_date"]) != trading_date
                or str(source["adjustment_status"]) != adjustment
                or str(source["source_hash"]) not in candidate["source_hashes"]
                or str(source["source_name"]) not in candidate["source_names"]
            ):
                raise ValueError(f"Lineage does not match logical row: {key}")
        primary_rows = [
            source
            for source in source_rows
            if str(source["source_name"]).startswith(MENDELEY_PRIMARY_PREFIX)
        ]
        if len(primary_rows) != 1:
            raise ValueError(f"Expected one Mendeley known-grain primary for {key}")
        primary = primary_rows[0]
        if (
            not int(primary["accepted_for_candidate"])
            or primary["mapping_confidence"] != "high"
            or primary["mapping_approval_status"] in {"rejected", "under_review"}
            or not _valid_ohlc(primary)
        ):
            raise ValueError(f"Primary source validation failed: {key}")
        lineage = [
            _lineage_record(
                source,
                role="primary"
                if source["source_row_id"] == primary["source_row_id"]
                else "validation_reference",
            )
            for source in sorted(source_rows, key=lambda row: str(row["source_row_id"]))
        ]
        active.append(
            {
                "logical_row_id": str(candidate["logical_row_id"]),
                "symbol": symbol,
                "date": trading_date,
                "open": str(primary["open"]),
                "high": str(primary["high"]),
                "low": str(primary["low"]),
                "close": str(primary["close"]),
                "volume": None if primary.get("volume") is None else str(primary["volume"]),
                "adjustment_status": adjustment,
                "final_disposition": ALLOWED_DISPOSITION,
                "selected_source": str(primary["source_name"]),
                "selected_source_row_id": str(primary["source_row_id"]),
                "raw_file_hashes": sorted({str(row["source_hash"]) for row in source_rows}),
                "source_row_ids": source_ids,
                "source_lineage": lineage,
                "source_independence_claimed": False,
                "corporate_actions_reconstructed": False,
                "missing_dates_inferred": False,
                "observed_research_window": {
                    **OBSERVED_WINDOW,
                    "official_listing_date_claim": False,
                    "lifecycle_evidence": "pending",
                },
                "timestamp_trust": "unknown",
                "source_trust": "third_party_research",
                "transformation_version": TRANSFORMATION_VERSION,
                "activation_timestamp": activation_timestamp,
                "human_decision_ids": sorted(human_decision_ids.values()),
                "audit_event_ids": sorted(audit_event_ids.values()),
            }
        )
    active.sort(key=lambda row: (row["symbol"], row["date"], row["adjustment_status"]))
    active_counts = Counter(str(row["symbol"]) for row in active)
    for symbol, maximum in expected_active_counts.items():
        if active_counts[symbol] > maximum:
            raise ValueError(f"{symbol} exceeds the authorized maximum")
        if active_counts[symbol] != maximum:
            raise ValueError(
                f"{symbol} validated T2 count differs from authorized pack: "
                f"{active_counts[symbol]} != {maximum}"
            )
    return active, {
        "active_rows": len(active),
        "active_by_symbol": dict(active_counts),
        "active_by_tier": {ALLOWED_DISPOSITION: len(active)},
        "excluded_by_symbol_and_disposition": actual_exclusions,
        "observed_windows": {symbol: dict(OBSERVED_WINDOW) for symbol in TARGET_SYMBOLS},
        "validation_failures": [],
    }


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
            handle.write("\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decision_specs(
    authorization_sha256: str, *, version: str, dataset_hash: str | None = None
) -> list[dict[str, Any]]:
    common = {
        "authorization_sha256": authorization_sha256,
        "qualification": "0/60",
        "research_only": True,
    }
    specifications: list[dict[str, Any]] = []
    for symbol in TARGET_SYMBOLS:
        specifications.extend(
            [
                {
                    "key": f"{symbol}.t2_activation",
                    "status": "approved_research_only",
                    "event": f"research_extension.{symbol.lower()}_t2_approved",
                    "value": {
                        **common,
                        "symbol": symbol,
                        "disposition": ALLOWED_DISPOSITION,
                        "maximum_rows": EXPECTED_ACTIVE_COUNTS[symbol],
                        "observed_window": dict(OBSERVED_WINDOW),
                        "official_listing_date_claim": False,
                    },
                },
                {
                    "key": f"{symbol}.t3_rejection",
                    "status": "rejected",
                    "event": f"research_extension.{symbol.lower()}_t3_rejected",
                    "value": {
                        **common,
                        "symbol": symbol,
                        "disposition": "tier_3_research_only",
                        "rows": EXPECTED_EXCLUSIONS[symbol]["tier_3_research_only"],
                        "active": False,
                    },
                },
            ]
        )
    specifications.extend(
        [
            {
                "key": "lifecycle_pending",
                "status": "accepted_observed_boundary_lifecycle_pending",
                "event": "research_extension.lifecycle_pending",
                "value": {
                    **common,
                    "symbols": list(TARGET_SYMBOLS),
                    "observed_windows": {
                        symbol: dict(OBSERVED_WINDOW) for symbol in TARGET_SYMBOLS
                    },
                    "official_listing_date_claim": False,
                    "squrpharma_held_rows_excluded": 1,
                    "lifecycle_evidence": "pending",
                },
            },
            {
                "key": "invalid_duplicate_exclusions",
                "status": "excluded",
                "event": "research_extension.invalid_duplicate_excluded",
                "value": {
                    **common,
                    "symbols": list(TARGET_SYMBOLS),
                    "invalid_rows": {"BATBC": 15, "SQURPHARMA": 15},
                    "duplicate_conflict_rows": {"BATBC": 14, "SQURPHARMA": 14},
                    "active": False,
                },
            },
        ]
    )
    for symbol in BLOCKED_SYMBOLS:
        specifications.append(
            {
                "key": f"{symbol}.activation_rejection",
                "status": "rejected_not_granted",
                "event": f"research_extension.{symbol.lower()}_activation_rejected",
                "value": {**common, "symbol": symbol, "active": False},
            }
        )
    if dataset_hash is not None:
        specifications.extend(
            [
                {
                    "key": "dataset_activation",
                    "status": "authorized_research_only",
                    "event": "research_extension.dataset_activated",
                    "value": {
                        **common,
                        "version": version,
                        "dataset_hash": dataset_hash,
                        "symbols": list(TARGET_SYMBOLS),
                        "label": ACTIVE_STATUS,
                    },
                },
                {
                    "key": "strategy_execution",
                    "status": "prohibited",
                    "event": "research_extension.strategy_execution_prohibited",
                    "value": {
                        **common,
                        "strategy": f"{STRATEGY_ID}@{STRATEGY_VERSION}",
                        "execution": False,
                        "promotion": False,
                    },
                },
            ]
        )
    return specifications


def record_decision(
    db: Session,
    *,
    spec: dict[str, Any],
    draft_version: str,
    operator_identity: str,
) -> GovernanceItemApproval:
    if db.scalar(
        select(GovernanceItemApproval).where(
            GovernanceItemApproval.approval_type == "pilot_research_extension",
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
        approval_type="pilot_research_extension",
        draft_version=draft_version,
        item_key=spec["key"],
        current_draft={"active": False},
        proposed_value=proposed,
        evidence_ids=[PACK_MANIFEST_HASH],
        conflicts=[],
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
        event_type=str(spec["event"]),
        entity_type="governance_item_approval",
        entity_id=approval.id,
        new_state={
            "decision_hash": decision_hash,
            "approval_status": spec["status"],
            "decision": proposed,
            "research_only": True,
        },
    )
    approval.audit_event_id = event.id
    db.commit()
    return approval


def create_extension_dataset_record(
    db: Session,
    *,
    version: str,
    dataset_path: Path,
    dataset_hash: str,
    source_bundle_hash: str,
    candidate_db_path: Path,
    candidate_db_hash: str,
    parent_dataset: ResearchDataset,
    summary: dict[str, Any],
    decisions: Mapping[str, GovernanceItemApproval],
    activation_approval: GovernanceItemApproval,
    git_head: str,
    audit_chain_id: str,
    operator_identity: str,
) -> ResearchDataset:
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain is invalid")
    if db.scalar(select(ResearchDataset).where(ResearchDataset.name == version)):
        raise ValueError("Research extension version already exists")
    dataset = ResearchDataset(
        name=version,
        symbols=list(TARGET_SYMBOLS),
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=[PACK_MANIFEST_HASH, *sorted(item.id for item in decisions.values())],
        source_hash=source_bundle_hash,
        dataset_hash=dataset_hash,
        timestamp_trust="unknown",
        raw_file_path=str(candidate_db_path),
        normalized_file_path=str(dataset_path),
        quality_report={
            **summary,
            "dataset_id": version,
            "parent_dataset_id": parent_dataset.id,
            "parent_dataset_version": parent_dataset.name,
            "parent_dataset_hash": parent_dataset.dataset_hash,
            "classification": ACTIVE_STATUS,
            "research_only": True,
            "qualification": "0/60",
            "source_policy": (
                "Mendeley known-grain coverage-metadata source primary; DSEStocks and "
                "AmarStock retained as validation references; independence not claimed"
            ),
            "transformation_version": TRANSFORMATION_VERSION,
            "git_head": git_head,
            "database_fingerprint": candidate_db_hash,
            "pack_manifest_hash": PACK_MANIFEST_HASH,
            "human_decision_ids": sorted(item.id for item in decisions.values()),
            "audit_chain_id": audit_chain_id,
            "exchange_verified": False,
            "official_lifecycle_verified": False,
            "paper_candidate": False,
            "strategy_approved": False,
            "production_ready": False,
            "real_money_ready": False,
            "campaign_approved": False,
        },
        status="research_dataset_active",
        approved_by=operator_identity,
        approved_at=datetime.now(UTC),
        audit_event_ids=sorted(
            str(item.audit_event_id) for item in decisions.values() if item.audit_event_id
        ),
    )
    db.add(dataset)
    db.flush()
    dataset.quality_report = {
        **dataset.quality_report,
        "registry_id": dataset.id,
        "activation_audit_event_id": activation_approval.audit_event_id,
    }
    db.commit()
    return dataset


def build_five_symbol_research_plan(
    *, parent_dataset: ResearchDataset, extension_dataset: ResearchDataset
) -> dict[str, Any]:
    return {
        "status": "prepared_not_authorized_not_executed",
        "strategy": f"{STRATEGY_ID}@{STRATEGY_VERSION}",
        "strategy_execution": False,
        "strategy_promotion": False,
        "universe": list(FIVE_SYMBOL_UNIVERSE),
        "datasets": {
            "parent": {
                "id": parent_dataset.id,
                "version": parent_dataset.name,
                "hash": parent_dataset.dataset_hash,
                "symbols": parent_dataset.symbols,
            },
            "extension": {
                "id": extension_dataset.id,
                "version": extension_dataset.name,
                "hash": extension_dataset.dataset_hash,
                "symbols": extension_dataset.symbols,
            },
        },
        "analyses": {
            "per_symbol": list(FIVE_SYMBOL_UNIVERSE),
            "equal_weight_portfolio": True,
            "sector_balanced_portfolio": {
                "telecommunication": ["GP"],
                "pharmaceuticals": ["ACI", "SQURPHARMA"],
                "banking": ["BRACBANK"],
                "food_allied": ["BATBC"],
            },
            "leave_bracbank_out": True,
            "leave_best_symbol_out": "determine best on training data only",
            "leave_one_symbol_out": list(FIVE_SYMBOL_UNIVERSE),
            "buy_and_hold_comparisons": list(FIVE_SYMBOL_UNIVERSE),
        },
        "validation": {
            "chronological_walk_forward": "expanding train and forward test; no look-ahead",
            "untouched_holdout": "reserve final chronological segment before parameter selection",
            "parameter_sensitivity": {"fast": [10, 20, 30], "slow": [40, 50, 75]},
            "cost_sensitivity_percent": ["0", "0.4", "0.75", "1.0"],
            "slippage_sensitivity_percent": ["0", "0.1", "0.25", "0.5"],
        },
        "limitations": {
            "source_tier": "T2 third-party research only; source independence not proven",
            "lifecycle": "observed windows only; official lifecycle evidence pending",
            "corporate_actions": "no approved reconstruction or inference",
            "benchmark": "buy-and-hold comparisons only unless a separately approved benchmark is available",
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
        raise ValueError("ma_crossover is promoted; activation must fail closed")


def observed_window_contains(value: str) -> bool:
    parsed = date.fromisoformat(value)
    return (
        date.fromisoformat(OBSERVED_WINDOW["start"])
        <= parsed
        <= date.fromisoformat(OBSERVED_WINDOW["end"])
    )

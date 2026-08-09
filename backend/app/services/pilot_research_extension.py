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
INDEPENDENT_EXTENSION_SYMBOLS = (
    "HEIDELBCEM",
    "GPHISPAT",
    "GREENDELT",
    "PARAMOUNT",
    "OLYMPIC",
    "JAMUNABANK",
    "MJLBD",
    "CITYBANK",
    "AMCL(PRAN)",
    "DBH",
    "MARICO",
    "UNILEVERCL",
    "SUMITPOWER",
    "SQUARETEXT",
    "RELIANCINS",
)
INDEPENDENT_EXPECTED_ADJUSTED_COUNTS = {
    "HEIDELBCEM": 3117,
    "GPHISPAT": 3137,
    "GREENDELT": 3111,
    "PARAMOUNT": 3152,
    "OLYMPIC": 3160,
    "JAMUNABANK": 3156,
    "MJLBD": 3155,
    "CITYBANK": 3153,
    "AMCL(PRAN)": 3147,
    "DBH": 3117,
    "MARICO": 3114,
    "UNILEVERCL": 3106,
    "SUMITPOWER": 3104,
    "SQUARETEXT": 3102,
    "RELIANCINS": 3065,
}
INDEPENDENT_EXPECTED_TOTAL_COUNTS = {
    "HEIDELBCEM": 6233,
    "GPHISPAT": 6273,
    "GREENDELT": 6220,
    "PARAMOUNT": 6302,
    "OLYMPIC": 6318,
    "JAMUNABANK": 6311,
    "MJLBD": 6309,
    "CITYBANK": 6305,
    "AMCL(PRAN)": 6292,
    "DBH": 6234,
    "MARICO": 6226,
    "UNILEVERCL": 6211,
    "SUMITPOWER": 6208,
    "SQUARETEXT": 6202,
    "RELIANCINS": 6130,
}
INDEPENDENT_EXPECTED_EXCLUSIONS = {
    "HEIDELBCEM": {
        "tier_3_research_only": 5047,
        "held_genuine_conflict": 1,
        "held_lifecycle": 5,
        "rejected_invalid": 6,
        "rejected_duplicate_conflict": 14,
    },
    "GPHISPAT": {
        "tier_3_research_only": 3017,
        "held_lifecycle": 3,
        "rejected_invalid": 14,
        "rejected_duplicate_conflict": 14,
    },
    "GREENDELT": {
        "tier_3_research_only": 5234,
        "held_genuine_conflict": 1,
        "held_lifecycle": 6,
        "rejected_invalid": 10,
        "rejected_duplicate_conflict": 13,
    },
    "PARAMOUNT": {
        "tier_3_research_only": 4102,
        "held_genuine_conflict": 1,
        "held_lifecycle": 1,
        "rejected_invalid": 2,
        "rejected_duplicate_conflict": 12,
    },
    "OLYMPIC": {
        "tier_3_research_only": 4164,
        "held_genuine_conflict": 1,
        "held_lifecycle": 2,
        "rejected_invalid": 2,
        "rejected_duplicate_conflict": 14,
    },
    "JAMUNABANK": {
        "tier_3_research_only": 4453,
        "held_lifecycle": 1,
        "rejected_invalid": 6,
        "rejected_duplicate_conflict": 14,
    },
    "MJLBD": {
        "tier_3_research_only": 3228,
        "held_genuine_conflict": 1,
        "held_lifecycle": 5,
        "rejected_invalid": 3,
        "rejected_duplicate_conflict": 14,
    },
    "CITYBANK": {
        "tier_3_research_only": 5176,
        "held_genuine_conflict": 2,
        "held_lifecycle": 4,
        "rejected_invalid": 6,
        "rejected_duplicate_conflict": 14,
    },
    "AMCL(PRAN)": {
        "tier_3_research_only": 6338,
        "held_genuine_conflict": 1,
        "rejected_invalid": 7,
        "rejected_duplicate_conflict": 14,
    },
    "DBH": {
        "tier_3_research_only": 3948,
        "held_lifecycle": 4,
        "rejected_invalid": 30,
        "rejected_duplicate_conflict": 13,
    },
    "MARICO": {
        "tier_3_research_only": 3609,
        "held_genuine_conflict": 1,
        "held_lifecycle": 4,
        "rejected_invalid": 6,
        "rejected_duplicate_conflict": 14,
    },
    "UNILEVERCL": {
        "tier_3_research_only": 1009,
        "held_lifecycle": 6,
        "rejected_invalid": 2,
        "rejected_duplicate_conflict": 14,
    },
    "SUMITPOWER": {
        "tier_3_research_only": 4496,
        "held_lifecycle": 1,
        "rejected_invalid": 21,
        "rejected_duplicate_conflict": 14,
    },
    "SQUARETEXT": {
        "tier_3_research_only": 5506,
        "held_genuine_conflict": 1,
        "rejected_invalid": 54,
        "rejected_duplicate_conflict": 14,
    },
    "RELIANCINS": {
        "tier_3_research_only": 4018,
        "held_genuine_conflict": 1,
        "held_lifecycle": 9,
        "rejected_invalid": 4,
        "rejected_duplicate_conflict": 14,
    },
}
INDEPENDENT_CONFLICTS = (
    ("AMCL(PRAN)", "2021-04-26", "unadjusted"),
    ("CITYBANK", "2021-04-26", "unadjusted"),
    ("CITYBANK", "2023-04-16", "adjusted"),
    ("GREENDELT", "2021-04-26", "unadjusted"),
    ("HEIDELBCEM", "2021-04-26", "unadjusted"),
    ("MARICO", "2021-04-26", "unadjusted"),
    ("MJLBD", "2021-04-26", "unadjusted"),
    ("OLYMPIC", "2021-04-26", "unadjusted"),
    ("PARAMOUNT", "2021-04-26", "unadjusted"),
    ("RELIANCINS", "2021-04-26", "unadjusted"),
    ("SQUARETEXT", "2021-04-26", "unadjusted"),
)
INDEPENDENT_TRANSFORMATION_VERSION = "independent-fifteen-t2-extension-v1"
INDEPENDENT_RECONCILED_ROWS = 157560
FROZEN_CANDIDATE_REVIEW_SHA256 = "19fd34b291dd21b053db7dfc56392e5c3d2b0d36ef6655ded65432a007ac161f"
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
    target_symbols: Sequence[str] = TARGET_SYMBOLS,
    blocked_symbols: Sequence[str] = BLOCKED_SYMBOLS,
    observed_windows: Mapping[str, Mapping[str, str]] | None = None,
    transformation_version: str = TRANSFORMATION_VERSION,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    targets = tuple(target_symbols)
    if not targets or len(targets) != len(set(targets)):
        raise ValueError("Target symbols must be non-empty and unique")
    if set(expected_active_counts) != set(targets):
        raise ValueError("Expected active counts must cover exactly the target symbols")
    if set(expected_exclusions) != set(targets):
        raise ValueError("Expected exclusions must cover exactly the target symbols")
    if observed_windows is not None and set(observed_windows) != set(targets):
        raise ValueError("Observed windows must cover exactly the target symbols")
    windows = {
        symbol: dict((observed_windows or {}).get(symbol, OBSERVED_WINDOW)) for symbol in targets
    }
    for symbol, window in windows.items():
        if set(window) != {"start", "end"} or window["start"] > window["end"]:
            raise ValueError(f"Invalid observed window for {symbol}")
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

    target_rows = [row for row in dispositions if str(row["symbol"]) in targets]
    blocked_active = [
        row
        for row in dispositions
        if str(row["symbol"]) in blocked_symbols
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
        for symbol in targets
    }
    for symbol, expected in expected_exclusions.items():
        if actual_exclusions[symbol] != dict(expected):
            raise ValueError(
                f"{symbol} exclusion drift: {actual_exclusions[symbol]} != {dict(expected)}"
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
        window = windows[symbol]
        if not window["start"] <= trading_date <= window["end"]:
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
                    **window,
                    "official_listing_date_claim": False,
                    "lifecycle_evidence": "pending",
                },
                "timestamp_trust": "unknown",
                "source_trust": "third_party_research",
                "transformation_version": transformation_version,
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
        "observed_windows": windows,
        "validation_failures": [],
    }


def verify_independent_execution_grain(
    rows: Sequence[dict[str, Any]],
    *,
    expected_adjusted_counts: Mapping[str, int] = INDEPENDENT_EXPECTED_ADJUSTED_COUNTS,
    expected_total_counts: Mapping[str, int] = INDEPENDENT_EXPECTED_TOTAL_COUNTS,
) -> dict[str, Any]:
    """Verify that alternative T2 grains cannot duplicate the adjusted execution view."""
    symbols = tuple(expected_total_counts)
    if set(expected_adjusted_counts) != set(symbols):
        raise ValueError("Adjusted and total count contracts cover different symbols")
    total_counts = Counter(str(row.get("symbol")) for row in rows)
    adjusted_counts = Counter(
        str(row.get("symbol")) for row in rows if row.get("adjustment_status") == "adjusted"
    )
    unadjusted_counts = Counter(
        str(row.get("symbol")) for row in rows if row.get("adjustment_status") == "unadjusted"
    )
    full_keys = [
        (str(row.get("symbol")), str(row.get("date")), str(row.get("adjustment_status")))
        for row in rows
    ]
    adjusted_keys = [
        (str(row.get("symbol")), str(row.get("date")))
        for row in rows
        if row.get("adjustment_status") == "adjusted"
    ]
    if dict(total_counts) != dict(expected_total_counts):
        raise ValueError("Independent-extension total T2 counts drifted")
    if dict(adjusted_counts) != dict(expected_adjusted_counts):
        raise ValueError("Independent-extension adjusted T2 counts drifted")
    if len(full_keys) != len(set(full_keys)):
        raise ValueError("Duplicate symbol/date/adjustment grain in independent extension")
    if len(adjusted_keys) != len(set(adjusted_keys)):
        raise ValueError("Duplicate symbol/date in adjusted execution grain")
    if any(
        row.get("final_disposition") != ALLOWED_DISPOSITION
        or row.get("adjustment_status") not in {"adjusted", "unadjusted"}
        for row in rows
    ):
        raise ValueError("Ineligible disposition or adjustment grain reached extension")
    if any(any(str(key).lower().startswith("sector") for key in row) for row in rows):
        raise ValueError("Provisional sector metadata reached normalized strategy rows")
    return {
        "approved_dataset_rows": len(rows),
        "adjusted_execution_rows": sum(adjusted_counts.values()),
        "unadjusted_non_execution_research_rows": sum(unadjusted_counts.values()),
        "adjusted_execution_by_symbol": dict(adjusted_counts),
        "unadjusted_non_execution_by_symbol": dict(unadjusted_counts),
        "full_grain_duplicates": 0,
        "adjusted_symbol_date_duplicates": 0,
        "execution_grain": "adjusted",
        "unadjusted_execution_eligible": False,
        "sector_fields_in_normalized_rows": False,
    }


def build_independent_extension_rows(
    dispositions: Sequence[dict[str, Any]],
    observations_by_row_id: Mapping[str, dict[str, Any]],
    *,
    activation_timestamp: str,
    human_decision_ids: Mapping[str, str],
    audit_event_ids: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the authorized fifteen-symbol T2 dataset with adjusted execution isolation."""
    windows = {symbol: dict(OBSERVED_WINDOW) for symbol in INDEPENDENT_EXTENSION_SYMBOLS}
    rows, summary = build_extension_rows(
        dispositions,
        observations_by_row_id,
        activation_timestamp=activation_timestamp,
        human_decision_ids=human_decision_ids,
        audit_event_ids=audit_event_ids,
        expected_active_counts=INDEPENDENT_EXPECTED_TOTAL_COUNTS,
        expected_exclusions=INDEPENDENT_EXPECTED_EXCLUSIONS,
        expected_reconciled_rows=INDEPENDENT_RECONCILED_ROWS,
        target_symbols=INDEPENDENT_EXTENSION_SYMBOLS,
        blocked_symbols=(),
        observed_windows=windows,
        transformation_version=INDEPENDENT_TRANSFORMATION_VERSION,
    )
    grain = verify_independent_execution_grain(rows)
    summary.update(
        {
            **grain,
            "coverage": {
                symbol: {
                    "start": OBSERVED_WINDOW["start"],
                    "end": OBSERVED_WINDOW["end"],
                    "rows": INDEPENDENT_EXPECTED_TOTAL_COUNTS[symbol],
                    "adjusted_rows": INDEPENDENT_EXPECTED_ADJUSTED_COUNTS[symbol],
                    "unadjusted_rows": (
                        INDEPENDENT_EXPECTED_TOTAL_COUNTS[symbol]
                        - INDEPENDENT_EXPECTED_ADJUSTED_COUNTS[symbol]
                    ),
                    "official_lifecycle_claim": False,
                    "lifecycle_evidence": "pending",
                }
                for symbol in INDEPENDENT_EXTENSION_SYMBOLS
            },
            "lifecycle_evidence_pending": list(INDEPENDENT_EXTENSION_SYMBOLS),
            "sector_evidence_pending": list(INDEPENDENT_EXTENSION_SYMBOLS),
            "provisional_sector_required_by_strategy": False,
            "conflicting_logical_rows_excluded": len(INDEPENDENT_CONFLICTS),
            "tier_3_execution_eligible": 0,
        }
    )
    return rows, summary


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


def independent_extension_decision_specs(
    authorization_sha256: str,
    *,
    version: str,
    frozen_review_sha256: str = FROZEN_CANDIDATE_REVIEW_SHA256,
    dataset_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Record the operator's conservative decisions using existing audit event types."""
    common = {
        "authorization_sha256": authorization_sha256,
        "frozen_candidate_review_sha256": frozen_review_sha256,
        "qualification": "0/60",
        "research_only": True,
    }
    specs: list[dict[str, Any]] = [
        {
            "key": "conflicts_held",
            "status": "held_genuine_conflict",
            "event": "research_subset.conflicts_held",
            "value": {
                **common,
                "logical_observations": [
                    {"symbol": symbol, "date": day, "adjustment_status": adjustment}
                    for symbol, day, adjustment in INDEPENDENT_CONFLICTS
                ],
                "logical_observation_count": len(INDEPENDENT_CONFLICTS),
                "symbols_affected": len({item[0] for item in INDEPENDENT_CONFLICTS}),
                "select_neither_source": True,
                "averaged": False,
                "active": False,
            },
        },
        {
            "key": "lifecycle_pending",
            "status": "lifecycle_evidence_pending",
            "event": "research_extension.lifecycle_pending",
            "value": {
                **common,
                "symbols": list(INDEPENDENT_EXTENSION_SYMBOLS),
                "observed_windows": {
                    symbol: dict(OBSERVED_WINDOW) for symbol in INDEPENDENT_EXTENSION_SYMBOLS
                },
                "official_listing_or_delisting_claim": False,
                "lifecycle_held_rows_excluded": sum(
                    counts.get("held_lifecycle", 0)
                    for counts in INDEPENDENT_EXPECTED_EXCLUSIONS.values()
                ),
                "active_held_rows": 0,
            },
        },
        {
            "key": "sector_evidence_pending",
            "status": "sector_evidence_pending",
            "event": "research_extension.lifecycle_pending",
            "value": {
                **common,
                "symbols": list(INDEPENDENT_EXTENSION_SYMBOLS),
                "metadata_status": "provisional",
                "required_by_frozen_strategies": False,
                "included_in_normalized_rows": False,
            },
        },
        {
            "key": "row_exclusions",
            "status": "excluded",
            "event": "research_extension.invalid_duplicate_excluded",
            "value": {
                **common,
                "excluded_by_symbol_and_disposition": INDEPENDENT_EXPECTED_EXCLUSIONS,
                "tier_3_active": 0,
                "held_active": 0,
                "invalid_active": 0,
                "duplicate_conflict_active": 0,
            },
        },
    ]
    if dataset_hash is not None:
        specs.extend(
            [
                {
                    "key": "dataset_activation",
                    "status": "authorized_research_only",
                    "event": "research_extension.dataset_activated",
                    "value": {
                        **common,
                        "version": version,
                        "dataset_hash": dataset_hash,
                        "symbols": list(INDEPENDENT_EXTENSION_SYMBOLS),
                        "approved_dataset_rows": sum(INDEPENDENT_EXPECTED_TOTAL_COUNTS.values()),
                        "adjusted_execution_rows": sum(
                            INDEPENDENT_EXPECTED_ADJUSTED_COUNTS.values()
                        ),
                        "label": ACTIVE_STATUS,
                    },
                },
                {
                    "key": "strategy_execution_prohibited",
                    "status": "prohibited",
                    "event": "research_extension.strategy_execution_prohibited",
                    "value": {
                        **common,
                        "frozen_strategies": [
                            "ma_crossover@1.0.0",
                            "cross_sectional_momentum@0.1.0",
                            "defensive_low_volatility@0.1.0",
                            "absolute_momentum_filter@0.1.0",
                        ],
                        "execution": False,
                        "promotion": False,
                        "campaign": False,
                    },
                },
            ]
        )
    return specs


def record_decision(
    db: Session,
    *,
    spec: dict[str, Any],
    draft_version: str,
    operator_identity: str,
    approval_type: str = "pilot_research_extension",
    evidence_ids: Sequence[str] = (PACK_MANIFEST_HASH,),
) -> GovernanceItemApproval:
    if not evidence_ids or any(not item for item in evidence_ids):
        raise ValueError("Decision evidence IDs must be non-empty")
    if db.scalar(
        select(GovernanceItemApproval).where(
            GovernanceItemApproval.approval_type == approval_type,
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
        approval_type=approval_type,
        draft_version=draft_version,
        item_key=spec["key"],
        current_draft={"active": False},
        proposed_value=proposed,
        evidence_ids=list(evidence_ids),
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
    symbols: Sequence[str] = TARGET_SYMBOLS,
    review_evidence_hash: str = PACK_MANIFEST_HASH,
    transformation_version: str = TRANSFORMATION_VERSION,
    parent_review_commit: str | None = None,
) -> ResearchDataset:
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain is invalid")
    if db.scalar(select(ResearchDataset).where(ResearchDataset.name == version)):
        raise ValueError("Research extension version already exists")
    quality_report = {
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
        "transformation_version": transformation_version,
        "git_head": git_head,
        "database_fingerprint": candidate_db_hash,
        "review_artifact_hash": review_evidence_hash,
        "parent_review_commit": parent_review_commit,
        "human_decision_ids": sorted(item.id for item in decisions.values()),
        "audit_chain_id": audit_chain_id,
        "exchange_verified": False,
        "official_lifecycle_verified": False,
        "paper_candidate": False,
        "strategy_approved": False,
        "production_ready": False,
        "real_money_ready": False,
        "campaign_approved": False,
    }
    if review_evidence_hash == PACK_MANIFEST_HASH:
        quality_report["pack_manifest_hash"] = review_evidence_hash
    dataset = ResearchDataset(
        name=version,
        symbols=list(symbols),
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=[
            review_evidence_hash,
            *sorted(item.id for item in decisions.values()),
        ],
        source_hash=source_bundle_hash,
        dataset_hash=dataset_hash,
        timestamp_trust="unknown",
        raw_file_path=str(candidate_db_path),
        normalized_file_path=str(dataset_path),
        quality_report=quality_report,
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


def create_independent_extension_dataset_record(
    db: Session,
    *,
    version: str,
    dataset_path: Path,
    dataset_hash: str,
    source_bundle_hash: str,
    source_file_hashes: Sequence[str],
    candidate_db_path: Path,
    candidate_db_hash: str,
    frozen_review_sha256: str,
    authorization_sha256: str,
    summary: dict[str, Any],
    decisions: Mapping[str, GovernanceItemApproval],
    preserved_datasets: Sequence[ResearchDataset],
    git_head: str,
    audit_chain_id: str,
    operator_identity: str,
) -> ResearchDataset:
    """Register one immutable research-only extension while preserving prior identities."""
    if not verify_audit_chain(db):
        raise ValueError("Canonical audit chain is invalid")
    if db.scalar(select(ResearchDataset).where(ResearchDataset.name == version)):
        raise ValueError("Independent extension version already exists")
    if not dataset_path.is_file() or hashlib.sha256(dataset_path.read_bytes()).hexdigest() != (
        dataset_hash
    ):
        raise ValueError("Normalized independent-extension file identity mismatch")
    if set(summary["active_by_symbol"]) != set(INDEPENDENT_EXTENSION_SYMBOLS):
        raise ValueError("Independent extension symbol scope changed")
    if summary["adjusted_execution_by_symbol"] != INDEPENDENT_EXPECTED_ADJUSTED_COUNTS:
        raise ValueError("Adjusted execution-grain count contract changed")
    if summary["active_by_symbol"] != INDEPENDENT_EXPECTED_TOTAL_COUNTS:
        raise ValueError("Approved dataset-row count contract changed")
    if frozen_review_sha256 != FROZEN_CANDIDATE_REVIEW_SHA256:
        raise ValueError("Frozen candidate-review identity changed")
    if len(source_file_hashes) == 0 or any(len(value) != 64 for value in source_file_hashes):
        raise ValueError("Source file hashes are incomplete")
    preserved = [
        {
            "id": item.id,
            "version": item.name,
            "dataset_hash": item.dataset_hash,
            "source_hash": item.source_hash,
            "symbols": list(item.symbols),
            "status": item.status,
        }
        for item in preserved_datasets
    ]
    if len(preserved) != 3 or any(
        item["status"] != "research_dataset_active" for item in preserved
    ):
        raise ValueError("Expected exactly three preserved active dataset identities")
    existing_symbols = [symbol for item in preserved for symbol in item["symbols"]]
    expected_existing_symbols = [
        "GP",
        "ACI",
        "BRACBANK",
        "BATBC",
        "SQURPHARMA",
        "IDLC",
        "LANKABAFIN",
        "POWERGRID",
        "RENATA",
        "BERGERPBL",
    ]
    if existing_symbols != expected_existing_symbols:
        raise ValueError("Existing active research universe is not the frozen ten-symbol universe")
    combined_symbols = [*existing_symbols, *INDEPENDENT_EXTENSION_SYMBOLS]
    if len(combined_symbols) != 25 or len(set(combined_symbols)) != 25:
        raise ValueError("Combined active research universe is not exactly 25 unique symbols")
    activation_approval = decisions.get("dataset_activation")
    if activation_approval is None or activation_approval.approval_status != (
        "authorized_research_only"
    ):
        raise ValueError("Research-only dataset activation approval is missing")
    quality_report = {
        **summary,
        "dataset_id": version,
        "classification": ACTIVE_STATUS,
        "research_only": True,
        "qualification": "0/60",
        "adjustment_grain_contract": {
            "stored_grains": ["adjusted", "unadjusted"],
            "logical_relationship": "alternative grains of the same market history",
            "strategy_execution_grain": "adjusted",
            "unadjusted_role": "non_execution_research_reference",
            "combined_into_one_execution_series": False,
        },
        "frozen_candidate_review_sha256": frozen_review_sha256,
        "authorization_sha256": authorization_sha256,
        "source_file_hashes": sorted(source_file_hashes),
        "source_bundle_hash": source_bundle_hash,
        "candidate_database_sha256": candidate_db_hash,
        "transformation_version": INDEPENDENT_TRANSFORMATION_VERSION,
        "git_head": git_head,
        "audit_chain_id": audit_chain_id,
        "human_decision_ids": sorted(item.id for item in decisions.values()),
        "preserved_active_dataset_identities": preserved,
        "combined_active_universe": combined_symbols,
        "combined_active_universe_count": 25,
        "exchange_verified": False,
        "official_lifecycle_verified": False,
        "sector_evidence_status": "pending",
        "sector_fields_in_normalized_rows": False,
        "paper_candidate": False,
        "strategy_approved": False,
        "campaign_approved": False,
        "production_ready": False,
        "real_money_ready": False,
    }
    dataset = ResearchDataset(
        name=version,
        symbols=list(INDEPENDENT_EXTENSION_SYMBOLS),
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=[
            frozen_review_sha256,
            authorization_sha256,
            *sorted(item.id for item in decisions.values()),
        ],
        source_hash=source_bundle_hash,
        dataset_hash=dataset_hash,
        timestamp_trust="unknown",
        raw_file_path=str(candidate_db_path),
        normalized_file_path=str(dataset_path),
        quality_report=quality_report,
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

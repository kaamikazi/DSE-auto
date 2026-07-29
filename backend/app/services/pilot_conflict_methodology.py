from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any

PILOT_SYMBOLS = ("IDLC", "LANKABAFIN", "BATBC", "SQURPHARMA", "POWERGRID")
PRICE_FIELDS = ("open", "high", "low", "close")
KNOWN_GRAINS = {"adjusted", "unadjusted"}
PRICE_TOLERANCE = Decimal("0.001")
HIGH_QUALITY_MINIMUM = Decimal("70")
HIGH_QUALITY_MINIMUM_ROWS = 252
FINAL_DISPOSITIONS = (
    "tier_1_cross_source_confirmed",
    "tier_2_single_source_high_quality",
    "tier_3_research_only",
    "held_genuine_conflict",
    "held_lifecycle",
    "held_corporate_action",
    "held_mapping",
    "rejected_invalid",
    "rejected_duplicate_conflict",
    "rejected_other",
)
TIER_3_REASON_CODES = (
    "source_independence_unproven",
    "adjustment_documentation_incomplete",
    "provenance_weaker",
    "timestamp_trust_weaker",
    "validation_overlap_missing",
    "source_quality_below_tier2",
    "lifecycle_uncertainty_nonblocking",
    "other",
)
TIER_DEFINITIONS: dict[str, dict[str, Any]] = {
    "tier_1_cross_source_confirmed": {
        "requires": [
            "complete_lineage",
            "valid_ohlc",
            "known_adjustment_grain",
            "high_confidence_symbol_mapping",
            "independently_eligible_cross_source_agreement",
            "no_unresolved_conflict",
            "no_lifecycle_hold",
            "no_corporate_action_hold",
            "no_invalid_status",
        ],
        "activation_eligible_by_proposed_policy": True,
    },
    "tier_2_single_source_high_quality": {
        "requires": [
            "complete_lineage",
            "valid_ohlc",
            "known_adjustment_grain",
            "high_confidence_symbol_mapping",
            "high_quality_primary_source",
            "no_eligible_independent_overlap_or_validation_unavailable",
            "no_unresolved_conflict",
            "no_lifecycle_hold",
            "no_corporate_action_hold",
            "no_invalid_status",
        ],
        "activation_eligible_by_proposed_policy": True,
    },
    "tier_3_research_only": {
        "requires": [
            "complete_lineage",
            "no_structural_ohlc_failure",
            "at_least_one_exact_tier_3_reason_code",
        ],
        "activation_eligible_by_proposed_policy": False,
        "separate_human_authorization_required_by_reason": True,
    },
}
ROOT_CAUSE_CATEGORIES = (
    "adjusted_unadjusted_comparison",
    "duplicate_logical_dataset",
    "multiple_rows_from_same_source",
    "incompatible_adjustment_grain",
    "turnover_value_volume_mismatch",
    "exact_duplicate_before_deduplication",
    "symbol_alias_mismatch",
    "date_shift_mismatch",
    "precision_or_rounding",
    "corporate_action_divergence",
    "genuine_same_grain_source_disagreement",
    "malformed_source_data",
    "unverified_volume_unit_only",
    "unknown",
)

COMPARISON_ELIGIBILITY_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "left": "adjusted OHLC",
        "right": "adjusted OHLC",
        "eligible": True,
        "reason_code": "eligible_same_adjustment_ohlc",
    },
    {
        "left": "unadjusted OHLC",
        "right": "unadjusted OHLC",
        "eligible": True,
        "reason_code": "eligible_same_adjustment_ohlc",
    },
    {
        "left": "adjusted OHLC",
        "right": "unadjusted OHLC",
        "eligible": False,
        "reason_code": "adjustment_grain_mismatch",
    },
    {
        "left": "known-adjustment OHLC",
        "right": "unknown-adjustment OHLC",
        "eligible": False,
        "reason_code": "unknown_adjustment_grain",
    },
    {
        "left": "volume",
        "right": "volume",
        "eligible": False,
        "reason_code": "volume_unit_not_registered",
        "condition": "eligible only after both units and aggregation semantics are registered",
    },
    {
        "left": "turnover/value",
        "right": "volume",
        "eligible": False,
        "reason_code": "field_semantic_mismatch",
    },
    {
        "left": "turnover/value",
        "right": "turnover/value",
        "eligible": False,
        "reason_code": "turnover_unit_not_registered",
        "condition": "eligible only after both currency and aggregation semantics are registered",
    },
    {
        "left": "number of trades",
        "right": "number of trades",
        "eligible": False,
        "reason_code": "trade_count_semantics_not_registered",
    },
    {
        "left": "same source or same raw hash",
        "right": "same source or same raw hash",
        "eligible": False,
        "reason_code": "source_not_distinct",
    },
    {
        "left": "conflicting same-source duplicate",
        "right": "any source",
        "eligible": False,
        "reason_code": "source_has_conflicting_duplicates",
    },
    {
        "left": "insufficient-confidence mapping",
        "right": "any source",
        "eligible": False,
        "reason_code": "mapping_confidence_insufficient",
    },
    {
        "left": "unaligned date",
        "right": "any date",
        "eligible": False,
        "reason_code": "date_not_aligned",
    },
)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _relative(left: str | None, right: str | None) -> Decimal | None:
    if left is None or right is None:
        return None
    a, b = Decimal(left), Decimal(right)
    return abs(a - b) / max(abs(a), abs(b), Decimal("0.0001"))


def _price_differences(left: dict[str, Any], right: dict[str, Any]) -> dict[str, str | None]:
    return {
        field: str(value)
        if (value := _relative(left.get(field), right.get(field)))
        else "0"
        if left.get(field) is not None and right.get(field) is not None
        else None
        for field in PRICE_FIELDS
    }


def _outside_tolerance(differences: dict[str, str | None]) -> bool:
    return any(value is None or Decimal(value) > PRICE_TOLERANCE for value in differences.values())


def _complete_lineage(row: dict[str, Any]) -> bool:
    return all(
        str(row.get(field) or "").strip()
        for field in ("source_dataset_id", "source_hash", "source_name", "source_row_id")
    )


def _valid_ohlc(row: dict[str, Any]) -> bool:
    try:
        open_, high, low, close = (Decimal(str(row[field])) for field in PRICE_FIELDS)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False
    return min(open_, close) >= low and max(open_, close) <= high and low >= 0


def classify_final_disposition(
    *,
    accepted: bool,
    complete_lineage: bool,
    valid_ohlc: bool,
    known_adjustment: bool,
    high_confidence_mapping: bool,
    independent_agreement: bool,
    high_quality_source: bool,
    unresolved_conflict: bool,
    lifecycle_hold: bool,
    corporate_action_hold: bool,
    duplicate_conflict: bool,
    tier_3_reasons: Sequence[str] = (),
) -> tuple[str, list[str]]:
    """Return one fail-closed row disposition and its diagnostic reasons."""
    reasons = sorted(set(tier_3_reasons))
    unknown_reasons = set(reasons) - set(TIER_3_REASON_CODES)
    if unknown_reasons:
        raise ValueError(f"Unknown Tier-3 reason codes: {sorted(unknown_reasons)}")
    if not accepted or not valid_ohlc:
        return "rejected_invalid", ["source_row_not_accepted_or_structural_ohlc_failure"]
    if not complete_lineage:
        return "rejected_other", ["incomplete_lineage"]
    if not high_confidence_mapping:
        return "held_mapping", ["mapping_confidence_or_approval_insufficient"]
    if unresolved_conflict:
        return "held_genuine_conflict", ["eligible_cross_source_ohlc_disagreement"]
    if duplicate_conflict:
        return "rejected_duplicate_conflict", ["conflicting_same_source_duplicate"]
    if lifecycle_hold:
        return "held_lifecycle", ["material_lifecycle_interval_requires_review"]
    if corporate_action_hold:
        return "held_corporate_action", ["registered_corporate_action_evidence_pending"]
    if independent_agreement and known_adjustment:
        return "tier_1_cross_source_confirmed", []
    if known_adjustment and high_quality_source:
        return "tier_2_single_source_high_quality", []
    if not reasons:
        reasons = ["other"]
    return "tier_3_research_only", reasons


def classify_existing_conflict(row: dict[str, Any]) -> dict[str, Any]:
    adjustment_a = str(row["adjustment_a"])
    adjustment_b = str(row["adjustment_b"])
    reasons: list[str] = []
    contributing: list[str] = []
    pair = {adjustment_a, adjustment_b}
    if pair == KNOWN_GRAINS:
        cause = "adjusted_unadjusted_comparison"
        reasons.append("adjustment_grain_mismatch")
        if {
            str(row["source_a"]),
            str(row["source_b"]),
        } == {
            "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / adjusted",
            "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / unadjusted",
        }:
            contributing.append("duplicate_logical_dataset")
    elif adjustment_a != adjustment_b or pair - KNOWN_GRAINS:
        cause = "incompatible_adjustment_grain"
        reasons.append("unknown_adjustment_grain")
    elif row["source_a"] == row["source_b"]:
        cause = "multiple_rows_from_same_source"
        reasons.append("source_not_distinct")
    else:
        price_differences = {
            field: row["percentage_difference"].get(field) for field in PRICE_FIELDS
        }
        volume = row["percentage_difference"].get("volume")
        if not _outside_tolerance(price_differences):
            if volume is not None and Decimal(volume) > PRICE_TOLERANCE:
                cause = "unverified_volume_unit_only"
                reasons.append("volume_unit_not_registered")
            else:
                cause = "precision_or_rounding"
                reasons.append("within_price_tolerance")
        else:
            cause = "genuine_same_grain_source_disagreement"
            reasons.append("eligible_same_adjustment_ohlc")
    return {
        **row,
        "primary_root_cause": cause,
        "contributing_root_causes": contributing,
        "reason_codes": reasons,
        "corrected_disposition": "genuine_conflict"
        if cause == "genuine_same_grain_source_disagreement"
        else "comparison_ineligible_or_agreement",
    }


def comparison_eligibility(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if left["trading_date"] != right["trading_date"]:
        reasons.append("date_not_aligned")
    if left["normalized_symbol"] != right["normalized_symbol"]:
        reasons.append("symbol_not_aligned")
    if left["mapping_confidence"] != "high" or right["mapping_confidence"] != "high":
        reasons.append("mapping_confidence_insufficient")
    if left["mapping_approval_status"] in {"rejected", "under_review"} or right[
        "mapping_approval_status"
    ] in {"rejected", "under_review"}:
        reasons.append("mapping_approval_insufficient")
    if (
        left["adjustment_status"] not in KNOWN_GRAINS
        or right["adjustment_status"] not in KNOWN_GRAINS
    ):
        reasons.append("unknown_adjustment_grain")
    elif left["adjustment_status"] != right["adjustment_status"]:
        reasons.append("adjustment_grain_mismatch")
    if left["source_dataset_id"] == right["source_dataset_id"]:
        reasons.append("same_source_dataset")
    if left["source_hash"] == right["source_hash"]:
        reasons.append("duplicate_logical_dataset")
    if left.get("source_conflicting_duplicate") or right.get("source_conflicting_duplicate"):
        reasons.append("source_has_conflicting_duplicates")
    eligible = not reasons
    return {
        "eligible": eligible,
        "eligible_fields": list(PRICE_FIELDS) if eligible else [],
        "excluded_fields": {"volume": "volume_unit_not_registered"},
        "reason_codes": reasons,
    }


def collapse_duplicate_group(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (int(row["id"]), str(row["source_row_id"])))
    by_fingerprint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ordered:
        by_fingerprint[str(row["value_fingerprint"])].append(row)
    representatives = [items[0] for _, items in sorted(by_fingerprint.items())]
    group_basis = {
        "source_dataset_id": ordered[0]["source_dataset_id"],
        "symbol": ordered[0]["normalized_symbol"],
        "date": ordered[0]["trading_date"],
        "adjustment_status": ordered[0]["adjustment_status"],
    }
    duplicate_type = (
        "conflicting_same_source_duplicate"
        if len(by_fingerprint) > 1
        else "exact_duplicate"
        if len(rows) > 1
        else "unique"
    )
    return {
        **group_basis,
        "duplicate_group_id": _canonical_hash(group_basis),
        "duplicate_type": duplicate_type,
        "source_row_ids": [str(row["source_row_id"]) for row in ordered],
        "source_hashes": sorted({str(row["source_hash"]) for row in ordered}),
        "value_fingerprints": sorted(by_fingerprint),
        "representative_row_ids": [str(row["source_row_id"]) for row in representatives],
        "selected_representative_row": str(representatives[0]["source_row_id"]),
        "collapsed_row_count": len(rows) - len(representatives),
        "collapse_rationale": "identical normalized OHLCV fingerprint; all lineage preserved"
        if duplicate_type == "exact_duplicate"
        else "not collapsed; conflicting fingerprints remain separate and held"
        if duplicate_type == "conflicting_same_source_duplicate"
        else "single row",
        "representatives": representatives,
    }


def classify_corporate_action_candidate(
    *,
    gap_days: int,
    adjustment_factor_changed: bool,
    conflicting_duplicate: bool,
    registered_evidence: bool,
) -> tuple[str, str]:
    if registered_evidence:
        return "supported_by_registered_evidence", "evidence_supported_candidate"
    if conflicting_duplicate:
        return "duplicate_source_divergence", "insufficient_evidence"
    if adjustment_factor_changed:
        return "adjusted_unadjusted_divergence", "adjustment_divergence"
    if gap_days > 10:
        return "long_source_gap", "suspension_candidate"
    if gap_days > 1:
        return "missing_session_discontinuity", "discontinuity_for_review"
    return "ordinary_price_movement", "insufficient_evidence"


def _load_observations(db: sqlite3.Connection) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in PILOT_SYMBOLS)
    rows = db.execute(
        f"SELECT * FROM observations WHERE normalized_symbol IN ({placeholders}) "
        "ORDER BY normalized_symbol,trading_date,source_dataset_id,adjustment_status,id",  # noqa: S608
        PILOT_SYMBOLS,
    )
    return [dict(row) for row in rows]


def _load_actions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in PILOT_SYMBOLS)
    rows = db.execute(
        f"SELECT * FROM corporate_action_candidates WHERE normalized_symbol IN ({placeholders}) "
        "ORDER BY normalized_symbol,trading_date,source_dataset_id",  # noqa: S608
        PILOT_SYMBOLS,
    )
    return [dict(row) for row in rows]


def _source_quality(source_quality_path: Path) -> dict[str, dict[str, Any]]:
    rows = json.loads(source_quality_path.read_text(encoding="utf-8"))
    aliases = {
        "Mendeley 23553sm4tn v4 adjusted": "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / adjusted",
        "Mendeley 23553sm4tn v4 unadjusted": "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / unadjusted",
        "Mendeley 5mww8rb9td v1 historical": "Dhaka Stock Exchange Historical Data (1999-2025) - DSE_Data.csv",
    }
    return {
        aliases.get(str(row["logical_name"]), str(row["logical_name"])): row
        for row in rows
        if row.get("score") is not None
    }


def _source_scores(source_quality_path: Path) -> dict[str, Decimal]:
    return {
        name: Decimal(str(row["score"]))
        for name, row in _source_quality(source_quality_path).items()
    }


def _source_counts(observations: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(
        str(row["source_name"]) for row in observations if int(row["accepted_for_candidate"])
    )


def _factor_changes(observations: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    closes: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
    for row in observations:
        if not int(row["accepted_for_candidate"]) or row["close"] is None:
            continue
        adjustment = str(row["adjustment_status"])
        if adjustment in KNOWN_GRAINS:
            closes[(str(row["normalized_symbol"]), str(row["trading_date"]))][adjustment] = Decimal(
                str(row["close"])
            )
    factors: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    for (symbol, trading_date), values in closes.items():
        if values.keys() >= KNOWN_GRAINS and values["adjusted"] != 0:
            factors[symbol].append((trading_date, values["unadjusted"] / values["adjusted"]))
    changed: set[tuple[str, str]] = set()
    for symbol, rows in factors.items():
        rows.sort()
        for (_, previous), (trading_date, current) in zip(rows, rows[1:], strict=False):
            if abs(current / previous - 1) > Decimal("0.01"):
                changed.add((symbol, trading_date))
    return changed


def _lifecycle(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for symbol in PILOT_SYMBOLS:
        valid = [
            row
            for row in observations
            if row["normalized_symbol"] == symbol
            and int(row["accepted_for_candidate"])
            and row["trading_date"]
        ]
        known = [row for row in valid if row["adjustment_status"] in KNOWN_GRAINS]
        output.append(
            {
                "symbol": symbol,
                "official_listing_date": None,
                "official_first_trading_date": None,
                "official_delisting_status": None,
                "official_suspension_periods": [],
                "official_resumption_periods": [],
                "official_symbol_rename": None,
                "official_instrument_type": None,
                "trusted_secondary_evidence": [],
                "observed_first_date": min(str(row["trading_date"]) for row in valid),
                "observed_last_date": max(str(row["trading_date"]) for row in valid),
                "conservative_research_window": {
                    "start": min(str(row["trading_date"]) for row in known),
                    "end": max(str(row["trading_date"]) for row in known),
                    "basis": "accepted known-adjustment observations; not a listing-date claim",
                },
                "lifecycle_status": "lifecycle_evidence_pending",
                "unresolved_assumptions": [
                    "listing and first-trading dates are unverified",
                    "delisting and suspension history are unverified",
                    "observed bounds do not establish legal instrument lifecycle",
                ],
            }
        )
    return output


def _surrounding_dates(
    observations: Sequence[dict[str, Any]], target: dict[str, Any]
) -> dict[str, str | None]:
    dates = sorted(
        {
            str(row["trading_date"])
            for row in observations
            if row["normalized_symbol"] == target["normalized_symbol"]
            and row["source_dataset_id"] == target["source_dataset_id"]
            and row["adjustment_status"] == target["adjustment_status"]
            and row["trading_date"]
        }
    )
    current = str(target["trading_date"])
    index = dates.index(current)
    return {
        "previous": dates[index - 1] if index else None,
        "next": dates[index + 1] if index + 1 < len(dates) else None,
    }


def _approval_id(kind: str, value: object) -> str:
    return f"{kind}_{_canonical_hash(value)[:24]}"


def _baseline_conflicts(conflict_path: Path) -> list[dict[str, Any]]:
    rows = json.loads(conflict_path.read_text(encoding="utf-8"))
    return [classify_existing_conflict(row) for row in rows if row["symbol"] in PILOT_SYMBOLS]


def build_pilot_methodology_audit(
    database_path: Path,
    conflict_path: Path,
    source_quality_path: Path,
) -> dict[str, Any]:
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as db:
        observations = _load_observations(db)
        actions = _load_actions(db)
    baseline = _baseline_conflicts(conflict_path)
    source_quality = _source_quality(source_quality_path)
    source_scores = _source_scores(source_quality_path)
    source_counts = _source_counts(observations)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    invalid_counts: Counter[str] = Counter()
    invalid_observations: list[dict[str, Any]] = []
    raw_counts: Counter[str] = Counter()
    for row in observations:
        symbol = str(row["normalized_symbol"])
        raw_counts[symbol] += 1
        if not int(row["accepted_for_candidate"]):
            invalid_counts[symbol] += 1
            invalid_observations.append(row)
            continue
        grouped[
            (
                str(row["source_dataset_id"]),
                symbol,
                str(row["trading_date"]),
                str(row["adjustment_status"]),
            )
        ].append(row)

    duplicate_ledger: list[dict[str, Any]] = []
    representatives: list[dict[str, Any]] = []
    conflicting_keys: set[tuple[str, str, str]] = set()
    exact_collapsed: Counter[str] = Counter()
    for rows in grouped.values():
        collapsed = collapse_duplicate_group(rows)
        if collapsed["duplicate_type"] != "unique":
            duplicate_ledger.append(
                {key: value for key, value in collapsed.items() if key != "representatives"}
            )
        symbol = str(collapsed["symbol"])
        exact_collapsed[symbol] += int(collapsed["collapsed_row_count"])
        conflicting = collapsed["duplicate_type"] == "conflicting_same_source_duplicate"
        if conflicting:
            conflicting_keys.add(
                (symbol, str(collapsed["date"]), str(collapsed["adjustment_status"]))
            )
        for row in collapsed["representatives"]:
            row["source_conflicting_duplicate"] = conflicting
            representatives.append(row)

    by_date: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in representatives:
        by_date[(str(row["normalized_symbol"]), str(row["trading_date"]))].append(row)

    ineligible: list[dict[str, Any]] = []
    corrected: list[dict[str, Any]] = []
    genuine_keys: set[tuple[str, str, str]] = set()
    confirmed_keys: set[tuple[str, str, str]] = set()
    overlap: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for (symbol, trading_date), rows in sorted(by_date.items()):
        for left, right in combinations(rows, 2):
            eligibility = comparison_eligibility(left, right)
            base = {
                "symbol": symbol,
                "date": trading_date,
                "source_a": left["source_name"],
                "source_b": right["source_name"],
                "source_row_id_a": left["source_row_id"],
                "source_row_id_b": right["source_row_id"],
                "source_hash_a": left["source_hash"],
                "source_hash_b": right["source_hash"],
                "adjustment_a": left["adjustment_status"],
                "adjustment_b": right["adjustment_status"],
                **eligibility,
            }
            if not eligibility["eligible"]:
                ineligible.append(base)
                continue
            differences = _price_differences(left, right)
            conflict = _outside_tolerance(differences)
            status = "genuine_conflict" if conflict else "agreement"
            corrected.append(
                {
                    **base,
                    "price_percentage_differences": differences,
                    "status": status,
                    "reviewer_decision": "",
                    "operator_decision": "",
                }
            )
            key = (symbol, trading_date, str(left["adjustment_status"]))
            if conflict:
                genuine_keys.add(key)
            else:
                confirmed_keys.add(key)
            pair = tuple(sorted((str(left["source_name"]), str(right["source_name"]))))
            overlap_key = (symbol, str(left["adjustment_status"]), pair[0], pair[1])
            item = overlap.setdefault(
                overlap_key,
                {
                    "symbol": symbol,
                    "adjustment_status": left["adjustment_status"],
                    "source_a": pair[0],
                    "source_b": pair[1],
                    "independence_status": "distinct_registered_files_independence_not_proven",
                    "dates": [],
                    "valid_comparison_count": 0,
                    "agreement_count": 0,
                    "genuine_conflict_count": 0,
                },
            )
            item["dates"].append(trading_date)
            item["valid_comparison_count"] += 1
            item[f"{status}_count"] += 1

    overlap_rows = []
    for item in overlap.values():
        count = int(item["valid_comparison_count"])
        dates = list(item.pop("dates"))
        item["overlap_start"] = min(dates)
        item["overlap_end"] = max(dates)
        item["overlap_calendar_days"] = (
            date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))
        ).days + 1
        item["agreement_rate"] = round(int(item["agreement_count"]) / count, 6)
        overlap_rows.append(item)

    factor_changes = _factor_changes(observations)
    observations_by_source_row_id = {str(item["source_row_id"]): item for item in observations}
    action_audit: list[dict[str, Any]] = []
    lifecycle_material: set[tuple[str, str]] = set()
    for row in actions:
        evidence_ids = json.loads(str(row["evidence"]))
        previous_id, current_id = evidence_ids[0], evidence_ids[-1]
        previous = observations_by_source_row_id.get(previous_id)
        current = observations_by_source_row_id.get(current_id)
        gap_days = (
            (
                date.fromisoformat(str(current["trading_date"]))
                - date.fromisoformat(str(previous["trading_date"]))
            ).days
            if previous and current
            else 0
        )
        symbol = str(row["normalized_symbol"])
        trading_date = str(row["trading_date"])
        cause, status = classify_corporate_action_candidate(
            gap_days=gap_days,
            adjustment_factor_changed=(symbol, trading_date) in factor_changes,
            conflicting_duplicate=(
                symbol,
                trading_date,
                str(current["adjustment_status"]) if current else "",
            )
            in conflicting_keys,
            registered_evidence=False,
        )
        if status == "suspension_candidate":
            lifecycle_material.add((symbol, trading_date))
        likely_action = (symbol, trading_date) in factor_changes and str(row["candidate_type"]) in {
            "probable_split",
            "probable_bonus_share_adjustment",
            "probable_dividend_adjustment",
        }
        action_audit.append(
            {
                **row,
                "gap_days": gap_days,
                "audit_cause": cause,
                "conservative_status": status,
                "registered_evidence": False,
                "multiple_supporting_signals": likely_action,
                "approved": False,
            }
        )

    action_by_date: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in action_audit:
        action_by_date[(str(row["normalized_symbol"]), str(row["trading_date"]))].add(
            str(row["conservative_status"])
        )

    logical: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in representatives:
        logical[
            (
                str(row["normalized_symbol"]),
                str(row["trading_date"]),
                str(row["adjustment_status"]),
            )
        ].append(row)
    candidates: list[dict[str, Any]] = []
    independent_confirmed_keys: set[tuple[str, str, str]] = set()
    for key, rows in sorted(logical.items()):
        symbol, trading_date, adjustment = key
        best_score = max(
            (source_scores.get(str(row["source_name"]), Decimal("0")) for row in rows),
            default=Decimal("0"),
        )
        best_count = max((source_counts[str(row["source_name"])] for row in rows), default=0)
        tier_3_reasons: list[str] = []
        if adjustment not in KNOWN_GRAINS:
            tier_3_reasons.append("adjustment_documentation_incomplete")
        if best_score < HIGH_QUALITY_MINIMUM or best_count < HIGH_QUALITY_MINIMUM_ROWS:
            tier_3_reasons.extend(("provenance_weaker", "source_quality_below_tier2"))
        if all(
            Decimal(
                str(
                    source_quality.get(str(row["source_name"]), {})
                    .get("components", {})
                    .get("timestamp_provenance", 0)
                )
            )
            < Decimal("50")
            for row in rows
        ):
            tier_3_reasons.append("timestamp_trust_weaker")
        high_mapping = all(
            row["mapping_confidence"] == "high"
            and row["mapping_approval_status"] not in {"rejected", "under_review"}
            for row in rows
        )
        status, reason_codes = classify_final_disposition(
            accepted=True,
            complete_lineage=all(_complete_lineage(row) for row in rows),
            valid_ohlc=all(_valid_ohlc(row) for row in rows),
            known_adjustment=adjustment in KNOWN_GRAINS,
            high_confidence_mapping=high_mapping,
            independent_agreement=key in independent_confirmed_keys,
            high_quality_source=(
                best_score >= HIGH_QUALITY_MINIMUM and best_count >= HIGH_QUALITY_MINIMUM_ROWS
            ),
            unresolved_conflict=key in genuine_keys,
            lifecycle_hold=(symbol, trading_date) in lifecycle_material,
            corporate_action_hold=(
                "evidence_supported_candidate" in action_by_date[(symbol, trading_date)]
            ),
            duplicate_conflict=key in conflicting_keys,
            tier_3_reasons=tier_3_reasons,
        )
        candidates.append(
            {
                "logical_row_id": _canonical_hash(
                    {"symbol": symbol, "date": trading_date, "adjustment_status": adjustment}
                ),
                "symbol": symbol,
                "date": trading_date,
                "adjustment_status": adjustment,
                "status": status,
                "final_disposition": status,
                "diagnostic_reason_codes": reason_codes,
                "source_row_ids": [str(row["source_row_id"]) for row in rows],
                "source_hashes": sorted({str(row["source_hash"]) for row in rows}),
                "source_names": sorted({str(row["source_name"]) for row in rows}),
                "active": False,
            }
        )

    for row in invalid_observations:
        symbol = str(row["normalized_symbol"])
        trading_date = str(row["trading_date"])
        adjustment = str(row["adjustment_status"])
        status, reason_codes = classify_final_disposition(
            accepted=False,
            complete_lineage=_complete_lineage(row),
            valid_ohlc=_valid_ohlc(row),
            known_adjustment=adjustment in KNOWN_GRAINS,
            high_confidence_mapping=False,
            independent_agreement=False,
            high_quality_source=False,
            unresolved_conflict=False,
            lifecycle_hold=False,
            corporate_action_hold=False,
            duplicate_conflict=False,
        )
        candidates.append(
            {
                "logical_row_id": _canonical_hash(
                    {
                        "source_row_id": row["source_row_id"],
                        "symbol": symbol,
                        "date": trading_date,
                        "adjustment_status": adjustment,
                    }
                ),
                "symbol": symbol,
                "date": trading_date,
                "adjustment_status": adjustment,
                "status": status,
                "final_disposition": status,
                "diagnostic_reason_codes": reason_codes,
                "source_row_ids": [str(row["source_row_id"])],
                "source_hashes": [str(row["source_hash"])],
                "source_names": [str(row["source_name"])],
                "active": False,
            }
        )
    candidates.sort(key=lambda row: (row["symbol"], row["date"], row["logical_row_id"]))

    lifecycle = _lifecycle(observations)
    conflict_approval_records: list[dict[str, Any]] = []
    for comparison in corrected:
        if comparison["status"] != "genuine_conflict":
            continue
        left = observations_by_source_row_id[str(comparison["source_row_id_a"])]
        right = observations_by_source_row_id[str(comparison["source_row_id_b"])]
        basis = {
            "symbol": comparison["symbol"],
            "date": comparison["date"],
            "source_row_id_a": comparison["source_row_id_a"],
            "source_row_id_b": comparison["source_row_id_b"],
        }

        def source_record(label: str, row: dict[str, Any]) -> dict[str, Any]:
            return {
                "label": label,
                "source_name": str(row["source_name"]),
                "source_dataset_id": str(row["source_dataset_id"]),
                "source_row_id": str(row["source_row_id"]),
                "file_hash": str(row["source_hash"]),
                "adjustment_grain": str(row["adjustment_status"]),
                "raw_ohlcv": {field: row.get(field) for field in (*PRICE_FIELDS, "volume")},
                "source_quality_score": str(
                    source_scores.get(str(row["source_name"]), Decimal("0"))
                ),
                "surrounding_dates": _surrounding_dates(observations, row),
            }

        conflict_approval_records.append(
            {
                "approval_record_id": _approval_id("conflict", basis),
                "queue_type": "genuine_eligible_source_conflict",
                "symbol": comparison["symbol"],
                "date": comparison["date"],
                "source_a": source_record("source_a", left),
                "source_b": source_record("source_b", right),
                "percentage_differences": comparison["price_percentage_differences"],
                "possible_explanations": [
                    "source correction or transcription difference",
                    "unregistered vendor transformation",
                    "corporate-action treatment difference without registered supporting evidence",
                ],
                "allowed_recommendations": [
                    "accept_source_a",
                    "accept_source_b",
                    "reject_both",
                    "hold_for_review",
                ],
                "recommended_action": "hold_for_review",
                "reviewer_decision": "",
                "operator_decision": "",
            }
        )
    lifecycle_approval_records = [
        {
            "approval_record_id": _approval_id(
                "lifecycle",
                {"symbol": row["symbol"], "window": row["conservative_research_window"]},
            ),
            "queue_type": "material_lifecycle_decision",
            "symbol": row["symbol"],
            "observed_boundary_or_interval": {
                "observed_first_date": row["observed_first_date"],
                "observed_last_date": row["observed_last_date"],
                "conservative_research_window": row["conservative_research_window"],
            },
            "eligibility_effect": (
                "Official listing, first-trade, delisting, suspension, resumption, rename, "
                "and instrument evidence is absent; observed dates cannot establish legal lifecycle."
            ),
            "official_evidence_found": [],
            "secondary_evidence_found": row["trusted_secondary_evidence"],
            "observed_data_evidence": {
                "first": row["observed_first_date"],
                "last": row["observed_last_date"],
                "status": row["lifecycle_status"],
            },
            "conservative_inclusion_option": (
                "Include only the stated conservative research window after separate human approval; "
                "make no listing-date claim."
            ),
            "conservative_exclusion_option": "Exclude the symbol until official lifecycle evidence is registered.",
            "reviewer_decision": "",
            "operator_decision": "",
        }
        for row in lifecycle
    ]
    review_queue = [*conflict_approval_records, *lifecycle_approval_records]

    symbol_summary: list[dict[str, Any]] = []
    for symbol in PILOT_SYMBOLS:
        symbol_candidates = [row for row in candidates if row["symbol"] == symbol]
        statuses = Counter(str(row["status"]) for row in symbol_candidates)
        tier_3_reason_counts = Counter(
            reason
            for row in symbol_candidates
            if row["status"] == "tier_3_research_only"
            for reason in row["diagnostic_reason_codes"]
        )
        known_keys = {
            (str(row["symbol"]), str(row["date"]), str(row["adjustment_status"]))
            for row in symbol_candidates
            if row["adjustment_status"] in KNOWN_GRAINS
        }
        validated_keys = {key for key in confirmed_keys | genuine_keys if key[0] == symbol}
        reconciliation = {status: statuses[status] for status in FINAL_DISPOSITIONS}
        equation_right = sum(reconciliation.values())
        symbol_summary.append(
            {
                "symbol": symbol,
                "raw_rows": raw_counts[symbol],
                "logical_rows": len(symbol_candidates),
                "eligible_logical_rows": len(symbol_candidates) - statuses["rejected_invalid"],
                "duplicate_groups": sum(1 for row in duplicate_ledger if row["symbol"] == symbol),
                "exact_duplicates_collapsed": exact_collapsed[symbol],
                "comparison_pairs": sum(
                    1 for row in (*ineligible, *corrected) if row["symbol"] == symbol
                ),
                "ineligible_comparisons": sum(1 for row in ineligible if row["symbol"] == symbol),
                "genuine_conflict_pairs": sum(
                    1
                    for row in corrected
                    if row["symbol"] == symbol and row["status"] == "genuine_conflict"
                ),
                "genuine_conflicts": sum(
                    1
                    for row in corrected
                    if row["symbol"] == symbol and row["status"] == "genuine_conflict"
                ),
                "corporate_action_holds": statuses["held_corporate_action"],
                "lifecycle_holds": statuses["held_lifecycle"],
                "invalid_rows": invalid_counts[symbol],
                "tier_1_cross_source_confirmed": statuses["tier_1_cross_source_confirmed"],
                "tier_2_single_source_high_quality": statuses["tier_2_single_source_high_quality"],
                "tier_3_research_only": statuses["tier_3_research_only"],
                "held_genuine_conflict": statuses["held_genuine_conflict"],
                "rejected_duplicate_conflict": statuses["rejected_duplicate_conflict"],
                "rejected_invalid": statuses["rejected_invalid"],
                "tier_3_reason_counts": {
                    reason: tier_3_reason_counts[reason] for reason in TIER_3_REASON_CODES
                },
                "final_disposition_counts": reconciliation,
                "reconciliation_equation": {
                    "logical_rows": len(symbol_candidates),
                    "disposition_sum": equation_right,
                    "balanced": len(symbol_candidates) == equation_right,
                },
                "validation_comparison_dates": len(validated_keys),
                "validation_unavailable_logical_rows": len(known_keys - validated_keys),
                "validation_independence": "distinct_registered_files_independence_not_proven",
                "long_coverage_source_only_usable_for_most_periods": True,
                "tier_2_justified": statuses["tier_2_single_source_high_quality"] > 0,
                "activation_review_status": "human_decision_required",
            }
        )

    combined_dispositions = Counter(str(row["status"]) for row in candidates)
    combined_reconciliation = {
        status: combined_dispositions[status] for status in FINAL_DISPOSITIONS
    }
    if any(not row["reconciliation_equation"]["balanced"] for row in symbol_summary):
        raise RuntimeError("Per-symbol final-disposition reconciliation failed")
    if len(candidates) != sum(combined_reconciliation.values()):
        raise RuntimeError("Combined final-disposition reconciliation failed")

    source_hierarchy = sorted(
        (
            {
                "source_name": name,
                "quality_score": str(Decimal(str(row["score"]))),
                "truth_established": bool(row.get("truth_established", False)),
                "use": row.get("use"),
            }
            for name, row in source_quality.items()
        ),
        key=lambda row: (-Decimal(str(row["quality_score"])), str(row["source_name"])),
    )
    symbol_readiness = []
    for summary in symbol_summary:
        symbol = str(summary["symbol"])
        symbol_readiness.append(
            {
                "symbol": symbol,
                "priority": symbol in {"BATBC", "SQURPHARMA"},
                "status": "human_decision_required",
                "final_reconciled_counts": summary["final_disposition_counts"],
                "tier_3_reasons": summary["tier_3_reason_counts"],
                "source_hierarchy": source_hierarchy,
                "lifecycle_uncertainty": "lifecycle_evidence_pending",
                "corporate_action_holds": summary["corporate_action_holds"],
                "invalid_rows": summary["rejected_invalid"],
                "independent_validation_coverage": {
                    "status": "distinct_registered_files_independence_not_proven",
                    "eligible_independent_dates": 0,
                    "distinct_file_comparison_dates": summary["validation_comparison_dates"],
                },
                "activation_blockers": [
                    "pilot activation was not requested or granted",
                    "symbol lifecycle decision is blank",
                    "source derivation independence is not proven",
                    "Tier-3 rows remain ineligible by default",
                    *(
                        ["genuine source conflict decisions are blank"]
                        if summary["held_genuine_conflict"]
                        else []
                    ),
                ],
            }
        )

    primary_causes = Counter(row["primary_root_cause"] for row in baseline)
    contributing_causes = Counter(
        cause for row in baseline for cause in row["contributing_root_causes"]
    )
    action_audit_counts = {
        "adjusted_unadjusted_divergence": sum(
            row["audit_cause"] == "adjusted_unadjusted_divergence" for row in action_audit
        ),
        "duplicate_source_divergence": sum(
            row["audit_cause"] == "duplicate_source_divergence" for row in action_audit
        ),
        "ordinary_price_movement": sum(
            row["audit_cause"] == "ordinary_price_movement" for row in action_audit
        ),
        "missing_session_discontinuity": sum(
            row["audit_cause"] == "missing_session_discontinuity" for row in action_audit
        ),
        "long_source_gap": sum(row["audit_cause"] == "long_source_gap" for row in action_audit),
        "suspension_candidate": sum(
            row["conservative_status"] == "suspension_candidate" for row in action_audit
        ),
        "likely_corporate_action": sum(
            bool(row["multiple_supporting_signals"]) for row in action_audit
        ),
        "supported_by_registered_evidence": sum(
            bool(row["registered_evidence"]) for row in action_audit
        ),
        "insufficient_evidence": sum(
            row["conservative_status"] == "insufficient_evidence" for row in action_audit
        ),
    }
    return {
        "scope": list(PILOT_SYMBOLS),
        "qualification": "0/60",
        "activation": False,
        "strategy_execution": False,
        "comparison_tolerance": str(PRICE_TOLERANCE),
        "comparison_eligibility_matrix": list(COMPARISON_ELIGIBILITY_MATRIX),
        "tier_definitions": TIER_DEFINITIONS,
        "tier_3_reason_codes": list(TIER_3_REASON_CODES),
        "baseline_conflicts": baseline,
        "baseline_root_causes": {cause: primary_causes[cause] for cause in ROOT_CAUSE_CATEGORIES},
        "baseline_contributing_causes": {
            cause: contributing_causes[cause] for cause in ROOT_CAUSE_CATEGORIES
        },
        "duplicate_collapse_ledger": duplicate_ledger,
        "ineligible_comparisons": ineligible,
        "corrected_comparisons": corrected,
        "corporate_action_audit": action_audit,
        "corporate_action_audit_counts": action_audit_counts,
        "lifecycle_evidence": lifecycle,
        "source_overlap": sorted(
            overlap_rows,
            key=lambda row: (row["symbol"], row["adjustment_status"], row["source_a"]),
        ),
        "candidates": candidates,
        "conflict_approval_records": conflict_approval_records,
        "lifecycle_approval_records": lifecycle_approval_records,
        "human_review_queue": review_queue,
        "symbol_summary": symbol_summary,
        "symbol_readiness": sorted(
            symbol_readiness,
            key=lambda row: (not row["priority"], PILOT_SYMBOLS.index(row["symbol"])),
        ),
        "source_hierarchy": source_hierarchy,
        "proposed_activation_policy": {
            "status": "REJECTED / NOT GRANTED",
            "active": False,
            "eligible_by_default": [
                "tier_1_cross_source_confirmed",
                "tier_2_single_source_high_quality",
            ],
            "ineligible_by_default": [
                "tier_3_research_only",
                "held_*",
                "rejected_*",
            ],
            "tier_3_exception": "separate explicit human authorization by reason category",
        },
        "totals": {
            "baseline_conflicts": len(baseline),
            "genuine_conflicts": sum(1 for row in corrected if row["status"] == "genuine_conflict"),
            "raw_source_rows": len(observations),
            "logical_rows": len(candidates),
            "duplicate_groups": len(duplicate_ledger),
            "comparison_pairs": len(ineligible) + len(corrected),
            "genuine_conflict_pairs": sum(
                1 for row in corrected if row["status"] == "genuine_conflict"
            ),
            "ineligible_comparisons": len(ineligible),
            "exact_duplicates_collapsed": sum(exact_collapsed.values()),
            "corporate_action_candidates": len(action_audit),
            "human_review_queue": len(review_queue),
            "review_queue_manageable": len(review_queue) <= 500,
            "final_disposition_counts": combined_reconciliation,
            "tier_counts": dict(Counter(row["status"] for row in candidates)),
            "tier_3_reason_counts": dict(
                Counter(
                    reason
                    for row in candidates
                    if row["status"] == "tier_3_research_only"
                    for reason in row["diagnostic_reason_codes"]
                )
            ),
            "reconciliation_equation": {
                "logical_rows": len(candidates),
                "disposition_sum": sum(combined_reconciliation.values()),
                "balanced": len(candidates) == sum(combined_reconciliation.values()),
            },
        },
    }

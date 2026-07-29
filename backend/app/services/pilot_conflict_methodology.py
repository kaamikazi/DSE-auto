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


def _source_scores(source_quality_path: Path) -> dict[str, Decimal]:
    rows = json.loads(source_quality_path.read_text(encoding="utf-8"))
    aliases = {
        "Mendeley 23553sm4tn v4 adjusted": "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / adjusted",
        "Mendeley 23553sm4tn v4 unadjusted": "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / unadjusted",
        "Mendeley 5mww8rb9td v1 historical": "Dhaka Stock Exchange Historical Data (1999-2025) - DSE_Data.csv",
    }
    return {
        aliases.get(str(row["logical_name"]), str(row["logical_name"])): Decimal(str(row["score"]))
        for row in rows
        if row.get("score") is not None
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
    source_scores = _source_scores(source_quality_path)
    source_counts = _source_counts(observations)

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    invalid_counts: Counter[str] = Counter()
    raw_counts: Counter[str] = Counter()
    for row in observations:
        symbol = str(row["normalized_symbol"])
        raw_counts[symbol] += 1
        if not int(row["accepted_for_candidate"]):
            invalid_counts[symbol] += 1
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
    for key, rows in sorted(logical.items()):
        symbol, trading_date, adjustment = key
        if key in genuine_keys or key in conflicting_keys:
            status = "held_genuine_conflict"
        elif (symbol, trading_date) in lifecycle_material:
            status = "held_lifecycle"
        elif "evidence_supported_candidate" in action_by_date[(symbol, trading_date)]:
            status = "held_corporate_action"
        elif key in confirmed_keys:
            status = "tier_1_cross_source_confirmed"
        else:
            best_score = max(
                (source_scores.get(str(row["source_name"]), Decimal("0")) for row in rows),
                default=Decimal("0"),
            )
            best_count = max((source_counts[str(row["source_name"])] for row in rows), default=0)
            status = (
                "tier_2_single_source_high_quality"
                if adjustment in KNOWN_GRAINS
                and best_score >= HIGH_QUALITY_MINIMUM
                and best_count >= HIGH_QUALITY_MINIMUM_ROWS
                else "tier_3_research_only"
            )
        candidates.append(
            {
                "symbol": symbol,
                "date": trading_date,
                "adjustment_status": adjustment,
                "status": status,
                "source_row_ids": [str(row["source_row_id"]) for row in rows],
                "source_hashes": sorted({str(row["source_hash"]) for row in rows}),
                "source_names": sorted({str(row["source_name"]) for row in rows}),
                "active": False,
            }
        )

    lifecycle = _lifecycle(observations)
    review_queue = [
        {
            "queue_type": "genuine_eligible_source_conflict",
            "symbol": row["symbol"],
            "date": row["date"],
            "detail": f"{row['source_a']} versus {row['source_b']} ({row['adjustment_a']})",
            "reviewer_decision": "",
            "operator_decision": "",
        }
        for row in corrected
        if row["status"] == "genuine_conflict"
    ]
    review_queue.extend(
        {
            "queue_type": "material_lifecycle_decision",
            "symbol": row["symbol"],
            "date": "",
            "detail": "Official listing, delisting, suspension, resumption, rename, and instrument evidence unavailable",
            "reviewer_decision": "",
            "operator_decision": "",
        }
        for row in lifecycle
    )

    symbol_summary = []
    for symbol in PILOT_SYMBOLS:
        symbol_candidates = [row for row in candidates if row["symbol"] == symbol]
        statuses = Counter(str(row["status"]) for row in symbol_candidates)
        known_keys = {
            (str(row["symbol"]), str(row["date"]), str(row["adjustment_status"]))
            for row in symbol_candidates
            if row["adjustment_status"] in KNOWN_GRAINS
        }
        validated_keys = {key for key in confirmed_keys | genuine_keys if key[0] == symbol}
        symbol_summary.append(
            {
                "symbol": symbol,
                "raw_rows": raw_counts[symbol],
                "eligible_logical_rows": len(symbol_candidates),
                "exact_duplicates_collapsed": exact_collapsed[symbol],
                "ineligible_comparisons": sum(1 for row in ineligible if row["symbol"] == symbol),
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
                "same_source_duplicate_holds": sum(
                    1 for key in conflicting_keys if key[0] == symbol and key not in genuine_keys
                ),
                "validation_comparison_dates": len(validated_keys),
                "validation_unavailable_logical_rows": len(known_keys - validated_keys),
                "validation_independence": "distinct_registered_files_independence_not_proven",
                "long_coverage_source_only_usable_for_most_periods": True,
                "tier_2_justified": statuses["tier_2_single_source_high_quality"] > 0,
                "activation_review_status": "methodology_review_only_not_ready",
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
        "human_review_queue": review_queue,
        "symbol_summary": symbol_summary,
        "totals": {
            "baseline_conflicts": len(baseline),
            "genuine_conflicts": sum(1 for row in corrected if row["status"] == "genuine_conflict"),
            "ineligible_comparisons": len(ineligible),
            "exact_duplicates_collapsed": sum(exact_collapsed.values()),
            "corporate_action_candidates": len(action_audit),
            "human_review_queue": len(review_queue),
            "review_queue_manageable": len(review_queue) <= 500,
            "tier_counts": dict(Counter(row["status"] for row in candidates)),
        },
    }

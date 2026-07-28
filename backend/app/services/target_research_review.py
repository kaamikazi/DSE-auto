from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import groupby
from pathlib import Path
from typing import Any

TARGET_SYMBOLS = ("ACI", "BRACBANK", "DSEX", "GP")
PRICE_FIELDS = ("open", "high", "low", "close")
PRICE_RELATIVE_TOLERANCE = Decimal("0.001")
VOLUME_RELATIVE_TOLERANCE = Decimal("0.02")
SOURCE_PRIORITY = {
    "adjusted": (
        "End-of-Day Financial Dataset with Coverage Metadata / adjusted",
        "AmarStock adjusted",
    ),
    "unadjusted": (
        "End-of-Day Financial Dataset with Coverage Metadata / unadjusted",
        "Dhaka Stock Exchange DSE 2021 yearly CSV",
        "AmarStock unadjusted",
    ),
}


@dataclass(frozen=True)
class ComparisonDecision:
    eligible: bool
    classification: str
    price_within_tolerance: bool
    volume_within_tolerance: bool
    price_max_relative_difference: str | None
    volume_relative_difference: str | None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def _relative(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right) / max(abs(left), abs(right), Decimal("0.0001"))


def compare_eligible_values(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    price_tolerance: Decimal = PRICE_RELATIVE_TOLERANCE,
    volume_tolerance: Decimal = VOLUME_RELATIVE_TOLERANCE,
) -> ComparisonDecision:
    if left.get("adjustment_status") != right.get("adjustment_status"):
        return ComparisonDecision(False, "adjusted_unadjusted_ineligible", False, False, None, None)
    if "under_review" in {
        left.get("mapping_approval_status"),
        right.get("mapping_approval_status"),
    }:
        return ComparisonDecision(False, "symbol_mapping_ineligible", False, False, None, None)
    price_differences: list[Decimal] = []
    for field in PRICE_FIELDS:
        a, b = _decimal(left.get(field)), _decimal(right.get(field))
        if a is None or b is None:
            return ComparisonDecision(False, "missing_comparable_price", False, False, None, None)
        price_differences.append(_relative(a, b))
    left_volume, right_volume = _decimal(left.get("volume")), _decimal(right.get("volume"))
    if left_volume is None or right_volume is None:
        return ComparisonDecision(False, "missing_comparable_volume", False, False, None, None)
    price_max = max(price_differences)
    volume_difference = _relative(left_volume, right_volume)
    prices_ok = price_max <= price_tolerance
    volume_ok = volume_difference <= volume_tolerance
    if prices_ok and volume_ok:
        classification = (
            "eligible_exact" if price_max == 0 and volume_difference == 0 else "eligible_tolerance"
        )
    elif prices_ok:
        classification = "volume_conflict"
    else:
        classification = "price_conflict"
    return ComparisonDecision(
        True,
        classification,
        prices_ok,
        volume_ok,
        str(price_max),
        str(volume_difference),
    )


def conservative_action_label(
    *,
    official_evidence: bool,
    adjustment_factor_discontinuity: bool,
    gap_days: int,
    source_scale_mismatch: bool,
    mapping_uncertain: bool,
) -> str:
    if official_evidence:
        return "official_evidence_requires_human_classification"
    if mapping_uncertain:
        return "insufficient_evidence"
    if adjustment_factor_discontinuity:
        return "adjustment_divergence"
    if gap_days > 7 or source_scale_mismatch:
        return "discontinuity_for_review"
    return "insufficient_evidence"


def audit_corporate_action_queue(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    actions = [dict(row) for row in connection.execute("SELECT * FROM corporate_action_candidates")]
    wanted_row_ids: set[tuple[str, str]] = set()
    for action in actions:
        for row_id in json.loads(action["evidence"]):
            wanted_row_ids.add((str(action["source_dataset_id"]), str(row_id)))
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    factors: defaultdict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(dict)
    for row in connection.execute(
        """SELECT source_dataset_id,source_row_id,source_name,normalized_symbol,trading_date,
        close,adjustment_status,invalid_categories,mapping_approval_status
        FROM observations ORDER BY id"""
    ):
        key = (str(row["source_dataset_id"]), str(row["source_row_id"]))
        if key not in wanted_row_ids:
            continue
        item = dict(row)
        observations[key] = item
        close = _decimal(item["close"])
        if close is not None:
            factors[(item["source_dataset_id"], item["normalized_symbol"], item["trading_date"])][
                item["adjustment_status"]
            ] = close

    old_labels: Counter[str] = Counter()
    cause_counts: Counter[str] = Counter(
        {
            "adjusted_unadjusted_comparison_by_design": 0,
            "duplicate_source_rows": 0,
            "malformed_ohlc_rows": 0,
            "ordinary_price_movement": 0,
            "missing_previous_trading_day": 0,
            "source_scale_differences": 0,
            "symbol_mapping_errors": 0,
            "genuine_adjustment_factor_changes": 0,
            "actual_corporate_action_evidence": 0,
            "unresolved_cases": 0,
        }
    )
    revised: Counter[str] = Counter()
    revised_rows: list[dict[str, Any]] = []
    for action in actions:
        old_labels[str(action["candidate_type"])] += 1
        evidence = json.loads(action["evidence"])
        previous = observations.get((str(action["source_dataset_id"]), str(evidence[0])))
        current = observations.get((str(action["source_dataset_id"]), str(evidence[1])))
        if previous is None or current is None:
            cause_counts["unresolved_cases"] += 1
            label = "insufficient_evidence"
            gap_days = 0
            factor_change = False
            scale = False
            mapping_uncertain = True
        else:
            gap_days = (
                date.fromisoformat(str(current["trading_date"]))
                - date.fromisoformat(str(previous["trading_date"]))
            ).days
            previous_close = _decimal(previous["close"]) or Decimal("0")
            current_close = _decimal(current["close"]) or Decimal("0")
            ratio = (
                max(previous_close, current_close) / min(previous_close, current_close)
                if previous_close > 0 and current_close > 0
                else Decimal("0")
            )
            scale = any(
                abs(ratio - expected) / expected <= Decimal("0.02")
                for expected in (Decimal("10"), Decimal("100"), Decimal("1000"))
            )
            mapping_uncertain = "under_review" in {
                previous["mapping_approval_status"],
                current["mapping_approval_status"],
            }
            invalid = set(json.loads(previous["invalid_categories"])) | set(
                json.loads(current["invalid_categories"])
            )
            if invalid:
                cause_counts["malformed_ohlc_rows"] += 1
            if gap_days > 7:
                cause_counts["missing_previous_trading_day"] += 1
            if scale:
                cause_counts["source_scale_differences"] += 1
            if mapping_uncertain:
                cause_counts["symbol_mapping_errors"] += 1
            previous_factor_values = factors.get(
                (
                    str(previous["source_dataset_id"]),
                    str(previous["normalized_symbol"]),
                    str(previous["trading_date"]),
                ),
                {},
            )
            current_factor_values = factors.get(
                (
                    str(current["source_dataset_id"]),
                    str(current["normalized_symbol"]),
                    str(current["trading_date"]),
                ),
                {},
            )
            previous_factor = None
            current_factor = None
            if {"adjusted", "unadjusted"}.issubset(previous_factor_values):
                previous_factor = (
                    previous_factor_values["adjusted"] / previous_factor_values["unadjusted"]
                )
            if {"adjusted", "unadjusted"}.issubset(current_factor_values):
                current_factor = (
                    current_factor_values["adjusted"] / current_factor_values["unadjusted"]
                )
            factor_change = bool(
                previous_factor is not None
                and current_factor is not None
                and _relative(previous_factor, current_factor) > Decimal("0.01")
            )
            if factor_change:
                cause_counts["genuine_adjustment_factor_changes"] += 1
            label = conservative_action_label(
                official_evidence=False,
                adjustment_factor_discontinuity=factor_change,
                gap_days=gap_days,
                source_scale_mismatch=scale,
                mapping_uncertain=mapping_uncertain,
            )
            if label == "insufficient_evidence":
                cause_counts["unresolved_cases"] += 1
        revised[label] += 1
        revised_rows.append(
            {
                **action,
                "old_classification": action["candidate_type"],
                "revised_classification": label,
                "gap_days": gap_days,
                "adjustment_factor_discontinuity": factor_change,
                "source_scale_mismatch": scale,
                "mapping_uncertain": mapping_uncertain,
                "official_evidence": False,
                "review_status": "under_review",
            }
        )
    return {
        "old_classification_counts": dict(old_labels),
        "cause_counts": dict(cause_counts),
        "revised_classification_counts": dict(revised),
        "false_positive_finding": (
            "The old detector inferred event types from a single close-to-close ratio. "
            "No candidate carried official announcement evidence, so no probable bonus, split, "
            "rights, dividend, or suspension label survives the conservative audit."
        ),
        "rows": revised_rows,
    }


def _scale_mismatch(values_a: dict[str, Any], values_b: dict[str, Any]) -> bool:
    ratios: list[Decimal] = []
    for field in PRICE_FIELDS:
        a, b = _decimal(values_a.get(field)), _decimal(values_b.get(field))
        if a is None or b is None or min(abs(a), abs(b)) == 0:
            continue
        ratios.append(max(abs(a), abs(b)) / min(abs(a), abs(b)))
    return bool(ratios) and all(
        any(
            abs(ratio - expected) / expected <= Decimal("0.02")
            for expected in (Decimal("10"), Decimal("100"), Decimal("1000"))
        )
        for ratio in ratios
    )


def segment_cross_source_conflicts(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    action_dates = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT DISTINCT normalized_symbol,trading_date FROM corporate_action_candidates"
        )
    }
    mapping_rows = list(
        connection.execute(
            "SELECT normalized_symbol,approval_status FROM symbol_mappings WHERE normalized_symbol<>''"
        )
    )
    statuses: defaultdict[str, set[str]] = defaultdict(set)
    for symbol, status in mapping_rows:
        statuses[str(symbol)].add(str(status))
    wholly_uncertain = {symbol for symbol, values in statuses.items() if values == {"under_review"}}
    segments: Counter[str] = Counter(
        {
            "adjusted_versus_unadjusted_comparison": 0,
            "price_scale_mismatch": 0,
            "rounding_difference": 0,
            "date_alignment_mismatch": 0,
            "duplicate_source_conflict": 0,
            "corporate_action_period": 0,
            "symbol_mapping_problem": 0,
            "volume_unit_mismatch": 0,
            "real_unexplained_conflict": 0,
        }
    )
    eligible_counts: Counter[str] = Counter()
    total_outside = 0
    for row in connection.execute(
        "SELECT * FROM cross_source_comparisons WHERE tolerance_result='outside_tolerance'"
    ):
        total_outside += 1
        if row["adjustment_a"] != row["adjustment_b"]:
            segments["adjusted_versus_unadjusted_comparison"] += 1
            eligible_counts["ineligible_adjustment_mismatch"] += 1
            continue
        if row["normalized_symbol"] in wholly_uncertain:
            segments["symbol_mapping_problem"] += 1
            eligible_counts["ineligible_symbol_mapping"] += 1
            continue
        if row["source_a"] == row["source_b"] and row["source_name_a"] == row["source_name_b"]:
            segments["duplicate_source_conflict"] += 1
            eligible_counts["ineligible_same_source"] += 1
            continue
        values_a = json.loads(row["values_a"])
        values_b = json.loads(row["values_b"])
        if _scale_mismatch(values_a, values_b):
            segments["price_scale_mismatch"] += 1
            eligible_counts["eligible_but_conflicting"] += 1
            continue
        decision = compare_eligible_values(
            {**values_a, "adjustment_status": row["adjustment_a"]},
            {**values_b, "adjustment_status": row["adjustment_b"]},
        )
        if decision.price_within_tolerance and not decision.volume_within_tolerance:
            left_volume, right_volume = (
                _decimal(values_a.get("volume")),
                _decimal(values_b.get("volume")),
            )
            ratio = (
                max(abs(left_volume), abs(right_volume)) / min(abs(left_volume), abs(right_volume))
                if left_volume and right_volume and min(abs(left_volume), abs(right_volume)) > 0
                else Decimal("0")
            )
            if ratio >= Decimal("10"):
                segments["volume_unit_mismatch"] += 1
            else:
                segments["rounding_difference"] += 1
            eligible_counts["eligible_but_conflicting"] += 1
        elif (row["normalized_symbol"], row["trading_date"]) in action_dates:
            segments["corporate_action_period"] += 1
            eligible_counts["eligible_but_conflicting"] += 1
        else:
            segments["real_unexplained_conflict"] += 1
            eligible_counts["eligible_but_conflicting"] += 1
    return {
        "outside_tolerance_total": total_outside,
        "segments": dict(segments),
        "comparison_eligibility": dict(eligible_counts),
        "field_tolerances": {
            "ohlc_relative": str(PRICE_RELATIVE_TOLERANCE),
            "volume_relative": str(VOLUME_RELATIVE_TOLERANCE),
            "number_of_trades": "exact_or_separately_classified_when_available",
            "adjusted_unadjusted": "ineligible",
            "uncertain_symbol_mapping": "ineligible",
        },
    }


def _source_rank(source_name: str, adjustment_status: str) -> int:
    for rank, marker in enumerate(SOURCE_PRIORITY.get(adjustment_status, ())):
        if marker.lower() in source_name.lower():
            return rank
    return 99


def build_target_subset(
    connection: sqlite3.Connection,
    *,
    source_scores: dict[str, float],
    source_urls: dict[str, str],
) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in TARGET_SYMBOLS)
    cursor = connection.execute(
        f"""SELECT * FROM observations WHERE normalized_symbol IN ({placeholders})
        ORDER BY normalized_symbol,trading_date,adjustment_status,id""",  # noqa: S608
        TARGET_SYMBOLS,
    )

    def key(row: sqlite3.Row) -> tuple[str, str | None, str]:
        return (row["normalized_symbol"], row["trading_date"], row["adjustment_status"])

    candidates: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    readiness: dict[str, dict[str, Any]] = {
        symbol: {
            "symbol": symbol,
            "contributing_datasets": set(),
            "coverage_dates": [],
            "duplicate_groups": 0,
            "invalid_rows": 0,
            "conflicting_rows": 0,
            "adjusted_available": False,
            "unadjusted_available": False,
            "missing_trading_dates": 0,
            "suspected_corporate_actions": 0,
            "source_quality_scores": {},
            "unresolved_symbol_mappings": 0,
            "recommended_primary_series": SOURCE_PRIORITY,
            "recommended_secondary_validation_series": SOURCE_PRIORITY,
        }
        for symbol in TARGET_SYMBOLS
    }
    for group_key, raw_group in groupby(cursor, key=key):
        rows = [dict(row) for row in raw_group]
        symbol, trading_date, adjustment = group_key
        target = readiness[symbol]
        for row in rows:
            target["contributing_datasets"].add(row["source_name"])
            if trading_date:
                target["coverage_dates"].append(trading_date)
            if row["adjustment_status"] == "adjusted":
                target["adjusted_available"] = True
            if row["adjustment_status"] == "unadjusted":
                target["unadjusted_available"] = True
            if row["mapping_approval_status"] == "under_review":
                target["unresolved_symbol_mappings"] += 1
            if not row["accepted_for_candidate"]:
                target["invalid_rows"] += 1
            target["source_quality_scores"][row["source_name"]] = source_scores.get(
                row["source_name"], 0.0
            )
        valid = [
            row
            for row in rows
            if row["accepted_for_candidate"]
            and row["mapping_approval_status"] != "under_review"
            and adjustment in {"adjusted", "unadjusted"}
        ]
        if not valid:
            held.append(
                {
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "adjustment_status": adjustment,
                    "reason": "invalid_unknown_adjustment_or_mapping_under_review",
                }
            )
            continue
        representatives: dict[tuple[str, str], dict[str, Any]] = {}
        for row in valid:
            representatives.setdefault((row["source_name"], row["value_fingerprint"]), row)
        values = list(representatives.values())
        conflicts: list[ComparisonDecision] = []
        for index, left in enumerate(values):
            for right in values[index + 1 :]:
                if left["source_hash"] == right["source_hash"]:
                    continue
                conflicts.append(compare_eligible_values(left, right))
        if any(
            decision.eligible
            and (not decision.price_within_tolerance or not decision.volume_within_tolerance)
            for decision in conflicts
        ):
            target["conflicting_rows"] += len(values)
            held.append(
                {
                    "symbol": symbol,
                    "trading_date": trading_date,
                    "adjustment_status": adjustment,
                    "reason": "unresolved_eligible_source_conflict",
                }
            )
            continue
        selected = min(values, key=lambda row: _source_rank(row["source_name"], adjustment))
        distinct_hashes = {row["source_hash"] for row in values}
        if len(distinct_hashes) >= 2 and conflicts:
            tier = "tier_1_cross_source_confirmed"
        elif source_scores.get(selected["source_name"], 0.0) >= 70:
            tier = "tier_2_single_high_quality_source"
        else:
            tier = "tier_3_low_confidence_research_only"
        lineage = [
            {
                "source_dataset_id": row["source_dataset_id"],
                "source_file_hash": row["source_hash"],
                "source_row_identifier": row["source_row_id"],
                "source_url": source_urls.get(row["source_hash"]),
                "original_raw_values": {
                    field: row[f"raw_{field}"]
                    for field in ("date", "open", "high", "low", "close", "volume")
                }
                | {"symbol": row["original_symbol"]},
                "transformation_version": row["transformation_version"],
                "transformation_reason": row["transformation_reason"],
            }
            for row in valid
        ]
        candidates.append(
            {
                "symbol": symbol,
                "trading_date": trading_date,
                "open": selected["open"],
                "high": selected["high"],
                "low": selected["low"],
                "close": selected["close"],
                "volume": selected["volume"],
                "adjustment_status": adjustment,
                "selected_source": selected["source_name"],
                "selected_source_rule": list(SOURCE_PRIORITY[adjustment]),
                "quality_status": tier,
                "confidence": "medium" if tier.startswith("tier_1") else "low",
                "lineage": lineage,
                "review_status": "pending_human_approval",
                "active": False,
            }
        )

    duplicate_counts = dict(
        connection.execute(
            f"""SELECT normalized_symbol,COUNT(*) FROM duplicate_groups
            WHERE normalized_symbol IN ({placeholders}) GROUP BY normalized_symbol""",  # noqa: S608
            TARGET_SYMBOLS,
        )
    )
    action_counts = dict(
        connection.execute(
            f"""SELECT normalized_symbol,COUNT(*) FROM corporate_action_candidates
            WHERE normalized_symbol IN ({placeholders}) GROUP BY normalized_symbol""",  # noqa: S608
            TARGET_SYMBOLS,
        )
    )
    for symbol, target in readiness.items():
        dates = sorted(set(target.pop("coverage_dates")))
        target["coverage_start"] = dates[0] if dates else None
        target["coverage_end"] = dates[-1] if dates else None
        target["observed_rows"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE normalized_symbol=?", (symbol,)
            ).fetchone()[0]
        )
        target["duplicate_groups"] = duplicate_counts.get(symbol, 0)
        target["suspected_corporate_actions"] = action_counts.get(symbol, 0)
        target["candidate_rows"] = sum(row["symbol"] == symbol for row in candidates)
        target["held_rows"] = sum(row["symbol"] == symbol for row in held)
        target["missing_trading_dates"] = _missing_weekdays(dates)
        target["contributing_datasets"] = sorted(target["contributing_datasets"])
        target["research_readiness_status"] = "review_required"
        target["automatically_approved"] = False
    return {
        "policy": {
            "accepted_requirements": [
                "approved symbol mapping",
                "valid trading date and OHLC invariants",
                "known source and adjustment state",
                "no unresolved eligible duplicate or source conflict",
                "eligible field-specific comparison",
                "complete immutable lineage",
                "explicit inactive quality tier",
            ],
            "source_priority": SOURCE_PRIORITY,
            "activation": "blocked",
        },
        "candidate_counts": dict(Counter(row["quality_status"] for row in candidates)),
        "candidate_rows": candidates,
        "held_counts": dict(Counter(row["reason"] for row in held)),
        "held_rows": held,
        "target_readiness": [readiness[symbol] for symbol in TARGET_SYMBOLS],
    }


def _missing_weekdays(dates: list[str]) -> int:
    parsed = {date.fromisoformat(item) for item in dates if item}
    if not parsed:
        return 0
    cursor, end = min(parsed), max(parsed)
    expected = 0
    while cursor <= end:
        if cursor.weekday() not in {4, 5}:
            expected += 1
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return max(expected - len(parsed), 0)


def build_review_samples(
    connection: sqlite3.Connection,
    subset: dict[str, Any],
    action_audit: dict[str, Any],
    *,
    per_category: int = 10,
    source_urls: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    candidates = subset["candidate_rows"]
    held = subset["held_rows"]
    action_rows = action_audit["rows"]
    for symbol in TARGET_SYMBOLS:
        valid = [row for row in candidates if row["symbol"] == symbol]
        deterministic = sorted(
            valid,
            key=lambda row: hashlib.sha256(
                f"{row['symbol']}|{row['trading_date']}|{row['adjustment_status']}".encode()
            ).hexdigest(),
        )[:per_category]
        samples.extend(
            {"sample_type": "deterministic_random_valid", **row} for row in deterministic
        )
        symbol_held = [row for row in held if row["symbol"] == symbol][:per_category]
        samples.extend({"sample_type": "conflict_or_hold", **row} for row in symbol_held)
        symbol_actions = [row for row in action_rows if row["normalized_symbol"] == symbol][
            :per_category
        ]
        samples.extend(
            {"sample_type": "suspected_corporate_action", **row} for row in symbol_actions
        )
        if valid:
            ordered = sorted(valid, key=lambda row: (row["trading_date"], row["adjustment_status"]))
            samples.append({"sample_type": "first_date", **ordered[0]})
            samples.append({"sample_type": "last_date", **ordered[-1]})
            unique_dates = sorted({date.fromisoformat(row["trading_date"]) for row in valid})
            for previous, current in zip(unique_dates, unique_dates[1:], strict=False):
                gap = (current - previous).days
                if gap > 7:
                    samples.append(
                        {
                            "sample_type": "long_gap_boundary",
                            "symbol": symbol,
                            "previous_date": previous.isoformat(),
                            "current_date": current.isoformat(),
                            "gap_days": gap,
                        }
                    )
                    if (
                        sum(
                            row["sample_type"] == "long_gap_boundary" and row["symbol"] == symbol
                            for row in samples
                        )
                        >= per_category
                    ):
                        break
        for metric, json_path, sample_type in (
            ("percentage_differences", "$.close", "largest_price_discrepancy"),
            ("percentage_differences", "$.volume", "largest_volume_discrepancy"),
        ):
            rows = connection.execute(
                f"""SELECT * FROM cross_source_comparisons WHERE normalized_symbol=?
                AND adjustment_a=adjustment_b AND tolerance_result='outside_tolerance'
                ORDER BY CAST(json_extract({metric}, ?) AS REAL) DESC LIMIT ?""",  # noqa: S608
                (symbol, json_path, per_category),
            )
            for comparison in rows:
                item = dict(comparison)
                item["values_a"] = json.loads(item["values_a"])
                item["values_b"] = json.loads(item["values_b"])
                item["absolute_differences"] = json.loads(item["absolute_differences"])
                item["percentage_differences"] = json.loads(item["percentage_differences"])
                raw_sources: list[dict[str, Any]] = []
                for source_name in (item["source_name_a"], item["source_name_b"]):
                    source_row = connection.execute(
                        """SELECT source_hash,source_row_id,original_symbol,raw_date,raw_open,
                        raw_high,raw_low,raw_close,raw_volume FROM observations
                        WHERE normalized_symbol=? AND trading_date=? AND source_name=? LIMIT 1""",
                        (symbol, item["trading_date"], source_name),
                    ).fetchone()
                    if source_row:
                        source_item = dict(source_row)
                        source_item["source_url"] = (source_urls or {}).get(
                            source_item["source_hash"]
                        )
                        raw_sources.append(source_item)
                item["raw_source_evidence"] = raw_sources
                item["sample_type"] = sample_type
                samples.append(item)
        duplicate_rows = connection.execute(
            """SELECT * FROM duplicate_groups WHERE normalized_symbol=?
            ORDER BY trading_date LIMIT ?""",
            (symbol, per_category),
        )
        samples.extend({"sample_type": "duplicate_example", **dict(row)} for row in duplicate_rows)
    return samples


def open_candidate_database(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)

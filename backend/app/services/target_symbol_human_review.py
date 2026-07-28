from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.services.target_research_review import TARGET_SYMBOLS

MappingClass = Literal[
    "valid_dsex_index_row",
    "alternate_dsex_label",
    "another_index",
    "equity_symbol_collision",
    "malformed_symbol",
    "metadata_row",
    "duplicate_alias",
    "unresolved",
]

PRICE_FIELDS = ("open", "high", "low", "close")
OFFICIAL_REVIEW_EVIDENCE = (
    "DSE End-of-Day Data Product (official_document; applicability under review)",
    "DSE Data Sale Services (official_document; licensing and field definitions under review)",
    "DSE Automated Trading Regulations (official_document; version under review)",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _relative(left: Decimal, right: Decimal) -> Decimal:
    return abs(left - right) / max(abs(left), abs(right), Decimal("0.0001"))


def classify_dsex_alias(
    *,
    raw_symbol: str,
    instrument_class: str,
    mapping_reason: str,
    exact_literal_overlap: bool,
    same_source_literal_overlap: bool,
    value_scale_consistent: bool,
) -> MappingClass:
    normalized = raw_symbol.strip().upper()
    if not normalized or normalized in {"SYMBOL", "TRADINGCODE", "CODE", "DATE"}:
        return "metadata_row"
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&.-" for character in normalized):
        return "malformed_symbol"
    if normalized in {"DS30", "DSES", "DSE30"}:
        return "another_index"
    if normalized == "DSEX" and instrument_class == "equity":
        return "equity_symbol_collision"
    if normalized == "DSEX" and instrument_class == "index":
        return "valid_dsex_index_row"
    if normalized == "00DSEX":
        if instrument_class != "index":
            return "equity_symbol_collision"
        if same_source_literal_overlap:
            return "duplicate_alias"
        if (
            mapping_reason == "known_index_alias_candidate"
            and exact_literal_overlap
            and value_scale_consistent
        ):
            return "alternate_dsex_label"
        return "unresolved"
    if normalized.endswith("DSEX") and instrument_class == "index":
        return "duplicate_alias" if exact_literal_overlap else "unresolved"
    return "unresolved"


def build_dsex_mapping_review(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    literal_ranges = [
        dict(row)
        for row in connection.execute(
            """SELECT source_name,trading_date,close FROM observations
            WHERE original_symbol='DSEX' AND accepted_for_candidate=1"""
        )
    ]
    literal_by_date: defaultdict[str, list[Decimal]] = defaultdict(list)
    literal_by_source_date: defaultdict[tuple[str, str], list[Decimal]] = defaultdict(list)
    for row in literal_ranges:
        close = _decimal(row["close"])
        if close is not None and row["trading_date"]:
            literal_by_date[str(row["trading_date"])].append(close)
            literal_by_source_date[(str(row["source_name"]), str(row["trading_date"]))].append(
                close
            )
    ledger: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT * FROM observations WHERE normalized_symbol='DSEX'
        AND mapping_approval_status='under_review' ORDER BY source_name,trading_date,id"""
    ):
        item = dict(row)
        close = _decimal(item["close"])
        literal_values = literal_by_date.get(str(item["trading_date"]), [])
        exact_overlap = bool(close is not None and close in literal_values)
        same_source_overlap = bool(
            close is not None
            and close
            in literal_by_source_date.get((str(item["source_name"]), str(item["trading_date"])), [])
        )
        scale_consistent = bool(close is not None and Decimal("3000") <= close <= Decimal("10000"))
        classification = classify_dsex_alias(
            raw_symbol=str(item["original_symbol"]),
            instrument_class=str(item["instrument_class"]),
            mapping_reason=str(item["mapping_reason"]),
            exact_literal_overlap=exact_overlap,
            same_source_literal_overlap=same_source_overlap,
            value_scale_consistent=scale_consistent,
        )
        ledger.append(
            {
                "raw_symbol": item["original_symbol"],
                "proposed_normalized_symbol": "DSEX",
                "classification": classification,
                "source": item["source_name"],
                "source_dataset_id": item["source_dataset_id"],
                "source_file_hash": item["source_hash"],
                "source_row_identifier": item["source_row_id"],
                "trading_date": item["trading_date"],
                "example_values": {
                    field: item[f"raw_{field}"]
                    for field in ("open", "high", "low", "close", "volume")
                },
                "row_quality": (
                    "valid_ohlcv" if item["accepted_for_candidate"] else "invalid_ohlcv_preserved"
                ),
                "invalid_categories": json.loads(item["invalid_categories"]),
                "mapping_rationale": (
                    "00DSEX is index-class metadata with DSEX-scale values and 240 exact "
                    "literal-DSEX overlaps; no official document in the registry explicitly "
                    "approves the alias."
                ),
                "exact_literal_dsex_overlap": exact_overlap,
                "same_source_literal_dsex_overlap": same_source_overlap,
                "confidence": "high_cross_source_low_official",
                "review_status": "under_review",
                "human_approval": "required",
            }
        )
    grouped: list[dict[str, Any]] = []
    for key in sorted({(row["source"], row["raw_symbol"]) for row in ledger}):
        rows = [row for row in ledger if (row["source"], row["raw_symbol"]) == key]
        grouped.append(
            {
                "raw_symbol": key[1],
                "source": key[0],
                "proposed_normalized_symbol": "DSEX",
                "date_start": min(str(row["trading_date"]) for row in rows),
                "date_end": max(str(row["trading_date"]) for row in rows),
                "affected_rows": len(rows),
                "valid_rows": sum(row["row_quality"] == "valid_ohlcv" for row in rows),
                "invalid_rows": sum(row["row_quality"] != "valid_ohlcv" for row in rows),
                "classification_counts": dict(Counter(row["classification"] for row in rows)),
                "confidence": "high_cross_source_low_official",
                "review_status": "under_review",
            }
        )
    return {
        "total_rows": len(ledger),
        "classification_counts": dict(Counter(row["classification"] for row in ledger)),
        "quality_counts": dict(Counter(row["row_quality"] for row in ledger)),
        "exact_literal_overlap_rows": sum(row["exact_literal_dsex_overlap"] for row in ledger),
        "official_alias_evidence": "not_found",
        "automatic_merge": False,
        "groups": grouped,
        "ledger": ledger,
    }


def analyze_volume_ratios(ratios: list[Decimal]) -> dict[str, Any]:
    positive = [value for value in ratios if value > 0 and value.is_finite()]
    if not positive:
        return {
            "candidate_conversion_factor": None,
            "stable_ratio_share": 0.0,
            "confidence": "none",
            "classification": "unresolved_mismatch",
            "automatic_rescale": False,
            "approval_requirement": "human unit evidence required",
        }
    median = Decimal(str(statistics.median(positive)))
    candidates = (Decimal("10"), Decimal("100"), Decimal("1000"))
    factor = min(candidates, key=lambda candidate: _relative(median, candidate))
    stable = sum(_relative(value, factor) <= Decimal("0.02") for value in positive)
    share = stable / len(positive)
    statistically_stable = share >= 0.95 and _relative(median, factor) <= Decimal("0.02")
    return {
        "candidate_conversion_factor": str(factor) if statistically_stable else None,
        "median_ratio": str(median),
        "minimum_ratio": str(min(positive)),
        "maximum_ratio": str(max(positive)),
        "stable_ratio_share": share,
        "confidence": "medium" if statistically_stable else "low",
        "classification": "unresolved_mismatch",
        "evidence": (
            "Stable proportional scale is statistically supported, but shares/lots/value/trades "
            "semantics are not established by an approved data dictionary."
            if statistically_stable
            else "No stable defensible unit factor."
        ),
        "automatic_rescale": False,
        "approval_requirement": "official field-unit evidence and human approval required",
    }


def _comparison_values(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    return json.loads(row["values_a"]), json.loads(row["values_b"])


def _price_max_relative(values_a: dict[str, Any], values_b: dict[str, Any]) -> Decimal:
    differences: list[Decimal] = []
    for field in PRICE_FIELDS:
        left, right = _decimal(values_a.get(field)), _decimal(values_b.get(field))
        if left is None or right is None:
            return Decimal("Infinity")
        differences.append(_relative(left, right))
    return max(differences)


def _volume_ratio(values_a: dict[str, Any], values_b: dict[str, Any]) -> Decimal:
    left, right = _decimal(values_a.get("volume")), _decimal(values_b.get("volume"))
    if left is None or right is None or left == 0 or right == 0:
        return Decimal("0")
    return max(abs(left), abs(right)) / min(abs(left), abs(right))


def build_volume_unit_review(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    global_rows: list[sqlite3.Row] = []
    target_rows: list[dict[str, Any]] = []
    pair_ratios: defaultdict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    for row in connection.execute(
        """SELECT * FROM cross_source_comparisons WHERE tolerance_result='outside_tolerance'
        AND adjustment_a=adjustment_b"""
    ):
        values_a, values_b = _comparison_values(row)
        ratio = _volume_ratio(values_a, values_b)
        if _price_max_relative(values_a, values_b) <= Decimal("0.001") and ratio >= Decimal("10"):
            global_rows.append(row)
            if row["normalized_symbol"] in TARGET_SYMBOLS:
                pair_key = (
                    str(row["source_name_a"]),
                    str(row["source_name_b"]),
                    str(row["normalized_symbol"]),
                )
                pair_ratios[pair_key].append(ratio)
                target_rows.append(
                    {
                        "symbol": row["normalized_symbol"],
                        "date": row["trading_date"],
                        "source_a": row["source_name_a"],
                        "source_b": row["source_name_b"],
                        "volume_a": values_a.get("volume"),
                        "volume_b": values_b.get("volume"),
                        "ratio": str(ratio),
                        "candidate_unit_relationship": "unresolved_mismatch",
                        "review_status": "under_review",
                    }
                )
    relationships = []
    for (source_a, source_b, symbol), ratios in sorted(pair_ratios.items()):
        relationships.append(
            {
                "symbol": symbol,
                "source_a": source_a,
                "source_b": source_b,
                "affected_rows": len(ratios),
                **analyze_volume_ratios(ratios),
            }
        )
    return {
        "legacy_global_volume_unit_count": len(global_rows),
        "target_scope_count": len(target_rows),
        "out_of_scope_preserved_count": len(global_rows) - len(target_rows),
        "relationships": relationships,
        "rows": target_rows,
    }


def classify_rounding_conflict(
    *,
    max_price_relative: Decimal,
    max_price_absolute: Decimal,
    volume_relative: Decimal,
    adjustment_status: str,
    source_precision_differs: bool,
) -> str:
    if volume_relative > Decimal("0.02") or max_price_relative > Decimal("0.001"):
        return "material_discrepancy"
    if adjustment_status == "adjusted" and max_price_absolute > 0:
        return "adjusted_price_precision"
    if source_precision_differs and max_price_absolute <= Decimal("0.01"):
        return "source_precision_loss"
    if max_price_absolute <= Decimal("0.01"):
        return "harmless_decimal_rounding"
    return "tick_size_rounding"


def build_rounding_review(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    global_count = 0
    rows: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT * FROM cross_source_comparisons WHERE tolerance_result='outside_tolerance'
        AND adjustment_a=adjustment_b"""
    ):
        values_a, values_b = _comparison_values(row)
        price_relative = _price_max_relative(values_a, values_b)
        ratio = _volume_ratio(values_a, values_b)
        left_volume = _decimal(values_a.get("volume")) or Decimal("0")
        right_volume = _decimal(values_b.get("volume")) or Decimal("0")
        volume_relative = _relative(left_volume, right_volume)
        if price_relative <= Decimal("0.001") and volume_relative > Decimal("0.02") and ratio < 10:
            global_count += 1
            if row["normalized_symbol"] not in TARGET_SYMBOLS:
                continue
            absolute = json.loads(row["absolute_differences"])
            max_absolute = max(
                (_decimal(absolute.get(field)) or Decimal("0")) for field in PRICE_FIELDS
            )
            precision_differs = any(
                len(str(values_a.get(field)).partition(".")[2])
                != len(str(values_b.get(field)).partition(".")[2])
                for field in PRICE_FIELDS
            )
            classification = classify_rounding_conflict(
                max_price_relative=price_relative,
                max_price_absolute=max_absolute,
                volume_relative=volume_relative,
                adjustment_status=str(row["adjustment_a"]),
                source_precision_differs=precision_differs,
            )
            rows.append(
                {
                    "symbol": row["normalized_symbol"],
                    "date": row["trading_date"],
                    "source_a": row["source_name_a"],
                    "source_b": row["source_name_b"],
                    "values_a": values_a,
                    "values_b": values_b,
                    "max_price_absolute": str(max_absolute),
                    "max_price_relative": str(price_relative),
                    "volume_relative": str(volume_relative),
                    "classification": classification,
                    "recommended_price_tolerance": (
                        "relative <= 0.001 plus an operator-approved tick-size absolute cap"
                    ),
                    "raw_values_modified": False,
                    "review_status": "under_review",
                }
            )
    return {
        "legacy_global_rounding_bucket_count": global_count,
        "target_scope_count": len(rows),
        "out_of_scope_preserved_count": global_count - len(rows),
        "finding": "The legacy rounding bucket also contains material volume disagreements.",
        "rows": rows,
    }


def _mapping_wholly_uncertain(connection: sqlite3.Connection) -> set[str]:
    statuses: defaultdict[str, set[str]] = defaultdict(set)
    for symbol, status in connection.execute(
        "SELECT normalized_symbol,approval_status FROM symbol_mappings WHERE normalized_symbol<>''"
    ):
        statuses[str(symbol)].add(str(status))
    return {symbol for symbol, values in statuses.items() if values == {"under_review"}}


def _raw_evidence(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    trading_date: str,
    source_name: str,
    source_urls: dict[str, str],
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT source_hash,source_row_id,original_symbol,raw_date,raw_open,raw_high,
        raw_low,raw_close,raw_volume FROM observations WHERE normalized_symbol=?
        AND trading_date=? AND source_name=? LIMIT 1""",
        (symbol, trading_date, source_name),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["source_url"] = source_urls.get(result["source_hash"])
    return result


def _nearby_values(
    connection: sqlite3.Connection, symbol: str, trading_date: str, source_name: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """SELECT trading_date,open,high,low,close,volume FROM observations
            WHERE normalized_symbol=? AND source_name=? AND trading_date BETWEEN date(?,'-7 day')
            AND date(?,'+7 day') ORDER BY trading_date""",
            (symbol, source_name, trading_date, trading_date),
        )
    ]


def build_unexplained_conflict_review(
    connection: sqlite3.Connection, *, source_urls: dict[str, str]
) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    action_dates = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT DISTINCT normalized_symbol,trading_date FROM corporate_action_candidates"
        )
    }
    wholly_uncertain = _mapping_wholly_uncertain(connection)
    global_unexplained = 0
    rows: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM cross_source_comparisons WHERE tolerance_result='outside_tolerance'"
    ):
        if row["adjustment_a"] != row["adjustment_b"]:
            continue
        if row["normalized_symbol"] in wholly_uncertain:
            continue
        if row["source_a"] == row["source_b"] and row["source_name_a"] == row["source_name_b"]:
            continue
        values_a, values_b = _comparison_values(row)
        price_relative = _price_max_relative(values_a, values_b)
        volume_ratio = _volume_ratio(values_a, values_b)
        left_volume = _decimal(values_a.get("volume")) or Decimal("0")
        right_volume = _decimal(values_b.get("volume")) or Decimal("0")
        volume_relative = _relative(left_volume, right_volume)
        if price_relative <= Decimal("0.001") and volume_relative > Decimal("0.02"):
            continue
        if (row["normalized_symbol"], row["trading_date"]) in action_dates:
            continue
        global_unexplained += 1
        if row["normalized_symbol"] not in TARGET_SYMBOLS:
            continue
        raw_a = _raw_evidence(
            connection,
            symbol=str(row["normalized_symbol"]),
            trading_date=str(row["trading_date"]),
            source_name=str(row["source_name_a"]),
            source_urls=source_urls,
        )
        raw_b = _raw_evidence(
            connection,
            symbol=str(row["normalized_symbol"]),
            trading_date=str(row["trading_date"]),
            source_name=str(row["source_name_b"]),
            source_urls=source_urls,
        )
        rows.append(
            {
                "symbol": row["normalized_symbol"],
                "date": row["trading_date"],
                "source_a": row["source_name_a"],
                "source_b": row["source_name_b"],
                "source_a_values": values_a,
                "source_b_values": values_b,
                "absolute_difference": json.loads(row["absolute_differences"]),
                "percentage_difference": json.loads(row["percentage_differences"]),
                "adjustment_status": row["adjustment_a"],
                "nearby_source_a": _nearby_values(
                    connection,
                    str(row["normalized_symbol"]),
                    str(row["trading_date"]),
                    str(row["source_name_a"]),
                ),
                "nearby_source_b": _nearby_values(
                    connection,
                    str(row["normalized_symbol"]),
                    str(row["trading_date"]),
                    str(row["source_name_b"]),
                ),
                "possible_corporate_action_evidence": (
                    "none in candidate queue; official issuer/ex-date evidence not registered"
                ),
                "possible_source_error": (
                    "source disagreement; independence and field semantics are unverified"
                ),
                "volume_ratio_context": str(volume_ratio),
                "raw_source_a": raw_a,
                "raw_source_b": raw_b,
                "recommended_decision": "hold_for_review",
                "human_review_status": "under_review",
            }
        )
    return {
        "legacy_global_unexplained_count": global_unexplained,
        "target_scope_count": len(rows),
        "out_of_scope_preserved_count": global_unexplained - len(rows),
        "automatic_resolution": False,
        "rows": rows,
    }


def _missing_expected_days(dates: set[date]) -> int:
    if not dates:
        return 0
    cursor, end = min(dates), max(dates)
    expected = 0
    while cursor <= end:
        if cursor.weekday() not in {4, 5}:
            expected += 1
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return max(expected - len(dates), 0)


def build_source_hierarchy_review(
    connection: sqlite3.Connection,
    *,
    source_scores: dict[str, float],
    source_catalog: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    results: list[dict[str, Any]] = []
    for symbol in TARGET_SYMBOLS:
        source_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_name FROM observations WHERE normalized_symbol=?",
                (symbol,),
            )
        ]
        for source_name in sorted(source_names):
            for adjustment in (
                str(row[0])
                for row in connection.execute(
                    """SELECT DISTINCT adjustment_status FROM observations
                    WHERE normalized_symbol=? AND source_name=?""",
                    (symbol, source_name),
                )
            ):
                stats = connection.execute(
                    """SELECT MIN(trading_date),MAX(trading_date),
                    SUM(CASE WHEN accepted_for_candidate=1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END)
                    FROM observations WHERE normalized_symbol=? AND source_name=?
                    AND adjustment_status=?""",
                    (symbol, source_name, adjustment),
                ).fetchone()
                observed_dates = {
                    date.fromisoformat(str(row[0]))
                    for row in connection.execute(
                        """SELECT DISTINCT trading_date FROM observations WHERE normalized_symbol=?
                        AND source_name=? AND adjustment_status=? AND trading_date IS NOT NULL""",
                        (symbol, source_name, adjustment),
                    )
                }
                duplicates = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM duplicate_groups WHERE normalized_symbol=?
                        AND source_name=? AND adjustment_status=?""",
                        (symbol, source_name, adjustment),
                    ).fetchone()[0]
                )
                conflicts = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM cross_source_comparisons
                        WHERE normalized_symbol=? AND adjustment_a=adjustment_b
                        AND tolerance_result='outside_tolerance' AND
                        ((source_name_a=? AND adjustment_a=?) OR
                         (source_name_b=? AND adjustment_b=?))""",
                        (
                            symbol,
                            source_name,
                            adjustment,
                            source_name,
                            adjustment,
                        ),
                    ).fetchone()[0]
                )
                metadata = source_catalog.get(source_name, {})
                if "Coverage Metadata" in source_name:
                    role = "primary_candidate"
                elif "DSE 2021" in source_name:
                    role = "secondary_validation"
                elif "AmarStock" in source_name and adjustment == "adjusted":
                    role = "adjustment_reference"
                elif "AmarStock" in source_name:
                    role = "secondary_validation"
                else:
                    role = "fallback_only"
                if (
                    symbol == "DSEX"
                    and source_name
                    != "Dhaka Stock Exchange Historical Data (1999-2025) - DSE_Data.csv"
                ):
                    role = "unresolved"
                results.append(
                    {
                        "symbol": symbol,
                        "source_name": source_name,
                        "adjustment_status": adjustment,
                        "observed_start": stats[0],
                        "observed_end": stats[1],
                        "valid_row_count": int(stats[2] or 0),
                        "invalid_row_count": int(stats[3] or 0),
                        "duplicate_count": duplicates,
                        "eligible_conflict_count": conflicts,
                        "missing_day_count": _missing_expected_days(observed_dates),
                        "source_quality_score": source_scores.get(source_name),
                        "license_status": metadata.get("license_note", "not_registered"),
                        "trust_classification": metadata.get("source_trust", "unknown"),
                        "timestamp_provenance": metadata.get("timestamp_trust", "unknown"),
                        "recommended_role": role,
                        "recommendation_is_final": False,
                        "human_approval": "pending",
                    }
                )
    return results


def build_calendar_review(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    results: list[dict[str, Any]] = []
    for symbol in TARGET_SYMBOLS:
        all_dates = {
            date.fromisoformat(str(row[0]))
            for row in connection.execute(
                """SELECT DISTINCT trading_date FROM observations
                WHERE normalized_symbol=? AND trading_date IS NOT NULL""",
                (symbol,),
            )
        }
        weekend = sorted(item.isoformat() for item in all_dates if item.weekday() in {4, 5})
        long_gaps: list[dict[str, Any]] = []
        ordered = sorted(all_dates)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            gap = (current - previous).days
            if gap > 7:
                long_gaps.append(
                    {
                        "previous_date": previous.isoformat(),
                        "next_date": current.isoformat(),
                        "calendar_days": gap,
                        "classification": "all_source_gap_requires_calendar_review",
                    }
                )
        single_source_gaps = 0
        source_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_name FROM observations WHERE normalized_symbol=?",
                (symbol,),
            )
        ]
        for source_name in source_names:
            source_dates = {
                date.fromisoformat(str(row[0]))
                for row in connection.execute(
                    """SELECT DISTINCT trading_date FROM observations WHERE normalized_symbol=?
                    AND source_name=? AND trading_date IS NOT NULL""",
                    (symbol, source_name),
                )
            }
            single_source_gaps += _missing_expected_days(source_dates)
        results.append(
            {
                "symbol": symbol,
                "coverage_start": min(all_dates).isoformat() if all_dates else None,
                "coverage_end": max(all_dates).isoformat() if all_dates else None,
                "observed_trading_dates": len(all_dates),
                "weekend_rows": weekend,
                "weekend_row_count": len(weekend),
                "weekend_interpretation": (
                    "Friday/Saturday under current convention; historical weekend regimes "
                    "and exceptions remain under review"
                ),
                "long_gaps": long_gaps,
                "long_gap_count": len(long_gaps),
                "single_source_gap_count": single_source_gaps,
                "all_source_missing_expected_days": _missing_expected_days(all_dates),
                "suspected_holidays": "under_review_not_authoritative",
                "suspension_candidates": "human evidence required",
                "data_collection_failures": "not distinguishable from holidays/suspensions",
                "calendar_authoritative": False,
            }
        )
    return results


def build_corporate_action_review(
    action_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidate_dates = {(str(row["symbol"]), str(row["trading_date"])) for row in candidate_rows}
    results: list[dict[str, Any]] = []
    for row in action_rows:
        symbol = str(row["normalized_symbol"])
        if symbol not in {"GP", "ACI", "BRACBANK"}:
            continue
        affects_candidate = (symbol, str(row["trading_date"])) in candidate_dates
        if row["revised_classification"] == "adjustment_divergence":
            classification = "likely_adjustment"
        elif row["revised_classification"] == "discontinuity_for_review":
            classification = "suspension_resumption_candidate"
        else:
            classification = "insufficient_evidence"
        results.append(
            {
                **row,
                "affects_candidate_row": affects_candidate,
                "human_classification": classification,
                "evidence_links": list(OFFICIAL_REVIEW_EVIDENCE),
                "confirmed_by_official_evidence": False,
                "review_status": "under_review",
                "automatic_approval": False,
            }
        )
    return results


def provisional_policies() -> list[dict[str, Any]]:
    results = []
    for symbol in TARGET_SYMBOLS:
        results.append(
            {
                "symbol": symbol,
                "tiers": {
                    "tier_1_cross_source_confirmed": "two eligible same-grain sources agree",
                    "tier_2_single_source_high_quality": "one human-approved high-quality source",
                    "tier_3_research_only": "complete lineage but limited independent confirmation",
                    "held_for_review": "mapping, conflict, calendar, unit, or action uncertainty",
                    "rejected": "invalid OHLC/date/symbol or irreconcilable provenance",
                },
                "source_requirement": "human-approved role; no automatic primary",
                "ohlc_requirement": "finite positive values and low<=open/close<=high",
                "volume_requirement": "known units or preserved unresolved without rescaling",
                "adjustment_requirement": "known and compared only with the same status",
                "conflict_behavior": "hold eligible unexplained differences",
                "missing_day_behavior": "do not impute; require calendar/suspension review",
                "corporate_action_behavior": "hold until source-linked human review",
                "lineage_requirement": "raw hash, row identifier, source URL, transformation",
                "active": False,
                "human_approval": "pending",
            }
        )
    return results


def readiness_statuses(
    *,
    dsex_mapping_rows: int,
    conflicts_by_symbol: dict[str, int],
    source_approvals_by_symbol: dict[str, int],
) -> list[dict[str, Any]]:
    results = []
    for symbol in TARGET_SYMBOLS:
        blockers: list[str] = []
        if symbol == "DSEX" and dsex_mapping_rows:
            blockers.append("mapping_review_required")
        if conflicts_by_symbol.get(symbol, 0):
            blockers.append("conflict_review_required")
        if source_approvals_by_symbol.get(symbol, 0):
            blockers.append("source_approval_required")
        if "mapping_review_required" in blockers:
            status = "mapping_review_required"
        elif "conflict_review_required" in blockers:
            status = "conflict_review_required"
        elif "source_approval_required" in blockers:
            status = "source_approval_required"
        else:
            status = "ready_for_research_approval"
        results.append(
            {
                "symbol": symbol,
                "status": status,
                "blockers": blockers,
                "activation": False,
            }
        )
    return results


def deterministic_sample(
    rows: list[dict[str, Any]], count: int, seed_key: str
) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(
            (seed_key + json.dumps(row, sort_keys=True, default=str)).encode()
        ).hexdigest(),
    )[:count]


def build_review_samples(
    connection: sqlite3.Connection,
    *,
    subset: dict[str, Any],
    unexplained_rows: list[dict[str, Any]],
    volume_rows: list[dict[str, Any]],
    corporate_rows: list[dict[str, Any]],
    source_urls: dict[str, str],
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    samples: list[dict[str, Any]] = []
    for symbol in TARGET_SYMBOLS:
        accepted = [row for row in subset["candidate_rows"] if row["symbol"] == symbol]
        held = [row for row in subset["held_rows"] if row["symbol"] == symbol]
        samples.extend(
            {"sample_type": "accepted_candidate", **row}
            for row in deterministic_sample(accepted, 20, f"accepted:{symbol}")
        )
        for held_row in deterministic_sample(held, 20, f"held:{symbol}"):
            evidence = []
            for raw in connection.execute(
                """SELECT source_hash,source_row_id,source_name,original_symbol,raw_date,
                raw_open,raw_high,raw_low,raw_close,raw_volume FROM observations
                WHERE normalized_symbol=? AND trading_date IS ? AND adjustment_status=? LIMIT 10""",
                (symbol, held_row.get("trading_date"), held_row.get("adjustment_status")),
            ):
                item = dict(raw)
                item["source_url"] = source_urls.get(item["source_hash"])
                evidence.append(item)
            samples.append({"sample_type": "held_row", **held_row, "raw_evidence": evidence})
        samples.extend(
            {"sample_type": "eligible_unexplained_conflict", **row}
            for row in unexplained_rows
            if row["symbol"] == symbol
        )
        symbol_volumes = [row for row in volume_rows if row["symbol"] == symbol]
        samples.extend(
            {"sample_type": "largest_volume_disagreement", **row}
            for row in sorted(symbol_volumes, key=lambda row: Decimal(row["ratio"]), reverse=True)[
                :20
            ]
        )
        samples.extend(
            {"sample_type": "corporate_action_candidate", **row}
            for row in corporate_rows
            if row["normalized_symbol"] == symbol
        )
        observations = [
            dict(row)
            for row in connection.execute(
                """SELECT source_hash,source_row_id,source_name,original_symbol,trading_date,
                raw_open,raw_high,raw_low,raw_close,raw_volume FROM observations
                WHERE normalized_symbol=? ORDER BY trading_date,id""",
                (symbol,),
            )
        ]
        if observations:
            for sample_type, item in (
                ("first_observation", observations[0]),
                ("last_observation", observations[-1]),
            ):
                item["source_url"] = source_urls.get(item["source_hash"])
                samples.append({"sample_type": sample_type, **item})
            unique_dates = sorted(
                {date.fromisoformat(str(item["trading_date"])) for item in observations}
            )
            gap_count = 0
            for previous, current in zip(unique_dates, unique_dates[1:], strict=False):
                if (current - previous).days <= 7:
                    continue
                boundary_rows = [
                    item
                    for item in observations
                    if item["trading_date"] in {previous.isoformat(), current.isoformat()}
                ]
                for item in boundary_rows:
                    item["source_url"] = source_urls.get(item["source_hash"])
                samples.append(
                    {
                        "sample_type": "long_gap_boundary",
                        "symbol": symbol,
                        "previous_date": previous.isoformat(),
                        "next_date": current.isoformat(),
                        "calendar_days": (current - previous).days,
                        "raw_boundary_evidence": boundary_rows,
                    }
                )
                gap_count += 1
                if gap_count >= 20:
                    break
        duplicate_rows = connection.execute(
            """SELECT * FROM duplicate_groups WHERE normalized_symbol=?
            ORDER BY trading_date LIMIT 20""",
            (symbol,),
        )
        samples.extend({"sample_type": "duplicate_example", **dict(row)} for row in duplicate_rows)
    return samples

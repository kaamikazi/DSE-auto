from __future__ import annotations

import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.services.target_symbol_human_review import (
    PRICE_FIELDS,
    build_dsex_mapping_review,
)

TARGETS = ("GP", "ACI", "BRACBANK", "DSEX")
COVERAGE_ADJUSTED = (
    "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / adjusted"
)
COVERAGE_UNADJUSTED = (
    "Dhaka Stock Exchange End-of-Day Financial Dataset with Coverage Metadata / unadjusted"
)
DSE_STOCKS = "Dhaka Stock Exchange DSE 2021 yearly CSV"
HISTORICAL = "Dhaka Stock Exchange Historical Data (1999-2025) - DSE_Data.csv"
AMAR_ADJUSTED = "AmarStock adjusted DSE end-of-day CSV for 2023-04-16"
AMAR_UNADJUSTED = "AmarStock unadjusted DSE end-of-day CSV for 2023-04-16"

Readiness = Literal[
    "not_ready",
    "human_decision_required",
    "ready_for_research_activation_review",
    "rejected",
]


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:  # pragma: no cover - defensive around preserved evidence
        return None
    return result if result.is_finite() else None


def _missing_expected_days(values: set[date]) -> int:
    if not values:
        return 0
    cursor, end = min(values), max(values)
    expected = 0
    while cursor <= end:
        if cursor.weekday() not in {4, 5}:
            expected += 1
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return max(expected - len(values), 0)


def classify_invalid_dsex_row(categories: list[str]) -> str:
    category_set = set(categories)
    if "zero_price" in category_set:
        return "zero_index_value"
    if "missing_field" in category_set:
        return "missing_field"
    if "non_numeric" in category_set:
        return "non_numeric_value"
    if "malformed_date" in category_set:
        return "malformed_date"
    if "metadata_row" in category_set:
        return "metadata_row"
    if "high_below_low" in category_set:
        return "high_low_violation"
    if "open_outside_range" in category_set or "close_outside_range" in category_set:
        return "open_close_outside_range"
    if "duplicate_corruption" in category_set:
        return "duplicate_corruption"
    if "suspected_source_corruption" in category_set:
        return "source_corruption"
    return "unresolved"


def _schema_for_source(source: str) -> dict[str, Any]:
    if "Coverage Metadata" in source:
        return {
            "format": "CSV inside preserved ZIP",
            "columns": ["Date", "Ticker", "Open", "High", "Low", "Close", "Volume"],
            "header": True,
        }
    if source == DSE_STOCKS:
        return {
            "format": "CSV",
            "columns": ["Ticker", "Date", "Open", "High", "Low", "Close", "Volume"],
            "header": False,
        }
    return {
        "format": "CSV",
        "columns": ["Date", "Scrip", "Open", "High", "Low", "Close", "Volume"],
        "header": True,
    }


def _pair_facts(connection: sqlite3.Connection) -> dict[str, int]:
    row = connection.execute(
        """SELECT COUNT(*) AS paired,
        SUM(CASE WHEN a.open=u.open AND a.high=u.high AND a.low=u.low AND a.close=u.close
            THEN 1 ELSE 0 END) AS ohlc_exact,
        SUM(CASE WHEN a.volume=u.volume THEN 1 ELSE 0 END) AS volume_exact,
        SUM(CASE WHEN CAST(a.volume AS REAL)=100*CAST(u.volume AS REAL)
            THEN 1 ELSE 0 END) AS adjusted_volume_100x,
        SUM(CASE WHEN a.accepted_for_candidate=u.accepted_for_candidate
            THEN 1 ELSE 0 END) AS quality_same
        FROM observations a JOIN observations u ON a.trading_date=u.trading_date
        WHERE a.source_name=? AND u.source_name=? AND a.original_symbol='00DSEX'
        AND u.original_symbol='00DSEX'""",
        (COVERAGE_ADJUSTED, COVERAGE_UNADJUSTED),
    ).fetchone()
    return {key: int(value or 0) for key, value in dict(row).items()}


def build_dsex_forensics(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    mapping = build_dsex_mapping_review(connection)
    classification_by_key = {
        (str(row["source"]), str(row["source_row_identifier"])): str(row["classification"])
        for row in mapping["ledger"]
    }
    opposite: dict[tuple[str, str], dict[str, Any]] = {}
    for row in connection.execute(
        """SELECT source_name,source_row_id,trading_date,open,high,low,close,volume,
        adjustment_status,accepted_for_candidate FROM observations
        WHERE original_symbol='00DSEX' AND source_name IN (?,?)""",
        (COVERAGE_ADJUSTED, COVERAGE_UNADJUSTED),
    ):
        opposite[(str(row["adjustment_status"]), str(row["trading_date"]))] = dict(row)

    cause_counts: Counter[str] = Counter()
    unresolved_rows = [row for row in mapping["ledger"] if row["classification"] == "unresolved"]
    for row in unresolved_rows:
        if row["row_quality"] != "valid_ohlcv":
            cause_counts["malformed_ohlcv_record"] += 1
            continue
        source = str(row["source"])
        if source in {AMAR_ADJUSTED, AMAR_UNADJUSTED}:
            cause_counts["same_index_represented_at_adjusted_and_unadjusted_grains"] += 1
            continue
        adjustment = "adjusted" if source == COVERAGE_ADJUSTED else "unadjusted"
        other = "unadjusted" if adjustment == "adjusted" else "adjusted"
        peer = opposite.get((other, str(row["trading_date"])))
        values = row["example_values"]
        if peer and all(str(values[field]) == str(peer[field]) for field in PRICE_FIELDS):
            cause_counts["adjusted_unadjusted_ohlc_duplication"] += 1
        else:
            cause_counts["unknown_cross_grain_difference"] += 1

    clusters: list[dict[str, Any]] = []
    groups: defaultdict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """SELECT * FROM observations WHERE normalized_symbol='DSEX'
        AND mapping_approval_status='under_review' ORDER BY source_name,trading_date,id"""
    ):
        groups[(str(row["source_name"]), str(row["adjustment_status"]))].append(row)
    for (source, adjustment), rows in sorted(groups.items()):
        dates = {
            date.fromisoformat(str(row["trading_date"])) for row in rows if row["trading_date"]
        }
        prices = [_decimal(row["close"]) for row in rows]
        price_values = [value for value in prices if value is not None]
        volumes = [_decimal(row["volume"]) for row in rows]
        volume_values = [value for value in volumes if value is not None]
        classes = Counter(
            classification_by_key[(source, str(row["source_row_id"]))] for row in rows
        )
        proposed: Counter[str] = Counter(
            {
                "approved_alias_candidate": 0,
                "duplicate_alias_candidate": 0,
                "invalid_row_candidate": 0,
                "non_dsex_candidate": 0,
                "unresolved_candidate": 0,
            }
        )
        for row in rows:
            classification = classification_by_key[(source, str(row["source_row_id"]))]
            if not row["accepted_for_candidate"]:
                proposed["invalid_row_candidate"] += 1
            elif classification == "alternate_dsex_label":
                proposed["approved_alias_candidate"] += 1
            elif classification == "duplicate_alias":
                proposed["duplicate_alias_candidate"] += 1
            else:
                proposed["unresolved_candidate"] += 1
        clusters.append(
            {
                "cluster_id": f"dsex-{len(clusters) + 1:02d}",
                "source_file": source,
                "source_hashes": sorted({str(row["source_hash"]) for row in rows}),
                "schema": _schema_for_source(source),
                "adjustment_grain": adjustment,
                "identifier_pattern": "00DSEX",
                "date_start": min(dates).isoformat() if dates else None,
                "date_end": max(dates).isoformat() if dates else None,
                "row_count": len(rows),
                "valid_rows": sum(bool(row["accepted_for_candidate"]) for row in rows),
                "invalid_rows": sum(not bool(row["accepted_for_candidate"]) for row in rows),
                "missing_expected_days_current_weekend_convention": _missing_expected_days(dates),
                "price_scale": {
                    "minimum_close": str(min(price_values)) if price_values else None,
                    "median_close": str(statistics.median(price_values)) if price_values else None,
                    "maximum_close": str(max(price_values)) if price_values else None,
                },
                "volume_field": {
                    "column_name": "Volume",
                    "minimum": str(min(volume_values)) if volume_values else None,
                    "maximum": str(max(volume_values)) if volume_values else None,
                    "semantics": "unproven_for_index",
                },
                "mapping_classifications": dict(classes),
                "proposed_mapping_groups": dict(proposed),
                "duplicate_relationship": (
                    "paired adjusted/unadjusted grain; see pair facts"
                    if "Coverage Metadata" in source
                    else "same-date index-label comparison retained"
                ),
                "non_dsex_evidence": "none_found",
                "automatic_mapping": False,
                "approval_status": "under_review",
            }
        )
    return {
        "population": mapping["total_rows"],
        "classification_counts": mapping["classification_counts"],
        "quality_counts": mapping["quality_counts"],
        "unresolved_cause_counts": dict(cause_counts),
        "adjusted_unadjusted_pair_facts": _pair_facts(connection),
        "clusters": clusters,
        "mapping_ledger": mapping["ledger"],
        "official_alias_evidence": "not_found",
        "automatic_mapping": False,
    }


def build_dsex_invalid_review(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT * FROM observations WHERE normalized_symbol='DSEX'
        AND mapping_approval_status='under_review' AND accepted_for_candidate=0
        ORDER BY trading_date,source_name,id"""
    ):
        categories = json.loads(row["invalid_categories"])
        counterpart = connection.execute(
            """SELECT source_name,source_hash,source_row_id,open,high,low,close,volume,
            adjustment_status FROM observations WHERE normalized_symbol='DSEX'
            AND trading_date=? AND accepted_for_candidate=1 ORDER BY source_name LIMIT 1""",
            (row["trading_date"],),
        ).fetchone()
        rows.append(
            {
                "source": row["source_name"],
                "source_file_hash": row["source_hash"],
                "source_row_identifier": row["source_row_id"],
                "date": row["trading_date"],
                "raw_values": {
                    field: row[f"raw_{field}"]
                    for field in ("open", "high", "low", "close", "volume")
                },
                "invalid_categories": categories,
                "primary_classification": classify_invalid_dsex_row(categories),
                "possible_recovery_evidence": dict(counterpart) if counterpart else None,
                "recovery_status": (
                    "counterpart_available_requires_lineage_and_human_review"
                    if counterpart
                    else "no_safe_counterpart_found"
                ),
                "automatic_repair": False,
                "proposed_mapping_group": "invalid_row_candidate",
            }
        )
    return {
        "row_count": len(rows),
        "primary_classification_counts": dict(
            Counter(row["primary_classification"] for row in rows)
        ),
        "all_category_counts": dict(
            Counter(cat for row in rows for cat in row["invalid_categories"])
        ),
        "recoverable_candidate_count": sum(
            row["possible_recovery_evidence"] is not None for row in rows
        ),
        "automatic_repair_count": 0,
        "rows": rows,
    }


def conclude_dsex_volume_semantics(
    *, ratio_rows: int, stable_ratio_share: float, official_index_has_volume: bool
) -> dict[str, Any]:
    outcome = "unresolved" if official_index_has_volume else "field_not_comparable"
    return {
        "outcome": outcome,
        "scoped_conflicts": ratio_rows,
        "stable_ratio_share": stable_ratio_share,
        "statistical_scale_candidate": "approximately_100x",
        "confirmed_conversion": False,
        "automatic_rescale": False,
        "include_dsex_volume_in_research": False,
        "recommendation": "exclude DSEX volume until a source-specific data dictionary proves semantics",
    }


def build_dsex_volume_semantics(
    connection: sqlite3.Connection, volume_review: dict[str, Any]
) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    peer_rows = [
        dict(row)
        for row in connection.execute(
            """WITH peers AS (
                SELECT source_name,trading_date,volume,COUNT(*) AS peer_count
                FROM observations WHERE original_symbol IN ('00DS30','00DSES','00DSMEX')
                GROUP BY source_name,trading_date,volume
            ), d AS (
                SELECT * FROM observations WHERE original_symbol='00DSEX'
                AND mapping_approval_status='under_review'
            )
            SELECT d.source_name,COUNT(*) AS dsex_rows,
            SUM(CASE WHEN p.peer_count IS NOT NULL THEN 1 ELSE 0 END)
                AS same_volume_as_other_index
            FROM d LEFT JOIN peers p ON p.source_name=d.source_name
            AND p.trading_date=d.trading_date AND CAST(p.volume AS REAL)=CAST(d.volume AS REAL)
            GROUP BY d.source_name ORDER BY d.source_name"""
        )
    ]
    relationships = volume_review.get("relationships", [])
    stable = max((float(row.get("stable_ratio_share", 0.0)) for row in relationships), default=0.0)
    conclusion = conclude_dsex_volume_semantics(
        ratio_rows=int(volume_review["target_scope_count"]),
        stable_ratio_share=stable,
        official_index_has_volume=False,
    )
    return {
        **conclusion,
        "candidate_meanings_reviewed": {
            "shares": "official EOD instrument volume means quantity, but the third-party index field is not documented",
            "lots_or_hundreds_of_shares": "not proven; the ratio alone is insufficient",
            "index_turnover": "not an official index-table field in registered DSE page 12",
            "trade_value": "official documentation uses a distinct value field in millions",
            "number_of_trades": "official documentation uses a distinct total-trades field",
            "scaled_display_units": "possible in third-party transformations, not documented",
            "another_source_specific_metric": "possible and unresolved",
        },
        "source_schema_evidence": [
            {
                "source": COVERAGE_ADJUSTED,
                "columns": _schema_for_source(COVERAGE_ADJUSTED)["columns"],
            },
            {
                "source": COVERAGE_UNADJUSTED,
                "columns": _schema_for_source(COVERAGE_UNADJUSTED)["columns"],
            },
            {"source": DSE_STOCKS, "columns": _schema_for_source(DSE_STOCKS)["columns"]},
            {"source": AMAR_UNADJUSTED, "columns": _schema_for_source(AMAR_UNADJUSTED)["columns"]},
        ],
        "same_volume_as_other_index": peer_rows,
        "official_document_evidence": [
            {
                "title": "Introduction of DSE Data Sale Services",
                "sha256": "cd2e29e0c91c0a47f0b4938c5903d636470329ffc0cc3213271aa9917f97af28",
                "source_url": "https://www.dsebd.org/assets/pdf/Introduction%20of%20DSE%20Data%20Sale%20Services.pdf",
                "pages": "7, 8, 12-14",
                "finding": "instrument quantity, trade count, trade value, and index fields are distinct; the IDX table has no volume field",
            },
            {
                "title": "DSE End-of-Day Data Product",
                "sha256": "5273a622054fe94397887bf06b30751f420ef7563201bd32e50a1b63610d11bf",
                "source_url": "https://www.dsebd.org/assets/pdf/EOD.pdf",
                "pages": "1",
                "finding": "instrument volume and value appear in transaction sections, but no 00DSEX CSV semantics or factor is defined",
            },
        ],
        "ratio_relationships": relationships,
        "reasoning": (
            "A stable ratio supports a transformation hypothesis only. The registered official "
            "IDX schema has capital value/deviation fields but no index volume, while many 00DSEX "
            "rows repeat another index's volume. Therefore the compared fields are not proven to "
            "measure the same DSEX quantity."
        ),
    }


def source_role_recommendations(rows: list[dict[str, Any]], *, symbol: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if row["symbol"] != symbol:
            continue
        source, adjustment = row["source_name"], row["adjustment_status"]
        if source == COVERAGE_ADJUSTED and adjustment == "adjusted":
            role = "primary_adjusted_research_source"
            consequence = "broad adjusted coverage; preserves third-party adjustment-method risk"
            alternative = "hold adjusted research until official methodology evidence exists"
        elif source == COVERAGE_UNADJUSTED and adjustment == "unadjusted":
            role = "primary_unadjusted_validation_source"
            consequence = "broad raw-price coverage; disputed dates remain held"
            alternative = "use only cross-source-confirmed dates"
        elif source in {DSE_STOCKS, AMAR_ADJUSTED, AMAR_UNADJUSTED}:
            role = "secondary_cross_check"
            consequence = (
                "adds independent date-level comparison but limited coverage/license assurance"
            )
            alternative = "fallback_only"
        elif source == HISTORICAL:
            role = "fallback_only"
            consequence = "extends history with unknown adjustment status and duplicates"
            alternative = "rejected"
        else:
            role = "rejected"
            consequence = "excluded from the proposed grain"
            alternative = "retain as preserved evidence only"
        result.append(
            {
                **row,
                "coverage": f"{row['observed_start']}..{row['observed_end']}",
                "recommended_role": role,
                "consequences_of_selecting": consequence,
                "conservative_alternative": alternative,
                "approval_field": "PENDING - HUMAN DECISION REQUIRED",
            }
        )
    return result


def source_role_decision(symbol: str) -> dict[str, Any]:
    if symbol not in {"GP", "ACI", "BRACBANK"}:
        raise ValueError("Source-role decisions are limited to GP, ACI, and BRACBANK")
    conflict_risk = {
        "GP": "2021-04-26 has a material volume disagreement and small price differences",
        "ACI": "2021-04-26 has an unresolved close and volume disagreement",
        "BRACBANK": "2021-04-26 and 2023-04-16 require separate conflict decisions",
    }[symbol]
    return {
        "symbol": symbol,
        "recommended_adjusted_source": COVERAGE_ADJUSTED,
        "recommended_unadjusted_source": COVERAGE_UNADJUSTED,
        "validation_sources": [AMAR_ADJUSTED, DSE_STOCKS, AMAR_UNADJUSTED],
        "excluded_sources": [
            f"{HISTORICAL} from adjusted/unadjusted primary roles because adjustment status is unknown"
        ],
        "rationale": (
            "Coverage-metadata grains provide the broadest scoped coverage; the other sources "
            "remain date-limited cross-checks and do not independently prove every row."
        ),
        "unresolved_risk": (
            f"{conflict_risk}; all sources are third-party research and source timestamps are "
            "not exchange-verified"
        ),
        "approval_field": "PENDING - HUMAN DECISION REQUIRED",
        "automatic_approval": False,
        "active": False,
    }


def build_conflict_approval_records(
    unexplained_rows: list[dict[str, Any]],
    rounding_rows: list[dict[str, Any]],
    source_scores: dict[str, float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, row in enumerate(unexplained_rows, start=1):
        records.append(
            {
                "approval_record_id": f"target-conflict-{index:02d}",
                "symbol": row["symbol"],
                "date": row["date"],
                "conflict_type": "eligible_price_and_or_volume_disagreement",
                "adjustment_status": row["adjustment_status"],
                "source_values": {
                    "source_a": row["source_a"],
                    "values_a": row["source_a_values"],
                    "source_b": row["source_b"],
                    "values_b": row["source_b_values"],
                },
                "percentage_difference": row["percentage_difference"],
                "source_quality": {
                    row["source_a"]: source_scores.get(str(row["source_a"])),
                    row["source_b"]: source_scores.get(str(row["source_b"])),
                },
                "nearby_trading_dates": {
                    "source_a": row["nearby_source_a"],
                    "source_b": row["nearby_source_b"],
                },
                "duplicate_status": "not_resolved_as_duplicate",
                "possible_corporate_action_relationship": row["possible_corporate_action_evidence"],
                "possible_data_entry_error": row["possible_source_error"],
                "recommendation": "hold_for_review",
                "confidence": "high_that_conflict_is_real_low_on_correct_source",
                "accept_source_a_effect": "uses source A and rejects B for this grain/date with explicit lineage",
                "accept_source_b_effect": "uses source B and rejects A for this grain/date with explicit lineage",
                "hold_or_reject_effect": "excludes this grain/date from the inactive subset proposal",
                "reviewer_decision": "",
                "operator_decision": "",
                "automatic_decision": False,
            }
        )
    for row in rounding_rows:
        records.append(
            {
                "approval_record_id": f"target-conflict-{len(records) + 1:02d}",
                "symbol": row["symbol"],
                "date": row["date"],
                "conflict_type": "material_volume_disagreement_with_small_price_difference",
                "adjustment_status": "unadjusted",
                "source_values": {
                    "source_a": row["source_a"],
                    "values_a": row["values_a"],
                    "source_b": row["source_b"],
                    "values_b": row["values_b"],
                },
                "percentage_difference": {
                    "max_price_relative": row["max_price_relative"],
                    "volume_relative": row["volume_relative"],
                },
                "source_quality": {
                    row["source_a"]: source_scores.get(str(row["source_a"])),
                    row["source_b"]: source_scores.get(str(row["source_b"])),
                },
                "nearby_trading_dates": "available in preserved source evidence",
                "duplicate_status": "not_resolved_as_duplicate",
                "possible_corporate_action_relationship": "none_registered_for_this_date",
                "possible_data_entry_error": "possible; volume differs by more than 15 percent",
                "recommendation": "hold_for_review",
                "confidence": "high_that_volume_difference_is_material_low_on_correct_source",
                "accept_source_a_effect": "selects coverage-metadata values for this date",
                "accept_source_b_effect": "selects yearly-CSV values for this date",
                "hold_or_reject_effect": "excludes disputed volume and optionally the full date",
                "reviewer_decision": "",
                "operator_decision": "",
                "automatic_decision": False,
            }
        )
    return sorted(records, key=lambda row: (TARGETS.index(row["symbol"]), row["date"]))


def final_source_hierarchies() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in ("GP", "ACI", "BRACBANK"):
        rows.extend(
            [
                {
                    "grain": f"{symbol}_adjusted",
                    "primary_source": COVERAGE_ADJUSTED,
                    "validation_source": AMAR_ADJUSTED,
                    "fallback_source": None,
                    "rejected_sources": [HISTORICAL],
                    "conflict_rule": "hold same-grain disagreement for separate human decision",
                    "missing_row_rule": "do not impute; use approved fallback only",
                    "invalid_row_rule": "reject invalid OHLCV with lineage retained",
                    "corporate_action_rule": "no adjustment inference without source-linked evidence",
                    "lineage_requirement": "raw hash, dataset id, row id, URL, transformation version",
                    "approval_status": "human_decision_required",
                    "active": False,
                },
                {
                    "grain": f"{symbol}_unadjusted",
                    "primary_source": COVERAGE_UNADJUSTED,
                    "validation_source": DSE_STOCKS,
                    "fallback_source": AMAR_UNADJUSTED,
                    "rejected_sources": [HISTORICAL],
                    "conflict_rule": "hold same-grain disagreement for separate human decision",
                    "missing_row_rule": "do not impute; use approved fallback only",
                    "invalid_row_rule": "reject invalid OHLCV with lineage retained",
                    "corporate_action_rule": "preserve raw prices; do not infer adjustment",
                    "lineage_requirement": "raw hash, dataset id, row id, URL, transformation version",
                    "approval_status": "human_decision_required",
                    "active": False,
                },
            ]
        )
    rows.extend(
        [
            {
                "grain": "DSEX_price_index",
                "primary_source": f"{DSE_STOCKS} after alias approval",
                "validation_source": f"{COVERAGE_UNADJUSTED} after alias approval",
                "fallback_source": HISTORICAL,
                "rejected_sources": ["OHLC-invalid rows", "unapproved aliases"],
                "conflict_rule": "hold both price disagreements and all unapproved aliases",
                "missing_row_rule": "do not impute",
                "invalid_row_rule": "reject invalid OHLC values; preserve raw evidence",
                "corporate_action_rule": "not applicable to index unless documented otherwise",
                "lineage_requirement": "raw hash, row id, alias decision, source URL, transformation",
                "approval_status": "not_ready",
                "active": False,
            },
            {
                "grain": "DSEX_volume",
                "primary_source": None,
                "validation_source": None,
                "fallback_source": None,
                "rejected_sources": [
                    COVERAGE_ADJUSTED,
                    COVERAGE_UNADJUSTED,
                    DSE_STOCKS,
                    AMAR_ADJUSTED,
                    AMAR_UNADJUSTED,
                ],
                "conflict_rule": "exclude as field_not_comparable; never apply 100x automatically",
                "missing_row_rule": "volume remains absent",
                "invalid_row_rule": "exclude",
                "corporate_action_rule": "not applicable",
                "lineage_requirement": "retain original raw field and semantics decision",
                "approval_status": "not_ready",
                "active": False,
            },
        ]
    )
    return rows


def corporate_action_statuses(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        status = (
            "likely_but_unconfirmed"
            if row["human_classification"] == "suspension_resumption_candidate"
            else "insufficient_evidence"
        )
        result.append(
            {
                **row,
                "final_evidence_status": status,
                "official_match": "no issuer/date-specific match in already-registered evidence",
                "registered_official_context": [
                    {
                        "title": "DSE Automated Trading Regulations",
                        "page": "11",
                        "scope": "generic spot-market corporate-action context only",
                    },
                    {
                        "title": "Introduction of DSE Data Sale Services",
                        "pages": "8, 13-14",
                        "scope": "generic EOD market-statistics context only",
                    },
                ],
                "verified": False,
                "human_review_required": True,
            }
        )
    return result


def calendar_decision_pack(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **row,
            "observed_weekday_pattern": "mixed historical rows; Friday/Saturday cannot be globally rejected",
            "official_document_evidence": (
                "registered DSE EOD publication confirms trading-day EOD data but does not establish "
                "a complete historical holiday calendar"
            ),
            "unresolved_assumptions": [
                "historical weekend regimes",
                "holiday dates",
                "symbol suspensions",
                "source-specific collection failures",
            ],
            "approval_status": "human_decision_required",
            "calendar_activated": False,
        }
        for row in rows
    ]


def build_subset_status_proposal(
    connection: sqlite3.Connection,
    *,
    subset: dict[str, Any],
    conflicts: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
    corporate_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    conflict_keys = {(str(row["symbol"]), str(row["date"])) for row in conflicts}
    calendar_keys: set[tuple[str, str]] = set()
    for item in calendar:
        symbol = str(item["symbol"])
        calendar_keys.update((symbol, str(day)) for day in item["weekend_rows"])
        for gap in item["long_gaps"]:
            calendar_keys.add((symbol, str(gap["previous_date"])))
            calendar_keys.add((symbol, str(gap["next_date"])))
    action_keys = {
        (str(row["normalized_symbol"]), str(row["trading_date"]))
        for row in corporate_actions
        if row.get("affects_candidate_row")
    }
    ledger: list[dict[str, Any]] = []
    for row in subset["candidate_rows"]:
        key = (str(row["symbol"]), str(row["trading_date"]))
        if row["symbol"] == "DSEX":
            status = "held_for_mapping"
        elif key in conflict_keys:
            status = "held_for_conflict"
        elif key in action_keys:
            status = "held_for_corporate_action"
        elif key in calendar_keys:
            status = "held_for_calendar"
        else:
            status = "approvable_after_human_decision"
        ledger.append(
            {
                "population": "canonical_candidate",
                "symbol": row["symbol"],
                "date": row["trading_date"],
                "adjustment_status": row["adjustment_status"],
                "status": status,
                "source": row["selected_source"],
                "lineage": row["lineage"],
            }
        )
    for row in subset["held_rows"]:
        if row["reason"] != "unresolved_eligible_source_conflict":
            continue
        ledger.append(
            {
                "population": "held_candidate",
                "symbol": row["symbol"],
                "date": row["trading_date"],
                "adjustment_status": row["adjustment_status"],
                "status": "held_for_conflict",
                "source": None,
                "lineage": [],
            }
        )
    for row in connection.execute(
        """SELECT normalized_symbol,trading_date,adjustment_status,source_name,source_hash,
        source_row_id,invalid_categories FROM observations WHERE normalized_symbol IN
        ('GP','ACI','BRACBANK','DSEX') AND accepted_for_candidate=0"""
    ):
        ledger.append(
            {
                "population": "invalid_observation",
                "symbol": row["normalized_symbol"],
                "date": row["trading_date"],
                "adjustment_status": row["adjustment_status"],
                "status": "rejected_invalid",
                "source": row["source_name"],
                "lineage": {
                    "source_file_hash": row["source_hash"],
                    "source_row_identifier": row["source_row_id"],
                    "invalid_categories": json.loads(row["invalid_categories"]),
                },
            }
        )
    return {
        "candidate_status_counts": dict(Counter(row["status"] for row in ledger)),
        "population_counts": dict(Counter(row["population"] for row in ledger)),
        "ledger": ledger,
        "active": False,
        "activation_permission": "REJECTED / NOT GRANTED",
    }


def approval_decisions() -> list[dict[str, Any]]:
    labels = [
        "GP adjusted primary source",
        "GP unadjusted primary source",
        "ACI conflict",
        "ACI source hierarchy",
        "BRACBANK conflict 1",
        "BRACBANK conflict 2",
        "BRACBANK source hierarchy",
        "DSEX alias treatment",
        "DSEX invalid-row treatment",
        "DSEX volume semantics",
        "DSEX source hierarchy",
        "target corporate-action treatment",
        "observed calendar treatment",
        "provisional canonical subset policy",
        "research activation permission",
    ]
    return [
        {
            "decision_id": f"decision-{index:02d}",
            "decision": label,
            "recommended_action": (
                "REJECTED / NOT GRANTED"
                if label == "research activation permission"
                else "HOLD - HUMAN DECISION REQUIRED"
            ),
            "reviewer": "",
            "reviewer_decision": "",
            "operator": "",
            "operator_decision": "",
            "default_effect": "no approval and no activation",
            "status": "under_review",
        }
        for index, label in enumerate(labels, start=1)
    ]


def research_readiness() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "GP",
            "status": "human_decision_required",
            "machine_findings": ["material 2021-04-26 volume disagreement is explicitly held"],
            "human_decisions": ["adjusted source", "unadjusted source", "conflict treatment"],
            "activation": False,
        },
        {
            "symbol": "ACI",
            "status": "human_decision_required",
            "machine_findings": ["single conflict isolated with no automatic winner"],
            "human_decisions": ["2021-04-26 conflict", "source hierarchy"],
            "activation": False,
        },
        {
            "symbol": "BRACBANK",
            "status": "human_decision_required",
            "machine_findings": ["two conflicts isolated separately with no automatic winner"],
            "human_decisions": ["two date decisions", "source hierarchy"],
            "activation": False,
        },
        {
            "symbol": "DSEX",
            "status": "not_ready",
            "machine_findings": [
                "5,295 alias rows remain unresolved",
                "680 invalid rows excluded",
                "volume is field_not_comparable",
            ],
            "human_decisions": ["alias treatment", "invalid-row policy", "price hierarchy"],
            "activation": False,
        },
    ]


def validate_pack_invariants(payload: dict[str, Any]) -> None:
    if len(payload["approval_decisions"]) != 15:
        raise ValueError("Approval pack must contain exactly 15 separate decisions")
    if len(payload["conflict_approval_records"]) != 6:
        raise ValueError("Approval pack must contain exactly six conflict records")
    allowed: set[Readiness] = {
        "not_ready",
        "human_decision_required",
        "ready_for_research_activation_review",
        "rejected",
    }
    if any(row["status"] not in allowed for row in payload["readiness"]):
        raise ValueError("Invalid research-readiness status")
    if payload["activation_permission"] != "REJECTED / NOT GRANTED":
        raise ValueError("Research activation must default to rejected")
    if payload["qualification"] != "0/60":
        raise ValueError("Qualification changed")
    if any(row.get("active") for row in payload["source_hierarchies"]):
        raise ValueError("A source hierarchy was activated")

from __future__ import annotations

import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

TARGET_SYMBOLS = (
    "IDLC",
    "LANKABAFIN",
    "BATBC",
    "SQURPHARMA",
    "RENATA",
    "GP",
    "ACI",
    "BRACBANK",
    "POWERGRID",
    "SUMITPOWER",
    "BERGERPBL",
    "OLYMPIC",
)
SECONDARY_SYMBOLS = (
    "HEIDELBCEM",
    "PREMIERCEM",
    "WALTONHIL",
    "RSRMSTEEL",
    "BSRMLTD",
    "AMCL(PRAN)",
    "TITASGAS",
    "GREENDELT",
    "RELIANCINS",
    "UNILEVERCL",
    "MARICO",
    "SQUARETEXT",
    "PARAMOUNT",
)
SECTORS = {
    "GP": "telecommunication",
    "ACI": "pharmaceuticals_chemicals",
    "BRACBANK": "bank",
    "IDLC": "financial_institution",
    "LANKABAFIN": "financial_institution",
    "BATBC": "food_allied",
    "SQURPHARMA": "pharmaceuticals_chemicals",
    "RENATA": "pharmaceuticals_chemicals",
    "POWERGRID": "fuel_power",
    "SUMITPOWER": "fuel_power",
    "BERGERPBL": "miscellaneous",
    "OLYMPIC": "food_allied",
}
PROFILE_SQL = """SELECT normalized_symbol, COUNT(*) AS total_rows,
SUM(CASE WHEN accepted_for_candidate=1 THEN 1 ELSE 0 END) AS valid_rows,
SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END) AS invalid_rows,
MIN(CASE WHEN accepted_for_candidate=1 THEN trading_date END) AS first_valid_date,
MAX(CASE WHEN accepted_for_candidate=1 THEN trading_date END) AS last_valid_date,
SUM(CASE WHEN adjustment_status='adjusted' AND accepted_for_candidate=1 THEN 1 ELSE 0 END) AS adjusted_rows,
SUM(CASE WHEN adjustment_status='unadjusted' AND accepted_for_candidate=1 THEN 1 ELSE 0 END) AS unadjusted_rows,
SUM(CASE WHEN strftime('%w',trading_date) IN ('0','6') THEN 1 ELSE 0 END) AS weekend_rows,
SUM(CASE WHEN volume IS NOT NULL AND CAST(volume AS REAL)>0 THEN 1 ELSE 0 END) AS positive_volume_rows,
SUM(CASE WHEN mapping_confidence='high' THEN 1 ELSE 0 END) AS high_mapping_rows
FROM observations WHERE normalized_symbol IN ({placeholders}) GROUP BY normalized_symbol"""


def _placeholders() -> str:
    return ",".join("?" for _ in TARGET_SYMBOLS)


def _load_scores(path: Path) -> tuple[dict[str, float], dict[str, str]]:
    scores = json.loads((path / "source_quality_scores.json").read_text(encoding="utf-8"))
    inventory = json.loads((path / "dataset_inventory.json").read_text(encoding="utf-8"))
    quality = {str(row["logical_name"]): float(row["score"]) for row in scores}
    licenses = {str(row["source_name"]): str(row["license_status"]) for row in inventory}
    return quality, licenses


def _parse(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _dates_by_symbol(db: sqlite3.Connection) -> dict[str, list[str]]:
    rows = db.execute(
        f"SELECT normalized_symbol,trading_date FROM observations WHERE normalized_symbol IN ({_placeholders()}) AND accepted_for_candidate=1 AND trading_date IS NOT NULL GROUP BY normalized_symbol,trading_date ORDER BY normalized_symbol,trading_date",  # noqa: S608
        TARGET_SYMBOLS,
    )
    result: dict[str, list[str]] = defaultdict(list)
    for symbol, trading_date in rows:
        result[str(symbol)].append(str(trading_date))
    return result


def _surrounding(dates: list[str], current: str) -> dict[str, str | None]:
    index = bisect.bisect_left(dates, current)
    return {
        "previous_valid_date": dates[index - 1] if index > 0 else None,
        "next_valid_date": dates[index + 1] if index + 1 < len(dates) else None,
    }


def _source_profiles(
    db: sqlite3.Connection, quality: dict[str, float], licenses: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    query = f"""SELECT normalized_symbol,source_name,adjustment_status,COUNT(*),
    SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END),MIN(trading_date),MAX(trading_date)
    FROM observations WHERE normalized_symbol IN ({_placeholders()})
    GROUP BY normalized_symbol,source_name,adjustment_status"""  # noqa: S608
    conflicts: Counter[tuple[str, str]] = Counter()
    for symbol, source_a, source_b in db.execute(
        f"SELECT normalized_symbol,source_name_a,source_name_b FROM cross_source_comparisons WHERE normalized_symbol IN ({_placeholders()}) AND final_review_status='unresolved'",  # noqa: S608
        TARGET_SYMBOLS,
    ):
        conflicts[(str(symbol), str(source_a))] += 1
        conflicts[(str(symbol), str(source_b))] += 1
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol, source, adjustment, rows, invalid, start, end in db.execute(query, TARGET_SYMBOLS):
        source_name = str(source)
        result[str(symbol)].append(
            {
                "source": source_name,
                "adjustment_status": str(adjustment),
                "coverage_start": start,
                "coverage_end": end,
                "rows": int(rows),
                "quality_score": quality.get(source_name),
                "conflict_burden": conflicts[(str(symbol), source_name)],
                "invalid_row_burden": int(invalid or 0),
                "license_status": licenses.get(source_name, "registered_license_note_unavailable"),
                "human_approval": "",
            }
        )
    return result


def propose_source_hierarchy(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    primary_minimum_rows = 252

    def role_score(row: dict[str, Any]) -> float:
        row_count = max(int(row["rows"]), 1)
        quality = float(row["quality_score"]) if row["quality_score"] is not None else 0.0
        coverage = min(row_count / 2500, 1) * 25
        conflict_penalty = min(int(row["conflict_burden"]) / row_count, 1) * 10
        invalid_penalty = min(int(row["invalid_row_burden"]) / row_count, 1) * 20
        return round(quality + coverage - conflict_penalty - invalid_penalty, 4)

    ranked = sorted(
        rows,
        key=lambda row: (
            -role_score(row),
            int(row["invalid_row_burden"]),
            int(row["conflict_burden"]),
            str(row["source"]),
        ),
    )

    def primary(adjustment: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in ranked
                if row["adjustment_status"] == adjustment
                and int(row["rows"]) >= primary_minimum_rows
            ),
            None,
        )

    adjusted, unadjusted = primary("adjusted"), primary("unadjusted")
    assigned = {id(row) for row in (adjusted, unadjusted) if row is not None}
    remaining = [row for row in ranked if id(row) not in assigned]
    roles: list[tuple[str, dict[str, Any] | None]] = [
        ("primary_adjusted_source", adjusted),
        ("primary_unadjusted_source", unadjusted),
        ("secondary_validation_source", remaining[0] if remaining else None),
        ("fallback_source", remaining[1] if len(remaining) > 1 else None),
        (
            "rejected_source",
            next(
                (
                    row
                    for row in reversed(ranked)
                    if int(row["invalid_row_burden"]) > 0 or row["adjustment_status"] == "unknown"
                ),
                None,
            ),
        ),
        (
            "unresolved_source",
            next((row for row in ranked if int(row["conflict_burden"]) > 0), None),
        ),
    ]
    output = []
    for role, row in roles:
        output.append(
            {
                "role": role,
                "source": row["source"] if row else None,
                "evidence": "registered canonical-candidate observations and source-quality ledger"
                if row
                else "no eligible source identified",
                "coverage": {
                    "start": row["coverage_start"],
                    "end": row["coverage_end"],
                    "rows": row["rows"],
                }
                if row
                else None,
                "quality_score": row["quality_score"] if row else None,
                "source_selection_score": role_score(row) if row else None,
                "conflict_burden": row["conflict_burden"] if row else None,
                "invalid_row_burden": row["invalid_row_burden"] if row else None,
                "adjustment_status": row["adjustment_status"] if row else None,
                "rationale": (
                    "primary roles require at least 252 symbol observations; highest "
                    "conservative selection score then combines registered quality, usable "
                    "coverage, conflict burden, and invalid-row burden; no automatic approval"
                )
                if row
                else "human evidence required",
                "risk": "third-party research provenance, unresolved conflicts, and lifecycle evidence pending",
                "human_approval": "",
            }
        )
    return output


def _conflicts(
    db: sqlite3.Connection,
    dates: dict[str, list[str]],
    quality: dict[str, float],
) -> list[dict[str, Any]]:
    query = f"""SELECT normalized_symbol,trading_date,source_name_a,source_name_b,
    adjustment_a,adjustment_b,values_a,values_b,percentage_differences,
    possible_corporate_action,evidence_quality FROM cross_source_comparisons
    WHERE normalized_symbol IN ({_placeholders()}) AND final_review_status='unresolved'
    ORDER BY normalized_symbol,trading_date,source_name_a,source_name_b"""  # noqa: S608
    output = []
    for row in db.execute(query, TARGET_SYMBOLS):
        symbol, trading_date, source_a, source_b = map(str, row[:4])
        output.append(
            {
                "symbol": symbol,
                "date": trading_date,
                "source_a": source_a,
                "source_b": source_b,
                "source_a_values": _parse(row[6]),
                "source_b_values": _parse(row[7]),
                "adjustment_a": row[4],
                "adjustment_b": row[5],
                "source_a_quality": quality.get(source_a),
                "source_b_quality": quality.get(source_b),
                "percentage_difference": _parse(row[8]),
                **_surrounding(dates[symbol], trading_date),
                "corporate_action_relationship": row[9],
                "evidence_quality": row[10],
                "recommended_action": "hold_for_review",
                "reviewer_decision": "",
                "operator_decision": "",
            }
        )
    return output


def classify_corporate_action(candidate_type: str, adjusted: Any, unadjusted: Any) -> str:
    if candidate_type == "possible_suspension_resumption":
        return "suspension_resumption_candidate"
    if candidate_type == "ordinary_market_movement":
        return "ordinary_gap"
    if candidate_type in {
        "probable_split",
        "probable_bonus_share_adjustment",
        "probable_rights_issue",
        "probable_dividend_adjustment",
    }:
        return "adjustment_divergence" if adjusted and unadjusted else "likely_corporate_action"
    if candidate_type == "unresolved":
        return "insufficient_evidence"
    return "data_gap"


def lifecycle_evidence(first_valid_date: str, last_valid_date: str) -> dict[str, Any]:
    return {
        "observed_first_valid_date": first_valid_date,
        "observed_last_valid_date": last_valid_date,
        "official_listing_evidence": None,
        "official_delisting_evidence": None,
        "suspension_evidence": None,
        "lifecycle_status": "lifecycle_evidence_pending",
        "unresolved_lifecycle_assumptions": [
            "observed bounds are not verified listing bounds",
            "long gaps are not verified suspensions",
        ],
    }


def _corporate_actions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    query = f"""SELECT normalized_symbol,trading_date,source_dataset_id,candidate_type,
    previous_close,current_close,adjusted_close,unadjusted_close,volume_change,evidence,review_status
    FROM corporate_action_candidates WHERE normalized_symbol IN ({_placeholders()})
    ORDER BY normalized_symbol,trading_date,source_dataset_id"""  # noqa: S608
    return [
        {
            "symbol": row[0],
            "date": row[1],
            "source_dataset_id": row[2],
            "candidate_type": row[3],
            "classification": classify_corporate_action(str(row[3]), row[6], row[7]),
            "previous_close": row[4],
            "current_close": row[5],
            "adjusted_close": row[6],
            "unadjusted_close": row[7],
            "volume_change": row[8],
            "evidence": json.loads(row[9]) if row[9] else [],
            "review_status": row[10],
            "reconstructed": False,
            "future_inclusion": "excluded_until_separately_approved",
        }
        for row in db.execute(query, TARGET_SYMBOLS)
    ]


def _candidate_tiers(
    db: sqlite3.Connection,
    conflicts: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    conflict_dates = {(row["symbol"], row["date"]) for row in conflicts}
    action_dates = {(row["symbol"], row["date"]) for row in actions}
    counts: dict[str, Counter[str]] = {symbol: Counter() for symbol in TARGET_SYMBOLS}
    query = f"""SELECT normalized_symbol,trading_date,adjustment_status,
    COUNT(DISTINCT source_dataset_id),MAX(mapping_confidence),
    SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END)
    FROM observations WHERE normalized_symbol IN ({_placeholders()})
    GROUP BY normalized_symbol,trading_date,adjustment_status"""  # noqa: S608
    for symbol, trading_date, adjustment, sources, mapping, invalid in db.execute(
        query, TARGET_SYMBOLS
    ):
        key = (str(symbol), str(trading_date))
        if invalid:
            tier = "rejected_invalid"
        elif (
            adjustment not in {"adjusted", "unadjusted"}
            or mapping != "high"
            or key in conflict_dates
            or key in action_dates
        ):
            tier = "held_for_review"
        elif int(sources) >= 2:
            tier = "tier_1_cross_source_confirmed"
        elif int(sources) == 1:
            tier = "tier_2_single_source_high_quality"
        else:
            tier = "tier_3_research_only"
        counts[str(symbol)][tier] += 1
    normalized = {
        symbol: {
            tier: values.get(tier, 0)
            for tier in (
                "tier_1_cross_source_confirmed",
                "tier_2_single_source_high_quality",
                "tier_3_research_only",
                "held_for_review",
                "rejected_invalid",
            )
        }
        for symbol, values in counts.items()
    }
    policy = {
        "status": "inactive_candidate_policy_only",
        "requirements": [
            "OHLC valid",
            "high-confidence mapping",
            "known adjustment status",
            "complete registered lineage",
            "not conflict-held",
            "not corporate-action-held",
            "within provisional observed bounds",
            "source selection proposed but not approved",
        ],
        "row_activation": False,
    }
    return normalized, policy


def build_final_batch_review(database_path: Path, evidence_dir: Path) -> dict[str, Any]:
    quality, licenses = _load_scores(evidence_dir)
    db = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        dates = _dates_by_symbol(db)
        profiles_raw = {
            str(row[0]): row
            for row in db.execute(PROFILE_SQL.format(placeholders=_placeholders()), TARGET_SYMBOLS)
        }
        duplicates: dict[str, Counter[str]] = {symbol: Counter() for symbol in TARGET_SYMBOLS}
        for symbol, duplicate_type, groups in db.execute(
            f"SELECT normalized_symbol,duplicate_type,COUNT(*) FROM duplicate_groups WHERE normalized_symbol IN ({_placeholders()}) GROUP BY normalized_symbol,duplicate_type",  # noqa: S608
            TARGET_SYMBOLS,
        ):
            duplicates[str(symbol)][str(duplicate_type)] = int(groups)
        source_profiles = _source_profiles(db, quality, licenses)
        conflicts = _conflicts(db, dates, quality)
        actions = _corporate_actions(db)
        conflict_counts = Counter(str(row["symbol"]) for row in conflicts)
        action_counts = Counter(str(row["symbol"]) for row in actions)
        suspension_counts = Counter(
            str(row["symbol"])
            for row in actions
            if row["classification"] == "suspension_resumption_candidate"
        )
        tiers, policy = _candidate_tiers(db, conflicts, actions)
        symbols: list[dict[str, Any]] = []
        for symbol in TARGET_SYMBOLS:
            row = profiles_raw[symbol]
            total, valid, invalid = int(row[1]), int(row[2]), int(row[3])
            missing_gaps = max(
                len(dates[symbol])
                - 1
                - sum(
                    (
                        date.fromisoformat(dates[symbol][index])
                        - date.fromisoformat(dates[symbol][index - 1])
                    ).days
                    == 1
                    for index in range(1, len(dates[symbol]))
                ),
                0,
            )
            hierarchy = propose_source_hierarchy(source_profiles[symbol])
            if invalid / max(total, 1) > 0.02:
                readiness = "cleaning_required"
            elif conflict_counts[symbol]:
                readiness = "conflict_review_required"
            elif row[4] is None or row[5] is None:
                readiness = "not_ready"
            else:
                readiness = "lifecycle_review_required"
            symbols.append(
                {
                    "symbol": symbol,
                    "sector": SECTORS[symbol],
                    "observed_coverage": {"first_valid_date": row[4], "last_valid_date": row[5]},
                    "adjusted_valid_rows": int(row[6]),
                    "unadjusted_valid_rows": int(row[7]),
                    "valid_rows": valid,
                    "invalid_rows": invalid,
                    "duplicate_groups": sum(duplicates[symbol].values()),
                    "exact_duplicates": duplicates[symbol].get("duplicate_exact", 0),
                    "conflicting_duplicates": (
                        duplicates[symbol].get("conflicting_ohlc", 0)
                        + duplicates[symbol].get("same_price_different_volume", 0)
                    ),
                    "eligible_cross_source_conflicts": conflict_counts[symbol],
                    "missing_date_gaps": missing_gaps,
                    "weekend_rows": int(row[8]),
                    "corporate_action_held_rows": action_counts[symbol],
                    "suspension_candidates": suspension_counts[symbol],
                    "source_quality_ranking": sorted(
                        source_profiles[symbol],
                        key=lambda item: (-(item["quality_score"] or -1), item["source"]),
                    ),
                    "provisional_source_hierarchy": hierarchy,
                    "license_status": sorted(
                        {item["license_status"] for item in source_profiles[symbol]}
                    ),
                    "mapping_confidence": {
                        "high_rate": round(int(row[10]) / max(total, 1), 6),
                        "verified": False,
                    },
                    "liquidity_data_availability": round(int(row[9]) / max(valid, 1), 6),
                    "readiness_status": readiness,
                    **lifecycle_evidence(str(row[4]), str(row[5])),
                    "candidate_tiers": tiers[symbol],
                    "inclusion_permission": "REJECTED / NOT GRANTED",
                }
            )
        diversity = portfolio_diversity(symbols)
        return {
            "scope": list(TARGET_SYMBOLS),
            "symbols": symbols,
            "source_hierarchy_approval_required": True,
            "conflicts": conflicts,
            "corporate_actions": actions,
            "candidate_policy": policy,
            "portfolio_diversity": diversity,
            "secondary_symbols": [
                {"symbol": symbol, "status": "rejected_not_granted", "deep_reviewed": False}
                for symbol in SECONDARY_SYMBOLS
            ],
            "dsex": {
                "separate": True,
                "rejected": True,
                "inactive": True,
                "benchmark_available": False,
                "modified": False,
            },
            "activation_permission": "REJECTED / NOT GRANTED",
            "strategy_executed": False,
            "qualification": "0/60",
        }
    finally:
        db.close()


def portfolio_diversity(symbols: list[dict[str, Any]]) -> dict[str, Any]:
    sector_counts = Counter(str(row["sector"]) for row in symbols)
    source_counts = Counter(
        str(
            next(
                item
                for item in row["provisional_source_hierarchy"]
                if item["role"] == "primary_adjusted_source"
            )["source"]
        )
        for row in symbols
    )
    first = max(str(row["observed_coverage"]["first_valid_date"]) for row in symbols)
    last = min(str(row["observed_coverage"]["last_valid_date"]) for row in symbols)
    tiers: Counter[str] = Counter()
    for row in symbols:
        tiers.update(row["candidate_tiers"])
    return {
        "symbol_count": len(symbols),
        "equal_symbol_weight_percent": round(100 / len(symbols), 4),
        "sector_counts": dict(sector_counts),
        "maximum_sector_weight_percent": round(max(sector_counts.values()) / len(symbols) * 100, 4),
        "primary_adjusted_source_counts": dict(source_counts),
        "maximum_primary_source_weight_percent": round(
            max(source_counts.values()) / len(symbols) * 100, 4
        ),
        "common_observed_coverage": {"start": first, "end": last},
        "survivorship_risk": "high_lifecycle_evidence_pending",
        "corporate_action_exposure_rows": sum(
            int(row["corporate_action_held_rows"]) for row in symbols
        ),
        "minimum_liquidity_data_availability": min(
            float(row["liquidity_data_availability"]) for row in symbols
        ),
        "tier_counts": dict(tiers),
        "performance_data_used": False,
    }


def approval_decisions() -> list[dict[str, Any]]:
    decisions = []
    labels = (
        "adjusted source",
        "unadjusted source",
        "validation source",
        "conflict treatment",
        "corporate-action treatment",
        "lifecycle treatment",
        "inclusion permission",
        "exclusion rules",
    )
    for symbol in TARGET_SYMBOLS:
        for index, label in enumerate(labels, 1):
            decisions.append(
                {
                    "decision_id": f"{symbol.lower()}-{index:02d}",
                    "symbol": symbol,
                    "decision": label,
                    "default": "REJECTED / NOT GRANTED"
                    if label == "inclusion permission"
                    else "HOLD FOR HUMAN REVIEW",
                    "reviewer_decision": "",
                    "operator_decision": "",
                }
            )
    return decisions

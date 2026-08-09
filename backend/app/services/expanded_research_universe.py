from __future__ import annotations

import calendar
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.services.pilot_conflict_methodology import build_pilot_methodology_audit

TARGET_UNIVERSE_SIZE = 25
MAX_PER_SECTOR = 3
MIN_VALID_ROWS = 1500
CONTINUITY_ANCHORS = ("GP", "ACI", "BRACBANK")
ACTIVE_RESEARCH_UNIVERSE = (
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
)
FROZEN_STRATEGY_IDENTITIES = (
    "ma_crossover@1.0.0",
    "cross_sectional_momentum@0.1.0",
    "defensive_low_volatility@0.1.0",
    "absolute_momentum_filter@0.1.0",
)
EXPANSION_CANDIDATE_COUNT = 15
MIN_EXPANSION_TIER_2_ROWS = 5000
MIN_EXPANSION_ADJUSTED_TIER_2_ROWS = 2000
MAX_EXPANSION_INVALID_RATE = 0.02
MAX_EXPANSION_CONFLICT_RATE = 0.001
MAX_EXPANSION_DUPLICATE_CONFLICT_RATE = 0.01


@dataclass(frozen=True)
class CandidateSpec:
    symbol: str
    sector: str
    listing_date: str | None = None
    delisting_date: str | None = None
    suspension_periods: tuple[tuple[str, str], ...] = ()


# Provisional sectors organize review only. They are not exchange-verified metadata.
# The pool was declared without consulting strategy returns.
CANDIDATE_POOL = (
    CandidateSpec("GP", "telecommunication"),
    CandidateSpec("ROBI", "telecommunication"),
    CandidateSpec("BRACBANK", "bank"),
    CandidateSpec("CITYBANK", "bank"),
    CandidateSpec("EBL", "bank"),
    CandidateSpec("DUTCHBANGL", "bank"),
    CandidateSpec("JAMUNABANK", "bank"),
    CandidateSpec("IDLC", "financial_institution"),
    CandidateSpec("LANKABAFIN", "financial_institution"),
    CandidateSpec("DBH", "financial_institution"),
    CandidateSpec("SQURPHARMA", "pharmaceuticals_chemicals"),
    CandidateSpec("RENATA", "pharmaceuticals_chemicals"),
    CandidateSpec("ACI", "pharmaceuticals_chemicals"),
    CandidateSpec("BEXIMCO", "pharmaceuticals_chemicals"),
    CandidateSpec("BATBC", "food_allied"),
    CandidateSpec("OLYMPIC", "food_allied"),
    CandidateSpec("AMCL(PRAN)", "food_allied"),
    CandidateSpec("HEIDELBCEM", "cement"),
    CandidateSpec("LHBL", "cement"),
    CandidateSpec("PREMIERCEM", "cement"),
    CandidateSpec("WALTONHIL", "engineering"),
    CandidateSpec("BSRMLTD", "engineering"),
    CandidateSpec("GPHISPAT", "engineering"),
    CandidateSpec("RSRMSTEEL", "engineering"),
    CandidateSpec("SUMITPOWER", "fuel_power"),
    CandidateSpec("POWERGRID", "fuel_power"),
    CandidateSpec("MJLBD", "fuel_power"),
    CandidateSpec("TITASGAS", "fuel_power"),
    CandidateSpec("SQUARETEXT", "textile"),
    CandidateSpec("ENVOYTEX", "textile"),
    CandidateSpec("PARAMOUNT", "textile"),
    CandidateSpec("GREENDELT", "insurance"),
    CandidateSpec("RELIANCINS", "insurance"),
    CandidateSpec("BERGERPBL", "miscellaneous"),
    CandidateSpec("MARICO", "miscellaneous"),
    CandidateSpec("UNILEVERCL", "miscellaneous"),
)


def _coverage_days(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


def freeze_independent_universe_candidate(
    methodology: dict[str, Any],
    *,
    specs: Sequence[CandidateSpec] | None = None,
) -> dict[str, Any]:
    """Freeze a review-only expansion using corrected row dispositions, never returns."""
    active = set(ACTIVE_RESEARCH_UNIVERSE)
    candidate_specs = tuple(
        specs
        if specs is not None
        else (spec for spec in CANDIDATE_POOL if spec.symbol not in active)
    )
    expected_symbols = {spec.symbol for spec in candidate_specs}
    if set(methodology["scope"]) != expected_symbols:
        raise ValueError("Corrected methodology scope does not match the expansion candidate pool")

    lifecycle_by_symbol = {str(row["symbol"]): row for row in methodology["lifecycle_evidence"]}
    summary_by_symbol = {str(row["symbol"]): row for row in methodology["symbol_summary"]}
    dispositions_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in methodology["candidates"]:
        dispositions_by_symbol[str(row["symbol"])].append(row)

    candidate_rows: list[dict[str, Any]] = []
    for spec in candidate_specs:
        summary = summary_by_symbol[spec.symbol]
        dispositions = dispositions_by_symbol[spec.symbol]
        tier_2_grains = Counter(
            str(row["adjustment_status"])
            for row in dispositions
            if row["status"] == "tier_2_single_source_high_quality"
        )
        tier_3_grains = Counter(
            str(row["adjustment_status"])
            for row in dispositions
            if row["status"] == "tier_3_research_only"
        )
        tier_2_sources = sorted(
            {
                str(source)
                for row in dispositions
                if row["status"] == "tier_2_single_source_high_quality"
                for source in row["source_names"]
            }
        )
        lifecycle = lifecycle_by_symbol[spec.symbol]
        coverage = lifecycle["conservative_research_window"]
        raw_rows = int(summary["raw_rows"])
        logical_rows = int(summary["logical_rows"])
        tier_2_rows = int(summary["tier_2_single_source_high_quality"])
        adjusted_tier_2_rows = tier_2_grains["adjusted"]
        conflict_count = int(summary["held_genuine_conflict"])
        invalid_rows = int(summary["rejected_invalid"])
        duplicate_conflicts = int(summary["rejected_duplicate_conflict"])
        invalid_rate = _safe_rate(invalid_rows, raw_rows)
        conflict_rate = _safe_rate(conflict_count, logical_rows)
        duplicate_conflict_rate = _safe_rate(duplicate_conflicts, logical_rows)
        blockers: list[str] = []
        if tier_2_rows < MIN_EXPANSION_TIER_2_ROWS:
            blockers.append("tier_2_rows_below_5000")
        if adjusted_tier_2_rows < MIN_EXPANSION_ADJUSTED_TIER_2_ROWS:
            blockers.append("adjusted_tier_2_rows_below_2000")
        if invalid_rate > MAX_EXPANSION_INVALID_RATE:
            blockers.append("invalid_rate_above_2_percent")
        if conflict_rate > MAX_EXPANSION_CONFLICT_RATE:
            blockers.append("genuine_conflict_rate_above_0_1_percent")
        if duplicate_conflict_rate > MAX_EXPANSION_DUPLICATE_CONFLICT_RATE:
            blockers.append("duplicate_conflict_rate_above_1_percent")
        if not tier_2_sources:
            blockers.append("no_tier_2_source_lineage")
        candidate_rows.append(
            {
                "record_type": "candidate",
                "symbol": spec.symbol,
                "sector": spec.sector,
                "sector_evidence": "provisional_operator_review_catalog_not_exchange_verified",
                "sources": tier_2_sources,
                "coverage_start": coverage["start"],
                "coverage_end": coverage["end"],
                "coverage_basis": coverage["basis"],
                "coverage_calendar_days": _coverage_days(coverage["start"], coverage["end"]),
                "tier_1_rows": int(summary["tier_1_cross_source_confirmed"]),
                "tier_2_rows": tier_2_rows,
                "tier_2_adjusted_rows": adjusted_tier_2_rows,
                "tier_2_unadjusted_rows": tier_2_grains["unadjusted"],
                "tier_3_rows": int(summary["tier_3_research_only"]),
                "tier_3_adjustment_grains": dict(sorted(tier_3_grains.items())),
                "genuine_conflicts": conflict_count,
                "lifecycle_holds": int(summary["lifecycle_holds"]),
                "lifecycle_status": lifecycle["lifecycle_status"],
                "invalid_rows": invalid_rows,
                "invalid_rate": round(invalid_rate, 8),
                "duplicate_groups": int(summary["duplicate_groups"]),
                "exact_duplicates_collapsed": int(summary["exact_duplicates_collapsed"]),
                "duplicate_conflicts": duplicate_conflicts,
                "validation_comparison_dates": int(summary["validation_comparison_dates"]),
                "source_independence": summary["validation_independence"],
                "known_adjustment_grain": tier_2_rows > 0,
                "complete_lineage": tier_2_rows > 0,
                "high_confidence_mapping": tier_2_rows > 0,
                "quality_gate_passed": not blockers,
                "quality_gate_blockers": blockers,
                "recommended": False,
                "active": False,
                "readiness_status": "not_recommended"
                if blockers
                else "quality_eligible_not_selected",
            }
        )

    active_sector_counts = Counter(spec.sector for spec in CANDIDATE_POOL if spec.symbol in active)
    recommended: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    sector_counts = Counter(active_sector_counts)
    eligible = [row for row in candidate_rows if row["quality_gate_passed"]]

    def quality_order(row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            -int(row["tier_2_adjusted_rows"]),
            -int(row["tier_2_rows"]),
            -int(row["coverage_calendar_days"]),
            int(row["genuine_conflicts"]),
            int(row["invalid_rows"]),
            int(row["duplicate_conflicts"]),
            -int(row["validation_comparison_dates"]),
            str(row["symbol"]),
        )

    def add(row: dict[str, Any]) -> None:
        sector = str(row["sector"])
        symbol = str(row["symbol"])
        if symbol in selected_symbols or sector_counts[sector] >= MAX_PER_SECTOR:
            return
        recommended.append(row)
        selected_symbols.add(symbol)
        sector_counts[sector] += 1

    missing_sectors = sorted({str(row["sector"]) for row in eligible} - set(active_sector_counts))
    for sector in missing_sectors:
        add(min((row for row in eligible if row["sector"] == sector), key=quality_order))
    for row in sorted(eligible, key=quality_order):
        add(row)
        if len(recommended) == EXPANSION_CANDIDATE_COUNT:
            break
    if len(recommended) != EXPANSION_CANDIDATE_COUNT:
        raise ValueError("Data-quality and sector gates could not produce 15 expansion candidates")

    for row in candidate_rows:
        if row["symbol"] in selected_symbols:
            row["recommended"] = True
            row["readiness_status"] = "recommended_pending_human_review_not_active"
        elif row["quality_gate_passed"]:
            row["readiness_status"] = "eligible_not_selected_under_size_and_sector_cap"

    conflict_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in methodology["conflict_approval_records"]:
        conflict_records[str(row["symbol"])].append(row)
    review_queue: list[dict[str, Any]] = []
    for symbol in sorted(selected_symbols):
        row = next(item for item in candidate_rows if item["symbol"] == symbol)
        review_queue.append(
            {
                "record_type": "human_review",
                "review_type": "lifecycle_evidence",
                "symbol": symbol,
                "decision_required": "confirm or conservatively approve the observed research window",
                "evidence_status": row["lifecycle_status"],
                "default_action": "hold_not_active",
            }
        )
        if conflict_records[symbol]:
            review_queue.append(
                {
                    "record_type": "human_review",
                    "review_type": "genuine_conflict_resolution",
                    "symbol": symbol,
                    "decision_required": "resolve each same-grain OHLC disagreement without averaging",
                    "conflict_count": len(conflict_records[symbol]),
                    "dates": sorted({str(item["date"]) for item in conflict_records[symbol]}),
                    "default_action": "hold_conflicting_rows_not_active",
                }
            )
    review_queue.append(
        {
            "record_type": "human_review",
            "review_type": "sector_evidence",
            "symbol": "MULTIPLE",
            "symbols": sorted(selected_symbols),
            "decision_required": "confirm provisional sector labels from registered authoritative evidence",
            "default_action": "retain_provisional_labels_not_active",
        }
    )

    candidate_rows.sort(key=lambda row: str(row["symbol"]))
    recommended_symbols = [str(row["symbol"]) for row in recommended]
    return {
        "schema": "independent_universe_expansion_v1",
        "qualification": "0/60",
        "active_research_universe_unchanged": list(ACTIVE_RESEARCH_UNIVERSE),
        "frozen_strategy_identities_unchanged": list(FROZEN_STRATEGY_IDENTITIES),
        "strategy_calculations_performed": False,
        "strategy_execution": False,
        "activation": False,
        "selection_policy": {
            "performance_fields_read": False,
            "ranking_inputs": [
                "adjusted Tier-2 rows",
                "total Tier-2 rows",
                "known-adjustment coverage duration",
                "genuine-conflict count",
                "invalid-row count",
                "duplicate-conflict count",
                "distinct-file comparison dates",
                "provisional sector diversity",
            ],
            "minimum_tier_2_rows": MIN_EXPANSION_TIER_2_ROWS,
            "minimum_adjusted_tier_2_rows": MIN_EXPANSION_ADJUSTED_TIER_2_ROWS,
            "maximum_invalid_rate": MAX_EXPANSION_INVALID_RATE,
            "maximum_genuine_conflict_rate": MAX_EXPANSION_CONFLICT_RATE,
            "maximum_duplicate_conflict_rate": MAX_EXPANSION_DUPLICATE_CONFLICT_RATE,
            "maximum_total_symbols_per_provisional_sector": MAX_PER_SECTOR,
            "tier_1_requires_proven_independence": True,
        },
        "candidate_symbols_inspected": len(candidate_rows),
        "recommended_symbols": recommended_symbols,
        "expected_achievable_final_universe_size": len(ACTIVE_RESEARCH_UNIVERSE)
        + len(recommended_symbols),
        "candidates": candidate_rows,
        "human_review_queue": review_queue,
        "sector_counts_if_approved": dict(sorted(sector_counts.items())),
        "corrected_methodology_totals": methodology["totals"],
        "source_hierarchy": methodology["source_hierarchy"],
        "no_mutation_assertions": {
            "datasets_activated": 0,
            "strategies_registered": 0,
            "campaigns_created": 0,
            "sessions_created": 0,
            "signals_created": 0,
            "orders_created": 0,
            "transactions_created": 0,
            "fills_created": 0,
            "broker_connections": 0,
        },
    }


def build_independent_universe_expansion(
    database_path: Path,
    conflict_path: Path,
    source_quality_path: Path,
) -> dict[str, Any]:
    """Apply the existing corrected reconciler to all non-active candidates."""
    specs = tuple(spec for spec in CANDIDATE_POOL if spec.symbol not in ACTIVE_RESEARCH_UNIVERSE)
    methodology = build_pilot_methodology_audit(
        database_path,
        conflict_path,
        source_quality_path,
        symbols=tuple(spec.symbol for spec in specs),
    )
    return freeze_independent_universe_candidate(methodology, specs=specs)


def _weekday_count(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    total = 0
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            current = date(year, month, day)
            total += int(start_date <= current <= end_date and current.weekday() < 5)
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return total


def _safe_rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def survivorship_intervals(
    spec: CandidateSpec, observed_start: str | None, observed_end: str | None
) -> list[dict[str, Any]]:
    if not observed_start or not observed_end:
        return []
    start = max(value for value in (observed_start, spec.listing_date) if value)
    end = min(value for value in (observed_end, spec.delisting_date) if value)
    if start > end:
        return []
    intervals: list[tuple[date, date]] = [(date.fromisoformat(start), date.fromisoformat(end))]
    for suspended_from, suspended_to in sorted(spec.suspension_periods):
        suspended_start = date.fromisoformat(suspended_from)
        suspended_end = date.fromisoformat(suspended_to)
        next_intervals: list[tuple[date, date]] = []
        for interval_start, interval_end in intervals:
            if suspended_end < interval_start or suspended_start > interval_end:
                next_intervals.append((interval_start, interval_end))
                continue
            if interval_start < suspended_start:
                next_intervals.append(
                    (interval_start, suspended_start.fromordinal(suspended_start.toordinal() - 1))
                )
            if suspended_end < interval_end:
                next_intervals.append(
                    (suspended_end.fromordinal(suspended_end.toordinal() + 1), interval_end)
                )
        intervals = next_intervals
    basis = (
        "verified_listing_lifecycle_bounds"
        if spec.listing_date or spec.delisting_date or spec.suspension_periods
        else "first_to_last_valid_observation_proxy"
    )
    return [
        {
            "from": interval_start.isoformat(),
            "to": interval_end.isoformat(),
            "basis": basis,
            "approval": (
                "eligible_if_dataset_is_separately_approved"
                if basis == "verified_listing_lifecycle_bounds"
                else "rejected_pending_official_listing_and_suspension_evidence"
            ),
        }
        for interval_start, interval_end in intervals
    ]


def _profile_symbol(db: sqlite3.Connection, spec: CandidateSpec) -> dict[str, Any]:
    observation = db.execute(
        """SELECT COUNT(*),
        SUM(CASE WHEN accepted_for_candidate=1 THEN 1 ELSE 0 END),
        SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END),
        MIN(CASE WHEN accepted_for_candidate=1 THEN trading_date END),
        MAX(CASE WHEN accepted_for_candidate=1 THEN trading_date END),
        COUNT(DISTINCT CASE WHEN accepted_for_candidate=1 THEN trading_date END),
        SUM(CASE WHEN accepted_for_candidate=1 AND volume IS NOT NULL AND CAST(volume AS REAL)>0 THEN 1 ELSE 0 END),
        SUM(CASE WHEN mapping_confidence='high' THEN 1 ELSE 0 END)
        FROM observations WHERE normalized_symbol=? AND instrument_class='equity'""",
        (spec.symbol,),
    ).fetchone()
    total = int(observation[0] or 0)
    valid = int(observation[1] or 0)
    invalid = int(observation[2] or 0)
    start, end = observation[3], observation[4]
    distinct_dates = int(observation[5] or 0)
    positive_volume = int(observation[6] or 0)
    high_mapping = int(observation[7] or 0)
    duplicates = int(
        db.execute(
            "SELECT COALESCE(SUM(row_count-1),0) FROM duplicate_groups WHERE normalized_symbol=?",
            (spec.symbol,),
        ).fetchone()[0]
    )
    conflicts = int(
        db.execute(
            "SELECT COUNT(*) FROM cross_source_comparisons WHERE normalized_symbol=? AND final_review_status='unresolved'",
            (spec.symbol,),
        ).fetchone()[0]
    )
    corporate_actions = int(
        db.execute(
            "SELECT COUNT(*) FROM corporate_action_candidates WHERE normalized_symbol=? AND review_status!='approved'",
            (spec.symbol,),
        ).fetchone()[0]
    )
    adjustments = {
        str(row[0])
        for row in db.execute(
            "SELECT DISTINCT adjustment_status FROM observations WHERE normalized_symbol=?",
            (spec.symbol,),
        )
    }
    weekday_dates = _weekday_count(start, end)
    missing_dates = max(weekday_dates - distinct_dates, 0)
    invalid_rate = _safe_rate(invalid, total)
    duplicate_rate = _safe_rate(duplicates, total)
    conflict_rate = _safe_rate(conflicts, valid)
    mapping_rate = _safe_rate(high_mapping, total)
    liquidity_rate = _safe_rate(positive_volume, valid)
    adjustment_score = int("adjusted" in adjustments and "unadjusted" in adjustments)
    quality_score = round(
        min(valid / 2500, 1) * 30
        + max(0.0, 1 - invalid_rate) * 20
        + max(0.0, 1 - min(duplicate_rate, 1)) * 10
        + max(0.0, 1 - min(conflict_rate, 1)) * 10
        + adjustment_score * 10
        + mapping_rate * 10
        + liquidity_rate * 10,
        2,
    )
    if valid < 500:
        readiness = "not_ready"
    elif valid < MIN_VALID_ROWS or invalid_rate > 0.02 or duplicate_rate > 0.05:
        readiness = "cleaning_required"
    else:
        readiness = "review_required"
    exclusion_reasons = []
    if valid < MIN_VALID_ROWS:
        exclusion_reasons.append("insufficient valid daily coverage")
    if invalid_rate > 0.02:
        exclusion_reasons.append("invalid-row rate exceeds 2%")
    if duplicate_rate > 0.05:
        exclusion_reasons.append("duplicate burden exceeds 5%")
    if not adjustment_score:
        exclusion_reasons.append("adjusted and unadjusted views are not both present")
    selection_eligible = not exclusion_reasons
    return {
        **asdict(spec),
        "sector_source": "provisional_operator_review_catalog_not_exchange_verified",
        "total_rows": total,
        "valid_rows": valid,
        "invalid_rows": invalid,
        "duplicate_rows": duplicates,
        "eligible_conflicts": conflicts,
        "missing_weekday_dates_provisional": missing_dates,
        "corporate_action_held_rows": corporate_actions,
        "coverage_start": start,
        "coverage_end": end,
        "adjusted_available": "adjusted" in adjustments,
        "unadjusted_available": "unadjusted" in adjustments,
        "unknown_adjustment_available": "unknown" in adjustments,
        "mapping_high_confidence_rate": round(mapping_rate, 6),
        "positive_volume_rate": round(liquidity_rate, 6),
        "quality_score": quality_score,
        "research_readiness_status": readiness,
        "source_hierarchy_recommendation": "adjusted research view primary; unadjusted validation-only; conflicts held",
        "listing_evidence_status": "unknown_requires_official_confirmation",
        "delisting_evidence_status": "unknown_requires_official_confirmation",
        "suspension_evidence_status": "unknown_requires_official_confirmation",
        "time_varying_eligibility": survivorship_intervals(spec, start, end),
        "selection_eligible": selection_eligible,
        "selection_reason": (
            "quality thresholds passed; sector quota candidate; no performance metric used"
            if selection_eligible
            else None
        ),
        "exclusion_reason": "; ".join(exclusion_reasons) if exclusion_reasons else None,
        "proposed_activation_decision": "rejected_not_granted",
    }


def build_universe_candidate(database_path: Path) -> dict[str, Any]:
    db = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        profiles = [_profile_symbol(db, spec) for spec in CANDIDATE_POOL]
        selected, sectors = select_universe(profiles)
        if not 20 <= len(selected) <= 30:
            raise ValueError(f"Data-quality gates produced {len(selected)} symbols; required 20-30")
        selected_symbols = {str(row["symbol"]) for row in selected}
        excluded = [row for row in profiles if row["symbol"] not in selected_symbols]
        for row in excluded:
            if row["exclusion_reason"] is None:
                row["exclusion_reason"] = (
                    "not selected after continuity anchors, sector-diversity minimums, "
                    "sector cap, and target-size cap"
                )
        dsex = _build_dsex_track(db)
        return {
            "selection_policy": {
                "candidate_pool_predeclared": True,
                "performance_fields_read": False,
                "minimum_valid_rows": MIN_VALID_ROWS,
                "maximum_invalid_rate": 0.02,
                "maximum_duplicate_rate": 0.05,
                "maximum_per_sector": MAX_PER_SECTOR,
                "target_size": TARGET_UNIVERSE_SIZE,
                "continuity_anchors": list(CONTINUITY_ANCHORS),
                "ranking": "quality_score_desc_then_symbol_asc",
                "quality_components": [
                    "valid coverage",
                    "OHLC validity",
                    "duplicate burden",
                    "conflict burden",
                    "adjustment availability",
                    "mapping confidence",
                    "positive-volume availability",
                ],
            },
            "proposed_universe": selected,
            "excluded_candidates": excluded,
            "sector_counts": sectors,
            "activation_permission": "REJECTED / NOT GRANTED",
            "survivorship_control": {
                "current_only_universe_applied_to_history": False,
                "pre_listing_rows_allowed": False,
                "post_delisting_rows_allowed": False,
                "suspension_rows_allowed": False,
                "official_lifecycle_evidence_available": False,
                "fallback": "first/last valid observations are provisional bounds; activation rejected",
            },
            "dsex_track": dsex,
        }
    finally:
        db.close()


def select_universe(
    profiles: list[dict[str, Any]], target: int = TARGET_UNIVERSE_SIZE
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    eligible = sorted(
        (row for row in profiles if row["selection_eligible"]),
        key=lambda row: (-float(row["quality_score"]), str(row["symbol"])),
    )
    selected: list[dict[str, Any]] = []
    sectors: dict[str, int] = {}
    selected_symbols: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        symbol, sector = str(row["symbol"]), str(row["sector"])
        if symbol in selected_symbols or sectors.get(sector, 0) >= MAX_PER_SECTOR:
            return
        selected.append(row)
        selected_symbols.add(symbol)
        sectors[sector] = sectors.get(sector, 0) + 1

    by_symbol = {str(row["symbol"]): row for row in eligible}
    for symbol in CONTINUITY_ANCHORS:
        if symbol in by_symbol:
            add(by_symbol[symbol])
    for sector in sorted({str(row["sector"]) for row in eligible}):
        if sectors.get(sector, 0) == 0:
            add(next(row for row in eligible if row["sector"] == sector))
    for row in eligible:
        add(row)
        if len(selected) == target:
            break
    return selected, sectors


def _build_dsex_track(db: sqlite3.Connection) -> dict[str, Any]:
    rows = db.execute(
        """SELECT original_symbol,COUNT(*),
        SUM(CASE WHEN accepted_for_candidate=0 THEN 1 ELSE 0 END),
        MIN(trading_date),MAX(trading_date)
        FROM observations WHERE normalized_symbol='DSEX' OR original_symbol IN ('DSEX','00DSEX')
        GROUP BY original_symbol ORDER BY original_symbol"""
    ).fetchall()
    duplicate_rows = int(
        db.execute(
            "SELECT COALESCE(SUM(row_count-1),0) FROM duplicate_groups WHERE normalized_symbol='DSEX'"
        ).fetchone()[0]
    )
    return {
        "separate_from_equity_activation": True,
        "alias_groups": [
            {
                "raw_symbol": row[0],
                "rows": row[1],
                "invalid_rows": row[2],
                "start": row[3],
                "end": row[4],
            }
            for row in rows
        ],
        "duplicate_rows": duplicate_rows,
        "index_price_series_identified": True,
        "official_alias_mapping_verified": False,
        "volume_semantics": "non_comparable_excluded",
        "malformed_rows_preserved": True,
        "price_series_continuity_passed": False,
        "activation_decision": "rejected_not_granted",
        "required_human_review": [
            "official 00DSEX/DSEX alias confirmation",
            "index OHLC continuity and malformed-row decisions",
            "field-level separation of index price from market volume/value",
        ],
    }


def expanded_research_plan(symbols: list[str], sectors: list[str]) -> dict[str, Any]:
    return {
        "status": "prepared_not_authorized_not_executed",
        "strategy": "ma_crossover@1.0.0",
        "universe": symbols,
        "sectors": sectors,
        "design": [
            "per-symbol runs",
            "equal-weight portfolio",
            "sector-balanced portfolio",
            "leave-one-symbol-out",
            "leave-one-sector-out",
            "rolling walk-forward",
            "final untouched holdout",
            "buy-and-hold comparison",
            "cash baseline",
            "drawdown comparison",
            "return/drawdown ratio",
            "cost sensitivity",
            "slippage sensitivity",
            "source-tier sensitivity",
            "corporate-action sensitivity",
            "parameter stability",
        ],
        "mandatory_ablations": [
            "without BRACBANK",
            "without best-performing symbol",
            "without each sector",
            "stronger-data-quality symbols only",
            "stricter costs",
        ],
        "selection_freeze_before_execution": True,
        "performance_used_for_selection": False,
        "execution_authorized": False,
        "promotion_authorized": False,
        "campaign_authorized": False,
        "qualification": "0/60",
    }

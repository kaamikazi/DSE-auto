from __future__ import annotations

import hashlib
import itertools
import json
from copy import deepcopy
from typing import Any

from app.models import StrategyRegistration

ARCHIVED_STRATEGY = "ma_crossover@1.0.0"
PROPOSED_STRATEGY = "cross_sectional_momentum@0.1.0"

REJECTION_REASONS: tuple[dict[str, str], ...] = (
    {
        "code": "below_equal_weight_buy_and_hold",
        "reason": "return below equal-weight buy-and-hold",
    },
    {
        "code": "below_monthly_rebalancing",
        "reason": "return below monthly rebalancing",
    },
    {
        "code": "dominant_bracbank_dependence",
        "reason": "dominant dependence on BRACBANK",
    },
    {
        "code": "weak_leave_bracbank_out",
        "reason": "weak leave-BRACBANK-out result",
    },
    {
        "code": "inconsistent_walk_forward",
        "reason": "inconsistent walk-forward performance",
    },
    {
        "code": "whipsaw_majority",
        "reason": "more whipsaws than profitable trend captures",
    },
    {
        "code": "small_effective_trade_sample",
        "reason": "small effective trade sample",
    },
    {
        "code": "incomplete_lifecycle_evidence",
        "reason": "incomplete lifecycle evidence",
    },
    {
        "code": "incomplete_corporate_action_evidence",
        "reason": "incomplete corporate-action evidence",
    },
    {
        "code": "single_underlying_source_dependence",
        "reason": "heavy dependence on one underlying data source",
    },
    {
        "code": "drawdown_partly_low_exposure",
        "reason": "drawdown benefit partly explained by low average exposure",
    },
    {
        "code": "higher_cost_and_turnover",
        "reason": "higher turnover, fees, and slippage than simpler baselines",
    },
)

MOMENTUM_MEASUREMENTS = (
    "trailing_3_month_return",
    "trailing_6_month_return",
    "trailing_12_month_return",
    "trailing_12_month_return_excluding_most_recent_month",
)
REBALANCE_FREQUENCIES = ("monthly", "quarterly")
PORTFOLIO_SELECTIONS = ("top_3", "top_5", "top_25_percent")
WEIGHTING_METHODS = ("equal_weight", "inverse_volatility", "capped_inverse_volatility")


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def bounded_experiment_matrix() -> dict[str, Any]:
    rows = []
    combinations = itertools.product(
        MOMENTUM_MEASUREMENTS,
        REBALANCE_FREQUENCIES,
        PORTFOLIO_SELECTIONS,
        WEIGHTING_METHODS,
    )
    for index, (measurement, frequency, selection, weighting) in enumerate(combinations, start=1):
        rows.append(
            {
                "experiment_id": f"CSM-{index:03d}",
                "momentum_measurement": measurement,
                "rebalance_frequency": frequency,
                "portfolio_selection": selection,
                "weighting_method": weighting,
                "selection_status": "predeclared_not_evaluated",
                "performance_observed": False,
            }
        )
    return {
        "strategy": PROPOSED_STRATEGY,
        "matrix_status": "bounded_predeclared_not_executed",
        "configuration_count": len(rows),
        "configuration_selection": "none",
        "optimization_authorized": False,
        "rows": rows,
        "matrix_sha256": canonical_hash(rows),
    }


def survivorship_requirements() -> dict[str, Any]:
    return {
        "dated_symbol_eligibility": "required",
        "eligibility_start_rule": "no observation before observed or verified eligibility start",
        "delisting_rule": "no observation after verified delisting",
        "suspension_rule": "exclude supported suspension intervals; otherwise record unresolved evidence",
        "minimum_lookback": "complete lookback required for the selected predeclared candidate",
        "ohlc_validity": "valid finite OHLC with low <= open/close <= high",
        "adjustment_grain": "known and consistent",
        "lineage": "complete source-file and row lineage",
        "row_dispositions": "held, conflicting, malformed, and invalid rows excluded",
        "universe_membership": "formed only from information available at each rebalance date",
        "listing_date_boundary": "observed data bounds are not official listing dates",
        "forward_information_permitted": False,
    }


def portfolio_risk_controls() -> dict[str, Any]:
    return {
        "maximum_position_weight_percent": 20,
        "maximum_sector_weight_percent": 35,
        "minimum_holdings": 3,
        "leverage_permitted": False,
        "short_selling_permitted": False,
        "insufficient_eligibility_action": "hold unallocated capital in cash",
        "volatility_cap": {
            "status": "research_candidate_predeclared",
            "annualized_percent": 20,
            "estimation": "trailing 60 source-present sessions using information available before rebalance",
            "action": "scale risky allocation down to cash; never lever up",
            "fail_closed": "disable the volatility-weighted configuration when covariance evidence is insufficient",
        },
        "turnover": "measure one-way traded notional divided by average portfolio equity",
        "liquidity_filter": "permitted only after volume units and semantics are verified",
        "invented_volume_units_permitted": False,
    }


def new_strategy_specification() -> dict[str, Any]:
    return {
        "strategy": PROPOSED_STRATEGY,
        "family_relationship": "new_family_not_a_ma_crossover_modification",
        "lifecycle": "design",
        "registration": "absent",
        "implementation_status": "not_implemented",
        "execution_authorization": False,
        "promotion_eligibility": False,
        "campaign_eligibility": False,
        "qualification_contribution": 0,
        "hypothesis": (
            "At fixed rebalance intervals, ranking eligible DSE equities by past relative "
            "performance and allocating only to the strongest candidates may produce more "
            "diversified returns than per-symbol moving-average timing when explicit "
            "concentration and volatility controls are applied."
        ),
        "signal_candidates": list(MOMENTUM_MEASUREMENTS),
        "rebalance_candidates": list(REBALANCE_FREQUENCIES),
        "portfolio_candidates": list(PORTFOLIO_SELECTIONS),
        "weighting_candidates": list(WEIGHTING_METHODS),
        "eligibility_and_survivorship": survivorship_requirements(),
        "portfolio_risk_controls": portfolio_risk_controls(),
        "benchmarks": [
            "equal_weight_buy_and_hold",
            "monthly_equal_weight_rebalance",
            "cash",
            "archived_ma_crossover@1.0.0",
            "sector_balanced_buy_and_hold_when_sector_evidence_is_defensible",
        ],
        "dsex": "unavailable_and_not_substituted",
        "required_robustness": [
            "chronological_walk_forward",
            "untouched_final_holdout",
            "leave_best_symbol_out",
            "leave_one_symbol_out",
            "leave_one_sector_out",
            "equal_weight_vs_volatility_weighted",
            "rebalance_frequency_sensitivity",
            "momentum_lookback_sensitivity",
            "fee_slippage_stress",
            "source_tier_sensitivity",
            "corporate_action_sensitivity",
            "lifecycle_sensitivity",
        ],
        "failure_rule": "fail closed when results depend on one symbol, one sector, or one narrow configuration",
        "separate_future_authorizations": [
            "implementation",
            "registration",
            "execution",
        ],
        "combined_authorization_permitted": False,
    }


def data_requirement_report(current_symbol_count: int = 5) -> dict[str, Any]:
    return {
        "minimum_research_approved_symbols": 10,
        "preferred_research_approved_symbols": "15-25",
        "minimum_sectors": 4,
        "minimum_overlap": (
            "13 complete months before the first ranking plus at least 24 months of "
            "overlapping out-of-sample evaluation history"
        ),
        "selection_rule": "eligibility and quality only; never historical performance",
        "current_research_approved_symbols": current_symbol_count,
        "current_five_symbol_use": "engineering_dry_run_only",
        "research_conclusion_permitted": current_symbol_count >= 10,
        "next_data_milestone": (
            "independently approve at least five additional symbols so at least ten span "
            "four or more sectors with dated lifecycle evidence, consistent adjustment "
            "grain, complete lineage, and sufficient overlapping history"
        ),
        "activation_authorized": False,
    }


def archived_benchmark_contract(
    identity: dict[str, Any],
    result: dict[str, Any],
    *,
    result_sha256: str,
    source_manifest_sha256: str,
) -> dict[str, Any]:
    baselines = {row["baseline"]: row for row in result["baseline_comparison"]}
    universes = result["symbol_dependence"]["universes"]
    return {
        "contract_version": "1.0.0",
        "status": "immutable_archived_rejected_benchmark",
        "registration_id": identity["registration_id"],
        "strategy": identity["strategy"],
        "code_hash": identity["code_hash"],
        "parameter_hash": identity["parameter_hash"],
        "parameters": deepcopy(identity["parameters"]),
        "dataset_identities": {
            "parent": {
                "id": identity["parent_registry_id"],
                "version": identity["parent_version"],
                "sha256": identity["parent_hash"],
                "symbols": identity["parent_symbols"],
            },
            "extension": {
                "id": identity["extension_registry_id"],
                "version": identity["extension_version"],
                "sha256": identity["extension_hash"],
                "symbols": identity["extension_symbols"],
            },
        },
        "timing_contract": {
            "signal": "20/50 close moving-average crossover",
            "execution": "next source-present bar open",
            "same_bar_execution": False,
            "exclusions": "no signal or execution on excluded rows",
        },
        "cost_assumptions": {"fee_percent": "0.40", "slippage_percent": "0.25"},
        "baseline_results": baselines,
        "five_symbol_results": result["exact_reproduction"],
        "leave_one_out_results": {
            key: universes[key] for key in ("without_bracbank", "without_best", "without_worst")
        },
        "walk_forward_results": result["walk_forward_failures"],
        "final_rejection_evidence": {
            "decision": result["decision"],
            "return_attribution": result["return_attribution"],
            "trade_failure_analysis": result["trade_failure_analysis"],
            "cost_benefit": result["cost_benefit"],
        },
        "source_result_sha256": result_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "future_use": "comparison benchmark only",
        "promotion_rule": (
            "requires an entirely new independent review and explicit governance authorization"
        ),
        "mutable": False,
    }


def archive_registration_evidence(
    registration: StrategyRegistration,
    benchmark_contract: dict[str, Any],
    *,
    decision_sha256: str,
    authorization_sha256: str,
) -> dict[str, Any]:
    original_identity = {
        "registration_id": registration.id,
        "strategy": f"{registration.strategy_id}@{registration.version}",
        "code_hash": registration.code_hash,
        "parameter_hash": canonical_hash(registration.parameters),
    }
    evidence = deepcopy(registration.evidence)
    evidence.update(
        {
            "research_verdict": "rejected",
            "research_role": "archived_rejected_benchmark",
            "promotion_status": "blocked",
            "promotion_authorized": False,
            "campaign_eligibility": False,
            "execution_authorization": False,
            "research_execution_authorized": False,
            "real_money_eligibility": False,
            "no_real_money_authorization": True,
            "qualification": "0/60",
            "rejection_reasons": deepcopy(REJECTION_REASONS),
            "rejection_interpretation": (
                "technical validation passed; the research hypothesis did not establish a robust edge"
            ),
            "archived_benchmark_contract": deepcopy(benchmark_contract),
            "archival_decision_sha256": decision_sha256,
            "archival_authorization_sha256": authorization_sha256,
        }
    )
    registration.lifecycle_state = "research"
    registration.evidence = evidence
    if original_identity != {
        "registration_id": registration.id,
        "strategy": f"{registration.strategy_id}@{registration.version}",
        "code_hash": registration.code_hash,
        "parameter_hash": canonical_hash(registration.parameters),
    }:
        raise RuntimeError("Archived benchmark identity changed")
    return evidence


def assert_archived_state(registration: StrategyRegistration) -> None:
    expected = {
        "research_verdict": "rejected",
        "research_role": "archived_rejected_benchmark",
        "promotion_status": "blocked",
        "promotion_authorized": False,
        "campaign_eligibility": False,
        "execution_authorization": False,
        "research_execution_authorized": False,
        "real_money_eligibility": False,
        "no_real_money_authorization": True,
        "qualification": "0/60",
    }
    actual = {key: registration.evidence.get(key) for key in expected}
    if registration.lifecycle_state != "research" or actual != expected:
        raise RuntimeError("Rejected benchmark governance state is invalid")
    contract = registration.evidence.get("archived_benchmark_contract", {})
    if (
        contract.get("registration_id") != registration.id
        or contract.get("code_hash") != registration.code_hash
        or contract.get("parameter_hash") != canonical_hash(registration.parameters)
        or contract.get("mutable") is not False
    ):
        raise RuntimeError("Archived benchmark contract identity is invalid")

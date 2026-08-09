from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

from app.schemas.market import HistoricalBar
from app.services.cross_sectional_momentum import (
    EXTENSION_SYMBOLS,
    PARENT_SYMBOLS,
    UNIVERSE,
    PortfolioRun,
    ResearchBundle,
    _aligned_data,
    _portfolio_summary,
    canonical_hash,
    simulate_plans,
    subperiod_analysis,
    walk_forward_analysis,
)
from app.services.defensive_low_volatility import _add_defensive_metrics, _benchmarks
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
)

STRATEGY_ID = "absolute_momentum_filter"
STRATEGY_VERSION = "0.1.0"
STRATEGY_IDENTITY = f"{STRATEGY_ID}@{STRATEGY_VERSION}"
PRIMARY_PARAMETERS: dict[str, Any] = {
    "lookback_months": 12,
    "skip_recent_months": 1,
    "positive_return_required": True,
    "cross_sectional_ranking": False,
    "rebalance_frequency": "quarterly",
    "maximum_symbol_weight": 0.20,
    "weighting": "equal_among_qualifiers_capped_with_remainder_cash",
    "long_only": True,
    "leverage": False,
    "short_selling": False,
    "fee_percent": str(BASELINE_FEE_PERCENT),
    "slippage_percent": str(BASELINE_SLIPPAGE_PERCENT),
    "execution": "next_common_source_present_open",
}


@dataclass(frozen=True)
class AbsoluteMomentumConfig:
    name: str
    lookback_months: int
    skip_recent_months: int = 1
    rebalance_frequency: str = "quarterly"
    maximum_symbol_weight: float = 0.20


PRIMARY_CONFIG = AbsoluteMomentumConfig("primary_12m_skip1_quarterly_cap20", 12)
SENSITIVITY_CONFIGS = (
    AbsoluteMomentumConfig("variant_a_6m_skip1_quarterly_cap20", 6),
    AbsoluteMomentumConfig("variant_b_12m_skip1_monthly_cap20", 12, 1, "monthly"),
    AbsoluteMomentumConfig("variant_c_12m_include_latest_quarterly_cap20", 12, 0),
    AbsoluteMomentumConfig("variant_d_12m_skip1_quarterly_cap15", 12, 1, "quarterly", 0.15),
)


def parameter_hash() -> str:
    return canonical_hash(PRIMARY_PARAMETERS)


def code_hash(repository_root: Path) -> str:
    service = repository_root / "backend" / "app" / "services"
    payload = (
        (service / Path(__file__).name).read_bytes()
        + b"\0trusted-portfolio-engine\0"
        + (service / "cross_sectional_momentum.py").read_bytes()
        + b"\0trusted-defensive-metrics\0"
        + (service / "defensive_low_volatility.py").read_bytes()
        + b"\0"
        + STRATEGY_IDENTITY.encode()
    )
    return hashlib.sha256(payload).hexdigest()


def deterministic_registration_id(
    *, code_sha256: str, parameter_sha256: str, datasets: Sequence[Mapping[str, Any]]
) -> str:
    from uuid import NAMESPACE_URL, uuid5

    identity = canonical_hash(
        {
            "strategy": STRATEGY_IDENTITY,
            "code_hash": code_sha256,
            "parameter_hash": parameter_sha256,
            "datasets": [
                {"id": row["id"], "sha256": row["sha256"]}
                for row in sorted(datasets, key=lambda value: str(value["id"]))
            ],
        }
    )
    return str(uuid5(NAMESPACE_URL, f"dse-autotrader:{identity}"))


def absolute_momentum_scores(
    bars: Mapping[str, Sequence[HistoricalBar]],
    signal_date: date,
    *,
    lookback_months: int,
    skip_recent_months: int,
) -> tuple[dict[str, float], dict[str, str]]:
    _, by_symbol, month_ends = _aligned_data(bars)
    signal_month = signal_date.year * 12 + signal_date.month - 1
    end_month = signal_month - skip_recent_months
    start_month = end_month - lookback_months
    required_months = range(start_month, end_month + 1)
    scores: dict[str, float] = {}
    exclusions: dict[str, str] = {}
    for symbol in sorted(bars):
        if signal_date not in by_symbol[symbol]:
            exclusions[symbol] = "signal_session_missing"
            continue
        if any(month not in month_ends for month in required_months):
            exclusions[symbol] = "complete_lookback_missing"
            continue
        if any(month_ends[month] not in by_symbol[symbol] for month in required_months):
            exclusions[symbol] = "required_source_present_observation_missing"
            continue
        start = float(by_symbol[symbol][month_ends[start_month]].close)
        end = float(by_symbol[symbol][month_ends[end_month]].close)
        if start <= 0:
            exclusions[symbol] = "invalid_adjusted_lookback"
            continue
        scores[symbol] = end / start - 1
    return scores, exclusions


def build_rebalance_plans(
    bars: Mapping[str, Sequence[HistoricalBar]], config: AbsoluteMomentumConfig
) -> list[dict[str, Any]]:
    common_dates, _, month_ends = _aligned_data(bars)
    next_date = {
        common_dates[index]: common_dates[index + 1] for index in range(len(common_dates) - 1)
    }
    first_month = min(month_ends)
    if config.rebalance_frequency == "monthly":
        signal_dates = [signal for _, signal in sorted(month_ends.items())]
    elif config.rebalance_frequency == "quarterly":
        quarter_ends: dict[tuple[int, int], date] = {}
        for signal in month_ends.values():
            quarter_ends[(signal.year, (signal.month - 1) // 3 + 1)] = signal
        signal_dates = [signal for _, signal in sorted(quarter_ends.items())]
    else:
        raise ValueError(f"Unsupported rebalance frequency: {config.rebalance_frequency}")

    plans: list[dict[str, Any]] = []
    required_month_offset = config.lookback_months + config.skip_recent_months
    for signal_date in signal_dates:
        signal_month = signal_date.year * 12 + signal_date.month - 1
        if signal_date not in next_date or signal_month < first_month + required_month_offset:
            continue
        scores, exclusions = absolute_momentum_scores(
            bars,
            signal_date,
            lookback_months=config.lookback_months,
            skip_recent_months=config.skip_recent_months,
        )
        selected = sorted(symbol for symbol, score in scores.items() if score > 0)
        target_weight = min(1 / len(selected), config.maximum_symbol_weight) if selected else 0.0
        weights = {symbol: target_weight for symbol in selected}
        plans.append(
            {
                "period": (
                    f"{signal_date.year:04d}-{signal_date.month:02d}"
                    if config.rebalance_frequency == "monthly"
                    else f"{signal_date.year:04d}-Q{(signal_date.month - 1) // 3 + 1}"
                ),
                "signal_date": signal_date,
                "execution_date": next_date[signal_date],
                "scores": scores,
                "ranking": selected,
                "selected": selected,
                "target_weights": weights,
                "eligibility_exclusions": exclusions,
                "nonqualifying_symbols": sorted(
                    symbol for symbol, score in scores.items() if score <= 0
                ),
            }
        )
    return plans


def run_absolute_momentum(
    bars: Mapping[str, Sequence[HistoricalBar]],
    config: AbsoluteMomentumConfig,
    *,
    fee_percent: float = float(BASELINE_FEE_PERCENT),
    slippage_percent: float = float(BASELINE_SLIPPAGE_PERCENT),
) -> PortfolioRun:
    run = _add_defensive_metrics(
        simulate_plans(
            bars,
            build_rebalance_plans(bars, config),
            name=config.name,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        )
    )
    run.metrics["all_cash_period_count"] = len(run.cash_periods)
    return run


def _return(run: PortfolioRun) -> float:
    return float(run.metrics["total_return_percent"])


def _dependence(
    primary: PortfolioRun,
    leave_one_out: Mapping[str, PortfolioRun],
    isolated: Mapping[str, PortfolioRun],
    variants: Mapping[str, PortfolioRun],
    walk_forward: Mapping[str, Any],
    subperiods: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    contributions = {symbol: abs(value) for symbol, value in primary.symbol_contribution.items()}
    contribution_total = sum(contributions.values())
    leading_symbol = max(contributions, key=lambda symbol: contributions[symbol])
    origins = {origin: abs(value) for origin, value in primary.dataset_contribution.items()}
    origin_total = sum(origins.values())
    leading_origin = max(origins, key=lambda origin: origins[origin])
    return {
        "one_symbol": {
            "largest_absolute_contributor": leading_symbol,
            "largest_absolute_contribution_share": (
                contributions[leading_symbol] / contribution_total if contribution_total else 0.0
            ),
            "leave_one_out_returns_percent": {
                symbol: _return(run) for symbol, run in leave_one_out.items()
            },
        },
        "one_dataset": {
            "largest_absolute_dataset_contributor": leading_origin,
            "largest_absolute_dataset_contribution_share": (
                origins[leading_origin] / origin_total if origin_total else 0.0
            ),
            "parent_only_return_percent": _return(isolated["parent_only"]),
            "extensions_only_return_percent": _return(isolated["extensions_only"]),
        },
        "one_period": {
            "positive_holdouts": walk_forward["positive_holdouts"],
            "holdout_count": walk_forward["holdout_count"],
            "subperiod_returns_percent": [
                float(item["metrics"]["total_return_percent"]) for item in subperiods
            ],
        },
        "lookback_choice": {
            "primary_12m_skip1_return_percent": _return(primary),
            "six_month_return_percent": _return(variants["variant_a_6m_skip1_quarterly_cap20"]),
            "include_latest_month_return_percent": _return(
                variants["variant_c_12m_include_latest_quarterly_cap20"]
            ),
        },
        "rebalance_frequency": {
            "primary_quarterly_return_percent": _return(primary),
            "monthly_return_percent": _return(variants["variant_b_12m_skip1_monthly_cap20"]),
        },
    }


def _verdict(
    primary: PortfolioRun,
    benchmarks: Mapping[str, PortfolioRun],
    dependence: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    variants: Mapping[str, PortfolioRun],
    gross: PortfolioRun,
) -> str:
    if _return(primary) <= 0 or float(primary.metrics["maximum_drawdown_percent"]) <= -60:
        return "reject_strategy"
    equity_names = (
        "equal_weight_buy_and_hold",
        "monthly_rebalanced_equal_weight",
        "quarterly_rebalanced_equal_weight",
    )
    drawdown_improvements = [
        float(primary.metrics["maximum_drawdown_percent"])
        - float(benchmarks[name].metrics["maximum_drawdown_percent"])
        for name in equity_names
    ]
    holdouts = [
        float(item["holdout"]["metrics"]["total_return_percent"])
        for item in cast(list[dict[str, Any]], walk_forward["partitions"])
    ]
    primary_not_contradicted = sum(_return(run) > 0 for run in variants.values()) >= 3
    costs_preserve_edge = _return(gross) > 0 and _return(primary) / _return(gross) >= 0.5
    if (
        float(walk_forward["combined_holdout"]["metrics"]["total_return_percent"]) > 0
        and min(holdouts) > -25
        and min(drawdown_improvements) >= 5
        and float(dependence["one_symbol"]["largest_absolute_contribution_share"]) <= 0.5
        and float(dependence["one_dataset"]["largest_absolute_dataset_contribution_share"]) <= 0.75
        and costs_preserve_edge
        and primary_not_contradicted
    ):
        return "promising_but_unproven"
    return "insufficient_evidence"


def build_research_bundle(
    bars: Mapping[str, Sequence[HistoricalBar]],
    *,
    dataset_identities: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any],
    registration_identity: Mapping[str, Any],
    archived_context: Mapping[str, Any],
) -> ResearchBundle:
    primary = run_absolute_momentum(bars, PRIMARY_CONFIG)
    gross = run_absolute_momentum(bars, PRIMARY_CONFIG, fee_percent=0.0, slippage_percent=0.0)
    sensitivities = {
        config.name: run_absolute_momentum(bars, config) for config in SENSITIVITY_CONFIGS
    }
    benchmarks = _benchmarks(bars, primary)
    leave_one_out = {
        symbol: run_absolute_momentum(
            {key: value for key, value in bars.items() if key != symbol}, PRIMARY_CONFIG
        )
        for symbol in UNIVERSE
    }
    isolated = {
        "parent_only": run_absolute_momentum(
            {symbol: bars[symbol] for symbol in PARENT_SYMBOLS}, PRIMARY_CONFIG
        ),
        "extensions_only": run_absolute_momentum(
            {symbol: bars[symbol] for symbol in EXTENSION_SYMBOLS}, PRIMARY_CONFIG
        ),
    }
    walk_forward = walk_forward_analysis(primary)
    subperiods = subperiod_analysis(primary)
    dependence = _dependence(
        primary, leave_one_out, isolated, sensitivities, walk_forward, subperiods
    )
    verdict = _verdict(primary, benchmarks, dependence, walk_forward, sensitivities, gross)
    surviving_families = [STRATEGY_IDENTITY] if verdict == "promising_but_unproven" else []
    surviving_families.extend(
        name
        for name, context in archived_context.items()
        if cast(Mapping[str, Any], context)["verdict"].get("research_decision")
        == "promising_but_unproven"
    )
    freeze_decision = (
        "A_select_surviving_strategy_for_forward_paper_validation"
        if surviving_families
        else "B_current_evidence_has_no_suitable_forward_validation_strategy"
    )
    drawdown = {
        name: {
            "comparison_maximum_drawdown_percent": result["metrics"]["maximum_drawdown_percent"],
            "primary_improvement_percentage_points": float(
                primary.metrics["maximum_drawdown_percent"]
            )
            - float(result["metrics"]["maximum_drawdown_percent"]),
        }
        for name, result in {
            **{key: _portfolio_summary(run) for key, run in benchmarks.items()},
            **cast(dict[str, dict[str, Any]], archived_context),
        }.items()
    }
    payload: dict[str, Any] = {
        "schema": "minimal_v1_absolute_momentum_filter_research_v1",
        "strategy_registration": dict(registration_identity),
        "dataset_identities": [dict(item) for item in dataset_identities],
        "data_quality": dict(validation),
        "signal_contract": {
            "independent_symbol_judgment": True,
            "cross_sectional_ranking": False,
            "positive_return_qualifies": True,
            "zero_or_negative_return": "cash",
        },
        "timing_contract": {
            "signal": "12-month adjusted total price return excluding latest month",
            "signal_timestamp": "final common source-present session of quarter",
            "execution": "next common source-present session open",
            "signal_bar_execution": False,
            "forward_fill": False,
            "stale_substitution": False,
        },
        "eligibility_contract": {
            "complete_lookback_required": True,
            "active_dated_universe_membership": True,
            "adjusted_data_and_complete_lineage_required": True,
            "held_or_conflicting_observations_allowed": False,
            "forward_fill": False,
            "stale_price_substitution": False,
        },
        "cost_contract": {
            "fee_percent": float(BASELINE_FEE_PERCENT),
            "slippage_percent": float(BASELINE_SLIPPAGE_PERCENT),
            "fractional_shares": False,
            "insufficient_cash_behavior": "reduce_integer_quantity",
            "negative_cash": False,
        },
        "primary": _portfolio_summary(primary),
        "benchmarks": {name: _portfolio_summary(run) for name, run in benchmarks.items()},
        "archived_context": dict(archived_context),
        "drawdown_comparison": drawdown,
        "sensitivity_variants": {
            name: _portfolio_summary(run) for name, run in sensitivities.items()
        },
        "walk_forward": walk_forward,
        "subperiod_stability": subperiods,
        "leave_one_symbol_out": {
            symbol: _portfolio_summary(run) for symbol, run in leave_one_out.items()
        },
        "dataset_origin_analysis": {
            name: _portfolio_summary(run) for name, run in isolated.items()
        },
        "dependence_tests": dependence,
        "cost_impact": {
            "gross": _portfolio_summary(gross),
            "net_return_percent": _return(primary),
            "gross_return_percent": _return(gross),
            "return_cost_percentage_points": _return(gross) - _return(primary),
            "fees_bdt": primary.metrics["total_fees_bdt"],
            "slippage_bdt": primary.metrics["total_slippage_bdt"],
        },
        "research_verdict": verdict,
        "historical_strategy_family_discovery": {
            "status": "frozen_after_this_run",
            "automatic_new_strategy_proposals": False,
            "decision": freeze_decision,
            "surviving_families": surviving_families,
            "next_decision_required": [
                "select_a_surviving_strategy_for_forward_paper_validation",
                "conclude_current_evidence_has_no_suitable_forward_validation_strategy",
            ],
        },
        "promotion_permission": False,
        "campaign_eligibility": False,
        "external_execution_permission": False,
        "qualification": "0/60",
    }
    ledger = [
        row
        for run in [primary, *sensitivities.values(), *benchmarks.values()]
        for row in run.ledger
    ]
    return ResearchBundle(payload=payload, ledger=ledger)

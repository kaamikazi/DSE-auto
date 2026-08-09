from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, cast

from app.schemas.market import HistoricalBar
from app.services.cross_sectional_momentum import (
    EXTENSION_SYMBOLS,
    MAX_TARGET_WEIGHT,
    PARENT_SYMBOLS,
    UNIVERSE,
    PortfolioRun,
    ResearchBundle,
    _aligned_data,
    _portfolio_summary,
    _static_weight_plans,
    canonical_hash,
    load_active_universe,
    run_benchmarks,
    simulate_plans,
    subperiod_analysis,
    walk_forward_analysis,
)
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
)

STRATEGY_ID = "defensive_low_volatility"
STRATEGY_VERSION = "0.1.0"
STRATEGY_IDENTITY = f"{STRATEGY_ID}@{STRATEGY_VERSION}"
ANNUALIZATION_SESSIONS = 252
PRIMARY_PARAMETERS: dict[str, Any] = {
    "volatility_lookback_sessions": 126,
    "volatility_estimator": "population_standard_deviation_of_adjusted_close_returns",
    "annualization_sessions": ANNUALIZATION_SESSIONS,
    "top_n": 3,
    "rebalance_frequency": "quarterly",
    "weighting": "equal",
    "maximum_target_weight": MAX_TARGET_WEIGHT,
    "long_only": True,
    "leverage": False,
    "short_selling": False,
    "fee_percent": str(BASELINE_FEE_PERCENT),
    "slippage_percent": str(BASELINE_SLIPPAGE_PERCENT),
    "execution": "next_common_source_present_open",
    "universe_membership": "active_registry_and_source_present_at_signal_and_execution",
}


@dataclass(frozen=True)
class LowVolatilityConfig:
    name: str
    lookback_sessions: int
    top_n: int
    rebalance_frequency: str = "quarterly"


PRIMARY_CONFIG = LowVolatilityConfig("primary_126_session_quarterly_top3", 126, 3)
SENSITIVITY_CONFIGS = (
    LowVolatilityConfig("variant_a_63_session_quarterly_top3", 63, 3),
    LowVolatilityConfig("variant_b_252_session_quarterly_top3", 252, 3),
    LowVolatilityConfig("variant_c_126_session_monthly_top3", 126, 3, "monthly"),
    LowVolatilityConfig("variant_d_126_session_quarterly_top5", 126, 5),
)


def parameter_hash() -> str:
    return canonical_hash(PRIMARY_PARAMETERS)


def code_hash(repository_root: Path) -> str:
    service = repository_root / "backend" / "app" / "services"
    payload = (
        (service / Path(__file__).name).read_bytes()
        + b"\0trusted-portfolio-engine\0"
        + (service / "cross_sectional_momentum.py").read_bytes()
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


def realized_volatility_scores(
    bars: Mapping[str, Sequence[HistoricalBar]],
    signal_date: date,
    *,
    lookback_sessions: int,
) -> tuple[dict[str, float], dict[str, str]]:
    common_dates, by_symbol, _ = _aligned_data(bars)
    if signal_date not in common_dates:
        return {}, {symbol: "signal_session_missing" for symbol in bars}
    signal_index = common_dates.index(signal_date)
    if signal_index < lookback_sessions:
        return {}, {symbol: "full_lookback_missing" for symbol in bars}
    required_dates = common_dates[signal_index - lookback_sessions : signal_index + 1]
    scores: dict[str, float] = {}
    exclusions: dict[str, str] = {}
    for symbol in sorted(bars):
        try:
            closes = [float(by_symbol[symbol][day].close) for day in required_dates]
        except KeyError:
            exclusions[symbol] = "required_source_present_observation_missing"
            continue
        if len(closes) != lookback_sessions + 1 or any(
            not math.isfinite(value) or value <= 0 for value in closes
        ):
            exclusions[symbol] = "invalid_adjusted_lookback"
            continue
        returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
        scores[symbol] = pstdev(returns) * math.sqrt(ANNUALIZATION_SESSIONS)
    return scores, exclusions


def build_rebalance_plans(
    bars: Mapping[str, Sequence[HistoricalBar]], config: LowVolatilityConfig
) -> list[dict[str, Any]]:
    common_dates, _, month_ends = _aligned_data(bars)
    next_date = {
        common_dates[index]: common_dates[index + 1] for index in range(len(common_dates) - 1)
    }
    signal_dates: list[date]
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
    for signal_date in signal_dates:
        if (
            signal_date not in next_date
            or common_dates.index(signal_date) < config.lookback_sessions
        ):
            continue
        scores, exclusions = realized_volatility_scores(
            bars, signal_date, lookback_sessions=config.lookback_sessions
        )
        ranked = sorted(scores, key=lambda symbol: (scores[symbol], symbol))
        selected = ranked[: config.top_n]
        weights = {symbol: 1 / config.top_n for symbol in selected}
        if any(weight > MAX_TARGET_WEIGHT + 1e-12 for weight in weights.values()):
            raise ValueError("Target weight cap exceeded")
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
                "ranking": ranked,
                "selected": selected,
                "target_weights": weights,
                "eligibility_exclusions": exclusions,
            }
        )
    return plans


def _add_defensive_metrics(run: PortfolioRun) -> PortfolioRun:
    values = [float(point["equity"]) for point in run.equity_curve]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    downside = [min(value, 0.0) for value in returns]
    downside_volatility = (
        math.sqrt(mean(value * value for value in downside))
        * math.sqrt(ANNUALIZATION_SESSIONS)
        * 100
        if downside
        else 0.0
    )
    rolling = [
        values[index] / values[index - ANNUALIZATION_SESSIONS] - 1
        for index in range(ANNUALIZATION_SESSIONS, len(values))
    ]
    run.metrics.update(
        {
            "downside_volatility_percent": downside_volatility,
            "worst_rolling_12_month_return_percent": min(rolling) * 100 if rolling else None,
            "rolling_12_month_window_source_present_sessions": ANNUALIZATION_SESSIONS,
        }
    )
    return run


def run_low_volatility(
    bars: Mapping[str, Sequence[HistoricalBar]],
    config: LowVolatilityConfig,
    *,
    fee_percent: float = float(BASELINE_FEE_PERCENT),
    slippage_percent: float = float(BASELINE_SLIPPAGE_PERCENT),
) -> PortfolioRun:
    return _add_defensive_metrics(
        simulate_plans(
            bars,
            build_rebalance_plans(bars, config),
            name=config.name,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        )
    )


def _benchmarks(
    bars: Mapping[str, Sequence[HistoricalBar]], primary: PortfolioRun
) -> dict[str, PortfolioRun]:
    benchmarks = run_benchmarks(bars, primary)
    start_signal = date.fromisoformat(str(primary.rebalances[0]["signal_date"]))
    equal = {symbol: 1 / len(bars) for symbol in bars}
    monthly_plans = _static_weight_plans(
        bars,
        name="quarterly_rebalanced_equal_weight",
        weights=equal,
        start_signal=start_signal,
        monthly=True,
    )
    quarterly_plans = [
        plan
        for plan in monthly_plans
        if date.fromisoformat(str(plan["signal_date"])).month in {3, 6, 9, 12}
    ]
    benchmarks["quarterly_rebalanced_equal_weight"] = simulate_plans(
        bars,
        quarterly_plans,
        name="quarterly_rebalanced_equal_weight",
    )
    return {name: _add_defensive_metrics(run) for name, run in benchmarks.items()}


def _return(run: PortfolioRun) -> float:
    return float(run.metrics["total_return_percent"])


def _dependence_analysis(
    primary: PortfolioRun,
    leave_one_out: Mapping[str, PortfolioRun],
    isolated: Mapping[str, PortfolioRun],
    variants: Mapping[str, PortfolioRun],
    walk_forward: Mapping[str, Any],
    subperiods: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    absolute_contributions = {
        symbol: abs(value) for symbol, value in primary.symbol_contribution.items()
    }
    total_absolute = sum(absolute_contributions.values())
    leading_symbol = max(absolute_contributions, key=lambda symbol: absolute_contributions[symbol])
    origin_absolute = {origin: abs(value) for origin, value in primary.dataset_contribution.items()}
    origin_total = sum(origin_absolute.values())
    leading_origin = max(origin_absolute, key=lambda origin: origin_absolute[origin])
    return {
        "one_symbol": {
            "largest_absolute_contributor": leading_symbol,
            "largest_absolute_contribution_share": (
                absolute_contributions[leading_symbol] / total_absolute if total_absolute else 0.0
            ),
            "leave_one_out_returns_percent": {
                symbol: _return(run) for symbol, run in leave_one_out.items()
            },
        },
        "one_period": {
            "positive_holdouts": walk_forward["positive_holdouts"],
            "holdout_count": walk_forward["holdout_count"],
            "subperiod_returns_percent": [
                float(item["metrics"]["total_return_percent"]) for item in subperiods
            ],
        },
        "one_dataset": {
            "largest_absolute_dataset_contributor": leading_origin,
            "largest_absolute_dataset_contribution_share": (
                origin_absolute[leading_origin] / origin_total if origin_total else 0.0
            ),
            "parent_only_return_percent": _return(isolated["parent_only"]),
            "extensions_only_return_percent": _return(isolated["extensions_only"]),
        },
        "volatility_window": {
            "primary_126_session_return_percent": _return(primary),
            "variant_a_63_session_return_percent": _return(
                variants["variant_a_63_session_quarterly_top3"]
            ),
            "variant_b_252_session_return_percent": _return(
                variants["variant_b_252_session_quarterly_top3"]
            ),
        },
        "rebalance_frequency": {
            "primary_quarterly_return_percent": _return(primary),
            "variant_c_monthly_return_percent": _return(
                variants["variant_c_126_session_monthly_top3"]
            ),
        },
    }


def research_verdict(
    primary: PortfolioRun,
    dependence: Mapping[str, Any],
    walk_forward: Mapping[str, Any],
    variants: Mapping[str, PortfolioRun],
) -> str:
    if (
        _return(primary) <= 0
        or float(primary.metrics["maximum_drawdown_percent"]) <= -50
        or float(primary.metrics["worst_rolling_12_month_return_percent"]) <= -40
    ):
        return "reject_strategy"
    positive_holdouts = int(walk_forward["positive_holdouts"])
    positive_variants = sum(_return(run) > 0 for run in variants.values())
    symbol_share = float(dependence["one_symbol"]["largest_absolute_contribution_share"])
    if positive_holdouts >= 2 and positive_variants >= 3 and symbol_share <= 0.5:
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
    primary = run_low_volatility(bars, PRIMARY_CONFIG)
    gross = run_low_volatility(bars, PRIMARY_CONFIG, fee_percent=0.0, slippage_percent=0.0)
    sensitivities = {
        config.name: run_low_volatility(bars, config) for config in SENSITIVITY_CONFIGS
    }
    benchmarks = _benchmarks(bars, primary)
    leave_one_out = {
        symbol: run_low_volatility(
            {key: value for key, value in bars.items() if key != symbol}, PRIMARY_CONFIG
        )
        for symbol in UNIVERSE
    }
    isolated = {
        "parent_only": run_low_volatility(
            {symbol: bars[symbol] for symbol in PARENT_SYMBOLS}, PRIMARY_CONFIG
        ),
        "extensions_only": run_low_volatility(
            {symbol: bars[symbol] for symbol in EXTENSION_SYMBOLS}, PRIMARY_CONFIG
        ),
    }
    walk_forward = walk_forward_analysis(primary)
    subperiods = subperiod_analysis(primary)
    dependence = _dependence_analysis(
        primary, leave_one_out, isolated, sensitivities, walk_forward, subperiods
    )
    verdict = research_verdict(primary, dependence, walk_forward, sensitivities)
    drawdown_comparison = {
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
        "schema": "minimal_v1_defensive_low_volatility_research_v1",
        "strategy_registration": dict(registration_identity),
        "dataset_identities": [dict(item) for item in dataset_identities],
        "data_quality": dict(validation),
        "timing_contract": {
            "signal": "annualized population volatility of adjusted close returns",
            "lookback_source_present_returns": 126,
            "signal_timestamp": "final common source-present session of quarter",
            "execution": "next common source-present session open",
            "signal_bar_execution": False,
            "stale_substitution": False,
            "forward_fill": False,
        },
        "eligibility_contract": {
            "full_lookback_required": True,
            "active_dated_universe_membership": True,
            "adjusted_data_required": True,
            "held_or_conflicting_observations_allowed": False,
            "forward_fill": False,
            "stale_price_substitution": False,
            "fewer_than_target_symbols": "equal_slots_for_eligible_symbols_remainder_cash",
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
        "drawdown_comparison": drawdown_comparison,
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
        "promotion_permission": False,
        "paper_campaign_eligibility": False,
        "external_execution_permission": False,
        "qualification": "0/60",
    }
    ledger = [
        row
        for run in [primary, *sensitivities.values(), *benchmarks.values()]
        for row in run.ledger
    ]
    return ResearchBundle(payload=payload, ledger=ledger)


__all__ = [
    "PRIMARY_CONFIG",
    "PRIMARY_PARAMETERS",
    "SENSITIVITY_CONFIGS",
    "STRATEGY_ID",
    "STRATEGY_IDENTITY",
    "STRATEGY_VERSION",
    "UNIVERSE",
    "LowVolatilityConfig",
    "build_rebalance_plans",
    "build_research_bundle",
    "code_hash",
    "deterministic_registration_id",
    "load_active_universe",
    "parameter_hash",
    "realized_volatility_scores",
    "run_low_volatility",
]

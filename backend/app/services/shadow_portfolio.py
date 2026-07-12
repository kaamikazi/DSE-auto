from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any


def _metrics(
    values: list[float],
    fees: float = 0,
    slippage: float = 0,
    turnover: float = 0,
    rejected: int = 0,
    missed: int = 0,
    interventions: int = 0,
) -> dict[str, Any]:
    if len(values) < 2 or values[0] == 0:
        return {
            "return": 0.0,
            "drawdown": 0.0,
            "volatility": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "turnover": turnover,
            "fees": fees,
            "slippage": slippage,
            "rejected_trades": rejected,
            "missed_trades": missed,
            "risk_interventions": interventions,
        }
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]
    peak, max_drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    volatility = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    average = mean(returns) if returns else 0.0
    downside = [item for item in returns if item < 0]
    downside_deviation = pstdev(downside) * math.sqrt(252) if len(downside) > 1 else 0.0
    return {
        "return": values[-1] / values[0] - 1,
        "drawdown": max_drawdown,
        "volatility": volatility,
        "sharpe": average * 252 / volatility if volatility else 0.0,
        "sortino": average * 252 / downside_deviation if downside_deviation else 0.0,
        "turnover": turnover,
        "fees": fees,
        "slippage": slippage,
        "rejected_trades": rejected,
        "missed_trades": missed,
        "risk_interventions": interventions,
    }


def compare_shadow_portfolios(
    series: dict[str, list[float]], operations: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    required = {"reference_imported", "buy_and_hold", "dsex", "combined_paper"}
    missing = sorted(required - series.keys())
    if missing:
        raise ValueError(f"Missing shadow portfolios: {missing}")
    operations = operations or {}
    return {
        "portfolios": {
            name: _metrics(values, **operations.get(name, {})) for name, values in series.items()
        },
        "ranking_policy": "No portfolio is labelled best without risk-adjusted and robustness review.",
    }

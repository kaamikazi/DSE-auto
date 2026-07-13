from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any


def _returns(values: list[float]) -> list[float]:
    return [
        values[index] / values[index - 1] - 1
        for index in range(1, len(values))
        if values[index - 1]
    ]


def campaign_metrics(
    equity: list[float],
    benchmark: list[float],
    trades: list[dict[str, Any]] | None = None,
    effects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not equity:
        raise ValueError("Campaign analytics require at least one equity observation")
    daily = _returns(equity)
    benchmark_daily = _returns(benchmark)
    total_return = equity[-1] / equity[0] - 1 if equity[0] else 0.0
    benchmark_return = benchmark[-1] / benchmark[0] - 1 if benchmark and benchmark[0] else 0.0
    volatility = pstdev(daily) * math.sqrt(252) if len(daily) > 1 else 0.0
    downside = [min(value, 0.0) for value in daily]
    downside_deviation = math.sqrt(mean([value * value for value in downside])) if downside else 0.0
    sharpe = (
        mean(daily) / pstdev(daily) * math.sqrt(252) if len(daily) > 1 and pstdev(daily) else 0.0
    )
    sortino = (
        mean(daily) / downside_deviation * math.sqrt(252) if daily and downside_deviation else 0.0
    )
    peak = equity[0]
    max_drawdown = 0.0
    duration = current_duration = 0
    for value in equity:
        peak = max(peak, value)
        drawdown = value / peak - 1 if peak else 0.0
        max_drawdown = min(max_drawdown, drawdown)
        current_duration = current_duration + 1 if value < peak else 0
        duration = max(duration, current_duration)
    annualized = mean(daily) * 252 if daily else 0.0
    calmar = annualized / abs(max_drawdown) if max_drawdown else 0.0
    rows = trades or []
    pnl = [float(row.get("pnl", 0.0)) for row in rows]
    wins = sum(value for value in pnl if value > 0)
    losses = abs(sum(value for value in pnl if value < 0))
    values = effects or {}
    return {
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": total_return - benchmark_return,
        "maximum_drawdown": max_drawdown,
        "drawdown_duration_days": duration,
        "volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "profit_factor": wins / losses if losses else (None if not wins else float("inf")),
        "expectancy": mean(pnl) if pnl else 0.0,
        "turnover": float(values.get("turnover", 0.0)),
        "fees": float(values.get("fees", 0.0)),
        "slippage": float(values.get("slippage", 0.0)),
        "exposure": float(values.get("exposure", 0.0)),
        "average_holding_period": float(values.get("average_holding_period", 0.0)),
        "rejected_trades": int(values.get("rejected_trades", 0)),
        "missed_trades": int(values.get("missed_trades", 0)),
        "partial_fills": int(values.get("partial_fills", 0)),
        "risk_interventions": int(values.get("risk_interventions", 0)),
        "data_quality_incidents": int(values.get("data_quality_incidents", 0)),
        "operational_downtime_minutes": float(values.get("operational_downtime_minutes", 0.0)),
        "strategy_performance": values.get("strategy_performance", {}),
        "execution_effects": values.get("execution_effects", {}),
        "data_quality_effects": values.get("data_quality_effects", {}),
        "risk_engine_effects": values.get("risk_engine_effects", {}),
        "operator_decisions": values.get("operator_decisions", {}),
        "benchmark_observations": len(benchmark_daily) + 1 if benchmark else 0,
        "interpretation": "Short paper campaigns are evidence collection, not proof of profitability.",
    }

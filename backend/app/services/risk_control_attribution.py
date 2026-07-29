from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import mean, median, pstdev
from typing import Any, cast

from app.backtesting.engine import BacktestResult, Trade
from app.schemas.market import HistoricalBar
from app.services.five_symbol_robustness import (
    EXTENSION_SYMBOLS,
    PARENT_SYMBOLS,
    combine_weighted,
    run_portfolio,
    run_portfolio_buy_hold,
)
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
    STARTING_CAPITAL,
    _closed_trade_stats,
    _curve_metrics,
    run_symbol,
)

BASELINE_IDS = (
    "equal_weight_buy_and_hold",
    "cash_only",
    "fixed_50_equity_50_cash",
    "monthly_equal_weight_rebalance",
    "close_above_200_day_average",
    "registered_20_50_crossover",
)
MAJOR_DRAWDOWN_THRESHOLD_PERCENT = 10.0


def _trade_metrics(trades: list[Trade]) -> dict[str, Any]:
    stats = _closed_trade_stats(trades)
    pnls = cast(list[float], stats.pop("trade_pnls"))
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value <= 0))
    return {
        **stats,
        "win_rate": int(stats["winning_trades"]) / int(stats["completed_trades"])
        if int(stats["completed_trades"])
        else 0.0,
        "expectancy_bdt": mean(pnls) if pnls else None,
        "profit_factor": gross_profit / gross_loss
        if gross_loss
        else (None if not gross_profit else "infinite_no_losing_closed_trade"),
    }


def _result_metrics(
    curve: list[dict[str, object]],
    trades: list[Trade],
    *,
    exposure_percent: float,
) -> dict[str, Any]:
    metrics = _curve_metrics(curve, float(STARTING_CAPITAL))
    metrics.update(
        {
            **_trade_metrics(trades),
            "number_of_trades": len(trades),
            "exposure_percent": exposure_percent,
            "fee_impact_bdt": sum(trade.fee for trade in trades),
            "slippage_impact_bdt": sum(trade.slippage * trade.quantity for trade in trades),
            "turnover_rate": sum(trade.price * trade.quantity for trade in trades)
            / float(STARTING_CAPITAL),
        }
    )
    return metrics


def binary_signal_result(
    symbol: str,
    bars: list[HistoricalBar],
    desired: list[bool],
    *,
    strategy: str,
    allocation_fraction: float = 1.0,
    fee_percent: Decimal = BASELINE_FEE_PERCENT,
    slippage_percent: Decimal = BASELINE_SLIPPAGE_PERCENT,
) -> BacktestResult:
    if len(bars) != len(desired) or len(bars) < 2:
        raise ValueError("Signals must align one-for-one with at least two bars")
    if not 0 <= allocation_fraction <= 1:
        raise ValueError("Allocation fraction must be between zero and one")
    fee_rate = float(fee_percent) / 100
    slip_rate = float(slippage_percent) / 100
    cash = float(STARTING_CAPITAL)
    quantity = 0
    trades: list[Trade] = []
    curve: list[dict[str, object]] = []
    for index, bar in enumerate(bars):
        target = desired[index - 1] if index else False
        open_price = float(bar.open)
        if target and quantity == 0:
            fill = open_price * (1 + slip_rate)
            budget = cash * allocation_fraction
            affordable = int(budget / (fill * (1 + fee_rate)))
            if affordable > 0 and (bar.volume or 0) >= affordable:
                fee = affordable * fill * fee_rate
                cash -= affordable * fill + fee
                quantity = affordable
                trades.append(
                    Trade(
                        bar.timestamp.isoformat(),
                        "buy",
                        affordable,
                        fill,
                        fee,
                        open_price * slip_rate,
                    )
                )
        elif not target and quantity > 0:
            fill = open_price * (1 - slip_rate)
            fee = quantity * fill * fee_rate
            cash += quantity * fill - fee
            trades.append(
                Trade(
                    bar.timestamp.isoformat(), "sell", quantity, fill, fee, open_price * slip_rate
                )
            )
            quantity = 0
        curve.append(
            {"timestamp": bar.timestamp.isoformat(), "equity": cash + quantity * float(bar.close)}
        )
    metrics = _result_metrics(
        curve,
        trades,
        exposure_percent=sum(desired) / len(desired) * allocation_fraction * 100,
    )
    return BacktestResult(
        symbol=symbol,
        strategy=strategy,
        strategy_version="predeclared_research_baseline_v1",
        assumptions={
            "next_bar_execution": True,
            "allocation_fraction": allocation_fraction,
            "fee_percent": str(fee_percent),
            "slippage_percent": str(slippage_percent),
            "parameter_optimization": False,
        },
        metrics=metrics,
        trades=trades,
        equity_curve=curve,
        walk_forward=[],
        sensitivity=[],
    )


def two_hundred_day_signals(bars: list[HistoricalBar]) -> list[bool]:
    closes = [float(bar.close) for bar in bars]
    signals = [False] * len(bars)
    for index in range(199, len(bars)):
        signals[index] = closes[index] > mean(closes[index - 199 : index + 1])
    return signals


def fixed_exposure_portfolio(
    bars: dict[str, list[HistoricalBar]], allocation_fraction: float
) -> dict[str, Any]:
    results = {
        symbol: binary_signal_result(
            symbol,
            rows,
            [True] * len(rows),
            strategy="fixed_exposure_buy_hold",
            allocation_fraction=allocation_fraction,
        )
        for symbol, rows in bars.items()
    }
    weights = {symbol: 1 / len(results) for symbol in results}
    return combine_weighted(results, weights, bars)


def two_hundred_day_portfolio(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    results = {
        symbol: binary_signal_result(
            symbol,
            rows,
            two_hundred_day_signals(rows),
            strategy="close_above_200_day_average",
        )
        for symbol, rows in bars.items()
    }
    weights = {symbol: 1 / len(results) for symbol in results}
    return combine_weighted(results, weights, bars)


def monthly_equal_weight_rebalance(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    by_symbol = {
        symbol: {bar.timestamp.date().isoformat(): bar for bar in rows}
        for symbol, rows in bars.items()
    }
    dates = sorted({day for values in by_symbol.values() for day in values})
    if len(dates) < 2:
        raise ValueError("Monthly rebalance requires at least two dates")
    cash = float(STARTING_CAPITAL)
    quantities = {symbol: 0 for symbol in bars}
    latest_close: dict[str, float] = {}
    pending = set(bars)
    trades: list[Trade] = []
    curve: list[dict[str, object]] = []
    fee_rate = float(BASELINE_FEE_PERCENT) / 100
    slip_rate = float(BASELINE_SLIPPAGE_PERCENT) / 100
    invested_observations = 0
    for index, day in enumerate(dates):
        present = {symbol: values[day] for symbol, values in by_symbol.items() if day in values}
        marks = {
            symbol: float(present[symbol].open)
            if symbol in present
            else latest_close.get(symbol, 0.0)
            for symbol in bars
        }
        portfolio_open = cash + sum(quantities[symbol] * marks[symbol] for symbol in bars)
        target = portfolio_open / len(bars)
        for symbol in sorted(pending & present.keys()):
            bar = present[symbol]
            open_price = float(bar.open)
            current = quantities[symbol] * open_price
            if current > target and quantities[symbol] > 0:
                sell_quantity = min(quantities[symbol], int((current - target) / open_price))
                if sell_quantity:
                    fill = open_price * (1 - slip_rate)
                    fee = sell_quantity * fill * fee_rate
                    cash += sell_quantity * fill - fee
                    quantities[symbol] -= sell_quantity
                    trades.append(
                        Trade(
                            bar.timestamp.isoformat(),
                            "sell",
                            sell_quantity,
                            fill,
                            fee,
                            open_price * slip_rate,
                        )
                    )
        for symbol in sorted(pending & present.keys()):
            bar = present[symbol]
            open_price = float(bar.open)
            current = quantities[symbol] * open_price
            if current < target:
                fill = open_price * (1 + slip_rate)
                buy_quantity = min(
                    int((target - current) / (fill * (1 + fee_rate))),
                    int(cash / (fill * (1 + fee_rate))),
                )
                buy_quantity = min(buy_quantity, bar.volume or 0)
                if buy_quantity:
                    fee = buy_quantity * fill * fee_rate
                    cash -= buy_quantity * fill + fee
                    quantities[symbol] += buy_quantity
                    trades.append(
                        Trade(
                            bar.timestamp.isoformat(),
                            "buy",
                            buy_quantity,
                            fill,
                            fee,
                            open_price * slip_rate,
                        )
                    )
            pending.discard(symbol)
        for symbol, bar in present.items():
            latest_close[symbol] = float(bar.close)
        equity = cash + sum(quantities[symbol] * latest_close.get(symbol, 0.0) for symbol in bars)
        curve.append({"timestamp": day, "equity": equity})
        invested_observations += int(any(quantity > 0 for quantity in quantities.values()))
        if index + 1 < len(dates) and day[:7] != dates[index + 1][:7]:
            pending = set(bars)
    metrics = _result_metrics(
        curve, trades, exposure_percent=invested_observations / len(dates) * 100
    )
    # Partial rebalances across symbols are not round-trip trades.  Preserve
    # portfolio-level return, risk, exposure, turnover, and cost metrics, but do
    # not present arbitrary adjacent buys/sells as completed trade statistics.
    for field in (
        "completed_trades",
        "winning_trades",
        "losing_trades",
        "win_rate",
        "expectancy_bdt",
        "profit_factor",
        "average_holding_days",
    ):
        metrics[field] = None
    return {
        "metrics": metrics,
        "equity_curve": curve,
        "trades": [asdict(trade) for trade in trades],
        "rule": "rebalance to equal symbol weights at each symbol's first source-present open of a new month, scheduled after the prior union-date close",
        "trade_statistics": "not_applicable_to_partial_portfolio_rebalances",
    }


def simple_baselines(
    bars: dict[str, list[HistoricalBar]], strategy: dict[str, Any]
) -> dict[str, Any]:
    buy_hold = run_portfolio_buy_hold(bars)
    fixed_half = fixed_exposure_portfolio(bars, 0.5)
    monthly = monthly_equal_weight_rebalance(bars)
    filter_200 = two_hundred_day_portfolio(bars)
    first = strategy["net"]["equity_curve"][0]
    last = strategy["net"]["equity_curve"][-1]
    cash_curve = [
        {"timestamp": first["timestamp"], "equity": float(STARTING_CAPITAL)},
        {"timestamp": last["timestamp"], "equity": float(STARTING_CAPITAL)},
    ]
    cash_metrics = _result_metrics(cash_curve, [], exposure_percent=0.0)
    output = {
        "equal_weight_buy_and_hold": buy_hold["net"],
        "cash_only": {"metrics": cash_metrics, "equity_curve": cash_curve},
        "fixed_50_equity_50_cash": fixed_half,
        "monthly_equal_weight_rebalance": monthly,
        "close_above_200_day_average": filter_200,
        "registered_20_50_crossover": strategy["net"],
    }
    if tuple(output) != BASELINE_IDS:
        raise RuntimeError("Predeclared baseline set changed")
    return output


def _aligned_values(
    results: dict[str, BacktestResult],
) -> tuple[list[str], dict[str, dict[str, float]]]:
    dates = sorted(
        {
            str(point["timestamp"])[:10]
            for result in results.values()
            for point in result.equity_curve
        }
    )
    values = {
        symbol: {
            str(point["timestamp"])[:10]: float(cast(float | int, point["equity"]))
            for point in result.equity_curve
        }
        for symbol, result in results.items()
    }
    aligned: dict[str, dict[str, float]] = {symbol: {} for symbol in results}
    latest = {symbol: float(STARTING_CAPITAL) for symbol in results}
    for day in dates:
        for symbol in results:
            latest[symbol] = values[symbol].get(day, latest[symbol])
            aligned[symbol][day] = latest[symbol]
    return dates, aligned


def return_attribution(strategy: dict[str, Any]) -> dict[str, Any]:
    results = cast(dict[str, BacktestResult], strategy["net_results"])
    metrics = cast(dict[str, Any], strategy["net"]["metrics"])
    weights = {symbol: 1 / len(results) for symbol in results}
    total_profit = float(metrics["final_equity"]) - float(STARTING_CAPITAL)
    symbols: dict[str, Any] = {}
    for symbol, result in results.items():
        profit = weights[symbol] * (
            float(result.metrics["final_equity"] or 0) - float(STARTING_CAPITAL)
        )
        fees = weights[symbol] * float(result.metrics.get("fee_impact_bdt") or 0)
        slippage = weights[symbol] * float(result.metrics.get("slippage_impact_bdt") or 0)
        symbols[symbol] = {
            "return_contribution_bdt": profit,
            "return_contribution_percent_of_profit": profit / total_profit * 100
            if total_profit
            else None,
            "return_contribution_percentage_points": weights[symbol]
            * float(result.metrics["total_return_percent"] or 0),
            "trade_events": len(result.trades),
            "trade_event_share_percent": len(result.trades)
            / int(metrics["number_of_trades"])
            * 100,
            "fee_contribution_bdt": fees,
            "fee_share_percent": fees / float(metrics["fee_impact_bdt"]) * 100
            if metrics["fee_impact_bdt"]
            else None,
            "slippage_contribution_bdt": slippage,
            "slippage_share_percent": slippage / float(metrics["slippage_impact_bdt"]) * 100
            if metrics["slippage_impact_bdt"]
            else None,
            "average_exposure_percent": float(result.metrics["exposure_percent"] or 0),
            "exposure_share_percent": float(result.metrics["exposure_percent"] or 0)
            / sum(float(item.metrics["exposure_percent"] or 0) for item in results.values())
            * 100,
        }
    curve = cast(list[dict[str, object]], strategy["net"]["equity_curve"])
    monthly: dict[str, tuple[float, float]] = {}
    for point in curve:
        month = str(point["timestamp"])[:7]
        value = float(cast(float | int, point["equity"]))
        monthly[month] = (monthly.get(month, (value, value))[0], value)
    period_returns = {month: end / start - 1 for month, (start, end) in monthly.items() if start}
    extension_profit = sum(symbols[s]["return_contribution_bdt"] for s in EXTENSION_SYMBOLS)
    brac_profit = symbols["BRACBANK"]["return_contribution_bdt"]
    return {
        "symbols": symbols,
        "winning_months": sum(value > 0 for value in period_returns.values()),
        "losing_months": sum(value < 0 for value in period_returns.values()),
        "flat_months": sum(value == 0 for value in period_returns.values()),
        "winning_month_compounded_return_percent": (
            math.prod(1 + value for value in period_returns.values() if value > 0) - 1
        )
        * 100,
        "losing_month_compounded_return_percent": (
            math.prod(1 + value for value in period_returns.values() if value < 0) - 1
        )
        * 100,
        "bracbank": {
            "absolute_bdt": brac_profit,
            "percent_of_profit": brac_profit / total_profit * 100,
        },
        "excluding_bracbank": {
            "absolute_bdt": total_profit - brac_profit,
            "percent_of_profit": (total_profit - brac_profit) / total_profit * 100,
        },
        "extension": {
            "absolute_bdt": extension_profit,
            "percent_of_profit": extension_profit / total_profit * 100,
        },
        "diversification_claim_supported": max(
            item["return_contribution_percent_of_profit"] for item in symbols.values()
        )
        <= 50,
    }


def _market_index(bars: dict[str, list[HistoricalBar]]) -> tuple[list[str], dict[str, float]]:
    source = {
        symbol: {bar.timestamp.date().isoformat(): float(bar.close) for bar in rows}
        for symbol, rows in bars.items()
    }
    dates = sorted({day for values in source.values() for day in values})
    latest: dict[str, float] = {}
    first: dict[str, float] = {}
    index: dict[str, float] = {}
    for day in dates:
        for symbol in bars:
            if day in source[symbol]:
                latest[symbol] = source[symbol][day]
                first.setdefault(symbol, source[symbol][day])
        if len(latest) == len(bars):
            index[day] = mean(latest[symbol] / first[symbol] for symbol in bars)
    return list(index), index


def classify_regimes(bars: dict[str, list[HistoricalBar]]) -> dict[str, dict[str, str]]:
    dates, index = _market_index(bars)
    values = [index[day] for day in dates]
    prior_vols: list[float] = []
    regimes: dict[str, dict[str, str]] = {}
    for position in range(200, len(dates)):
        history = values[:position]
        prior_returns = [
            history[i] / history[i - 1] - 1 for i in range(max(1, len(history) - 20), len(history))
        ]
        vol = pstdev(prior_returns) * math.sqrt(252) if len(prior_returns) > 1 else 0.0
        threshold = median(prior_vols) if prior_vols else vol
        prior_vols.append(vol)
        level, sma = history[-1], mean(history[-200:])
        return_50 = history[-1] / history[-51] - 1
        if level > sma and return_50 > 0.10:
            trend = "strong_uptrend"
        elif level > sma and return_50 > 0:
            trend = "weak_uptrend"
        elif level < sma and return_50 < 0:
            trend = "downtrend"
        else:
            trend = "sideways"
        regimes[dates[position]] = {
            "trend": trend,
            "volatility": "high_volatility" if vol > threshold else "low_volatility",
        }
    return regimes


def _curve_returns(curve: list[dict[str, object]]) -> dict[str, float]:
    values = {
        str(point["timestamp"])[:10]: float(cast(float | int, point["equity"])) for point in curve
    }
    dates = sorted(values)
    return {
        dates[index]: values[dates[index]] / values[dates[index - 1]] - 1
        for index in range(1, len(dates))
        if values[dates[index - 1]]
    }


def _maximum_drawdown_percent(values: list[float]) -> float:
    peak = values[0]
    maximum_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        maximum_drawdown = min(maximum_drawdown, (value / peak - 1) * 100)
    return maximum_drawdown


def _position_days(results: dict[str, BacktestResult], dates: list[str]) -> dict[str, set[str]]:
    output: dict[str, set[str]] = {day: set() for day in dates}
    for symbol, result in results.items():
        held = False
        trades = {trade.timestamp[:10]: trade for trade in result.trades}
        for day in dates:
            if day in trades:
                held = trades[day].side == "buy"
            if held:
                output[day].add(symbol)
    return output


def regime_analysis(
    bars: dict[str, list[HistoricalBar]], strategy: dict[str, Any], benchmark: dict[str, Any]
) -> dict[str, Any]:
    regimes = classify_regimes(bars)
    strategy_returns = _curve_returns(strategy["net"]["equity_curve"])
    benchmark_returns = _curve_returns(benchmark["net"]["equity_curve"])
    dates = sorted(set(strategy_returns) & set(regimes))
    positions = _position_days(strategy["net_results"], dates)
    trade_exits = [
        (symbol, trade)
        for symbol, result in strategy["net_results"].items()
        for trade in result.trades
        if trade.side == "sell"
    ]
    labels = (
        "strong_uptrend",
        "weak_uptrend",
        "sideways",
        "downtrend",
        "high_volatility",
        "low_volatility",
    )
    output: dict[str, Any] = {}
    for label in labels:
        selected = [day for day in dates if label in regimes[day].values()]
        strategy_values = [strategy_returns[day] for day in selected]
        benchmark_values = [benchmark_returns.get(day, 0.0) for day in selected]
        cumulative = [float(STARTING_CAPITAL)]
        for value in strategy_values:
            cumulative.append(cumulative[-1] * (1 + value))
        exits = [trade for _, trade in trade_exits if trade.timestamp[:10] in selected]
        output[label] = {
            "observations": len(selected),
            "trade_exits": len(exits),
            "average_symbol_exposure_percent": mean(
                len(positions[day]) / len(bars) * 100 for day in selected
            )
            if selected
            else 0.0,
            "conditional_return_percent": (math.prod(1 + value for value in strategy_values) - 1)
            * 100,
            "conditional_maximum_drawdown_percent": _maximum_drawdown_percent(cumulative),
            "benchmark_conditional_return_percent": (
                math.prod(1 + value for value in benchmark_values) - 1
            )
            * 100,
            "relative_return_percent": (
                math.prod(1 + value for value in strategy_values)
                - math.prod(1 + value for value in benchmark_values)
            )
            * 100,
            "definition": "classification uses only equal-weight market-index history available before the measured return",
        }
    return {
        "rules": {
            "trend": "prior close versus trailing 200-day mean plus prior 50-day return: >10% strong up, >0 weak up, below both down, otherwise sideways",
            "volatility": "prior 20-day annualized volatility above/below the expanding median of prior trailing-volatility estimates",
        },
        "results": output,
        "future_information_used": False,
    }


def drawdown_attribution(
    bars: dict[str, list[HistoricalBar]],
    strategy: dict[str, Any],
    benchmark: dict[str, Any],
    excluded_dates: dict[str, list[str]],
    regimes: dict[str, dict[str, str]],
    threshold_percent: float = MAJOR_DRAWDOWN_THRESHOLD_PERCENT,
) -> dict[str, Any]:
    curve = cast(list[dict[str, object]], strategy["net"]["equity_curve"])
    dates = [str(point["timestamp"])[:10] for point in curve]
    values = [float(cast(float | int, point["equity"])) for point in curve]
    _, aligned = _aligned_values(strategy["net_results"])
    benchmark_values = {
        str(point["timestamp"])[:10]: float(cast(float | int, point["equity"]))
        for point in benchmark["net"]["equity_curve"]
    }
    positions = _position_days(strategy["net_results"], dates)
    episodes: list[dict[str, Any]] = []
    peak_index = 0
    index = 1
    while index < len(values):
        if values[index] >= values[peak_index]:
            peak_index = index
            index += 1
            continue
        start = peak_index
        trough = index
        while index < len(values) and values[index] < values[start]:
            if values[index] < values[trough]:
                trough = index
            index += 1
        loss_percent = (values[trough] / values[start] - 1) * 100
        recovery = index if index < len(values) else None
        if loss_percent <= -threshold_percent:
            start_day, trough_day = dates[start], dates[trough]
            recovery_day = dates[recovery] if recovery is not None else None
            symbol_contributions = {}
            for symbol in strategy["net_results"]:
                before = aligned[symbol].get(start_day, float(STARTING_CAPITAL))
                after = aligned[symbol].get(trough_day, before)
                change = (after - before) / len(strategy["net_results"])
                symbol_contributions[symbol] = {
                    "bdt": change,
                    "percent_of_portfolio_peak": change / values[start] * 100,
                }
            bh_start = benchmark_values.get(start_day)
            bh_trough = benchmark_values.get(trough_day)
            bh_loss = (bh_trough / bh_start - 1) * 100 if bh_start and bh_trough else None
            interval_end = date.fromisoformat(recovery_day or trough_day) + timedelta(days=7)
            interval_start = date.fromisoformat(start_day) - timedelta(days=7)
            nearby = {
                symbol: [
                    day
                    for day in excluded_dates.get(symbol, [])
                    if interval_start <= date.fromisoformat(day) <= interval_end
                ]
                for symbol in bars
            }
            active = sorted(
                {
                    symbol
                    for day in dates[start : (recovery or trough) + 1]
                    for symbol in positions.get(day, set())
                }
            )
            episode_regime = (
                regimes.get(start_day)
                or regimes.get(trough_day)
                or {"trend": "unclassified", "volatility": "unclassified"}
            )
            episodes.append(
                {
                    "start_date": start_day,
                    "trough_date": trough_day,
                    "recovery_date": recovery_day,
                    "duration_days": (
                        date.fromisoformat(recovery_day or dates[-1])
                        - date.fromisoformat(start_day)
                    ).days,
                    "portfolio_loss_bdt": values[trough] - values[start],
                    "portfolio_loss_percent": loss_percent,
                    "symbol_contributions": symbol_contributions,
                    "active_positions": active,
                    "market_regime": episode_regime,
                    "buy_and_hold_loss_percent_same_window": bh_loss,
                    "ma_loss_effect": "reduced"
                    if bh_loss is not None and loss_percent > bh_loss
                    else "increased_or_not_reduced",
                    "excluded_data_intervals_nearby": {
                        symbol: values for symbol, values in nearby.items() if values
                    },
                }
            )
        if recovery is None:
            break
        peak_index = recovery
        index = recovery + 1
    return {
        "major_threshold_percent": threshold_percent,
        "episodes": episodes,
        "count": len(episodes),
    }


def closed_trade_records(
    bars: dict[str, list[HistoricalBar]],
    results: dict[str, BacktestResult],
    excluded_dates: dict[str, list[str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for symbol, result in results.items():
        symbol_bars = bars[symbol]
        bar_index = {
            bar.timestamp.date().isoformat(): index for index, bar in enumerate(symbol_bars)
        }
        entries: list[Trade] = []
        for trade in result.trades:
            if trade.side == "buy":
                entries.append(trade)
                continue
            if not entries:
                continue
            entry = entries.pop(0)
            entry_index, exit_index = (
                bar_index[entry.timestamp[:10]],
                bar_index[trade.timestamp[:10]],
            )
            gross_pnl = trade.price * trade.quantity - entry.price * entry.quantity
            costs = (
                entry.fee
                + trade.fee
                + (entry.slippage * entry.quantity)
                + (trade.slippage * trade.quantity)
            )
            net_pnl = (trade.price * trade.quantity - trade.fee) - (
                entry.price * entry.quantity + entry.fee
            )
            holding = exit_index - entry_index
            held_bars = symbol_bars[entry_index : exit_index + 1]
            prior_close_entry = (
                float(symbol_bars[entry_index - 1].close)
                if entry_index
                else float(symbol_bars[entry_index].open)
            )
            prior_close_exit = (
                float(symbol_bars[exit_index - 1].close)
                if exit_index
                else float(symbol_bars[exit_index].open)
            )
            adverse_gap = max(
                entry.price / prior_close_entry - 1, prior_close_exit / trade.price - 1
            )
            late_entry = (
                entry_index >= 20
                and entry.price / float(symbol_bars[entry_index - 20].close) - 1 >= 0.05
            )
            max_close = max(float(bar.close) for bar in held_bars)
            late_exit = max_close > 0 and trade.price / max_close - 1 <= -0.10
            missing_adjacent = any(
                (symbol_bars[index].timestamp.date() - symbol_bars[index - 1].timestamp.date()).days
                > 7
                for index in range(max(1, entry_index - 1), min(len(symbol_bars), exit_index + 2))
            )
            exclusion_adjacent = any(
                abs((date.fromisoformat(day) - date.fromisoformat(event)).days) <= 5
                for day in excluded_dates.get(symbol, [])
                for event in (entry.timestamp[:10], trade.timestamp[:10])
            )
            flags = {
                "late_entry": late_entry,
                "late_exit": late_exit,
                "gap_loss": net_pnl < 0 and adverse_gap >= 0.03,
                "missing_data_adjacency": missing_adjacent,
                "exclusion_interval_adjacency": exclusion_adjacent,
                "high_cost_trade": costs >= 0.25 * abs(gross_pnl) if gross_pnl else costs > 0,
                "low_duration_trade": holding <= 10,
                "prolonged_losing_trade": net_pnl < 0 and holding >= 60,
                "whipsaw": net_pnl < 0 and holding < 60,
                "profitable_trend_capture": net_pnl > 0,
            }
            if flags["gap_loss"]:
                primary = "gap_loss"
            elif flags["prolonged_losing_trade"]:
                primary = "prolonged_losing_trade"
            elif flags["whipsaw"]:
                primary = "whipsaw"
            else:
                primary = "profitable_trend_capture"
            records.append(
                {
                    "symbol": symbol,
                    "entry_date": entry.timestamp[:10],
                    "exit_date": trade.timestamp[:10],
                    "holding_source_bars": holding,
                    "quantity": entry.quantity,
                    "gross_pnl_bdt": gross_pnl,
                    "net_pnl_bdt": net_pnl,
                    "cost_bdt": costs,
                    "primary_classification": primary,
                    **flags,
                }
            )
    return records


def trade_failure_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    labels = (
        "profitable_trend_capture",
        "whipsaw",
        "late_entry",
        "late_exit",
        "gap_loss",
        "missing_data_adjacency",
        "exclusion_interval_adjacency",
        "high_cost_trade",
        "low_duration_trade",
        "prolonged_losing_trade",
    )
    return {
        "closed_trades": len(records),
        "labels_are_nonexclusive_except_primary": True,
        "criteria": {
            "whipsaw": "net loss held fewer than 60 source-present bars",
            "late_entry": "entry at least 5% above the close 20 source-present bars earlier",
            "late_exit": "exit at least 10% below the maximum close during the holding interval",
            "gap_loss": "net loss with at least 3% adverse entry or exit opening gap",
            "missing_data_adjacency": "a source-present gap over seven calendar days within one bar of the holding interval",
            "exclusion_interval_adjacency": "entry or exit within five calendar days of a preserved excluded date",
            "high_cost_trade": "modeled fee plus slippage at least 25% of absolute gross P&L",
            "low_duration_trade": "held ten or fewer source-present bars",
            "prolonged_losing_trade": "net loss held at least 60 source-present bars",
            "profitable_trend_capture": "positive net closed-trade P&L",
        },
        "types": {
            label: {
                "count": sum(bool(row[label]) for row in records),
                "net_contribution_bdt": sum(
                    float(row["net_pnl_bdt"]) for row in records if row[label]
                ),
            }
            for label in labels
        },
        "primary_counts": dict(Counter(str(row["primary_classification"]) for row in records)),
    }


def symbol_dependence(
    bars: dict[str, list[HistoricalBar]], strategy_summaries: dict[str, Any]
) -> dict[str, Any]:
    best = sorted(
        strategy_summaries,
        key=lambda symbol: (
            -float(strategy_summaries[symbol]["net"]["total_return_percent"]),
            symbol,
        ),
    )[0]
    worst = sorted(
        strategy_summaries,
        key=lambda symbol: (
            float(strategy_summaries[symbol]["net"]["total_return_percent"]),
            symbol,
        ),
    )[0]
    universes = {
        "all_five": bars,
        "without_bracbank": {symbol: rows for symbol, rows in bars.items() if symbol != "BRACBANK"},
        "without_best": {symbol: rows for symbol, rows in bars.items() if symbol != best},
        "without_worst": {symbol: rows for symbol, rows in bars.items() if symbol != worst},
        "original_three": {symbol: bars[symbol] for symbol in PARENT_SYMBOLS},
        "extension_two": {symbol: bars[symbol] for symbol in EXTENSION_SYMBOLS},
    }
    return {
        "best_symbol": best,
        "worst_symbol": worst,
        "universes": {
            name: {
                "symbols": list(subset),
                "strategy": run_portfolio(subset)["net"]["metrics"],
                "buy_and_hold": run_portfolio_buy_hold(subset)["net"]["metrics"],
            }
            for name, subset in universes.items()
        },
    }


def exposure_matched_benchmark(
    bars: dict[str, list[HistoricalBar]], strategy_exposure_percent: float
) -> dict[str, Any]:
    fraction = strategy_exposure_percent / 100
    result = fixed_exposure_portfolio(bars, fraction)
    return {
        **result,
        "predeclared_rule": "fixed initial equity allocation equals the measured average 20/50 market exposure; the balance stays in cash and the fraction is not selected on return",
        "target_exposure_percent": strategy_exposure_percent,
        "performance_optimized": False,
    }


def cost_benefit(
    strategy: dict[str, Any], benchmark: dict[str, Any], exposure_matched: dict[str, Any]
) -> dict[str, Any]:
    ma = strategy["net"]["metrics"]
    bh = benchmark["net"]["metrics"]
    matched = exposure_matched["metrics"]
    drawdown_avoided = abs(float(bh["maximum_drawdown_percent"])) - abs(
        float(ma["maximum_drawdown_percent"])
    )
    return_sacrificed = float(bh["total_return_percent"]) - float(ma["total_return_percent"])
    return {
        "versus_buy_and_hold": {
            "return_sacrificed_percent_points": return_sacrificed,
            "drawdown_avoided_percent_points": drawdown_avoided,
            "return_sacrificed_per_drawdown_point_avoided": return_sacrificed / drawdown_avoided
            if drawdown_avoided
            else None,
            "annualized_return_difference_percent_points": float(ma["annualized_return_percent"])
            - float(bh["annualized_return_percent"]),
            "volatility_difference_percent_points": float(ma["volatility_percent"])
            - float(bh["volatility_percent"]),
            "sharpe_difference": float(ma.get("sharpe_ratio") or 0)
            - float(bh.get("sharpe_ratio") or 0),
            "sortino_difference": float(ma.get("sortino_ratio") or 0)
            - float(bh.get("sortino_ratio") or 0),
            "calmar_difference": float(ma.get("calmar_ratio") or 0)
            - float(bh.get("calmar_ratio") or 0),
            "exposure_difference_percent_points": float(ma["exposure_percent"])
            - float(bh["exposure_percent"]),
            "turnover_difference": float(ma["turnover_rate"]) - float(bh["turnover_rate"]),
            "additional_fees_bdt": float(ma["fee_impact_bdt"]) - float(bh["fee_impact_bdt"]),
            "additional_slippage_bdt": float(ma["slippage_impact_bdt"])
            - float(bh["slippage_impact_bdt"]),
        },
        "versus_exposure_matched": {
            "return_difference_percent_points": float(ma["total_return_percent"])
            - float(matched["total_return_percent"]),
            "drawdown_difference_percent_points": abs(float(matched["maximum_drawdown_percent"]))
            - abs(float(ma["maximum_drawdown_percent"])),
            "sharpe_difference": float(ma.get("sharpe_ratio") or 0)
            - float(matched.get("sharpe_ratio") or 0),
        },
        "interpretation_boundary": "descriptive risk/return trade-off only; investment preference is not inferred",
    }


def walk_forward_failure_analysis(
    existing: dict[str, Any],
    bars: dict[str, list[HistoricalBar]],
    regimes: dict[str, dict[str, str]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    grouped_returns: dict[str, dict[str, float]] = {}
    for symbol, symbol_result in existing["symbols"].items():
        for partition in symbol_result["partitions"]:
            start, end = str(partition["validation_start"]), str(partition["validation_end"])
            validation_bars = [
                bar for bar in bars[symbol] if start <= bar.timestamp.date().isoformat() <= end
            ]
            benchmark = run_symbol(symbol, validation_bars, strategy="buy_hold")
            strategy_return = float(
                partition["out_of_sample_metrics"].get("total_return_percent") or 0
            )
            benchmark_return = float(benchmark.metrics["total_return_percent"] or 0)
            regime_counts = Counter(regimes[day]["trend"] for day in regimes if start <= day <= end)
            market_regime = (
                sorted(regime_counts, key=lambda label: (-regime_counts[label], label))[0]
                if regime_counts
                else "unclassified"
            )
            key = str(partition["partition"])
            grouped_returns.setdefault(key, {})[symbol] = strategy_return
            if strategy_return < 0 and benchmark_return >= 0:
                reason = "failed_to_capture_positive_market"
            elif strategy_return < 0:
                reason = "negative_in_nonpositive_market"
            elif strategy_return < benchmark_return:
                reason = "positive_but_lagged_benchmark"
            else:
                reason = "positive_and_outperformed_benchmark"
            rows.append(
                {
                    "stage": "validation",
                    "partition": key,
                    "symbol": symbol,
                    "training_start": partition["training_start"],
                    "training_end": partition["training_end"],
                    "validation_start": start,
                    "validation_end": end,
                    "market_regime": market_regime,
                    "selected_parameters": partition["selected_parameters"],
                    "validation_return_percent": strategy_return,
                    "benchmark_return_percent": benchmark_return,
                    "maximum_drawdown_percent": partition["out_of_sample_metrics"].get(
                        "maximum_drawdown_percent"
                    ),
                    "trade_events": partition["number_of_trades"],
                    "reason": reason,
                    "holdout_retuned": False,
                }
            )
        holdout = symbol_result["final_holdout"]
        start, end = str(holdout["start"]), str(holdout["end"])
        holdout_bars = [
            bar for bar in bars[symbol] if start <= bar.timestamp.date().isoformat() <= end
        ]
        benchmark = run_symbol(symbol, holdout_bars, strategy="buy_hold")
        strategy_return = float(holdout["metrics"].get("total_return_percent") or 0)
        benchmark_return = float(benchmark.metrics["total_return_percent"] or 0)
        regime_counts = Counter(regimes[day]["trend"] for day in regimes if start <= day <= end)
        market_regime = (
            sorted(regime_counts, key=lambda label: (-regime_counts[label], label))[0]
            if regime_counts
            else "unclassified"
        )
        grouped_returns.setdefault("final_holdout", {})[symbol] = strategy_return
        rows.append(
            {
                "stage": "final_holdout",
                "partition": "final_holdout",
                "symbol": symbol,
                "training_start": symbol_result["partitions"][0]["training_start"],
                "training_end": symbol_result["partitions"][-1]["validation_end"],
                "validation_start": start,
                "validation_end": end,
                "market_regime": market_regime,
                "selected_parameters": holdout["selected_parameters"],
                "validation_return_percent": strategy_return,
                "benchmark_return_percent": benchmark_return,
                "maximum_drawdown_percent": holdout["metrics"].get("maximum_drawdown_percent"),
                "trade_events": holdout["number_of_trades"],
                "reason": "negative_holdout" if strategy_return < 0 else "positive_holdout",
                "holdout_retuned": False,
            }
        )
    dominant = {
        key: sorted(values, key=lambda symbol: (-values[symbol], symbol))[0]
        for key, values in grouped_returns.items()
    }
    for row in rows:
        row["dominant_symbol"] = dominant[str(row["partition"])]
    return {
        "rows": rows,
        "negative_validation_partitions": sum(
            row["stage"] == "validation" and float(row["validation_return_percent"]) < 0
            for row in rows
        ),
        "positive_validation_partitions": sum(
            row["stage"] == "validation" and float(row["validation_return_percent"]) > 0
            for row in rows
        ),
        "zero_validation_partitions": sum(
            row["stage"] == "validation" and float(row["validation_return_percent"]) == 0
            for row in rows
        ),
        "holdout_retuning_performed": False,
        "failure_reason_counts": dict(
            Counter(str(row["reason"]) for row in rows if row["stage"] == "validation")
        ),
    }


def research_decision(
    dependence: dict[str, Any],
    walk_forward: dict[str, Any],
    baselines: dict[str, Any],
    exposure_matched: dict[str, Any],
) -> dict[str, Any]:
    ma = baselines["registered_20_50_crossover"]["metrics"]
    matched = exposure_matched["metrics"]
    without_brac = dependence["universes"]["without_bracbank"]["strategy"]
    brac_dependent = float(without_brac["total_return_percent"]) < 0.5 * float(
        ma["total_return_percent"]
    )
    negative = int(walk_forward["dispersion"]["negative_partitions"])
    matched_replicates = abs(float(matched["maximum_drawdown_percent"])) <= abs(
        float(ma["maximum_drawdown_percent"])
    ) and float(matched["total_return_percent"]) >= float(ma["total_return_percent"])
    simpler_dominates = any(
        float(value["metrics"]["total_return_percent"]) >= float(ma["total_return_percent"])
        and abs(float(value["metrics"]["maximum_drawdown_percent"]))
        <= abs(float(ma["maximum_drawdown_percent"]))
        for key, value in baselines.items()
        if key != "registered_20_50_crossover"
    )
    reasons = []
    if brac_dependent:
        reasons.append("return remains materially BRACBANK-dependent")
    if negative:
        reasons.append(f"{negative} chronological validation partitions are negative")
    if matched_replicates:
        reasons.append("fixed exposure matching replicates or dominates the drawdown benefit")
    if simpler_dominates:
        reasons.append("at least one simpler predeclared baseline dominates return and drawdown")
    role = (
        "reject_strategy"
        if brac_dependent or negative or simpler_dominates
        else "retain_as_risk_overlay_candidate"
    )
    return {
        "research_role": role,
        "reasons": reasons,
        "promotion_authorized": False,
        "next_research_path": "A. reject and archive 20/50"
        if role == "reject_strategy"
        else "C. redesign as a portfolio risk overlay",
        "implementation_authorized": False,
        "qualification": "0/60",
    }

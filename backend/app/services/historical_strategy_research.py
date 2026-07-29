from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, cast

from app.backtesting.engine import BacktestResult, Trade, run_backtest
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.schemas.trading import BacktestRequest

ALLOWED_SYMBOLS = ("GP", "ACI", "BRACBANK")
REGISTERED_PARAMETERS: dict[str, int] = {"fast": 20, "slow": 50}
PARAMETER_GRID = tuple(
    {"fast": fast, "slow": slow} for fast in (10, 20, 30) for slow in (40, 50, 75) if fast < slow
)
FEE_SCENARIOS = {
    "low_cost_research": Decimal("0.10"),
    "current_conservative_draft": Decimal("0.40"),
    "stricter_cost_stress": Decimal("0.75"),
}
SLIPPAGE_SCENARIOS = {
    "optimistic": Decimal("0.00"),
    "balanced": Decimal("0.10"),
    "pessimistic": Decimal("0.25"),
    "severe_stress": Decimal("0.50"),
}
BASELINE_FEE_PERCENT = Decimal("0.40")
BASELINE_SLIPPAGE_PERCENT = Decimal("0.25")
STARTING_CAPITAL = Decimal("1000000")
MAX_VOLUME_PARTICIPATION = Decimal("0.05")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_and_load_dataset(path: Path) -> tuple[dict[str, list[HistoricalBar]], dict[str, Any]]:
    raw_rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    required = {
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adjustment_status",
        "selected_source",
        "contributing_sources",
        "source_row_ids",
        "raw_hashes",
        "source_lineage",
        "transformation_version",
        "quality_tier",
        "approval_decision_id",
        "activation_timestamp",
        "audit_linkage",
    }
    symbols = Counter(str(row.get("symbol")) for row in raw_rows)
    full_keys = Counter(
        (str(row.get("symbol")), str(row.get("date")), str(row.get("adjustment_status")))
        for row in raw_rows
    )
    symbol_date = Counter((str(row.get("symbol")), str(row.get("date"))) for row in raw_rows)
    incomplete_lineage = 0
    invalid_ohlc = 0
    adjusted_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        incomplete_lineage += int(
            not required.issubset(row)
            or not row.get("contributing_sources")
            or not row.get("source_row_ids")
            or not row.get("raw_hashes")
            or not row.get("source_lineage")
            or not row.get("audit_linkage")
        )
        try:
            open_, high, low, close, volume = (
                Decimal(str(row[field])) for field in ("open", "high", "low", "close", "volume")
            )
            invalid_ohlc += int(
                high < low or not low <= open_ <= high or not low <= close <= high or volume < 0
            )
        except (ArithmeticError, ValueError):
            invalid_ohlc += 1
        if row.get("adjustment_status") == "adjusted":
            adjusted_rows.append(row)
    selected_keys = Counter((str(row["symbol"]), str(row["date"])) for row in adjusted_rows)
    selected_by_symbol: dict[str, list[dict[str, Any]]] = {
        symbol: [row for row in adjusted_rows if row["symbol"] == symbol]
        for symbol in ALLOWED_SYMBOLS
    }
    ordered = {
        symbol: [str(row["date"]) for row in rows] == sorted(str(row["date"]) for row in rows)
        for symbol, rows in selected_by_symbol.items()
    }
    checks = {
        "only_allowed_symbols": set(symbols) == set(ALLOWED_SYMBOLS),
        "dsex_rows": symbols.get("DSEX", 0),
        "full_grain_duplicates": sum(value - 1 for value in full_keys.values() if value > 1),
        "parallel_adjustment_views": sum(value - 1 for value in symbol_date.values() if value > 1),
        "selected_series": "adjusted",
        "selected_series_rows": len(adjusted_rows),
        "selected_symbol_date_duplicates": sum(
            value - 1 for value in selected_keys.values() if value > 1
        ),
        "invalid_ohlc_rows": invalid_ohlc,
        "incomplete_lineage_rows": incomplete_lineage,
        "dates_ordered": ordered,
        "adjustment_usage": (
            "adjusted rows are the execution series; unadjusted rows are validation-only "
            "and are never pooled into prices or signals"
        ),
        "symbols": dict(symbols),
        "adjustment_counts": dict(Counter(str(row["adjustment_status"]) for row in raw_rows)),
        "quality_tiers": dict(Counter(str(row["quality_tier"]) for row in raw_rows)),
        "mandatory_passed": False,
    }
    checks["mandatory_passed"] = bool(
        checks["only_allowed_symbols"]
        and checks["dsex_rows"] == 0
        and checks["full_grain_duplicates"] == 0
        and checks["selected_symbol_date_duplicates"] == 0
        and checks["invalid_ohlc_rows"] == 0
        and checks["incomplete_lineage_rows"] == 0
        and all(ordered.values())
        and all(selected_by_symbol.values())
    )
    if not checks["mandatory_passed"]:
        raise ValueError(f"Mandatory active-dataset validation failed: {checks}")

    bars: dict[str, list[HistoricalBar]] = {}
    for symbol, rows in selected_by_symbol.items():
        bars[symbol] = []
        for row in rows:
            original_volume = int(Decimal(str(row["volume"])))
            capacity = int(Decimal(original_volume) * MAX_VOLUME_PARTICIPATION)
            bars[symbol].append(
                HistoricalBar(
                    timestamp=datetime.combine(
                        date.fromisoformat(str(row["date"])), datetime.min.time(), UTC
                    ),
                    symbol=symbol,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=capacity,
                    source=str(row["selected_source"]),
                    timestamp_provenance=TimestampProvenance.UNKNOWN,
                    quality_flags=[
                        str(row["quality_tier"]),
                        "volume_capacity_limited_to_5_percent_of_reported_volume",
                    ],
                )
            )
    return bars, checks


def timing_semantics() -> dict[str, Any]:
    return {
        "signal_fields": ["adjusted close"],
        "signal_rule": "fast trailing close mean strictly greater than slow trailing close mean",
        "signal_observable": "after the source-present bar close has completed",
        "earliest_execution": "next source-present bar open",
        "execution_reference": "next open with adverse modeled slippage",
        "same_bar_execution": False,
        "missing_next_session": "defer to the next source-present bar; never synthesize a bar",
        "excluded_dates": "no signal or execution occurs on excluded rows",
        "gaps": "observed sequence continues without calendar inference; gap risk is disclosed",
        "future_adjustment_leakage": (
            "adjusted series is used as an approved historical research view only; results are "
            "not treated as point-in-time corporate-action knowledge"
        ),
        "market_order_assumption": False,
        "fill_model": (
            "research-only participation-capacity check at next open; fills are not guaranteed "
            "and do not represent executable broker orders"
        ),
    }


def _request(
    symbol: str,
    *,
    strategy: str = "ma_crossover",
    parameters: dict[str, int] | None = None,
    fee_percent: Decimal = BASELINE_FEE_PERCENT,
    slippage_percent: Decimal = BASELINE_SLIPPAGE_PERCENT,
) -> BacktestRequest:
    return BacktestRequest(
        symbol=symbol,
        strategy=cast(Any, strategy),
        parameters=cast(dict[str, float | int], parameters or REGISTERED_PARAMETERS),
        starting_capital=STARTING_CAPITAL,
        fee_percent=fee_percent,
        slippage_percent=slippage_percent,
        minimum_quantity=1,
    )


def _closed_trade_stats(trades: list[Trade]) -> dict[str, Any]:
    entries: list[Trade] = []
    pnls: list[float] = []
    holding_days: list[int] = []
    for trade in trades:
        if trade.side == "buy":
            entries.append(trade)
        elif entries:
            entry = entries.pop(0)
            pnl = (trade.price * trade.quantity - trade.fee) - (
                entry.price * entry.quantity + entry.fee
            )
            pnls.append(pnl)
            holding_days.append(
                (
                    date.fromisoformat(trade.timestamp[:10])
                    - date.fromisoformat(entry.timestamp[:10])
                ).days
            )
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    count = len(pnls)
    if count:
        rate = len(wins) / count
        z = 1.96
        denominator = 1 + z * z / count
        center = (rate + z * z / (2 * count)) / denominator
        margin = (
            z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
        )
        interval: list[float] | None = [max(0.0, center - margin), min(1.0, center + margin)]
    else:
        interval = None
    return {
        "completed_trades": count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / count if count else 0.0,
        "win_rate_wilson_95_interval": interval,
        "average_holding_days": mean(holding_days) if holding_days else None,
        "trade_pnls": pnls,
    }


def _curve_metrics(curve: list[dict[str, object]], starting_capital: float) -> dict[str, Any]:
    values = [float(cast(float | int, item["equity"])) for item in curve]
    timestamps = [str(item["timestamp"])[:10] for item in curve]
    returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values))]
    total_return = values[-1] / starting_capital - 1
    years = max(
        (date.fromisoformat(timestamps[-1]) - date.fromisoformat(timestamps[0])).days / 365.25,
        1 / 365.25,
    )
    annualized = (values[-1] / starting_capital) ** (1 / years) - 1 if values[-1] > 0 else -1.0
    volatility = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    downside = [value for value in returns if value < 0]
    downside_deviation = pstdev(downside) * math.sqrt(252) if len(downside) > 1 else 0.0
    peak = values[0]
    max_drawdown = 0.0
    longest_bars = current_bars = 0
    longest_days = 0
    current_start: date | None = None
    for value, timestamp in zip(values, timestamps, strict=True):
        if value >= peak:
            peak = value
            current_bars = 0
            current_start = None
        else:
            current_bars += 1
            if current_start is None:
                current_start = date.fromisoformat(timestamp)
            longest_bars = max(longest_bars, current_bars)
            longest_days = max(
                longest_days,
                (date.fromisoformat(timestamp) - current_start).days,
            )
        max_drawdown = min(max_drawdown, value / peak - 1)
    return {
        "total_return_percent": total_return * 100,
        "annualized_return_percent": annualized * 100,
        "volatility_percent": volatility * 100,
        "sharpe_ratio": annualized / volatility if volatility else None,
        "sortino_ratio": annualized / downside_deviation if downside_deviation else None,
        "calmar_ratio": annualized / abs(max_drawdown) if max_drawdown else None,
        "maximum_drawdown_percent": max_drawdown * 100,
        "drawdown_duration_bars": longest_bars,
        "drawdown_duration_days": longest_days,
        "final_equity": values[-1],
    }


def summarize_result(
    result: BacktestResult,
    *,
    gross_result: BacktestResult | None = None,
    observations: int,
    missing_data_exclusions: int,
    liquidity_exclusions: int = 0,
) -> dict[str, Any]:
    stats = _closed_trade_stats(result.trades)
    trade_pnls = cast(list[float], stats.pop("trade_pnls"))
    gross_profit = sum(value for value in trade_pnls if value > 0)
    gross_loss = abs(sum(value for value in trade_pnls if value <= 0))
    entries = sum(trade.side == "buy" for trade in result.trades)
    exposure = float(result.metrics.get("exposure_percent") or 0.0)
    return {
        "observations": observations,
        "gross": (dict(gross_result.metrics) if gross_result else None),
        "net": dict(result.metrics),
        **stats,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss
            else (None if not gross_profit else "infinite_no_losing_closed_trade")
        ),
        "expectancy_bdt": mean(trade_pnls) if trade_pnls else None,
        "number_of_entries": entries,
        "average_holding_period_days": stats["average_holding_days"],
        "trade_frequency_per_year": (stats["completed_trades"] / max(observations / 252, 1 / 252)),
        "exposure_percent": exposure,
        "fees_bdt": result.metrics.get("fee_impact_bdt"),
        "slippage_bdt": result.metrics.get("slippage_impact_bdt"),
        "turnover": result.metrics.get("turnover_rate"),
        "skipped_entries": liquidity_exclusions,
        "liquidity_exclusions": liquidity_exclusions,
        "missing_data_exclusions": missing_data_exclusions,
        "effective_sample_caution": (
            "insufficient independent trades for strong inference"
            if int(stats["completed_trades"]) < 30
            else "trade count permits descriptive intervals only; serial dependence remains"
        ),
    }


def run_symbol(
    symbol: str,
    bars: list[HistoricalBar],
    *,
    parameters: dict[str, int] | None = None,
    fee_percent: Decimal = BASELINE_FEE_PERCENT,
    slippage_percent: Decimal = BASELINE_SLIPPAGE_PERCENT,
    strategy: str = "ma_crossover",
) -> BacktestResult:
    return run_backtest(
        bars,
        _request(
            symbol,
            strategy=strategy,
            parameters=parameters,
            fee_percent=fee_percent,
            slippage_percent=slippage_percent,
        ),
        is_subrun=True,
    )


def count_liquidity_exclusions(
    bars: list[HistoricalBar], parameters: dict[str, int] | None = None
) -> int:
    """Count candidate entries blocked by the declared five-percent capacity.

    This mirrors the registered close-signal/next-open timing without changing the
    registered implementation. It is deliberately conservative: capacity is
    compared with the maximum affordable quantity from starting capital.
    """
    parameters = parameters or REGISTERED_PARAMETERS
    fast, slow = parameters["fast"], parameters["slow"]
    closes = [float(bar.close) for bar in bars]
    signals = [False] * len(bars)
    for index in range(slow - 1, len(bars)):
        signals[index] = mean(closes[index - fast + 1 : index + 1]) > mean(
            closes[index - slow + 1 : index + 1]
        )
    exclusions = 0
    for index in range(1, len(bars)):
        if signals[index - 1] and (index == 1 or not signals[index - 2]):
            fill = float(bars[index].open) * (1 + float(BASELINE_SLIPPAGE_PERCENT) / 100)
            affordable = int(
                float(STARTING_CAPITAL) / (fill * (1 + float(BASELINE_FEE_PERCENT) / 100))
            )
            exclusions += int((bars[index].volume or 0) < affordable)
    return exclusions


def combine_results(results: dict[str, BacktestResult]) -> dict[str, Any]:
    all_dates = sorted(
        {
            str(point["timestamp"])[:10]
            for result in results.values()
            for point in result.equity_curve
        }
    )
    values_by_symbol = {
        symbol: {
            str(point["timestamp"])[:10]: float(cast(float | int, point["equity"]))
            for point in result.equity_curve
        }
        for symbol, result in results.items()
    }
    latest = {symbol: float(STARTING_CAPITAL) for symbol in results}
    curve: list[dict[str, object]] = []
    for day in all_dates:
        for symbol in results:
            latest[symbol] = values_by_symbol[symbol].get(day, latest[symbol])
        curve.append(
            {
                "timestamp": day,
                "equity": mean(latest.values()),
            }
        )
    metrics = _curve_metrics(curve, float(STARTING_CAPITAL))
    completed = sum(
        _closed_trade_stats(result.trades)["completed_trades"] for result in results.values()
    )
    metrics.update(
        {
            "number_of_trades": sum(len(result.trades) for result in results.values()),
            "completed_trades": completed,
            "fee_impact_bdt": mean(
                float(result.metrics.get("fee_impact_bdt") or 0.0) for result in results.values()
            ),
            "slippage_impact_bdt": mean(
                float(result.metrics.get("slippage_impact_bdt") or 0.0)
                for result in results.values()
            ),
            "turnover_rate": mean(
                float(result.metrics.get("turnover_rate") or 0.0) for result in results.values()
            ),
            "exposure_percent": mean(
                float(result.metrics.get("exposure_percent") or 0.0) for result in results.values()
            ),
        }
    )
    return {"metrics": metrics, "equity_curve": curve}


def baseline_analysis(
    bars: dict[str, list[HistoricalBar]], excluded_counts: dict[str, int]
) -> dict[str, Any]:
    net = {symbol: run_symbol(symbol, rows) for symbol, rows in bars.items()}
    gross = {
        symbol: run_symbol(symbol, rows, fee_percent=Decimal("0"), slippage_percent=Decimal("0"))
        for symbol, rows in bars.items()
    }
    summaries = {
        symbol: summarize_result(
            net[symbol],
            gross_result=gross[symbol],
            observations=len(rows),
            missing_data_exclusions=excluded_counts.get(symbol, 0),
            liquidity_exclusions=count_liquidity_exclusions(rows),
        )
        for symbol, rows in bars.items()
    }
    combined = combine_results(net)
    combined_gross = combine_results(gross)
    combined["gross_metrics"] = combined_gross["metrics"]
    combined["observations"] = sum(len(rows) for rows in bars.values())
    combined["missing_data_exclusions"] = sum(excluded_counts.values())
    combined["liquidity_exclusions"] = sum(
        count_liquidity_exclusions(rows) for rows in bars.values()
    )
    combined_stats = _closed_trade_stats(
        [trade for result in net.values() for trade in result.trades]
    )
    combined["sample_statistics"] = {
        key: value for key, value in combined_stats.items() if key != "trade_pnls"
    }
    return {"results": net, "gross_results": gross, "summaries": summaries, "combined": combined}


def benchmark_analysis(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    results = {
        symbol: run_symbol(symbol, rows, strategy="buy_hold") for symbol, rows in bars.items()
    }
    return {
        "symbols": {symbol: dict(result.metrics) for symbol, result in results.items()},
        "equal_weight": combine_results(results)["metrics"],
        "cash": {"total_return_percent": 0.0, "final_equity": float(STARTING_CAPITAL)},
        "dsex": "unavailable_not_substituted",
        "assumptions": {
            "fee_percent": str(BASELINE_FEE_PERCENT),
            "slippage_percent": str(BASELINE_SLIPPAGE_PERCENT),
            "first_possible_entry": "second source-present bar open",
            "ending_value": "last source-present close",
        },
    }


def _metric_on_window(result: BacktestResult, start: int, end: int) -> dict[str, Any]:
    curve = result.equity_curve[start:end]
    if len(curve) < 2:
        return {"total_return_percent": None, "maximum_drawdown_percent": None}
    metrics = _curve_metrics(curve, float(cast(float | int, curve[0]["equity"])))
    start_day = str(curve[0]["timestamp"])[:10]
    end_day = str(curve[-1]["timestamp"])[:10]
    trades = [trade for trade in result.trades if start_day <= trade.timestamp[:10] <= end_day]
    metrics.update(
        {
            "number_of_trades": len(trades),
            "fees_bdt": sum(trade.fee for trade in trades),
            "slippage_bdt": sum(trade.slippage * trade.quantity for trade in trades),
            "turnover": sum(trade.price * trade.quantity for trade in trades)
            / float(cast(float | int, curve[0]["equity"])),
        }
    )
    return metrics


def walk_forward_analysis(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    output: dict[str, Any] = {"symbols": {}, "final_holdout_untouched_during_tuning": True}
    for symbol, rows in bars.items():
        holdout_start = int(len(rows) * 0.80)
        pre = rows[:holdout_start]
        split_points = (int(len(pre) * 0.50), int(len(pre) * 0.70), int(len(pre) * 0.85))
        partitions: list[dict[str, Any]] = []
        validation_scores: dict[tuple[int, int], list[float]] = {
            (item["fast"], item["slow"]): [] for item in PARAMETER_GRID
        }
        prior_end = 0
        for index, train_end in enumerate(split_points):
            validation_end = split_points[index + 1] if index + 1 < len(split_points) else len(pre)
            if validation_end - train_end < 2:
                continue
            train_scores: list[tuple[float, dict[str, int]]] = []
            for parameters in PARAMETER_GRID:
                result = run_symbol(symbol, rows[:train_end], parameters=parameters)
                score = float(result.metrics.get("sharpe_ratio") or -999.0)
                train_scores.append((score, parameters))
            selected = max(
                train_scores, key=lambda item: (item[0], -item[1]["fast"], -item[1]["slow"])
            )[1]
            combined_run = run_symbol(symbol, rows[:validation_end], parameters=selected)
            validation_metrics = _metric_on_window(combined_run, train_end, validation_end)
            validation_scores[(selected["fast"], selected["slow"])].append(
                float(validation_metrics.get("total_return_percent") or 0.0)
            )
            partitions.append(
                {
                    "partition": f"walk_forward_{index + 1}",
                    "training_start": rows[0].timestamp.date().isoformat(),
                    "training_end": rows[train_end - 1].timestamp.date().isoformat(),
                    "validation_start": rows[train_end].timestamp.date().isoformat(),
                    "validation_end": rows[validation_end - 1].timestamp.date().isoformat(),
                    "selected_parameters": selected,
                    "out_of_sample_metrics": validation_metrics,
                    "number_of_trades": validation_metrics["number_of_trades"],
                    "data_quality_coverage": "tier_1_plus_tier_2_adjusted",
                }
            )
            prior_end = validation_end
        ranked = sorted(
            validation_scores.items(),
            key=lambda item: (mean(item[1]) if item[1] else -999.0, -item[0][0], -item[0][1]),
            reverse=True,
        )
        holdout_parameters = {"fast": ranked[0][0][0], "slow": ranked[0][0][1]}
        final_run = run_symbol(symbol, rows, parameters=holdout_parameters)
        final_metrics = _metric_on_window(final_run, holdout_start, len(rows))
        output["symbols"][symbol] = {
            "partitions": partitions,
            "final_holdout": {
                "selection_source": "pre-holdout validation partitions only",
                "selected_parameters": holdout_parameters,
                "start": rows[holdout_start].timestamp.date().isoformat(),
                "end": rows[-1].timestamp.date().isoformat(),
                "metrics": final_metrics,
                "number_of_trades": final_metrics["number_of_trades"],
            },
            "holdout_start_index": holdout_start,
            "pre_holdout_end_index": prior_end,
        }
    returns = [
        float(value["final_holdout"]["metrics"].get("total_return_percent") or 0.0)
        for value in output["symbols"].values()
    ]
    output["combined_holdout_return_percent"] = mean(returns)
    output["all_symbol_holdouts_positive"] = all(value > 0 for value in returns)
    return output


def parameter_sensitivity(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    experiments: list[dict[str, Any]] = []
    for parameters in PARAMETER_GRID:
        results = {
            symbol: run_symbol(symbol, rows, parameters=parameters) for symbol, rows in bars.items()
        }
        experiments.append(
            {
                "parameters": parameters,
                "symbols": {
                    symbol: {
                        "total_return_percent": result.metrics["total_return_percent"],
                        "maximum_drawdown_percent": result.metrics["maximum_drawdown_percent"],
                        "number_of_trades": len(result.trades),
                        "fees_bdt": result.metrics["fee_impact_bdt"],
                    }
                    for symbol, result in results.items()
                },
                "combined": combine_results(results)["metrics"],
            }
        )
    registered = next(item for item in experiments if item["parameters"] == REGISTERED_PARAMETERS)
    combined_returns = [float(item["combined"]["total_return_percent"]) for item in experiments]
    registered_return = float(registered["combined"]["total_return_percent"])
    return {
        "grid": list(PARAMETER_GRID),
        "experiment_count": len(experiments),
        "experiments": experiments,
        "registered_combined_return_percent": registered_return,
        "grid_median_combined_return_percent": median(combined_returns),
        "registered_percentile": sum(value <= registered_return for value in combined_returns)
        / len(combined_returns),
        "registered_unusually_lucky": registered_return == max(combined_returns),
        "nearby_stability": {
            "minimum_combined_return_percent": min(combined_returns),
            "maximum_combined_return_percent": max(combined_returns),
            "positive_share": sum(value > 0 for value in combined_returns) / len(combined_returns),
        },
    }


def cost_sensitivity(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    fee_rows: list[dict[str, Any]] = []
    for label, fee in FEE_SCENARIOS.items():
        results = {
            symbol: run_symbol(symbol, rows, fee_percent=fee) for symbol, rows in bars.items()
        }
        fee_rows.append(
            {
                "scenario": label,
                "fee_percent": str(fee),
                "combined": combine_results(results)["metrics"],
            }
        )
    slippage_rows: list[dict[str, Any]] = []
    for label, slippage in SLIPPAGE_SCENARIOS.items():
        results = {
            symbol: run_symbol(symbol, rows, slippage_percent=slippage)
            for symbol, rows in bars.items()
        }
        slippage_rows.append(
            {
                "scenario": label,
                "slippage_percent": str(slippage),
                "combined": combine_results(results)["metrics"],
            }
        )
    zero_cost = combine_results(
        {
            symbol: run_symbol(
                symbol, rows, fee_percent=Decimal("0"), slippage_percent=Decimal("0")
            )
            for symbol, rows in bars.items()
        }
    )["metrics"]
    return {
        "fees_are_authoritative": False,
        "fee_scenarios": fee_rows,
        "slippage_scenarios": slippage_rows,
        "gross_zero_cost": zero_cost,
        "break_even_cost": (
            "not_defensible_when_zero_cost_combined_return_is_non_positive"
            if float(zero_cost["total_return_percent"]) <= 0
            else "not estimated beyond the bounded declared scenarios"
        ),
    }


def tier_sensitivity(bars: dict[str, list[HistoricalBar]], raw_path: Path) -> dict[str, Any]:
    tier_one_dates: dict[str, set[str]] = {symbol: set() for symbol in ALLOWED_SYMBOLS}
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if (
            row["adjustment_status"] == "adjusted"
            and row["quality_tier"] == "tier_1_cross_source_confirmed"
        ):
            tier_one_dates[str(row["symbol"])].add(str(row["date"]))
    tier_one_bars = {
        symbol: [bar for bar in rows if bar.timestamp.date().isoformat() in tier_one_dates[symbol]]
        for symbol, rows in bars.items()
    }
    outcomes: dict[str, Any] = {}
    tier_one_results: dict[str, BacktestResult] = {}
    for symbol, rows in tier_one_bars.items():
        if len(rows) < REGISTERED_PARAMETERS["slow"] + 2:
            outcomes[symbol] = {
                "status": "insufficient_tier_1_sample",
                "observations": len(rows),
            }
        else:
            result = run_symbol(symbol, rows)
            tier_one_results[symbol] = result
            outcomes[symbol] = {
                "status": "descriptive_only",
                "observations": len(rows),
                "metrics": dict(result.metrics),
            }
    return {
        "tier_1_only": outcomes,
        "tier_1_equal_weight": (
            combine_results(tier_one_results)["metrics"] if len(tier_one_results) == 3 else None
        ),
        "tier_1_plus_tier_2": "baseline analysis",
        "equivalence_claim": False,
        "warning": "Tier 2 is single-source high-quality research data, not cross-source confirmation.",
    }


def corporate_action_analysis(
    bars: dict[str, list[HistoricalBar]],
    excluded_dates: dict[str, list[str]],
    baseline_results: dict[str, BacktestResult],
) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for symbol, rows in bars.items():
        excluded = sorted(set(excluded_dates.get(symbol, [])))
        trade_intervals = []
        open_trade: Trade | None = None
        for trade in baseline_results[symbol].trades:
            if trade.side == "buy":
                open_trade = trade
            elif open_trade is not None:
                start, end = open_trade.timestamp[:10], trade.timestamp[:10]
                spanned = [day for day in excluded if start < day < end]
                if spanned:
                    trade_intervals.append(
                        {"entry": start, "exit": end, "excluded_dates_spanned": spanned}
                    )
                open_trade = None
        boundaries = sorted(date.fromisoformat(value) for value in excluded)
        segments: list[list[HistoricalBar]] = [[]]
        boundary_index = 0
        for bar in rows:
            while (
                boundary_index < len(boundaries)
                and bar.timestamp.date() > boundaries[boundary_index]
            ):
                if segments[-1]:
                    segments.append([])
                boundary_index += 1
            segments[-1].append(bar)
        segment_results = [
            run_symbol(symbol, segment)
            for segment in segments
            if len(segment) >= REGISTERED_PARAMETERS["slow"] + 2
        ]
        paused_return = (
            (
                math.prod(
                    1 + float(cast(float | int, item.metrics["total_return_percent"])) / 100
                    for item in segment_results
                )
                - 1
            )
            * 100
            if segment_results
            else 0.0
        )
        symbols[symbol] = {
            "excluded_dates": excluded,
            "removed_adjusted_rows": len(excluded),
            "trades_spanning_excluded_intervals": trade_intervals,
            "pause_sensitivity": {
                "method": "split the active series at excluded dates and close exposure between segments; no reconstruction",
                "segments_tested": len(segment_results),
                "total_return_percent": paused_return,
                "maximum_segment_drawdown_percent": min(
                    (
                        float(cast(float | int, item.metrics["maximum_drawdown_percent"]))
                        for item in segment_results
                    ),
                    default=0.0,
                ),
            },
        }
    return {
        "symbols": symbols,
        "unresolved_corporate_actions_may_bias_results": True,
        "reconstruction_performed": False,
        "limitation": (
            "Adjusted research views may embed provider transformations whose point-in-time "
            "availability is unverified; excluded corporate-action candidates can create gaps."
        ),
    }


def robustness_analysis(
    bars: dict[str, list[HistoricalBar]], baseline: dict[str, BacktestResult]
) -> dict[str, Any]:
    regimes: dict[str, Any] = {}
    for symbol, rows in bars.items():
        closes = [float(bar.close) for bar in rows]
        daily_returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
        vol = median(abs(value) for value in daily_returns) if daily_returns else 0.0
        high = sum(abs(value) > vol for value in daily_returns)
        low = len(daily_returns) - high
        trend_windows = []
        for index in range(63, len(closes), 63):
            change = closes[index] / closes[index - 63] - 1
            trend_windows.append(change)
        regimes[symbol] = {
            "high_volatility_observations": high,
            "low_volatility_observations": low,
            "trending_63_bar_windows": sum(abs(value) >= 0.10 for value in trend_windows),
            "sideways_63_bar_windows": sum(abs(value) < 0.10 for value in trend_windows),
            "net_total_return_percent": baseline[symbol].metrics["total_return_percent"],
        }
    symbol_returns = {
        symbol: float(cast(float | int, result.metrics["total_return_percent"]))
        for symbol, result in baseline.items()
    }
    spread = max(symbol_returns.values()) - min(symbol_returns.values())
    return {
        "regimes": regimes,
        "symbol_returns": symbol_returns,
        "symbol_return_spread_percentage_points": spread,
        "symbol_dependence_warning": spread > 20
        or sum(value > 0 for value in symbol_returns.values()) < 2,
        "concentration_risk": "three-symbol universe only",
        "missing_data_sensitivity": "corporate-action and source-present-gap analysis reported separately",
    }


def research_verdict(analysis: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if not analysis["pre_run_validation"]["mandatory_passed"]:
        reasons.append("mandatory data validation failed")
    if analysis["timing_semantics"]["same_bar_execution"] is not False:
        reasons.append("look-ahead-safe timing was not established")
    combined_return = float(analysis["baseline"]["combined"]["metrics"]["total_return_percent"])
    if combined_return <= 0:
        reasons.append("combined net performance fails conservative costs")
    if analysis["robustness"]["symbol_dependence_warning"]:
        reasons.append("results show material symbol dependence")
    if not analysis["walk_forward"]["all_symbol_holdouts_positive"]:
        reasons.append("walk-forward holdout performance is inconsistent")
    if analysis["sensitivity"]["nearby_stability"]["positive_share"] < 0.75:
        reasons.append("nearby parameter results are unstable")
    completed = sum(
        int(value["completed_trades"]) for value in analysis["baseline"]["summaries"].values()
    )
    if completed < 90:
        reasons.append("effective independent trade sample is insufficient")
    if analysis["corporate_actions"]["unresolved_corporate_actions_may_bias_results"]:
        reasons.append("unresolved corporate-action uncertainty remains material")
    verdict = "insufficient_evidence" if reasons else "ready_for_independent_review"
    return {
        "verdict": verdict,
        "fail_closed_reasons": reasons,
        "promotion_authorized": False,
        "campaign_authorized": False,
        "profit_guarantee": False,
        "real_money_authorization": False,
        "qualification": "0/60",
    }

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from statistics import mean, pstdev
from typing import cast

from app.schemas.market import HistoricalBar
from app.schemas.trading import BacktestRequest


@dataclass(frozen=True)
class Trade:
    timestamp: str
    side: str
    quantity: int
    price: float
    fee: float
    slippage: float


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    strategy: str
    strategy_version: str
    assumptions: dict[str, object]
    metrics: dict[str, float | int | None]
    trades: list[Trade]
    equity_curve: list[dict[str, object]]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def to_html(self) -> str:
        rows = "".join(
            f"<tr><td>{key}</td><td>{value}</td></tr>" for key, value in self.metrics.items()
        )
        return f"<!doctype html><html><head><meta charset='utf-8'><title>Backtest</title></head><body><h1>{self.symbol} — {self.strategy}</h1><p>Research only; not investment advice.</p><table>{rows}</table></body></html>"


def _signals(
    bars: list[HistoricalBar], request: BacktestRequest, benchmark: list[HistoricalBar] | None
) -> list[bool]:
    closes = [float(item.close) for item in bars]
    volumes = [item.volume or 0 for item in bars]
    result = [False] * len(bars)
    if request.strategy == "buy_hold":
        return [True] * len(bars)
    if request.strategy == "ma_crossover":
        fast = int(request.parameters.get("fast", 20))
        slow = int(request.parameters.get("slow", 50))
        for idx in range(slow - 1, len(bars)):
            result[idx] = mean(closes[idx - fast + 1 : idx + 1]) > mean(
                closes[idx - slow + 1 : idx + 1]
            )
    elif request.strategy == "momentum_dsex":
        lookback = int(request.parameters.get("lookback", 60))
        bench = [float(item.close) for item in benchmark or []]
        for idx in range(lookback, len(bars)):
            bench_up = idx < len(bench) and bench[idx] > bench[idx - lookback]
            result[idx] = closes[idx] > closes[idx - lookback] and bench_up
    elif request.strategy == "volume_breakout":
        lookback = int(request.parameters.get("lookback", 20))
        multiplier = float(request.parameters.get("volume_multiplier", 1.5))
        for idx in range(lookback, len(bars)):
            result[idx] = (
                closes[idx] > max(closes[idx - lookback : idx])
                and volumes[idx] > mean(volumes[idx - lookback : idx]) * multiplier
            )
    return result


def run_backtest(
    bars: list[HistoricalBar],
    request: BacktestRequest,
    benchmark: list[HistoricalBar] | None = None,
) -> BacktestResult:
    if len(bars) < 2:
        raise ValueError("At least two bars are required")
    bars = sorted(bars, key=lambda item: item.timestamp)
    desired = _signals(bars, request, benchmark)
    cash = float(request.starting_capital)
    quantity = 0
    trades: list[Trade] = []
    curve: list[dict[str, object]] = []
    fee_rate = float(request.fee_percent) / 100
    slip_rate = float(request.slippage_percent) / 100
    # Execute yesterday's signal at today's open: this explicitly prevents same-bar look-ahead.
    for idx, bar in enumerate(bars):
        target_in_market = desired[idx - 1] if idx > 0 else False
        open_price = float(bar.open)
        if target_in_market and quantity == 0:
            fill = open_price * (1 + slip_rate)
            affordable = int(cash / (fill * (1 + fee_rate)))
            affordable -= affordable % request.minimum_quantity
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
        elif not target_in_market and quantity > 0:
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
    values = [float(cast(float, item["equity"])) for item in curve]
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]
    total_return = values[-1] / float(request.starting_capital) - 1
    years = max(len(values) / 252, 1 / 252)
    annualized = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1
    volatility = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    downside = [value for value in returns if value < 0]
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    sells = [trade for trade in trades if trade.side == "sell"]
    fees = sum(trade.fee for trade in trades)
    slippage = sum(trade.slippage * trade.quantity for trade in trades)
    benchmark_return = None
    if benchmark and len(benchmark) > 1:
        benchmark_return = float(benchmark[-1].close / benchmark[0].close - 1)
    metrics: dict[str, float | int | None] = {
        "total_return_percent": total_return * 100,
        "annualized_return_percent": annualized * 100,
        "volatility_percent": volatility * 100,
        "sharpe_ratio": annualized / volatility if volatility else None,
        "sortino_ratio": annualized / (pstdev(downside) * math.sqrt(252))
        if len(downside) > 1 and pstdev(downside)
        else None,
        "calmar_ratio": annualized / abs(max_drawdown) if max_drawdown else None,
        "maximum_drawdown_percent": max_drawdown * 100,
        "number_of_trades": len(trades),
        "completed_exits": len(sells),
        "exposure_percent": sum(desired) / len(desired) * 100,
        "fee_impact_bdt": fees,
        "slippage_impact_bdt": slippage,
        "benchmark_return_percent": benchmark_return * 100
        if benchmark_return is not None
        else None,
        "final_equity": values[-1],
    }
    return BacktestResult(
        symbol=request.symbol.upper(),
        strategy=request.strategy,
        strategy_version="1.0.0",
        assumptions={
            "next_bar_execution": True,
            "short_selling": False,
            "leverage": False,
            "infinite_liquidity": False,
            "corporate_actions": "input data only",
            "fee_percent": str(request.fee_percent),
            "slippage_percent": str(request.slippage_percent),
        },
        metrics=metrics,
        trades=trades,
        equity_curve=curve,
    )


def walk_forward_splits(
    length: int, train: int, validation: int, test: int
) -> list[tuple[range, range, range]]:
    if min(length, train, validation, test) <= 0:
        raise ValueError("Split sizes must be positive")
    splits: list[tuple[range, range, range]] = []
    start = 0
    while start + train + validation + test <= length:
        splits.append(
            (
                range(start, start + train),
                range(start + train, start + train + validation),
                range(start + train + validation, start + train + validation + test),
            )
        )
        start += test
    return splits

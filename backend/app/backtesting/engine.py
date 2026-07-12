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
class WalkForwardRun:
    partition: str  # train, validation, test
    start_date: str
    end_date: str
    metrics: dict[str, float | int | None]


@dataclass(frozen=True)
class ParameterSensitivity:
    parameters: dict[str, float | int]
    total_return_percent: float
    sharpe_ratio: float | None
    max_drawdown_percent: float


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    strategy: str
    strategy_version: str
    assumptions: dict[str, object]
    metrics: dict[str, float | int | None]
    trades: list[Trade]
    equity_curve: list[dict[str, object]]
    walk_forward: list[WalkForwardRun]
    sensitivity: list[ParameterSensitivity]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def to_html(self) -> str:
        rows = "".join(
            f"<tr><td style='padding: 8px; border-bottom: 1px solid #1e293b; color: #94a3b8;'>{key}</td>"
            f"<td style='padding: 8px; border-bottom: 1px solid #1e293b; font-weight: bold; text-align: right;'>{value}</td></tr>"
            for key, value in self.metrics.items()
        )
        return f"""<!doctype html>
<html>
<head>
    <meta charset='utf-8'>
    <title>Backtest Report - {self.symbol}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #090f18; color: #f8fafc; padding: 40px; }}
        .card {{ background-color: #0d1527; border: 1px solid #1e293b; border-radius: 12px; padding: 24px; max-width: 800px; margin: 0 auto; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
        h1 {{ font-size: 24px; color: #38d9c5; margin-bottom: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th {{ text-align: left; padding: 8px; border-bottom: 2px solid #1e293b; }}
    </style>
</head>
<body>
    <div class='card'>
        <h1>Backtest: {self.symbol} — {self.strategy}</h1>
        <p style='color: #64748b; font-size: 14px;'>Research Validation Report. Past performance is not indicative of future results.</p>
        <table>
            <thead>
                <tr>
                    <th style='color: #64748b;'>Metric</th>
                    <th style='text-align: right; color: #64748b;'>Value</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
    </div>
</body>
</html>"""


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
    is_subrun: bool = False,
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

    # 1. Backtest loop
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
                    bar.timestamp.isoformat(),
                    "sell",
                    quantity,
                    fill,
                    fee,
                    open_price * slip_rate,
                )
            )
            quantity = 0
        curve.append(
            {
                "timestamp": bar.timestamp.isoformat(),
                "equity": cash + quantity * float(bar.close),
            }
        )

    # 2. Base metrics
    values = [float(cast(float, item["equity"])) for item in curve]
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]
    total_return = values[-1] / float(request.starting_capital) - 1
    years = max(len(values) / 252, 1 / 252)
    annualized = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    volatility = pstdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    downside = [value for value in returns if value < 0]
    peak = values[0]
    max_drawdown = 0.0

    # Drawdown Duration and Max Drawdown calculation
    drawdown_duration = 0
    current_dd_duration = 0
    for value in values:
        if value >= peak:
            peak = value
            current_dd_duration = 0
        else:
            current_dd_duration += 1
            drawdown_duration = max(drawdown_duration, current_dd_duration)
        max_drawdown = min(max_drawdown, value / peak - 1)

    sells = [trade for trade in trades if trade.side == "sell"]
    fees = sum(trade.fee for trade in trades)
    slippage = sum(trade.slippage * trade.quantity for trade in trades)
    benchmark_return = None
    if benchmark and len(benchmark) > 1:
        benchmark_return = float(benchmark[-1].close / benchmark[0].close - 1)

    # 3. Extended metrics (expectancy, profit factor, turnover, alpha)
    trade_pnls: list[float] = []
    buy_stack: list[Trade] = []
    for t in trades:
        if t.side == "buy":
            buy_stack.append(t)
        elif t.side == "sell" and buy_stack:
            buy = buy_stack.pop(0)
            pnl = (t.price * t.quantity - t.fee) - (buy.price * buy.quantity + buy.fee)
            trade_pnls.append(pnl)

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    gross_profit = sum(wins)
    gross_loss = sum(map(abs, losses))
    profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 1.0)
    )

    total_traded_value = sum(t.price * t.quantity for t in trades)
    turnover = total_traded_value / float(request.starting_capital)
    alpha = (annualized - benchmark_return) if benchmark_return is not None else 0.0

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
        "drawdown_duration_bars": drawdown_duration,
        "expectancy_bdt": expectancy,
        "profit_factor": profit_factor,
        "turnover_rate": turnover,
        "benchmark_alpha_percent": alpha * 100,
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

    # 4. Walk-forward run simulation
    walk_forward: list[WalkForwardRun] = []
    if not is_subrun and len(bars) >= 100:
        splits = walk_forward_splits(len(bars), 50, 25, 25)
        for i, (_train_idx, _val_idx, test_idx) in enumerate(splits[:3]):
            part_name = f"split_{i + 1}_test"
            test_bars = [bars[idx] for idx in test_idx]
            test_bench = (
                [benchmark[idx] for idx in test_idx]
                if benchmark and len(benchmark) > max(test_idx)
                else None
            )
            try:
                sub_res = run_backtest(test_bars, request, test_bench, is_subrun=True)
                walk_forward.append(
                    WalkForwardRun(
                        partition=part_name,
                        start_date=test_bars[0].timestamp.date().isoformat(),
                        end_date=test_bars[-1].timestamp.date().isoformat(),
                        metrics=sub_res.metrics,
                    )
                )
            except Exception:
                pass

    # 5. Parameter sensitivity matrix
    sensitivity: list[ParameterSensitivity] = []
    if not is_subrun and request.strategy == "ma_crossover":
        param_sets: list[dict[str, float | int]] = [
            {"fast": 10, "slow": 30},
            {"fast": 20, "slow": 50},
            {"fast": 30, "slow": 100},
        ]
        for p in param_sets:
            try:
                sub_req = request.model_copy(update={"parameters": p})
                sub_res = run_backtest(bars, sub_req, benchmark, is_subrun=True)
                sensitivity.append(
                    ParameterSensitivity(
                        parameters=p,
                        total_return_percent=cast(float, sub_res.metrics["total_return_percent"]),
                        sharpe_ratio=cast(float | None, sub_res.metrics["sharpe_ratio"]),
                        max_drawdown_percent=cast(
                            float, sub_res.metrics["maximum_drawdown_percent"]
                        ),
                    )
                )
            except Exception:
                pass

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
        walk_forward=walk_forward,
        sensitivity=sensitivity,
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

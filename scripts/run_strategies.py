import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

# Ensure backend directory is in path so we can import app modules
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
)

from app.backtesting.engine import run_backtest
from app.schemas.market import HistoricalBar
from app.schemas.trading import BacktestRequest


def generate_synthetic_data(
    days: int = 500,
) -> tuple[list[HistoricalBar], list[HistoricalBar]]:
    gp_bars = []
    dsex_bars = []

    start_date = datetime(2023, 1, 1, tzinfo=UTC)
    gp_price = 200.0
    dsex_price = 6000.0

    for i in range(days):
        ts = start_date + timedelta(days=i)

        # Create deterministic price moves:
        # A cycle of 40 days:
        # - Day 0 to 20: slight uptrend (+0.5% per day)
        # - Day 20 to 25: sharp breakout (+3% per day) with high volume
        # - Day 25 to 40: consolidation (-1% per day)
        cycle_idx = i % 40
        if cycle_idx < 20:
            gp_price *= 1.005
            volume = 10000
        elif cycle_idx < 25:
            gp_price *= 1.03
            volume = 50000  # volume breakout
        else:
            gp_price *= 0.99
            volume = 8000

        # Benchmark DSEX has a steady 0.1% uptrend
        dsex_price *= 1.001

        gp_bars.append(
            HistoricalBar(
                symbol="GP",
                timestamp=ts,
                open=Decimal(str(gp_price * 0.995)),
                high=Decimal(str(gp_price * 1.01)),
                low=Decimal(str(gp_price * 0.99)),
                close=Decimal(str(gp_price)),
                volume=volume,
                source="synthetic",
            )
        )

        dsex_bars.append(
            HistoricalBar(
                symbol="DSEX",
                timestamp=ts,
                open=Decimal(str(dsex_price * 0.998)),
                high=Decimal(str(dsex_price * 1.002)),
                low=Decimal(str(dsex_price * 0.997)),
                close=Decimal(str(dsex_price)),
                volume=0,
                source="synthetic",
            )
        )

    return gp_bars, dsex_bars


def main() -> None:
    print("Generating synthetic market bars...")
    gp_bars, dsex_bars = generate_synthetic_data()

    strategies: list[
        tuple[
            Literal["buy_hold", "ma_crossover", "momentum_dsex", "volume_breakout"],
            dict[str, float | int],
        ]
    ] = [
        ("buy_hold", {}),
        ("ma_crossover", {"fast": 20, "slow": 50}),
        ("momentum_dsex", {"lookback": 60}),
        ("volume_breakout", {"lookback": 20, "volume_multiplier": 1.5}),
    ]

    os.makedirs("reports", exist_ok=True)

    summary_rows = []

    for strategy_name, params in strategies:
        print(f"Running strategy: {strategy_name}...")
        req = BacktestRequest(
            symbol="GP",
            strategy=strategy_name,
            starting_capital=Decimal("1000000"),
            fee_percent=Decimal("0.4"),
            slippage_percent=Decimal("0.1"),
            minimum_quantity=1,
            parameters=params,
        )

        res = run_backtest(gp_bars, req, benchmark=dsex_bars)

        # Save JSON
        json_path = f"reports/backtest_{strategy_name}.json"
        with open(json_path, "w") as f:
            f.write(res.to_json())

        # Save HTML
        html_path = f"reports/backtest_{strategy_name}.html"
        with open(html_path, "w") as f:
            f.write(res.to_html())

        m = res.metrics
        summary_rows.append(
            f"| {strategy_name:<16} "
            f"| {m['total_return_percent']:>10.2f}% "
            f"| {m.get('sharpe_ratio') or 0.0:>10.2f} "
            f"| {m.get('sortino_ratio') or 0.0:>10.2f} "
            f"| {m.get('calmar_ratio') or 0.0:>10.2f} "
            f"| {m['maximum_drawdown_percent']:>10.2f}% "
            f"| {m['number_of_trades']:>10} |"
        )
        print(f"-> Saved reports to reports/backtest_{strategy_name}.[json/html]")

    print("\nBacktest Executions Complete!")
    print("\nSummary Metrics Table:")
    print(
        "| Strategy         | Total Ret  | Sharpe     | Sortino    | Calmar     | Max DD     | Trades     |"
    )
    print(
        "|------------------|------------|------------|------------|------------|------------|------------|"
    )
    for row in summary_rows:
        print(row)


if __name__ == "__main__":
    main()

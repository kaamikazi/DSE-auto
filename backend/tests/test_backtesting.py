from datetime import date
from decimal import Decimal

from app.backtesting import run_backtest, walk_forward_splits
from app.data.providers.mock import MockProvider
from app.schemas.trading import BacktestRequest


def test_backtest_deterministic_and_delayed_execution() -> None:
    bars = MockProvider().get_history("GP", date(2024, 1, 1), date(2025, 12, 31))
    request = BacktestRequest(
        symbol="GP", strategy="ma_crossover", parameters={"fast": 20, "slow": 50}
    )
    one = run_backtest(bars, request)
    two = run_backtest(bars, request)
    assert one == two
    assert one.assumptions["next_bar_execution"] is True


def test_fees_and_slippage_reduce_buy_hold_return() -> None:
    bars = MockProvider().get_history("GP", date(2024, 1, 1), date(2025, 12, 31))
    free = run_backtest(
        bars, BacktestRequest(symbol="GP", strategy="buy_hold", fee_percent=0, slippage_percent=0)
    )
    costly = run_backtest(
        bars,
        BacktestRequest(
            symbol="GP",
            strategy="buy_hold",
            fee_percent=Decimal("1"),
            slippage_percent=Decimal("1"),
        ),
    )
    assert costly.metrics["final_equity"] < free.metrics["final_equity"]  # type: ignore[operator]


def test_walk_forward_split_integrity() -> None:
    splits = walk_forward_splits(300, 120, 60, 60)
    assert splits
    for train, validation, test in splits:
        assert max(train) < min(validation) < max(validation) < min(test)
        assert not set(train) & set(test)

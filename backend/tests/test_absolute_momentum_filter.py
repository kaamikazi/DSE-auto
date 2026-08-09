from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.absolute_momentum_filter import (
    AbsoluteMomentumConfig,
    absolute_momentum_scores,
    build_rebalance_plans,
    run_absolute_momentum,
)
from app.services.cross_sectional_momentum import canonical_hash


def _bar(symbol: str, day: datetime, price: float) -> HistoricalBar:
    return HistoricalBar(
        timestamp=day,
        symbol=symbol,
        open=Decimal(str(price * 0.999)),
        high=Decimal(str(price * 1.01)),
        low=Decimal(str(price * 0.99)),
        close=Decimal(str(price)),
        volume=1000,
        source="test_adjusted",
        timestamp_provenance=TimestampProvenance.UNKNOWN,
    )


def _bars(
    growth: dict[str, float] | None = None, sessions: int = 900
) -> dict[str, list[HistoricalBar]]:
    rates = growth or {
        "AAA": 0.0010,
        "BBB": 0.0008,
        "CCC": 0.0006,
        "DDD": -0.0002,
        "EEE": -0.0004,
        "FFF": -0.0006,
    }
    start = datetime(2018, 1, 1, tzinfo=UTC)
    output: dict[str, list[HistoricalBar]] = {}
    for symbol, rate in rates.items():
        price = 100.0
        values: list[HistoricalBar] = []
        for index in range(sessions):
            price *= 1 + rate
            values.append(_bar(symbol, start + timedelta(days=index), price))
        output[symbol] = values
    return output


def test_signal_is_independent_absolute_momentum_not_cross_sectional() -> None:
    bars = _bars()
    signal_date = bars["AAA"][600].timestamp.date()
    scores, exclusions = absolute_momentum_scores(
        bars, signal_date, lookback_months=12, skip_recent_months=1
    )
    assert not exclusions
    assert all(scores[symbol] > 0 for symbol in ("AAA", "BBB", "CCC"))
    assert all(scores[symbol] < 0 for symbol in ("DDD", "EEE", "FFF"))
    plans = build_rebalance_plans(bars, AbsoluteMomentumConfig("test", 12))
    assert plans
    assert plans[-1]["selected"] == ["AAA", "BBB", "CCC"]
    assert plans[-1]["target_weights"] == {"AAA": 0.2, "BBB": 0.2, "CCC": 0.2}


def test_no_look_ahead_and_next_open_execution() -> None:
    bars = _bars()
    plans = build_rebalance_plans(bars, AbsoluteMomentumConfig("test", 12))
    assert all(plan["execution_date"] > plan["signal_date"] for plan in plans)
    signal_date = plans[0]["signal_date"]
    before, _ = absolute_momentum_scores(
        bars, signal_date, lookback_months=12, skip_recent_months=1
    )
    changed = _bars()
    future_index = next(
        index for index, bar in enumerate(changed["FFF"]) if bar.timestamp.date() > signal_date
    )
    future = changed["FFF"][future_index]
    changed["FFF"][future_index] = _bar("FFF", future.timestamp, 1_000_000)
    after, _ = absolute_momentum_scores(
        changed, signal_date, lookback_months=12, skip_recent_months=1
    )
    assert before == after


def test_all_cash_and_equal_weight_cap_behavior() -> None:
    declining = _bars({symbol: -0.0005 for symbol in ("AAA", "BBB", "CCC", "DDD")})
    plans = build_rebalance_plans(declining, AbsoluteMomentumConfig("cash", 12))
    assert plans and all(not plan["selected"] for plan in plans)
    run = run_absolute_momentum(declining, AbsoluteMomentumConfig("cash", 12))
    assert run.metrics["all_cash_period_count"] == 1
    assert run.metrics["average_invested_exposure_percent"] == 0
    assert run.metrics["final_equity"] == 1_000_000

    rising = _bars({symbol: 0.0005 for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")})
    plan = build_rebalance_plans(rising, AbsoluteMomentumConfig("equal", 12))[-1]
    assert set(plan["target_weights"].values()) == {1 / 6}


def test_missing_data_fails_closed_and_run_reproduces_with_costs() -> None:
    bars = _bars()
    signal_date = bars["AAA"][600].timestamp.date()
    bars["FFF"] = [bar for bar in bars["FFF"] if bar.timestamp.date() != signal_date]
    scores, exclusions = absolute_momentum_scores(
        bars, signal_date, lookback_months=12, skip_recent_months=1
    )
    assert "FFF" not in scores
    assert exclusions == {"FFF": "signal_session_missing"}

    complete = _bars()
    config = AbsoluteMomentumConfig("test", 12)
    first = run_absolute_momentum(complete, config)
    second = run_absolute_momentum(complete, config)
    gross = run_absolute_momentum(complete, config, fee_percent=0.0, slippage_percent=0.0)
    assert canonical_hash(first.metrics) == canonical_hash(second.metrics)
    assert canonical_hash(first.ledger) == canonical_hash(second.ledger)
    assert first.metrics["total_fees_bdt"] > 0
    assert first.metrics["total_slippage_bdt"] > 0
    assert first.metrics["final_equity"] < gross.metrics["final_equity"]
    assert first.metrics["minimum_cash_bdt"] >= -1e-7

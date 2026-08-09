from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import pstdev

from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.cross_sectional_momentum import canonical_hash
from app.services.defensive_low_volatility import (
    MAX_TARGET_WEIGHT,
    LowVolatilityConfig,
    build_rebalance_plans,
    realized_volatility_scores,
    run_low_volatility,
)


def _bar(symbol: str, day: datetime, close: float) -> HistoricalBar:
    return HistoricalBar(
        timestamp=day,
        symbol=symbol,
        open=Decimal(str(close * 0.999)),
        high=Decimal(str(close * 1.01)),
        low=Decimal(str(close * 0.99)),
        close=Decimal(str(close)),
        volume=1000,
        source="test_adjusted",
        timestamp_provenance=TimestampProvenance.UNKNOWN,
    )


def _bars(sessions: int = 550) -> dict[str, list[HistoricalBar]]:
    output: dict[str, list[HistoricalBar]] = {}
    start = datetime(2019, 1, 1, tzinfo=UTC)
    for symbol, amplitude in {"AAA": 0.001, "BBB": 0.003, "CCC": 0.006, "DDD": 0.012}.items():
        price = 100.0
        values: list[HistoricalBar] = []
        for index in range(sessions):
            movement = 0.0004 + amplitude * math.sin(index * 0.37)
            price *= 1 + movement
            values.append(_bar(symbol, start + timedelta(days=index), price))
        output[symbol] = values
    return output


def test_volatility_calculation_and_lowest_first_ranking() -> None:
    bars = _bars()
    signal_date = bars["AAA"][200].timestamp.date()
    scores, exclusions = realized_volatility_scores(bars, signal_date, lookback_sessions=126)
    assert not exclusions
    assert sorted(scores, key=lambda symbol: (scores[symbol], symbol)) == [
        "AAA",
        "BBB",
        "CCC",
        "DDD",
    ]
    assert scores["AAA"] < scores["DDD"]
    closes = [float(bar.close) for bar in bars["AAA"][74:201]]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    assert math.isclose(scores["AAA"], pstdev(returns) * math.sqrt(252), rel_tol=1e-12)


def test_no_look_ahead_and_next_open_execution() -> None:
    bars = _bars()
    plans = build_rebalance_plans(bars, LowVolatilityConfig("test", 126, 3))
    assert plans
    assert all(plan["execution_date"] > plan["signal_date"] for plan in plans)
    assert all(
        max(plan["target_weights"].values(), default=0.0) <= MAX_TARGET_WEIGHT for plan in plans
    )
    signal_date = plans[0]["signal_date"]
    before, _ = realized_volatility_scores(bars, signal_date, lookback_sessions=126)
    changed = _bars()
    future_index = next(
        index for index, bar in enumerate(changed["DDD"]) if bar.timestamp.date() > signal_date
    )
    future = changed["DDD"][future_index]
    changed["DDD"][future_index] = _bar("DDD", future.timestamp, 1_000_000)
    after, _ = realized_volatility_scores(changed, signal_date, lookback_sessions=126)
    assert before == after


def test_missing_signal_or_lookback_fails_closed_without_fill() -> None:
    bars = _bars()
    early = bars["AAA"][100].timestamp.date()
    scores, exclusions = realized_volatility_scores(bars, early, lookback_sessions=126)
    assert scores == {}
    assert set(exclusions.values()) == {"full_lookback_missing"}

    signal = bars["AAA"][200].timestamp.date()
    bars["DDD"] = [bar for bar in bars["DDD"] if bar.timestamp.date() != signal]
    scores, exclusions = realized_volatility_scores(bars, signal, lookback_sessions=126)
    assert scores == {}
    assert set(exclusions.values()) == {"signal_session_missing"}


def test_deterministic_cost_and_cash_accounting() -> None:
    bars = _bars()
    config = LowVolatilityConfig("test", 126, 3)
    first = run_low_volatility(bars, config)
    second = run_low_volatility(bars, config)
    gross = run_low_volatility(bars, config, fee_percent=0.0, slippage_percent=0.0)

    assert canonical_hash(first.metrics) == canonical_hash(second.metrics)
    assert canonical_hash(first.ledger) == canonical_hash(second.ledger)
    assert first.metrics["total_fees_bdt"] > 0
    assert first.metrics["total_slippage_bdt"] > 0
    assert first.metrics["final_equity"] < gross.metrics["final_equity"]
    assert first.metrics["minimum_cash_bdt"] >= -1e-7
    assert first.metrics["downside_volatility_percent"] >= 0
    assert first.metrics["worst_rolling_12_month_return_percent"] is not None
    assert all(int(row["quantity"]) == row["quantity"] for row in first.ledger)

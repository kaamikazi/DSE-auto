from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.cross_sectional_momentum import (
    MAX_TARGET_WEIGHT,
    MomentumConfig,
    build_rebalance_plans,
    canonical_hash,
    momentum_scores,
    run_momentum,
)


def _bar(symbol: str, year: int, month: int, price: float) -> HistoricalBar:
    day = 28 if month == 2 else 30
    return HistoricalBar(
        timestamp=datetime(year, month, day, tzinfo=UTC),
        symbol=symbol,
        open=Decimal(str(price)),
        high=Decimal(str(price * 1.01)),
        low=Decimal(str(price * 0.99)),
        close=Decimal(str(price)),
        volume=1000,
        source="test_adjusted",
        timestamp_provenance=TimestampProvenance.UNKNOWN,
    )


def _bars(months: int = 20) -> dict[str, list[HistoricalBar]]:
    output: dict[str, list[HistoricalBar]] = {}
    for symbol, monthly_growth in {"AAA": 1.03, "BBB": 1.02, "CCC": 1.01, "DDD": 1.0}.items():
        values: list[HistoricalBar] = []
        price = 100.0
        for index in range(months):
            year = 2020 + index // 12
            month = index % 12 + 1
            values.append(_bar(symbol, year, month, price))
            price *= monthly_growth
        output[symbol] = values
    return output


def test_signal_ranking_excludes_latest_month_and_is_deterministic() -> None:
    bars = _bars()
    signal_date = bars["AAA"][15].timestamp.date()
    scores, exclusions = momentum_scores(bars, signal_date, lookback_months=12)
    assert not exclusions
    assert sorted(scores, key=lambda symbol: (-scores[symbol], symbol)) == [
        "AAA",
        "BBB",
        "CCC",
        "DDD",
    ]

    changed = _bars()
    changed["DDD"][15] = _bar("DDD", 2021, 4, 1_000_000)
    changed_scores, _ = momentum_scores(changed, signal_date, lookback_months=12)
    assert changed_scores == scores


def test_signal_never_executes_on_signal_bar() -> None:
    plans = build_rebalance_plans(_bars(), MomentumConfig("test", 12, 3))
    assert plans
    assert all(plan["execution_date"] > plan["signal_date"] for plan in plans)
    assert all(
        max(plan["target_weights"].values(), default=0.0) <= MAX_TARGET_WEIGHT for plan in plans
    )


def test_missing_month_is_ineligible_without_forward_fill() -> None:
    bars = _bars()
    del bars["DDD"][5]
    signal_date = bars["AAA"][15].timestamp.date()
    scores, exclusions = momentum_scores(bars, signal_date, lookback_months=12)
    assert scores == {}
    assert set(exclusions.values()) == {"common_month_missing"}


def test_cost_cash_accounting_and_reproduction() -> None:
    bars = _bars()
    config = MomentumConfig("test", 12, 3)
    first = run_momentum(bars, config)
    second = run_momentum(bars, config)
    gross = run_momentum(bars, config, fee_percent=0.0, slippage_percent=0.0)

    assert canonical_hash(first.metrics) == canonical_hash(second.metrics)
    assert canonical_hash(first.ledger) == canonical_hash(second.ledger)
    assert first.metrics["total_fees_bdt"] > 0
    assert first.metrics["total_slippage_bdt"] > 0
    assert first.metrics["final_equity"] < gross.metrics["final_equity"]
    assert first.metrics["minimum_cash_bdt"] >= -1e-7
    assert all(int(row["quantity"]) == row["quantity"] for row in first.ledger)

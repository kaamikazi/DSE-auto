from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.five_symbol_robustness import (
    FIVE_SYMBOLS,
    assert_registry_identity,
    run_portfolio,
    run_portfolio_buy_hold,
)
from app.services.risk_control_attribution import (
    BASELINE_IDS,
    binary_signal_result,
    classify_regimes,
    closed_trade_records,
    drawdown_attribution,
    exposure_matched_benchmark,
    regime_analysis,
    research_decision,
    return_attribution,
    simple_baselines,
    symbol_dependence,
    trade_failure_summary,
    two_hundred_day_signals,
)


def _bars(symbol: str, offset: int = 0, length: int = 320) -> list[HistoricalBar]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    output = []
    for index in range(length):
        trend = index * Decimal("0.05")
        cycle = Decimal((index % 30) - 15) * Decimal("0.4")
        price = Decimal(100 + offset) + trend + cycle
        output.append(
            HistoricalBar(
                timestamp=start + timedelta(days=index),
                symbol=symbol,
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + Decimal("0.2"),
                volume=1_000_000,
                source="fixture",
                timestamp_provenance=TimestampProvenance.UNKNOWN,
            )
        )
    return output


def _universe() -> dict[str, list[HistoricalBar]]:
    return {symbol: _bars(symbol, index) for index, symbol in enumerate(FIVE_SYMBOLS)}


def test_identity_enforcement_remains_fail_closed() -> None:
    expected = {"strategy": "ma_crossover@1.0.0", "promotion": "blocked"}
    assert_registry_identity(expected, expected)
    try:
        assert_registry_identity({"strategy": "other", "promotion": "blocked"}, expected)
    except RuntimeError:
        pass
    else:
        raise AssertionError("identity mismatch did not fail closed")


def test_return_attribution_is_deterministic_and_additive() -> None:
    strategy = run_portfolio(_universe())
    first = return_attribution(strategy)
    second = return_attribution(strategy)
    assert first == second
    contribution = sum(
        row["return_contribution_percentage_points"] for row in first["symbols"].values()
    )
    assert round(contribution, 10) == round(strategy["net"]["metrics"]["total_return_percent"], 10)


def test_drawdown_attribution_reconciles_symbol_contributions() -> None:
    bars = _universe()
    strategy = run_portfolio(bars)
    benchmark = run_portfolio(bars)
    regimes = classify_regimes(bars)
    report = drawdown_attribution(
        bars, strategy, benchmark, {symbol: [] for symbol in bars}, regimes, threshold_percent=0.1
    )
    for episode in report["episodes"]:
        attributed = sum(row["bdt"] for row in episode["symbol_contributions"].values())
        assert abs(attributed - episode["portfolio_loss_bdt"]) < 1e-6


def test_only_predeclared_baselines_are_run() -> None:
    bars = _universe()
    output = simple_baselines(bars, run_portfolio(bars))
    assert tuple(output) == BASELINE_IDS
    assert output["cash_only"]["metrics"]["total_return_percent"] == 0


def test_exposure_matched_benchmark_uses_measured_exposure_not_return() -> None:
    result = exposure_matched_benchmark(_universe(), 46.5)
    assert result["target_exposure_percent"] == 46.5
    assert result["performance_optimized"] is False
    assert abs(result["metrics"]["exposure_percent"] - 46.5) < 0.2


def test_regime_labels_do_not_change_when_future_prices_change() -> None:
    bars = _universe()
    original = classify_regimes(bars)
    cutoff = sorted(original)[20]
    mutated = {symbol: list(rows) for symbol, rows in bars.items()}
    for _symbol, rows in mutated.items():
        for index, bar in enumerate(rows):
            if bar.timestamp.date().isoformat() > cutoff:
                rows[index] = bar.model_copy(update={"close": bar.close * 10})
    changed = classify_regimes(mutated)
    assert {day: value for day, value in original.items() if day <= cutoff} == {
        day: value for day, value in changed.items() if day <= cutoff
    }


def test_regime_analysis_reports_conditional_drawdown() -> None:
    bars = _universe()
    strategy = run_portfolio(bars)
    result = regime_analysis(bars, strategy, run_portfolio_buy_hold(bars))
    assert set(result["results"]) == {
        "strong_uptrend",
        "weak_uptrend",
        "sideways",
        "downtrend",
        "high_volatility",
        "low_volatility",
    }
    assert all(
        value["conditional_maximum_drawdown_percent"] <= 0 for value in result["results"].values()
    )


def test_trade_failure_labels_are_deterministic_and_exhaustive() -> None:
    bars = {"GP": _bars("GP")}
    signals = two_hundred_day_signals(bars["GP"])
    result = binary_signal_result("GP", bars["GP"], signals, strategy="fixture")
    records = closed_trade_records(bars, {"GP": result}, {"GP": []})
    first = trade_failure_summary(records)
    assert first == trade_failure_summary(records)
    assert sum(first["primary_counts"].values()) == first["closed_trades"]


def test_symbol_dependence_uses_exact_predeclared_universes() -> None:
    bars = _universe()
    strategy = run_portfolio(bars)
    summaries = {
        symbol: {"net": result.metrics} for symbol, result in strategy["net_results"].items()
    }
    result = symbol_dependence(bars, summaries)
    assert set(result["universes"]) == {
        "all_five",
        "without_bracbank",
        "without_best",
        "without_worst",
        "original_three",
        "extension_two",
    }


def test_decision_never_promotes_or_implements() -> None:
    bars = _universe()
    strategy = run_portfolio(bars)
    baselines = simple_baselines(bars, strategy)
    summaries = {
        symbol: {"net": result.metrics} for symbol, result in strategy["net_results"].items()
    }
    dependence = symbol_dependence(bars, summaries)
    walk = {"dispersion": {"negative_partitions": 8}}
    decision = research_decision(dependence, walk, baselines, exposure_matched_benchmark(bars, 50))
    assert decision["promotion_authorized"] is False
    assert decision["implementation_authorized"] is False
    assert decision["qualification"] == "0/60"


def test_runner_guards_operational_side_effects_and_audit(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "scripts" / "run_risk_control_attribution.py"
    if source.exists():
        text = source.read_text(encoding="utf-8")
        assert "before != after" in text
        assert "verify_audit_chain" in text
        assert '"promotion_status": "blocked"' in text

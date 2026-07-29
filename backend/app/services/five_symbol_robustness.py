from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, cast

from app.backtesting.engine import BacktestResult
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.historical_strategy_research import (
    BASELINE_FEE_PERCENT,
    BASELINE_SLIPPAGE_PERCENT,
    FEE_SCENARIOS,
    MAX_VOLUME_PARTICIPATION,
    PARAMETER_GRID,
    REGISTERED_PARAMETERS,
    SLIPPAGE_SCENARIOS,
    STARTING_CAPITAL,
    _closed_trade_stats,
    _curve_metrics,
    combine_results,
    count_liquidity_exclusions,
    run_symbol,
    summarize_result,
)

FIVE_SYMBOLS = ("GP", "ACI", "BRACBANK", "BATBC", "SQURPHARMA")
PARENT_SYMBOLS = ("GP", "ACI", "BRACBANK")
EXTENSION_SYMBOLS = ("BATBC", "SQURPHARMA")
SECTORS = {
    "GP": "telecommunications",
    "ACI": "pharmaceuticals_and_chemicals",
    "BRACBANK": "bank",
    "BATBC": "food_and_allied",
    "SQURPHARMA": "pharmaceuticals_and_chemicals",
}
APPROVED_EXTENSION_DISPOSITION = "tier_2_single_source_high_quality"


def assert_registry_identity(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"Pinned identity mismatch: {mismatches}")


def _row_lineage(row: dict[str, Any]) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    return (
        list(row.get("source_lineage") or []),
        list(row.get("contributing_sources") or [row.get("selected_source")]),
        list(row.get("source_row_ids") or [row.get("selected_source_row_id")]),
        list(row.get("raw_hashes") or row.get("raw_file_hashes") or []),
    )


def validate_combined_datasets(
    parent_path: Path, extension_path: Path
) -> tuple[dict[str, list[HistoricalBar]], dict[str, Any]]:
    sources = (
        ("parent", parent_path, set(PARENT_SYMBOLS)),
        ("extension", extension_path, set(EXTENSION_SYMBOLS)),
    )
    all_rows: list[tuple[str, dict[str, Any]]] = []
    source_hashes: dict[str, str] = {}
    from app.services.historical_strategy_research import sha256_file

    for origin, path, expected_symbols in sources:
        source_hashes[origin] = sha256_file(path)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if {str(row.get("symbol")) for row in rows} != expected_symbols:
            raise ValueError(f"{origin} symbol universe mismatch")
        all_rows.extend((origin, row) for row in rows)

    grain = Counter(
        (str(row.get("symbol")), str(row.get("date")), str(row.get("adjustment_status")))
        for _, row in all_rows
    )
    invalid = incomplete = t3 = held = unknown_grain = 0
    disposition_counts: Counter[str] = Counter()
    adjusted: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in FIVE_SYMBOLS}
    for origin, row in all_rows:
        disposition = str(row.get("final_disposition") or "parent_approved_active")
        disposition_counts[disposition] += 1
        t3 += int("tier_3" in disposition.lower() or disposition.lower() == "t3")
        held += int("held" in disposition.lower() or "conflict" in disposition.lower())
        grain_name = str(row.get("adjustment_status"))
        unknown_grain += int(grain_name not in {"adjusted", "unadjusted"})
        lineage, contributors, row_ids, hashes = _row_lineage(row)
        incomplete += int(
            not row.get("selected_source")
            or not lineage
            or not contributors
            or not all(row_ids)
            or not hashes
            or not (row.get("audit_linkage") or row.get("audit_event_ids"))
        )
        try:
            open_, high, low, close, volume = (
                Decimal(str(row[field])) for field in ("open", "high", "low", "close", "volume")
            )
            invalid += int(
                high < low or not low <= open_ <= high or not low <= close <= high or volume < 0
            )
        except (ArithmeticError, KeyError, ValueError):
            invalid += 1
        if origin == "extension" and disposition != APPROVED_EXTENSION_DISPOSITION:
            held += 1
        if grain_name == "adjusted":
            adjusted[str(row.get("symbol"))].append(row)

    ordered = {
        symbol: [str(row["date"]) for row in rows] == sorted(str(row["date"]) for row in rows)
        for symbol, rows in adjusted.items()
    }
    adjusted_keys = Counter(
        (symbol, str(row["date"])) for symbol, rows in adjusted.items() for row in rows
    )
    checks: dict[str, Any] = {
        "combined_symbols": list(FIVE_SYMBOLS),
        "source_file_hashes": source_hashes,
        "total_rows": len(all_rows),
        "adjusted_execution_rows": sum(len(rows) for rows in adjusted.values()),
        "full_grain_duplicates": sum(count - 1 for count in grain.values() if count > 1),
        "adjusted_symbol_date_duplicates": sum(
            count - 1 for count in adjusted_keys.values() if count > 1
        ),
        "invalid_rows": invalid,
        "incomplete_lineage_rows": incomplete,
        "t3_rows": t3,
        "held_or_conflict_rows": held,
        "unknown_adjustment_grain_rows": unknown_grain,
        "dsex_rows": sum(1 for _, row in all_rows if row.get("symbol") == "DSEX"),
        "dates_ordered": ordered,
        "disposition_counts": dict(disposition_counts),
        "approved_research_windows": {
            symbol: {
                "start": rows[0]["date"],
                "end": rows[-1]["date"],
                "official_listing_date_claim": False,
            }
            for symbol, rows in adjusted.items()
            if rows
        },
    }
    checks["mandatory_passed"] = bool(
        set(adjusted) == set(FIVE_SYMBOLS)
        and all(adjusted.values())
        and not any(
            (
                checks["full_grain_duplicates"],
                checks["adjusted_symbol_date_duplicates"],
                invalid,
                incomplete,
                t3,
                held,
                unknown_grain,
                checks["dsex_rows"],
            )
        )
        and all(ordered.values())
    )
    if not checks["mandatory_passed"]:
        raise ValueError(f"Combined dataset validation failed: {checks}")

    bars: dict[str, list[HistoricalBar]] = {}
    for symbol, rows in adjusted.items():
        bars[symbol] = [
            HistoricalBar(
                timestamp=datetime.combine(
                    date.fromisoformat(str(row["date"])), datetime.min.time(), UTC
                ),
                symbol=symbol,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(Decimal(str(row["volume"])) * MAX_VOLUME_PARTICIPATION),
                source=str(row["selected_source"]),
                timestamp_provenance=TimestampProvenance.UNKNOWN,
                quality_flags=[
                    str(row.get("quality_tier") or row.get("final_disposition")),
                    "volume_capacity_limited_to_5_percent_of_reported_volume",
                ],
            )
            for row in rows
        ]
    return bars, checks


def combine_weighted(
    results: dict[str, BacktestResult], weights: dict[str, float]
) -> dict[str, Any]:
    if set(results) != set(weights) or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ValueError("Weights must cover results and total one")
    dates = sorted(
        {
            str(point["timestamp"])[:10]
            for result in results.values()
            for point in result.equity_curve
        }
    )
    values = {
        symbol: {
            str(p["timestamp"])[:10]: float(cast(float | int, p["equity"]))
            for p in result.equity_curve
        }
        for symbol, result in results.items()
    }
    latest = {symbol: float(STARTING_CAPITAL) for symbol in results}
    curve: list[dict[str, object]] = []
    for day in dates:
        for symbol in results:
            latest[symbol] = values[symbol].get(day, latest[symbol])
        curve.append({"timestamp": day, "equity": sum(latest[s] * weights[s] for s in results)})
    metrics = _curve_metrics(curve, float(STARTING_CAPITAL))
    metrics.update(
        {
            "number_of_trades": sum(len(result.trades) for result in results.values()),
            "completed_trades": sum(
                int(_closed_trade_stats(result.trades)["completed_trades"])
                for result in results.values()
            ),
            "fee_impact_bdt": sum(
                float(result.metrics.get("fee_impact_bdt") or 0) * weights[s]
                for s, result in results.items()
            ),
            "slippage_impact_bdt": sum(
                float(result.metrics.get("slippage_impact_bdt") or 0) * weights[s]
                for s, result in results.items()
            ),
            "turnover_rate": sum(
                float(result.metrics.get("turnover_rate") or 0) * weights[s]
                for s, result in results.items()
            ),
            "exposure_percent": sum(
                float(result.metrics.get("exposure_percent") or 0) * weights[s]
                for s, result in results.items()
            ),
        }
    )
    return {"metrics": metrics, "equity_curve": curve, "weights": weights}


def sector_weights(symbols: tuple[str, ...] | list[str]) -> dict[str, float]:
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(SECTORS[symbol], []).append(symbol)
    sector_weight = 1 / len(groups)
    return {symbol: sector_weight / len(groups[SECTORS[symbol]]) for symbol in symbols}


def run_portfolio(
    bars: dict[str, list[HistoricalBar]],
    *,
    parameters: dict[str, int] | None = None,
    fee: Decimal = BASELINE_FEE_PERCENT,
    slippage: Decimal = BASELINE_SLIPPAGE_PERCENT,
) -> dict[str, Any]:
    net = {
        s: run_symbol(s, rows, parameters=parameters, fee_percent=fee, slippage_percent=slippage)
        for s, rows in bars.items()
    }
    gross = {
        s: run_symbol(
            s, rows, parameters=parameters, fee_percent=Decimal("0"), slippage_percent=Decimal("0")
        )
        for s, rows in bars.items()
    }
    return {
        "net_results": net,
        "gross_results": gross,
        "net": combine_results(net),
        "gross": combine_results(gross),
    }


def symbol_summaries(
    portfolio: dict[str, Any], bars: dict[str, list[HistoricalBar]], excluded: dict[str, int]
) -> dict[str, Any]:
    return {
        s: summarize_result(
            portfolio["net_results"][s],
            gross_result=portfolio["gross_results"][s],
            observations=len(bars[s]),
            missing_data_exclusions=excluded.get(s, 0),
            liquidity_exclusions=count_liquidity_exclusions(bars[s]),
        )
        for s in bars
    }


def deterministic_best(summaries: dict[str, Any]) -> str:
    return sorted(
        summaries, key=lambda s: (-float(summaries[s]["net"]["total_return_percent"]), s)
    )[0]


def leave_one_out(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for excluded in sorted(bars):
        subset = {s: rows for s, rows in bars.items() if s != excluded}
        strategy = run_portfolio(subset)
        benchmark = run_portfolio_buy_hold(subset)
        net = dict(strategy["net"]["metrics"])
        net["benchmark_return_percent"] = benchmark["net"]["metrics"]["total_return_percent"]
        net["relative_return_percent"] = float(net["total_return_percent"]) - float(
            net["benchmark_return_percent"]
        )
        output[excluded] = {
            "excluded_symbol": excluded,
            "remaining_universe": list(subset),
            "net": net,
            "gross": strategy["gross"]["metrics"],
        }
    return output


def run_portfolio_buy_hold(bars: dict[str, list[HistoricalBar]]) -> dict[str, Any]:
    net = {s: run_symbol(s, rows, strategy="buy_hold") for s, rows in bars.items()}
    gross = {
        s: run_symbol(
            s, rows, strategy="buy_hold", fee_percent=Decimal("0"), slippage_percent=Decimal("0")
        )
        for s, rows in bars.items()
    }
    return {
        "net_results": net,
        "gross_results": gross,
        "net": combine_results(net),
        "gross": combine_results(gross),
    }


def parameter_universe_stability(bars: dict[str, list[HistoricalBar]], best: str) -> dict[str, Any]:
    rows = []
    universes = {
        "full": bars,
        "leave_bracbank_out": {s: v for s, v in bars.items() if s != "BRACBANK"},
        "leave_best_out": {s: v for s, v in bars.items() if s != best},
    }
    for parameters in PARAMETER_GRID:
        entry: dict[str, Any] = {"parameters": parameters, "symbols": {}}
        for symbol, values in bars.items():
            result = run_symbol(symbol, values, parameters=parameters)
            entry["symbols"][symbol] = dict(result.metrics)
        runs = {
            name: run_portfolio(subset, parameters=parameters) for name, subset in universes.items()
        }
        entry["universes"] = {name: run["net"]["metrics"] for name, run in runs.items()}
        entry["universes_gross"] = {name: run["gross"]["metrics"] for name, run in runs.items()}
        rows.append(entry)
    registered = next(row for row in rows if row["parameters"] == REGISTERED_PARAMETERS)
    full_returns = [float(row["universes"]["full"]["total_return_percent"]) for row in rows]
    return {
        "grid": list(PARAMETER_GRID),
        "experiments": rows,
        "registered_rank": 1
        + sorted(full_returns, reverse=True).index(
            float(registered["universes"]["full"]["total_return_percent"])
        ),
        "registered_percentile": sum(
            v <= float(registered["universes"]["full"]["total_return_percent"])
            for v in full_returns
        )
        / len(full_returns),
        "nearby_positive_share": sum(v > 0 for v in full_returns) / len(full_returns),
        "grid_median_return_percent": median(full_returns),
    }


def cost_stress(bars: dict[str, list[HistoricalBar]], best: str) -> dict[str, Any]:
    universes = {
        "full": bars,
        "leave_bracbank_out": {s: v for s, v in bars.items() if s != "BRACBANK"},
        "leave_best_out": {s: v for s, v in bars.items() if s != best},
    }
    scenarios = []
    for fee_name, fee in FEE_SCENARIOS.items():
        for slip_name, slip in SLIPPAGE_SCENARIOS.items():
            runs = {
                name: run_portfolio(subset, fee=fee, slippage=slip)
                for name, subset in universes.items()
            }
            scenarios.append(
                {
                    "fee_scenario": fee_name,
                    "fee_percent": str(fee),
                    "slippage_scenario": slip_name,
                    "slippage_percent": str(slip),
                    "authoritative": False,
                    "universes": {name: run["net"]["metrics"] for name, run in runs.items()},
                    "gross_zero_cost_universes": {
                        name: run["gross"]["metrics"] for name, run in runs.items()
                    },
                }
            )
    return {"scenarios": scenarios, "scenario_count": len(scenarios), "costs_authoritative": False}


def concentration_summary(full_return: float, loo: dict[str, Any]) -> dict[str, Any]:
    impacts = {
        symbol: float(row["net"]["total_return_percent"]) - full_return
        for symbol, row in loo.items()
    }
    return {
        "return_change_when_excluded_percent_points": impacts,
        "largest_absolute_dependency_symbol": sorted(impacts, key=lambda s: (-abs(impacts[s]), s))[
            0
        ],
        "range_percent_points": max(impacts.values()) - min(impacts.values()),
    }

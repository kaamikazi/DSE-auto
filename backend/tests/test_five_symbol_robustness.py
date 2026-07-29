from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.five_symbol_robustness import (
    FIVE_SYMBOLS,
    assert_registry_identity,
    cost_stress,
    deterministic_best,
    leave_one_out,
    parameter_universe_stability,
    validate_combined_datasets,
)
from app.services.historical_strategy_research import PARAMETER_GRID, run_symbol


def _row(symbol: str, day: str, *, disposition: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "date": day,
        "open": "10",
        "high": "11",
        "low": "9",
        "close": "10.5",
        "volume": "10000",
        "adjustment_status": "adjusted",
        "selected_source": "fixture",
        "source_lineage": [{"source": "fixture"}],
        "source_row_ids": [f"{symbol}:{day}"],
        "raw_hashes": ["a" * 64],
        "audit_linkage": ["audit"],
        "quality_tier": "tier_2_single_source_high_quality",
    }
    if disposition:
        row.pop("raw_hashes")
        row.pop("audit_linkage")
        row["raw_file_hashes"] = ["b" * 64]
        row["audit_event_ids"] = ["audit"]
        row["final_disposition"] = disposition
    return row


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_parent_and_extension_identity_enforcement() -> None:
    expected = {"parent": "p", "extension": "e"}
    assert_registry_identity(expected, expected)
    with pytest.raises(RuntimeError, match="Pinned identity mismatch"):
        assert_registry_identity({"parent": "wrong", "extension": "e"}, expected)


def test_combined_universe_and_t3_exclusion(tmp_path: Path) -> None:
    parent, extension = tmp_path / "parent.jsonl", tmp_path / "extension.jsonl"
    _write(parent, [_row(symbol, "2024-01-01") for symbol in ("GP", "ACI", "BRACBANK")])
    rows = [
        _row(symbol, "2024-01-01", disposition="tier_2_single_source_high_quality")
        for symbol in ("BATBC", "SQURPHARMA")
    ]
    _write(extension, rows)
    bars, checks = validate_combined_datasets(parent, extension)
    assert tuple(bars) == FIVE_SYMBOLS
    assert checks["t3_rows"] == 0
    rows[0]["final_disposition"] = "tier_3_unresolved"
    _write(extension, rows)
    with pytest.raises(ValueError, match="Combined dataset validation failed"):
        validate_combined_datasets(parent, extension)


def _bars(symbol: str, offset: int = 0) -> list[HistoricalBar]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    values = []
    for index in range(100):
        price = Decimal(100 + offset + (index % 20) - (index % 7))
        values.append(
            HistoricalBar(
                timestamp=start + timedelta(days=index),
                symbol=symbol,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=100000,
                source="fixture",
                timestamp_provenance=TimestampProvenance.UNKNOWN,
            )
        )
    return values


def test_look_ahead_safe_execution_uses_next_bar() -> None:
    bars = _bars("GP")
    result = run_symbol("GP", bars)
    dates = {bar.timestamp.date().isoformat(): index for index, bar in enumerate(bars)}
    for trade in result.trades:
        assert dates[trade.timestamp[:10]] >= 50


def test_leave_out_is_complete_and_best_is_deterministic() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(FIVE_SYMBOLS)}
    outcomes = leave_one_out(bars)
    assert set(outcomes) == set(FIVE_SYMBOLS)
    assert all(len(row["remaining_universe"]) == 4 for row in outcomes.values())
    summaries = {
        "GP": {"net": {"total_return_percent": 1}},
        "ACI": {"net": {"total_return_percent": 2}},
        "BRACBANK": {"net": {"total_return_percent": 2}},
    }
    assert deterministic_best(summaries) == "ACI"


def test_parameter_grid_is_bounded_for_all_universes() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(FIVE_SYMBOLS)}
    result = parameter_universe_stability(bars, "ACI")
    assert len(PARAMETER_GRID) == 9
    assert len(result["experiments"]) == 9
    assert all(
        set(row["universes"]) == {"full", "leave_bracbank_out", "leave_best_out"}
        for row in result["experiments"]
    )


def test_cost_stress_is_bounded_and_dsex_not_substituted() -> None:
    bars = {symbol: _bars(symbol, index) for index, symbol in enumerate(FIVE_SYMBOLS)}
    result = cost_stress(bars, "ACI")
    assert result["scenario_count"] == 12
    assert result["costs_authoritative"] is False
    assert "DSEX" not in bars


def test_runner_keeps_promotion_and_trading_entities_protected() -> None:
    source = (Path(__file__).parents[2] / "scripts" / "run_five_symbol_robustness.py").read_text(
        encoding="utf-8"
    )
    assert '"promotion_status": "blocked"' in source
    assert '"campaign_eligibility": False' in source
    assert "PROTECTED = (ValidationCampaign, PaperSession, Signal, Order, Transaction)" in source
    assert "before != after" in source
    assert '"dsex": "unavailable_not_substituted"' in source

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.models import ResearchDataset
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services import expanded_strategy_validation as validation
from app.services.cross_sectional_momentum import (
    PRIMARY_CONFIG as MOMENTUM_CONFIG,
)
from app.services.cross_sectional_momentum import (
    build_rebalance_plans as build_momentum_plans,
)
from app.services.cross_sectional_momentum import (
    canonical_hash,
    momentum_scores,
)
from app.services.historical_strategy_research import sha256_file
from app.services.research_governance import parameter_set_hash, strategy_code_hash

ROOT = Path(__file__).resolve().parents[2]


def _bar(symbol: str, day: datetime, price: float) -> HistoricalBar:
    return HistoricalBar(
        timestamp=day,
        symbol=symbol,
        open=Decimal(str(price)),
        high=Decimal(str(price * 1.01)),
        low=Decimal(str(price * 0.99)),
        close=Decimal(str(price * 1.002)),
        volume=10_000_000,
        source="adjusted_test",
        timestamp_provenance=TimestampProvenance.UNKNOWN,
        quality_flags=["adjusted_execution"],
    )


def _bars(sessions: int = 900) -> dict[str, list[HistoricalBar]]:
    start = datetime(2018, 1, 1, tzinfo=UTC)
    output: dict[str, list[HistoricalBar]] = {}
    for symbol_index, symbol in enumerate(validation.EXPANDED_UNIVERSE):
        price = 80.0 + symbol_index
        rows: list[HistoricalBar] = []
        rate = 0.0002 + symbol_index * 0.00002
        for index in range(sessions):
            price *= 1 + rate + (0.0003 if index % 17 == 0 else -0.00005)
            rows.append(_bar(symbol, start + timedelta(days=index), price))
        output[symbol] = rows
    return output


def _active_row(symbol: str, grain: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "date": "2020-01-02",
        "adjustment_status": grain,
        "final_disposition": "tier_2_single_source_high_quality",
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": "1000",
        "selected_source": "test",
        "source_lineage": [{"source_file_hash": "a" * 64}],
        "source_row_ids": [f"{symbol}:1"],
        "raw_file_hashes": ["a" * 64],
        "audit_event_ids": ["audit-test"],
    }


class _DatasetSession:
    def __init__(self, datasets: list[ResearchDataset]) -> None:
        self.datasets = datasets

    def scalars(self, _statement: object) -> list[ResearchDataset]:
        return self.datasets

    def get(self, _model: object, key: str) -> ResearchDataset | None:
        return next((item for item in self.datasets if item.id == key), None)


def test_frozen_hashes_and_25_symbol_identity_are_unchanged() -> None:
    assert len(validation.EXPANDED_UNIVERSE) == 25
    assert len(set(validation.EXPANDED_UNIVERSE)) == 25
    assert validation.FROZEN_IDENTITIES == (
        "ma_crossover@1.0.0",
        "cross_sectional_momentum@0.1.0",
        "defensive_low_volatility@0.1.0",
        "absolute_momentum_filter@0.1.0",
    )
    assert (
        strategy_code_hash() == "b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3"
    )
    assert (
        parameter_set_hash() == "51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e"
    )
    expected = {
        "cross_sectional_momentum@0.1.0": (
            validation.momentum_code_hash(ROOT),
            validation.momentum_parameter_hash(),
            "9d257fa8e495c2a22c567f7c546137c90f546cceb1f8eaf06430f8d677fd18f1",
            "192ee7b59e2882cf23c784b90528ae0de16d6736e833738372dbc79d4105c2c9",
        ),
        "defensive_low_volatility@0.1.0": (
            validation.defensive_code_hash(ROOT),
            validation.defensive_parameter_hash(),
            "9c692880cecec0b5135404acd43d89678e167add395f8cb92678d5f63e68b346",
            "b44a64cadb4b3522a9724b52bf701137db223ccb3ee492c5c15e4cc2e8070bd0",
        ),
        "absolute_momentum_filter@0.1.0": (
            validation.absolute_code_hash(ROOT),
            validation.absolute_parameter_hash(),
            "69ae36ca9665af4d87b32ccc998545ce6662f7b177164ece2f64bc4696830034",
            "4ee64d3da4b492a8292c5fabacf8b6118f6bc3a45ac2212af5e1d7452da4f3c8",
        ),
    }
    assert all(
        (code, params) == (expected_code, expected_params)
        for code, params, expected_code, expected_params in expected.values()
    )


def test_loader_admits_only_unique_adjusted_rows_with_complete_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    groups = (3, 2, 5, 15)
    offset = 0
    datasets: list[ResearchDataset] = []
    expected: list[dict[str, Any]] = []
    for index, size in enumerate(groups):
        symbols = list(validation.EXPANDED_UNIVERSE[offset : offset + size])
        offset += size
        path = tmp_path / f"dataset-{index}.jsonl"
        rows = [
            _active_row(symbol, grain) for symbol in symbols for grain in ("adjusted", "unadjusted")
        ]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        digest = sha256_file(path)
        dataset = ResearchDataset(
            id=f"dataset-{index}",
            name=f"version-{index}",
            symbols=symbols,
            data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
            source_evidence_ids=[f"source-{index}"],
            source_hash=f"{index + 1:064x}",
            dataset_hash=digest,
            timestamp_trust="unknown",
            raw_file_path=str(path),
            normalized_file_path=str(path),
            quality_report={},
            status="research_dataset_active",
            audit_event_ids=[f"audit-{index}"],
        )
        datasets.append(dataset)
        expected.append(
            {
                "id": dataset.id,
                "version": dataset.name,
                "sha256": digest,
                "source_sha256": dataset.source_hash,
                "symbols": symbols,
                "origin": f"origin-{index}",
            }
        )
    monkeypatch.setattr(validation, "EXPECTED_DATASETS", tuple(expected))
    loaded = validation.load_expanded_universe(_DatasetSession(datasets), tmp_path)  # type: ignore[arg-type]
    assert loaded.data_quality["unique_symbol_count"] == 25
    assert loaded.data_quality["total_approved_rows"] == 50
    assert loaded.data_quality["total_adjusted_execution_rows"] == 25
    assert loaded.data_quality["total_unadjusted_reference_rows"] == 25
    assert loaded.data_quality["complete_lineage"] is True
    assert all(len(rows) == 1 for rows in loaded.bars.values())


def test_dated_eligibility_no_lookahead_and_next_open() -> None:
    bars = _bars()
    eligibility = validation.strategy_eligibility_dates(bars, "defensive_low_volatility@0.1.0")
    assert len(eligibility["per_symbol_first_eligible_signal"]) == 25
    assert eligibility["all_25_lookback_satisfied_from"] == max(
        eligibility["per_symbol_first_eligible_signal"].values()
    )

    plans = build_momentum_plans(bars, MOMENTUM_CONFIG)
    assert plans and all(plan["execution_date"] > plan["signal_date"] for plan in plans)
    signal = plans[0]["signal_date"]
    before, _ = momentum_scores(bars, signal, lookback_months=12)
    changed = _bars()
    future = next(item for item in changed["GP"] if item.timestamp.date() > signal)
    changed["GP"][changed["GP"].index(future)] = _bar("GP", future.timestamp, 1_000_000)
    after, _ = momentum_scores(changed, signal, lookback_months=12)
    assert before == after


def test_common_window_reproduction_benchmarks_costs_and_cash() -> None:
    bars = _bars()
    floor = date.fromisoformat(
        validation.strategy_eligibility_dates(bars, "defensive_low_volatility@0.1.0")[
            "all_25_lookback_satisfied_from"
        ]
    )
    first = validation._portfolio_strategy_run(  # noqa: SLF001
        "defensive_low_volatility@0.1.0", bars, signal_floor=floor
    )
    second = validation._portfolio_strategy_run(  # noqa: SLF001
        "defensive_low_volatility@0.1.0", bars, signal_floor=floor
    )
    gross = validation._portfolio_strategy_run(  # noqa: SLF001
        "defensive_low_volatility@0.1.0", bars, signal_floor=floor, gross=True
    )
    assert canonical_hash(first.metrics) == canonical_hash(second.metrics)
    assert canonical_hash(first.ledger) == canonical_hash(second.ledger)
    assert first.metrics["total_fees_bdt"] > 0
    assert first.metrics["total_slippage_bdt"] > 0
    assert first.metrics["final_equity"] < gross.metrics["final_equity"]
    assert first.metrics["minimum_cash_bdt"] >= -1e-7
    assert all(row["execution_timestamp"] > row["signal_timestamp"] for row in first.ledger)
    first_signal = date.fromisoformat(first.rebalances[0]["signal_date"])
    benchmarks = validation._benchmark_runs(bars, first_signal)  # noqa: SLF001
    assert set(benchmarks) == {
        "equal_weight_buy_and_hold",
        "monthly_rebalanced_equal_weight",
        "quarterly_rebalanced_equal_weight",
        "half_equal_weight_equities_half_cash",
    }
    assert all(run.metrics["minimum_cash_bdt"] >= -1e-7 for run in benchmarks.values())


def test_assessment_fails_closed_on_negative_leave_one_out_result() -> None:
    metrics = {
        "total_return_percent": 20.0,
        "maximum_drawdown_percent": -10.0,
        "sharpe_ratio": 1.0,
    }
    common = {
        "primary": {
            "metrics": metrics,
            "largest_absolute_contributor_share": 0.20,
            "dataset_contribution_bdt": {"old": 50.0, "new": 50.0},
        },
        "walk_forward": {
            "partitions": [
                {"holdout": {"metrics": {"total_return_percent": value}}}
                for value in (5.0, 4.0, 3.0)
            ],
            "combined_holdout": {"metrics": {"total_return_percent": 12.0}},
        },
        "subperiods": [{"metrics": {"total_return_percent": value}} for value in (4.0, 3.0, 2.0)],
        "benchmarks": {
            "equal_weight_buy_and_hold": {"metrics": {"maximum_drawdown_percent": -12.0}}
        },
        "costs_erase_most_of_gross": False,
    }
    natural = {"primary": {"metrics": metrics}}
    assessment = validation._assessment(  # noqa: SLF001
        common,
        natural,
        {"DEPENDENT": {"return_percent": -1.0}},
    )
    assert assessment["criteria"]["not_dominated_by_one_symbol"] is False
    assert assessment["assessment"] == "remains_inconclusive"

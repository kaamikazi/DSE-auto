from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from scripts.run_historical_strategy_research import (
    APPROVAL_PACK_HASH,
    EXPECTED_HEAD,
    REGISTRATION_ID,
    assert_pinned_identity,
)
from sqlalchemy import func, select

from app.models import AuditChain, Order, PaperSession, Signal, Transaction, ValidationCampaign
from app.schemas.market import HistoricalBar, TimestampProvenance
from app.services.audit import append_audit, verify_audit_chain
from app.services.historical_strategy_research import (
    PARAMETER_GRID,
    REGISTERED_PARAMETERS,
    benchmark_analysis,
    corporate_action_analysis,
    cost_sensitivity,
    run_symbol,
    tier_sensitivity,
    timing_semantics,
    validate_and_load_dataset,
    walk_forward_analysis,
)


def _bars(symbol: str, count: int = 180) -> list[HistoricalBar]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return [
        HistoricalBar(
            timestamp=start + timedelta(days=index),
            symbol=symbol,
            open=Decimal(100 + index % 17),
            high=Decimal(102 + index % 17),
            low=Decimal(98 + index % 17),
            close=Decimal(101 + index % 17),
            volume=100000,
            source="test",
            timestamp_provenance=TimestampProvenance.UNKNOWN,
        )
        for index in range(count)
    ]


def test_timing_is_close_signal_then_next_open() -> None:
    semantics = timing_semantics()
    assert semantics["same_bar_execution"] is False
    assert semantics["earliest_execution"] == "next source-present bar open"
    rows = _bars("GP", 80)
    baseline = run_symbol("GP", rows)
    changed = list(rows)
    changed[-1] = changed[-1].model_copy(update={"close": Decimal("9999")})
    # The final close cannot create a final-bar trade: execution would require a future bar.
    assert run_symbol("GP", changed).trades == baseline.trades


def test_walk_forward_is_chronological_and_holdout_untouched() -> None:
    bars = {symbol: _bars(symbol) for symbol in ("GP", "ACI", "BRACBANK")}
    result = walk_forward_analysis(bars)
    assert result["final_holdout_untouched_during_tuning"] is True
    for symbol in bars:
        item = result["symbols"][symbol]
        for partition in item["partitions"]:
            assert partition["training_end"] < partition["validation_start"]
        assert item["pre_holdout_end_index"] == item["holdout_start_index"]


def test_parameter_grid_is_bounded_and_registered_pair_is_unchanged() -> None:
    assert len(PARAMETER_GRID) == 9
    assert REGISTERED_PARAMETERS == {"fast": 20, "slow": 50}
    assert all(item["fast"] < item["slow"] for item in PARAMETER_GRID)


def test_fee_and_slippage_scenarios_are_explicit() -> None:
    bars = {symbol: _bars(symbol) for symbol in ("GP", "ACI", "BRACBANK")}
    result = cost_sensitivity(bars)
    assert len(result["fee_scenarios"]) == 3
    assert len(result["slippage_scenarios"]) == 4
    assert result["fees_are_authoritative"] is False


def test_excluded_interval_pause_splits_exposure() -> None:
    bars = {symbol: _bars(symbol) for symbol in ("GP", "ACI", "BRACBANK")}
    baseline = {symbol: run_symbol(symbol, rows) for symbol, rows in bars.items()}
    dates = {symbol: [rows[90].timestamp.date().isoformat()] for symbol, rows in bars.items()}
    result = corporate_action_analysis(bars, dates, baseline)
    assert result["reconstruction_performed"] is False
    assert all(
        item["pause_sensitivity"]["segments_tested"] == 2 for item in result["symbols"].values()
    )


def test_active_dataset_validation_rejects_dsex(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    row = {
        "symbol": "DSEX",
        "date": "2024-01-01",
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "volume": 1,
        "adjustment_status": "adjusted",
        "selected_source": "x",
        "contributing_sources": ["x"],
        "source_row_ids": ["1"],
        "raw_hashes": ["0" * 64],
        "source_lineage": ["x"],
        "transformation_version": "v",
        "quality_tier": "tier_2",
        "approval_decision_id": "a",
        "activation_timestamp": "x",
        "audit_linkage": ["a"],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    try:
        validate_and_load_dataset(path)
    except ValueError as exc:
        assert "Mandatory active-dataset validation failed" in str(exc)
    else:
        raise AssertionError("DSEX input must fail closed")


def test_pinned_registration_and_dataset_identity_fail_closed() -> None:
    identity = {
        "registration_id": REGISTRATION_ID,
        "strategy": "ma_crossover@1.0.0",
        "lifecycle": "research",
        "code_hash": "b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3",
        "parameter_hash": "51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e",
        "parameters": {"fast": 20, "slow": 50},
        "dataset_registry_id": "ba5f2d99-6c66-4e37-ae31-d48c8ee47b15",
        "dataset_id": "gp-aci-bracbank-research-f24a48cb729e8a65",
        "dataset_hash": "ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791",
        "promotion_status": "blocked",
        "campaign_eligibility": False,
        "git_head": EXPECTED_HEAD,
    }
    artifacts = {"code_hash": identity["code_hash"], "parameter_hash": identity["parameter_hash"]}
    assert_pinned_identity(identity, artifacts)
    identity["dataset_hash"] = "0" * 64
    with pytest.raises(RuntimeError, match="Pinned identity mismatch"):
        assert_pinned_identity(identity, artifacts)
    assert len(APPROVAL_PACK_HASH) == 64


def test_dsex_is_unavailable_and_not_substituted() -> None:
    bars = {symbol: _bars(symbol) for symbol in ("GP", "ACI", "BRACBANK")}
    assert benchmark_analysis(bars)["dsex"] == "unavailable_not_substituted"


def test_tier_two_is_not_equated_to_cross_source_confirmation(tmp_path: Path) -> None:
    bars = {symbol: _bars(symbol) for symbol in ("GP", "ACI", "BRACBANK")}
    path = tmp_path / "tiers.jsonl"
    rows = [
        json.dumps(
            {
                "symbol": symbol,
                "date": bar.timestamp.date().isoformat(),
                "adjustment_status": "adjusted",
                "quality_tier": "tier_1_cross_source_confirmed",
            }
        )
        for symbol, symbol_bars in bars.items()
        for bar in symbol_bars[:60]
    ]
    path.write_text("\n".join(rows), encoding="utf-8")
    result = tier_sensitivity(bars, path)
    assert result["equivalence_claim"] is False
    assert result["tier_1_equal_weight"] is not None


def test_analysis_creates_no_trading_entities_and_audit_remains_valid(db) -> None:  # type: ignore[no-untyped-def]
    chain = AuditChain(
        status="active",
        genesis_reason="test",
        operator_acknowledgement="test",
        legacy_archive_path="test-archive.json",
        legacy_archive_hash="0" * 64,
    )
    db.add(chain)
    db.commit()
    protected = (ValidationCampaign, PaperSession, Signal, Order, Transaction)
    before = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    run_symbol("GP", _bars("GP"))
    append_audit(db, actor="test", event_type="strategy.research_test", entity_type="strategy")
    after = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before == after
    assert verify_audit_chain(db)

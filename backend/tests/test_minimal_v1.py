from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, assert_paper_only_safety
from app.core.database import Base
from app.core.database_identity import resolve_database_url
from app.minimal_v1_cli import build_parser
from app.models import (
    AuditEvent,
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.five_symbol_robustness import (
    FIVE_SYMBOLS,
    run_portfolio,
    run_portfolio_buy_hold,
    symbol_summaries,
    validate_combined_datasets,
)
from app.services.historical_strategy_research import sha256_file
from app.services.minimal_v1 import (
    DEFAULT_METRIC_TOLERANCE,
    MinimalV1Facade,
    derive_dataset_summary,
    derive_strategy_summary,
    enforce_metric_tolerances,
)
from app.services.strategy_research_archival import canonical_hash


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_rows(path: Path, symbols: tuple[str, ...], *, extension: bool) -> None:
    rows: list[str] = []
    start = date(2020, 1, 1)
    for symbol_index, symbol in enumerate(symbols):
        for index in range(180):
            phase = index % 80
            movement = phase if phase < 40 else 80 - phase
            price = 80 + symbol_index * 7 + movement
            day = (start + timedelta(days=index)).isoformat()
            row = {
                "symbol": symbol,
                "date": day,
                "open": str(price),
                "high": str(price + 2),
                "low": str(price - 2),
                "close": str(price + 1),
                "volume": "1000000",
                "adjustment_status": "adjusted",
                "selected_source": "fixture",
                "source_lineage": [{"source": "fixture"}],
                "contributing_sources": ["fixture"],
                "source_row_ids": [f"{symbol}:{day}"],
                "raw_hashes": ["a" * 64],
                "audit_linkage": ["fixture-audit"],
                "quality_tier": "tier_2_single_source_high_quality",
            }
            if extension:
                row["final_disposition"] = "tier_2_single_source_high_quality"
            rows.append(json.dumps(row, sort_keys=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _metrics_fixture(parent_path: Path, extension_path: Path) -> dict[str, Any]:
    bars, _ = validate_combined_datasets(parent_path, extension_path)
    portfolio = run_portfolio(bars)
    benchmark = run_portfolio_buy_hold(bars)
    without_bracbank = run_portfolio(
        {symbol: values for symbol, values in bars.items() if symbol != "BRACBANK"}
    )
    summaries = symbol_summaries(portfolio, bars, {symbol: 0 for symbol in FIVE_SYMBOLS})
    return {
        "baseline": {
            "symbols": summaries,
            "equal_weight": {"net": portfolio["net"]["metrics"]},
        },
        "benchmarks": {"equal_weight": {"net": benchmark["net"]["metrics"]}},
        "leave_bracbank_out": {"net": without_bracbank["net"]["metrics"]},
    }


@dataclass(frozen=True)
class MinimalFixture:
    facade: MinimalV1Facade
    source_files: tuple[Path, ...]
    run_id: str


@pytest.fixture
def minimal_fixture(db: Session, tmp_path: Path) -> MinimalFixture:
    parent_path = tmp_path / "data" / "parent.jsonl"
    extension_path = tmp_path / "data" / "extension.jsonl"
    _write_rows(parent_path, ("GP", "ACI", "BRACBANK"), extension=False)
    _write_rows(extension_path, ("BATBC", "SQURPHARMA"), extension=True)
    prior = _metrics_fixture(parent_path, extension_path)
    prior_dir = tmp_path / "reports" / "strategy_research" / "five-symbol-fixture"
    prior_dir.mkdir(parents=True)
    prior_path = prior_dir / "research_result.json"
    prior_path.write_text(json.dumps(prior, sort_keys=True), encoding="utf-8")
    run_id = "risk-control-attribution-fixture"
    source_dir = tmp_path / "reports" / "strategy_research" / run_id
    source_dir.mkdir(parents=True)
    source_path = source_dir / "research_result.json"
    source = {
        "identity": {
            "prior_result_path": prior_path.relative_to(tmp_path).as_posix(),
            "prior_result_sha256": sha256_file(prior_path),
        },
        "decision": {"research_role": "reject_strategy"},
    }
    source_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    parent = ResearchDataset(
        id="parent-dataset",
        name="parent-v1",
        symbols=["GP", "ACI", "BRACBANK"],
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=["fixture"],
        source_hash="1" * 64,
        dataset_hash=sha256_file(parent_path),
        timestamp_trust="unknown",
        raw_file_path=str(parent_path),
        normalized_file_path=str(parent_path),
        quality_report={
            "active_rows": 540,
            "coverage": {symbol: {"rows": 180} for symbol in ("GP", "ACI", "BRACBANK")},
        },
        status="research_dataset_active",
        audit_event_ids=["fixture"],
    )
    extension = ResearchDataset(
        id="extension-dataset",
        name="extension-v1",
        symbols=["BATBC", "SQURPHARMA"],
        data_types=["daily_ohlcv", "adjusted_and_unadjusted", "immutable_lineage"],
        source_evidence_ids=["fixture"],
        source_hash="2" * 64,
        dataset_hash=sha256_file(extension_path),
        timestamp_trust="unknown",
        raw_file_path=str(extension_path),
        normalized_file_path=str(extension_path),
        quality_report={
            "active_rows": 360,
            "observed_windows": {
                symbol: {"start": "2020-01-01", "end": "2020-06-28"}
                for symbol in ("BATBC", "SQURPHARMA")
            },
        },
        status="research_dataset_active",
        audit_event_ids=["fixture"],
    )
    parameter_hash = canonical_hash({"fast": 20, "slow": 50})
    contract = {
        "registration_id": "strategy-registration",
        "code_hash": "3" * 64,
        "parameter_hash": parameter_hash,
        "mutable": False,
        "dataset_identities": {
            "parent": {
                "id": parent.id,
                "version": parent.name,
                "sha256": parent.dataset_hash,
                "symbols": parent.symbols,
            },
            "extension": {
                "id": extension.id,
                "version": extension.name,
                "sha256": extension.dataset_hash,
                "symbols": extension.symbols,
            },
        },
        "timing_contract": {
            "signal": "20/50 close moving-average crossover",
            "execution": "next source-present bar open",
            "same_bar_execution": False,
            "exclusions": "no signal or execution on excluded rows",
        },
        "cost_assumptions": {"fee_percent": "0.40", "slippage_percent": "0.25"},
        "baseline_results": {
            "equal_weight_buy_and_hold": prior["benchmarks"]["equal_weight"]["net"]
        },
        "source_result_sha256": sha256_file(source_path),
    }
    registration = StrategyRegistration(
        id="strategy-registration",
        strategy_id="ma_crossover",
        version="1.0.0",
        lifecycle_state="research",
        code_hash="3" * 64,
        parameters={"fast": 20, "slow": 50},
        data_requirements={"symbols": list(FIVE_SYMBOLS)},
        evidence={
            "parameter_hash": parameter_hash,
            "research_verdict": "rejected",
            "research_role": "archived_rejected_benchmark",
            "promotion_status": "blocked",
            "promotion_authorized": False,
            "campaign_eligibility": False,
            "execution_authorization": False,
            "research_execution_authorized": False,
            "real_money_eligibility": False,
            "no_real_money_authorization": True,
            "qualification": "0/60",
            "archived_benchmark_contract": contract,
        },
        minimum_sample_size=100,
    )
    db.add_all([parent, extension, registration])
    db.commit()
    settings = Settings.model_construct(
        TRADING_MODE="paper",
        LIVE_TRADING_ENABLED=False,
        BROKER_ADAPTER="disabled",
        DATABASE_ROLE="test",
    )
    return MinimalFixture(
        facade=MinimalV1Facade(db, repository_root=tmp_path, settings=settings),
        source_files=(parent_path, extension_path, prior_path, source_path),
        run_id=run_id,
    )


def test_canonical_database_resolution(tmp_path: Path) -> None:
    resolved = resolve_database_url("sqlite:///./data/dse_autotrader.db", base_dir=tmp_path)
    assert resolved == f"sqlite:///{(tmp_path / 'data' / 'dse_autotrader.db').as_posix()}"


def test_centralized_safety_assertion() -> None:
    safe = Settings.model_construct(
        TRADING_MODE="paper", LIVE_TRADING_ENABLED=False, BROKER_ADAPTER="disabled"
    )
    assert_paper_only_safety(safe)
    unsafe = Settings.model_construct(
        TRADING_MODE="paper", LIVE_TRADING_ENABLED=False, BROKER_ADAPTER="paper"
    )
    with pytest.raises(RuntimeError, match="Paper-only safety mismatch"):
        assert_paper_only_safety(unsafe)


def test_dataset_and_strategy_summary_derivation(
    minimal_fixture: MinimalFixture,
) -> None:
    datasets = minimal_fixture.facade.active_datasets()
    assert len(datasets) == 2
    assert datasets[0].lineage_status == "complete"
    assert sum(item.row_count for item in datasets) == 900
    strategies = minimal_fixture.facade.registered_strategies()
    assert len(strategies) == 1
    assert strategies[0].research_verdict == "rejected"
    assert strategies[0].execution_permission is False
    assert strategies[0].promotion_permission is False


def test_pure_summary_helpers_fail_closed(tmp_path: Path) -> None:
    missing = ResearchDataset(
        id="missing",
        name="missing-v1",
        symbols=["GP"],
        data_types=["daily_ohlcv", "immutable_lineage"],
        source_evidence_ids=[],
        source_hash="4" * 64,
        dataset_hash="5" * 64,
        timestamp_trust="unknown",
        raw_file_path="missing",
        normalized_file_path="missing",
        quality_report={},
        status="submitted",
        audit_event_ids=[],
    )
    assert derive_dataset_summary(missing, repository_root=tmp_path).lineage_status == "incomplete"
    strategy = StrategyRegistration(
        id="unapproved",
        strategy_id="fixture",
        version="1",
        lifecycle_state="draft",
        code_hash="6" * 64,
        parameters={},
        data_requirements={},
        evidence={},
        minimum_sample_size=1,
    )
    summary = derive_strategy_summary(strategy)
    assert summary.execution_permission is False
    assert summary.promotion_permission is False


def test_research_run_summary_derives_from_preserved_evidence(
    minimal_fixture: MinimalFixture,
) -> None:
    summary = minimal_fixture.facade.historical_run(minimal_fixture.run_id)
    assert summary.strategy_identity["name"] == "ma_crossover"
    assert summary.dataset_identities["parent"]["id"] == "parent-dataset"
    assert summary.timing_contract["same_bar_execution"] is False
    assert summary.costs == {"fee_percent": "0.40", "slippage_percent": "0.25"}
    assert summary.verdict == {
        "research_decision": "reject_strategy",
        "research_role": "archived_rejected_benchmark",
    }


def test_archived_reproduction_contract_and_no_operational_effects(
    db: Session, minimal_fixture: MinimalFixture, tmp_path: Path
) -> None:
    protected = (ValidationCampaign, PaperSession, Signal, Order, Transaction, AuditEvent)
    before_counts = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    before_hashes = {path: sha256_file(path) for path in minimal_fixture.source_files}
    output = tmp_path / "reproduction"
    result = minimal_fixture.facade.reproduce_archived_run(output, run_id=minimal_fixture.run_id)
    assert result["trade_rows"] > 0
    assert set(path.name for path in output.iterdir()) == {
        "research_result.json",
        "trade_ledger.csv",
        "interpretation.md",
    }
    payload = json.loads((output / "research_result.json").read_text(encoding="utf-8"))
    assert payload["compatibility"]["passed"] is True
    assert payload["compatibility"]["verdict_exact_match"] is True
    assert max(abs(value) for value in payload["compatibility"]["differences"].values()) == 0
    assert payload["summary"]["verdict"] == {
        "research_decision": "reject_strategy",
        "research_role": "archived_rejected_benchmark",
    }
    assert set(payload["artifact_hashes"]) == {"csv", "markdown"}
    assert payload["canonical_payload_sha256"] == _hash_bytes(
        json.dumps(
            {key: value for key, value in payload.items() if key != "canonical_payload_sha256"},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    )
    after_counts = {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in protected
    }
    assert before_counts == after_counts
    assert before_hashes == {path: sha256_file(path) for path in minimal_fixture.source_files}


def test_metric_tolerance_enforcement() -> None:
    actual = {"return": 1.0, "trades": 2}
    expected = {"return": 1.0 + DEFAULT_METRIC_TOLERANCE / 2, "trades": 2}
    differences = enforce_metric_tolerances(
        actual, expected, {"return": DEFAULT_METRIC_TOLERANCE, "trades": 0.0}
    )
    assert abs(differences["return"]) <= DEFAULT_METRIC_TOLERANCE
    with pytest.raises(RuntimeError, match="Metric compatibility failure"):
        enforce_metric_tolerances(
            actual,
            {"return": 1.1, "trades": 2},
            {"return": DEFAULT_METRIC_TOLERANCE, "trades": 0.0},
        )


def test_cli_and_complexity_budget_have_no_hidden_surface() -> None:
    parser = build_parser()
    choices = next(
        action.choices for action in parser._actions if getattr(action, "choices", None) is not None
    )
    assert set(cast(dict[str, Any], choices)) == {
        "status",
        "datasets",
        "strategies",
        "runs",
        "reproduce",
        "forward-status",
        "forward-ingest",
        "forward-start",
        "forward-stop",
        "forward-emergency",
        "forward-portfolio",
        "forward-decision",
        "forward-reconcile",
    }
    import app.schemas.minimal_v1 as schemas_module
    import app.services.minimal_v1 as service_module

    additions = inspect.getsource(schemas_module) + inspect.getsource(service_module)
    assert "__tablename__" not in additions
    assert "append_audit" not in additions
    assert "event_type" not in additions
    assert "Literal[" not in additions
    assert len(Base.metadata.tables) == 52
    root = Path(__file__).parents[2]
    assert len(list((root / "backend" / "alembic" / "versions").glob("*.py"))) == 12

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, assert_paper_only_safety, get_settings
from app.core.database_identity import REPOSITORY_ROOT
from app.models import ResearchDataset, StrategyRegistration
from app.schemas.minimal_v1 import (
    DatasetSummary,
    ResearchRunSummary,
    SafetyStatus,
    StrategySummary,
)
from app.services.audit import audit_status, verify_audit_chain
from app.services.five_symbol_robustness import (
    FIVE_SYMBOLS,
    run_portfolio,
    run_portfolio_buy_hold,
    symbol_summaries,
    validate_combined_datasets,
)
from app.services.historical_strategy_research import sha256_file
from app.services.strategy_research_archival import assert_archived_state, canonical_hash

ARCHIVED_STRATEGY_ID = "ma_crossover"
ARCHIVED_STRATEGY_VERSION = "1.0.0"
MOMENTUM_STRATEGY_ID = "cross_sectional_momentum"
MOMENTUM_STRATEGY_VERSION = "0.1.0"
EXPECTED_RESEARCH_DECISION = "reject_strategy"
EXPECTED_RESEARCH_ROLE = "archived_rejected_benchmark"
DEFAULT_METRIC_TOLERANCE = 1e-8


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _dataset_file(dataset: ResearchDataset, root: Path) -> Path:
    path = Path(dataset.normalized_file_path)
    return path if path.is_absolute() else root / path


def derive_dataset_summary(dataset: ResearchDataset, *, repository_root: Path) -> DatasetSummary:
    quality = dict(dataset.quality_report or {})
    coverage = cast(
        dict[str, Any], quality.get("coverage") or quality.get("observed_windows") or {}
    )
    row_count = int(quality.get("active_rows") or 0)
    if not row_count:
        row_count = sum(
            int(cast(dict[str, Any], item).get("rows") or 0) for item in coverage.values()
        )
    data_types = set(dataset.data_types or [])
    if "adjusted_and_unadjusted" in data_types:
        adjustment_grain = "adjusted_and_unadjusted"
    else:
        adjustment_grain = str(quality.get("adjustment_grain") or "unknown")
    normalized = _dataset_file(dataset, repository_root)
    lineage_complete = (
        "immutable_lineage" in data_types
        and normalized.is_file()
        and sha256_file(normalized) == dataset.dataset_hash
    )
    return DatasetSummary(
        registry_id=dataset.id,
        version=dataset.name,
        dataset_hash=dataset.dataset_hash,
        symbols=list(dataset.symbols),
        coverage=coverage,
        row_count=row_count,
        adjustment_grain=adjustment_grain,
        activation_status=dataset.status,
        lineage_status="complete" if lineage_complete else "incomplete",
    )


def derive_strategy_summary(registration: StrategyRegistration) -> StrategySummary:
    evidence = dict(registration.evidence or {})
    return StrategySummary(
        registration_id=registration.id,
        name=registration.strategy_id,
        version=registration.version,
        code_hash=registration.code_hash,
        parameter_hash=str(
            evidence.get("parameter_hash") or canonical_hash(registration.parameters)
        ),
        research_verdict=str(evidence.get("research_verdict") or registration.lifecycle_state),
        execution_permission=bool(
            evidence.get("execution_authorization") is True
            or evidence.get("research_execution_authorized") is True
        ),
        promotion_permission=evidence.get("promotion_authorized") is True,
    )


def enforce_metric_tolerances(
    actual: dict[str, float | int],
    expected: dict[str, float | int],
    tolerances: dict[str, float],
) -> dict[str, float]:
    if set(actual) != set(expected) or set(actual) != set(tolerances):
        raise RuntimeError("Metric compatibility keys differ from the archived contract")
    differences = {key: float(actual[key]) - float(expected[key]) for key in actual}
    failures = {
        key: {"actual": actual[key], "expected": expected[key], "tolerance": tolerances[key]}
        for key, difference in differences.items()
        if abs(difference) > tolerances[key]
    }
    if failures:
        raise RuntimeError(f"Metric compatibility failure: {failures}")
    return differences


class MinimalV1Facade:
    """Read-compatible view over canonical records and the trusted research engine."""

    def __init__(
        self,
        db: Session,
        *,
        repository_root: Path = REPOSITORY_ROOT,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.repository_root = repository_root.resolve()
        self.settings = settings or get_settings()

    def safety_status(self) -> SafetyStatus:
        return SafetyStatus(
            trading_mode=self.settings.TRADING_MODE,
            live_trading_enabled=self.settings.LIVE_TRADING_ENABLED,
            broker_adapter=self.settings.BROKER_ADAPTER,
            database_role=self.settings.DATABASE_ROLE,
            audit_valid=verify_audit_chain(self.db),
        )

    def active_datasets(self) -> list[DatasetSummary]:
        datasets = self.db.scalars(
            select(ResearchDataset)
            .where(ResearchDataset.status == "research_dataset_active")
            .order_by(ResearchDataset.created_at, ResearchDataset.id)
        )
        return [
            derive_dataset_summary(item, repository_root=self.repository_root) for item in datasets
        ]

    def registered_strategies(self) -> list[StrategySummary]:
        strategies = self.db.scalars(
            select(StrategyRegistration).order_by(
                StrategyRegistration.created_at, StrategyRegistration.id
            )
        )
        return [derive_strategy_summary(item) for item in strategies]

    def _archived_registration(self) -> StrategyRegistration:
        registration = self.db.scalar(
            select(StrategyRegistration).where(
                StrategyRegistration.strategy_id == ARCHIVED_STRATEGY_ID,
                StrategyRegistration.version == ARCHIVED_STRATEGY_VERSION,
            )
        )
        if registration is None:
            raise LookupError("Archived ma_crossover registration is unavailable")
        assert_archived_state(registration)
        return registration

    def _find_hashed_result(self, pattern: str, expected_hash: str) -> Path:
        root = self.repository_root / "reports" / "strategy_research"
        for path in sorted(root.glob(pattern)):
            if path.is_file() and sha256_file(path) == expected_hash:
                return path
        raise FileNotFoundError(f"Preserved research result {expected_hash} is unavailable")

    def _archived_context(
        self,
    ) -> tuple[StrategyRegistration, dict[str, Any], Path, dict[str, Any], Path, dict[str, Any]]:
        registration = self._archived_registration()
        contract = cast(dict[str, Any], registration.evidence["archived_benchmark_contract"])
        source_path = self._find_hashed_result(
            "risk-control-attribution-*/research_result.json",
            str(contract["source_result_sha256"]),
        )
        source = cast(dict[str, Any], json.loads(source_path.read_text(encoding="utf-8")))
        prior_path = self.repository_root / Path(str(source["identity"]["prior_result_path"]))
        if not prior_path.is_file() or sha256_file(prior_path) != str(
            source["identity"]["prior_result_sha256"]
        ):
            raise RuntimeError("Preserved five-symbol baseline identity mismatch")
        prior = cast(dict[str, Any], json.loads(prior_path.read_text(encoding="utf-8")))
        if (
            source["decision"]["research_role"] != EXPECTED_RESEARCH_DECISION
            or registration.evidence.get("research_role") != EXPECTED_RESEARCH_ROLE
        ):
            raise RuntimeError("Archived rejection verdict changed")
        return registration, contract, source_path, source, prior_path, prior

    @staticmethod
    def _expected_metrics(prior: dict[str, Any]) -> dict[str, float | int]:
        metrics: dict[str, float | int] = {
            f"per_symbol_net_return_percent.{symbol}": float(
                prior["baseline"]["symbols"][symbol]["net"]["total_return_percent"]
            )
            for symbol in FIVE_SYMBOLS
        }
        combined = prior["baseline"]["equal_weight"]["net"]
        benchmark = prior["benchmarks"]["equal_weight"]["net"]
        without_bracbank = prior["leave_bracbank_out"]["net"]
        metrics.update(
            {
                "combined_net_return_percent": float(combined["total_return_percent"]),
                "maximum_drawdown_percent": float(combined["maximum_drawdown_percent"]),
                "trade_count": int(combined["number_of_trades"]),
                "completed_trade_count": int(combined["completed_trades"]),
                "buy_and_hold_net_return_percent": float(benchmark["total_return_percent"]),
                "leave_bracbank_out_net_return_percent": float(
                    without_bracbank["total_return_percent"]
                ),
            }
        )
        return metrics

    def historical_run(self, run_id: str | None = None) -> ResearchRunSummary:
        momentum = self._momentum_run()
        if run_id is not None and momentum is not None and run_id == momentum.run_id:
            return momentum
        registration, contract, source_path, source, prior_path, prior = self._archived_context()
        canonical_run_id = source_path.parent.name
        if run_id is not None and run_id != canonical_run_id:
            raise LookupError(f"Historical research run not found: {run_id}")
        expected = self._expected_metrics(prior)
        principal = {
            "per_symbol_net_return_percent": {
                symbol: expected[f"per_symbol_net_return_percent.{symbol}"]
                for symbol in FIVE_SYMBOLS
            },
            "combined_net_return_percent": expected["combined_net_return_percent"],
            "maximum_drawdown_percent": expected["maximum_drawdown_percent"],
            "trade_count": expected["trade_count"],
            "completed_trade_count": expected["completed_trade_count"],
            "buy_and_hold_net_return_percent": expected["buy_and_hold_net_return_percent"],
            "leave_bracbank_out_net_return_percent": expected[
                "leave_bracbank_out_net_return_percent"
            ],
        }
        return ResearchRunSummary(
            run_id=canonical_run_id,
            strategy_identity={
                "registration_id": registration.id,
                "name": registration.strategy_id,
                "version": registration.version,
                "code_hash": registration.code_hash,
                "parameter_hash": contract["parameter_hash"],
            },
            dataset_identities=cast(dict[str, Any], contract["dataset_identities"]),
            timing_contract=cast(dict[str, Any], contract["timing_contract"]),
            costs=cast(dict[str, Any], contract["cost_assumptions"]),
            benchmark={
                "name": "equal_weight_buy_and_hold",
                "metrics": contract["baseline_results"]["equal_weight_buy_and_hold"],
            },
            principal_metrics=principal,
            verdict={
                "research_decision": str(source["decision"]["research_role"]),
                "research_role": str(registration.evidence["research_role"]),
            },
            artifact_locations=[
                _relative(source_path, self.repository_root),
                _relative(prior_path, self.repository_root),
            ],
        )

    def historical_runs(self) -> list[ResearchRunSummary]:
        runs = [self.historical_run()]
        momentum = self._momentum_run()
        if momentum is not None:
            runs.append(momentum)
        return runs

    def _momentum_run(self) -> ResearchRunSummary | None:
        registration = self.db.scalar(
            select(StrategyRegistration).where(
                StrategyRegistration.strategy_id == MOMENTUM_STRATEGY_ID,
                StrategyRegistration.version == MOMENTUM_STRATEGY_VERSION,
            )
        )
        if registration is None:
            return None
        evidence = dict(registration.evidence or {})
        raw_summary = evidence.get("minimal_v1_run_summary")
        if not isinstance(raw_summary, dict):
            raise RuntimeError("Momentum registration lacks a Minimal V1 run summary")
        summary = ResearchRunSummary.model_validate(raw_summary)
        result_path = self.repository_root / Path(summary.artifact_locations[0])
        if not result_path.is_file() or sha256_file(result_path) != evidence.get(
            "result_file_sha256"
        ):
            raise RuntimeError("Momentum canonical result identity mismatch")
        identities = cast(
            list[dict[str, Any]],
            summary.dataset_identities.get("active_research_datasets", []),
        )
        for identity in identities:
            dataset = self.db.get(ResearchDataset, str(identity["id"]))
            if (
                dataset is None
                or dataset.status != "research_dataset_active"
                or dataset.dataset_hash != identity["sha256"]
            ):
                raise RuntimeError("Momentum active dataset identity changed")
            path = _dataset_file(dataset, self.repository_root)
            if not path.is_file() or sha256_file(path) != dataset.dataset_hash:
                raise RuntimeError("Momentum active dataset file identity changed")
        return summary

    def _active_contract_datasets(
        self, contract: dict[str, Any]
    ) -> tuple[ResearchDataset, ResearchDataset]:
        identities = cast(dict[str, dict[str, Any]], contract["dataset_identities"])
        rows: list[ResearchDataset] = []
        for key in ("parent", "extension"):
            identity = identities[key]
            dataset = self.db.get(ResearchDataset, str(identity["id"]))
            if (
                dataset is None
                or dataset.name != identity["version"]
                or dataset.dataset_hash != identity["sha256"]
                or dataset.status != "research_dataset_active"
            ):
                raise RuntimeError(f"Archived {key} dataset identity changed")
            path = _dataset_file(dataset, self.repository_root)
            if not path.is_file() or sha256_file(path) != dataset.dataset_hash:
                raise RuntimeError(f"Archived {key} dataset file identity changed")
            rows.append(dataset)
        return rows[0], rows[1]

    @staticmethod
    def _actual_metrics(
        portfolio: dict[str, Any],
        summaries: dict[str, Any],
        benchmark: dict[str, Any],
        without_bracbank: dict[str, Any],
    ) -> dict[str, float | int]:
        metrics: dict[str, float | int] = {
            f"per_symbol_net_return_percent.{symbol}": float(
                summaries[symbol]["net"]["total_return_percent"]
            )
            for symbol in FIVE_SYMBOLS
        }
        combined = portfolio["net"]["metrics"]
        benchmark_metrics = benchmark["net"]["metrics"]
        without_metrics = without_bracbank["net"]["metrics"]
        metrics.update(
            {
                "combined_net_return_percent": float(combined["total_return_percent"]),
                "maximum_drawdown_percent": float(combined["maximum_drawdown_percent"]),
                "trade_count": int(combined["number_of_trades"]),
                "completed_trade_count": int(combined["completed_trades"]),
                "buy_and_hold_net_return_percent": float(benchmark_metrics["total_return_percent"]),
                "leave_bracbank_out_net_return_percent": float(
                    without_metrics["total_return_percent"]
                ),
            }
        )
        return metrics

    @staticmethod
    def _trade_rows(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"symbol": symbol, **asdict(trade)}
            for symbol in FIVE_SYMBOLS
            for trade in portfolio["net_results"][symbol].trades
        ]

    @staticmethod
    def _interpretation(
        summary: ResearchRunSummary, differences: dict[str, float], tolerance: float
    ) -> str:
        metrics = summary.principal_metrics
        return "\n".join(
            [
                "# Minimal V1 archived-result reproduction",
                "",
                "The existing trusted engine reproduced the preserved five-symbol "
                "`ma_crossover@1.0.0` baseline without changing stored evidence or operational state.",
                "",
                f"- Combined net return: {metrics['combined_net_return_percent']:.8f}%",
                f"- Maximum drawdown: {metrics['maximum_drawdown_percent']:.8f}%",
                f"- Trade events: {metrics['trade_count']}",
                f"- Buy-and-hold return: {metrics['buy_and_hold_net_return_percent']:.8f}%",
                f"- Leave-BRACBANK-out return: {metrics['leave_bracbank_out_net_return_percent']:.8f}%",
                f"- Maximum absolute metric difference: {max(map(abs, differences.values())):.12g}",
                f"- Floating-point tolerance: {tolerance:.12g}; counts require exact equality",
                f"- Verdict: `{summary.verdict['research_decision']} / {summary.verdict['research_role']}`",
                "",
                "The strategy remains rejected and may be used only as an archived comparison benchmark. "
                "This is not a profitability claim, promotion, campaign, paper session, order, or fill.",
                "",
            ]
        )

    def reproduce_archived_run(
        self, output_dir: Path, *, run_id: str | None = None
    ) -> dict[str, Any]:
        assert_paper_only_safety(self.settings)
        if not verify_audit_chain(self.db):
            raise RuntimeError("Canonical audit chain is invalid")
        registration, contract, source_path, source, prior_path, prior = self._archived_context()
        canonical_run_id = source_path.parent.name
        if run_id is not None and run_id != canonical_run_id:
            raise LookupError(f"Historical research run not found: {run_id}")
        parent, extension = self._active_contract_datasets(contract)
        bars, validation = validate_combined_datasets(
            _dataset_file(parent, self.repository_root),
            _dataset_file(extension, self.repository_root),
        )
        portfolio = run_portfolio(bars)
        benchmark = run_portfolio_buy_hold(bars)
        without_bracbank = run_portfolio(
            {symbol: values for symbol, values in bars.items() if symbol != "BRACBANK"}
        )
        summaries = symbol_summaries(portfolio, bars, {symbol: 0 for symbol in FIVE_SYMBOLS})
        actual = self._actual_metrics(portfolio, summaries, benchmark, without_bracbank)
        expected = self._expected_metrics(prior)
        tolerances = {
            key: 0.0
            if key in {"trade_count", "completed_trade_count"}
            else DEFAULT_METRIC_TOLERANCE
            for key in actual
        }
        differences = enforce_metric_tolerances(actual, expected, tolerances)
        historical = self.historical_run(canonical_run_id)
        if historical.verdict != {
            "research_decision": EXPECTED_RESEARCH_DECISION,
            "research_role": EXPECTED_RESEARCH_ROLE,
        }:
            raise RuntimeError("Archived verdict compatibility failure")
        reproduction_id = f"minimal-v1-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
        artifacts = {
            "json": output_dir / "research_result.json",
            "csv": output_dir / "trade_ledger.csv",
            "markdown": output_dir / "interpretation.md",
        }
        summary = historical.model_copy(
            update={
                "run_id": reproduction_id,
                "principal_metrics": {
                    "per_symbol_net_return_percent": {
                        symbol: actual[f"per_symbol_net_return_percent.{symbol}"]
                        for symbol in FIVE_SYMBOLS
                    },
                    **{
                        key: value
                        for key, value in actual.items()
                        if not key.startswith("per_symbol")
                    },
                },
                "artifact_locations": [
                    _relative(artifacts["json"], self.repository_root),
                    _relative(artifacts["csv"], self.repository_root),
                    _relative(artifacts["markdown"], self.repository_root),
                ],
            }
        )
        output_dir.mkdir(parents=True, exist_ok=False)
        trade_rows = self._trade_rows(portfolio)
        with artifacts["csv"].open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["symbol", "timestamp", "side", "quantity", "price", "fee", "slippage"],
            )
            writer.writeheader()
            writer.writerows(trade_rows)
        interpretation = self._interpretation(summary, differences, DEFAULT_METRIC_TOLERANCE)
        artifacts["markdown"].write_text(interpretation, encoding="utf-8")
        artifact_hashes = {
            name: {
                "path": _relative(path, self.repository_root),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in artifacts.items()
            if name != "json"
        }
        audit = audit_status(self.db)
        git_root = (
            self.repository_root if (self.repository_root / ".git").exists() else REPOSITORY_ROOT
        )
        provenance = {
            "source_run_id": canonical_run_id,
            "source_result": _relative(source_path, self.repository_root),
            "source_result_sha256": sha256_file(source_path),
            "five_symbol_result": _relative(prior_path, self.repository_root),
            "five_symbol_result_sha256": sha256_file(prior_path),
            "strategy_registration_id": registration.id,
            "dataset_file_hashes": validation["source_file_hashes"],
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=git_root, text=True
            ).strip(),
            "database_role": self.settings.DATABASE_ROLE,
            "canonical_audit_chain_id": audit.get("canonical_chain_id"),
            "canonical_audit_valid": audit.get("canonical_valid", False),
            "engine": "app.services.five_symbol_robustness",
            "timing": contract["timing_contract"],
            "costs": contract["cost_assumptions"],
        }
        result: dict[str, Any] = {
            "summary": summary.model_dump(mode="json"),
            "compatibility": {
                "passed": True,
                "expected": expected,
                "actual": actual,
                "differences": differences,
                "tolerances": tolerances,
                "verdict_exact_match": True,
            },
            "provenance": provenance,
            "artifact_hashes": artifact_hashes,
            "operational_effect": False,
            "no_real_money_authorization": True,
        }
        result["canonical_payload_sha256"] = _canonical_sha256(result)
        artifacts["json"].write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        return {
            "summary": summary,
            "metric_differences": differences,
            "output_dir": output_dir,
            "artifacts": {name: path for name, path in artifacts.items()},
            "json_sha256": sha256_file(artifacts["json"]),
            "trade_rows": len(trade_rows),
        }

from __future__ import annotations

import sys

if __package__ in {None, ""}:
    sys.path.pop(0)

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from scripts.run_defensive_low_volatility_research import (  # type: ignore[import-untyped] # noqa: E402
    _git,
    _protected_counts,
    _write_ledger,
)

from app.core.config import assert_paper_only_safety, get_settings  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.models import StrategyRegistration  # noqa: E402
from app.services.absolute_momentum_filter import (  # noqa: E402
    PRIMARY_PARAMETERS,
    STRATEGY_ID,
    STRATEGY_IDENTITY,
    STRATEGY_VERSION,
    UNIVERSE,
    build_research_bundle,
    code_hash,
    deterministic_registration_id,
    parameter_hash,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain  # noqa: E402
from app.services.cross_sectional_momentum import load_active_universe  # noqa: E402
from app.services.historical_strategy_research import sha256_file  # noqa: E402
from app.services.minimal_v1 import MinimalV1Facade  # noqa: E402
from app.services.strategy_research_archival import canonical_hash  # noqa: E402

AUTHORIZATION = (
    "Operator requested absolute_momentum_filter@0.1.0 as the final predeclared "
    "historical strategy family before strategy discovery is frozen."
)


def _sqlite_backup() -> tuple[Path, Path]:
    if engine.dialect.name != "sqlite" or not engine.url.database:
        raise RuntimeError(
            "This bounded registration runner requires the canonical SQLite database"
        )
    source = Path(str(engine.url.database)).resolve()
    backup = ROOT / "data" / "backups" / "absolute_momentum_filter_pre_run.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as original, sqlite3.connect(backup) as target:
        original.backup(target)
    return source, backup


def _archived_context(facade: MinimalV1Facade) -> dict[str, Any]:
    expected = {
        "ma_crossover": "archived_ma_crossover",
        "cross_sectional_momentum": "archived_cross_sectional_momentum",
        "defensive_low_volatility": "defensive_low_volatility",
    }
    contexts: dict[str, Any] = {}
    for run in facade.historical_runs():
        name = str(run.strategy_identity.get("name") or run.strategy_identity.get("strategy_id"))
        if name in expected:
            contexts[expected[name]] = {
                "strategy_identity": run.strategy_identity,
                "metrics": run.principal_metrics,
                "verdict": run.verdict,
            }
    if set(contexts) != set(expected.values()):
        raise RuntimeError("Required historical strategy contexts are unavailable")
    return contexts


def _markdown(payload: dict[str, Any]) -> str:
    primary = payload["primary"]["metrics"]
    walk = payload["walk_forward"]
    variants = payload["sensitivity_variants"]
    lines = [
        "# Absolute momentum filter research interpretation",
        "",
        "Historical research only. Qualification remains 0/60; promotion and operational "
        "execution are prohibited.",
        "",
        f"Verdict: `{payload['research_verdict']}`.",
        "",
        f"Net return {primary['total_return_percent']:.4f}%; annualized return "
        f"{primary['annualized_return_percent']:.4f}%; maximum drawdown "
        f"{primary['maximum_drawdown_percent']:.4f}%; Sharpe "
        f"{primary['sharpe_ratio'] if primary['sharpe_ratio'] is not None else 'n/a'}.",
        f"Average invested exposure {primary['average_invested_exposure_percent']:.2f}%; "
        f"average cash exposure {primary['average_cash_exposure_percent']:.2f}%; "
        f"all-cash periods {primary['all_cash_period_count']}.",
        f"Positive chronological holdouts: {walk['positive_holdouts']}/"
        f"{walk['holdout_count']}; combined holdout "
        f"{walk['combined_holdout']['metrics']['total_return_percent']:.4f}%.",
        "Sensitivity returns: "
        + ", ".join(
            f"{name}={result['metrics']['total_return_percent']:.4f}%"
            for name, result in variants.items()
        )
        + ".",
        "",
        "Historical strategy-family discovery is now frozen. No further strategy family "
        "may be proposed automatically.",
        f"Freeze decision: `{payload['historical_strategy_family_discovery']['decision']}`.",
        "",
        "This is not real-market evidence, a profitability claim, strategy promotion, or "
        "authorization to create campaigns, sessions, signals, orders, transactions, or fills.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = get_settings()
    assert_paper_only_safety(settings)
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Tracked worktree must be clean before research registration")
    head = _git("rev-parse", "HEAD")
    output: Path | None = None
    source_db, backup_db = _sqlite_backup()
    try:
        with SessionLocal() as db:
            if not verify_audit_chain(db):
                raise RuntimeError("Canonical audit chain invalid before research")
            if db.scalar(
                select(StrategyRegistration).where(
                    StrategyRegistration.strategy_id == STRATEGY_ID,
                    StrategyRegistration.version == STRATEGY_VERSION,
                )
            ):
                raise RuntimeError(f"{STRATEGY_IDENTITY} is already registered")
            before = _protected_counts(db)
            bars, validation, datasets = load_active_universe(db, ROOT)
            facade = MinimalV1Facade(db, repository_root=ROOT)
            archived = _archived_context(facade)
            chain = audit_status(db)

        code_sha = code_hash(ROOT)
        parameter_sha = parameter_hash()
        registration_id = deterministic_registration_id(
            code_sha256=code_sha,
            parameter_sha256=parameter_sha,
            datasets=datasets,
        )
        run_id = f"absolute-momentum-filter-{registration_id.split('-')[0]}"
        output = ROOT / "reports" / "strategy_research" / run_id
        if output.exists():
            raise RuntimeError(f"Deterministic output already exists: {output}")
        output.mkdir(parents=True)
        identity = {
            "registration_id": registration_id,
            "strategy_id": STRATEGY_ID,
            "version": STRATEGY_VERSION,
            "identity": STRATEGY_IDENTITY,
            "lifecycle": "research",
            "code_hash": code_sha,
            "canonical_parameter_hash": parameter_sha,
            "git_commit": head,
            "promotion_permission": False,
            "campaign_eligibility": False,
            "external_execution_permission": False,
        }
        bundle = build_research_bundle(
            bars,
            dataset_identities=datasets,
            validation=validation,
            registration_identity=identity,
            archived_context=archived,
        )
        ledger_path = output / "trade_rebalance_ledger.csv"
        markdown_path = output / "interpretation.md"
        result_path = output / "research_result.json"
        _write_ledger(ledger_path, bundle.ledger)
        markdown_path.write_text(_markdown(bundle.payload), encoding="utf-8")
        preliminary_hashes = {
            "canonical_json_payload_sha256": canonical_hash(bundle.payload),
            "trade_rebalance_ledger.csv": sha256_file(ledger_path),
            "interpretation.md": sha256_file(markdown_path),
        }

        with SessionLocal() as db:
            registration = StrategyRegistration(
                id=registration_id,
                strategy_id=STRATEGY_ID,
                version=STRATEGY_VERSION,
                lifecycle_state="research",
                code_hash=code_sha,
                parameters=dict(PRIMARY_PARAMETERS),
                data_requirements={
                    "universe": list(UNIVERSE),
                    "active_dataset_ids_and_hashes": [
                        {"id": item["id"], "sha256": item["sha256"]} for item in datasets
                    ],
                    "adjusted_execution_data": True,
                    "complete_lineage": True,
                    "common_source_present_calendar": True,
                },
                evidence={
                    "parameter_hash": parameter_sha,
                    "authorization_text": AUTHORIZATION,
                    "authorization_sha256": canonical_hash(AUTHORIZATION),
                    "git_commit": head,
                    "audit_chain_id": chain["canonical_chain_id"],
                    "promotion_authorized": False,
                    "campaign_eligibility": False,
                    "research_execution_authorized": False,
                    "execution_authorization": False,
                    "external_execution_permission": False,
                    "historical_strategy_family_discovery": "frozen_after_this_run",
                    "qualification": "0/60",
                    "audit_event_ids": [],
                },
                minimum_sample_size=252,
                operator_approval=(
                    "Authorized for this deterministic historical research run only; no "
                    "operational or real-money execution authorization."
                ),
                suspension_reason="Promotion and paper-campaign eligibility prohibited.",
                created_at=datetime.now(UTC),
            )
            db.add(registration)
            db.flush()
            shared = {
                "strategy": STRATEGY_IDENTITY,
                "registration_id": registration_id,
                "run_id": run_id,
                "code_hash": code_sha,
                "parameter_hash": parameter_sha,
                "dataset_ids_and_hashes": [
                    {"id": item["id"], "sha256": item["sha256"]} for item in datasets
                ],
                "qualification": "0/60",
            }
            event_ids: list[str] = []
            for event_type, state in (
                ("strategy.artifact_hashes_verified", {**shared, **preliminary_hashes}),
                ("strategy.research_registration_created", {**shared, "lifecycle": "research"}),
                (
                    "strategy.research_execution_authorized",
                    {**shared, "scope": "this_historical_research_run_only"},
                ),
                ("strategy.research_pre_run_validated", {**shared, **validation}),
                (
                    "strategy.research_execution_completed",
                    {
                        **shared,
                        "operational_trading_entities_created": False,
                        "single_run_authorization_consumed": True,
                    },
                ),
                (
                    "strategy.research_verdict_recorded",
                    {**shared, "research_verdict": bundle.payload["research_verdict"]},
                ),
                (
                    "strategy.promotion_prohibited",
                    {
                        **shared,
                        "promotion_permission": False,
                        "campaign_eligibility": False,
                        "historical_strategy_family_discovery": "frozen_after_this_run",
                    },
                ),
            ):
                event = append_audit(
                    db,
                    actor="operator",
                    event_type=event_type,
                    entity_type="strategy_registration",
                    entity_id=registration_id,
                    new_state=state,
                )
                event_ids.append(event.id)

            bundle.payload["audit_linkage"] = {
                "canonical_chain_id": chain["canonical_chain_id"],
                "event_ids": event_ids,
            }
            bundle.payload["artifact_hashes"] = preliminary_hashes
            result_path.write_text(
                json.dumps(bundle.payload, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            result_sha = sha256_file(result_path)
            summary = {
                "run_id": run_id,
                "strategy_identity": identity,
                "dataset_identities": {"active_research_datasets": datasets},
                "timing_contract": bundle.payload["timing_contract"],
                "costs": bundle.payload["cost_contract"],
                "benchmark": {
                    "name": "equal_weight_buy_and_hold",
                    "metrics": bundle.payload["benchmarks"]["equal_weight_buy_and_hold"]["metrics"],
                },
                "principal_metrics": bundle.payload["primary"]["metrics"],
                "verdict": {
                    "research_decision": bundle.payload["research_verdict"],
                    "research_role": "historical_research_only",
                },
                "artifact_locations": [
                    result_path.relative_to(ROOT).as_posix(),
                    ledger_path.relative_to(ROOT).as_posix(),
                    markdown_path.relative_to(ROOT).as_posix(),
                ],
            }
            registration.evidence = {
                **registration.evidence,
                "research_verdict": bundle.payload["research_verdict"],
                "artifact_hashes": {**preliminary_hashes, "research_result.json": result_sha},
                "result_file_sha256": result_sha,
                "minimal_v1_run_summary": summary,
                "audit_event_ids": event_ids,
            }
            event = append_audit(
                db,
                actor="operator",
                event_type="strategy.research_evidence_generated",
                entity_type="strategy_registration",
                entity_id=registration_id,
                new_state={
                    **shared,
                    "artifact_hashes": registration.evidence["artifact_hashes"],
                    "artifact_count": 3,
                    "research_execution_authorized": False,
                    "historical_strategy_family_discovery": "frozen",
                },
            )
            registration.evidence = {
                **registration.evidence,
                "audit_event_ids": [*event_ids, event.id],
            }
            db.commit()
            if not verify_audit_chain(db):
                raise RuntimeError("Canonical audit invalid after research registration")
            if _protected_counts(db) != before:
                raise RuntimeError("Operational trading entity count changed")
            visible = MinimalV1Facade(db, repository_root=ROOT).historical_run(run_id)
            if visible.run_id != run_id:
                raise RuntimeError("Minimal V1 run visibility failed")

        backup_db.unlink(missing_ok=True)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "registration_id": registration_id,
                    "verdict": bundle.payload["research_verdict"],
                    "research_discovery": "frozen",
                    "result": str(result_path),
                    "result_sha256": sha256_file(result_path),
                    "protected_counts": before,
                },
                indent=2,
            )
        )
    except Exception:
        engine.dispose()
        if backup_db.is_file():
            shutil.copy2(backup_db, source_db)
        if output is not None and output.is_dir():
            shutil.rmtree(output)
        raise


if __name__ == "__main__":
    main()

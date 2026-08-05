from __future__ import annotations

import sys

if __package__ in {None, ""}:
    sys.path.pop(0)

import csv
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import assert_paper_only_safety, get_settings  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    CampaignDay,
    Order,
    PaperSession,
    PaperSessionRun,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain  # noqa: E402
from app.services.cross_sectional_momentum import (  # noqa: E402
    PRIMARY_PARAMETERS,
    STRATEGY_ID,
    STRATEGY_IDENTITY,
    STRATEGY_VERSION,
    UNIVERSE,
    build_research_bundle,
    canonical_hash,
    code_hash,
    deterministic_registration_id,
    load_active_universe,
    parameter_hash,
)
from app.services.historical_strategy_research import sha256_file  # noqa: E402
from app.services.minimal_v1 import MinimalV1Facade  # noqa: E402

PROTECTED = (
    ValidationCampaign,
    CampaignDay,
    PaperSession,
    PaperSessionRun,
    Signal,
    Order,
    Transaction,
)
AUTHORIZATION = (
    "I explicitly approve this exact five-symbol T2 research dataset activation and its "
    "stated registry/audit mutations."
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _protected_counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(db.scalar(select(func.count()).select_from(model)) or 0)
        for model in PROTECTED
    }


def _write_ledger(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "experiment",
        "event_type",
        "signal_timestamp",
        "execution_timestamp",
        "symbol",
        "side",
        "quantity",
        "source_open",
        "fill_price",
        "fee",
        "slippage",
        "cash_after",
        "selected",
        "target_weights",
        "ranking",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "selected": json.dumps(row["selected"], separators=(",", ":")),
                    "target_weights": json.dumps(
                        row["target_weights"], sort_keys=True, separators=(",", ":")
                    ),
                    "ranking": json.dumps(row["ranking"], separators=(",", ":")),
                }
            )


def _markdown(payload: dict[str, Any]) -> str:
    primary = payload["primary"]["metrics"]
    benchmarks = payload["benchmarks"]
    variants = payload["sensitivity_variants"]
    walk = payload["walk_forward"]
    dependence = payload["dependence_tests"]
    lines = [
        "# Cross-sectional momentum research interpretation",
        "",
        "Historical research only. Qualification remains 0/60; promotion and operational "
        "execution are prohibited.",
        "",
        f"Verdict: `{payload['research_verdict']}`.",
        "",
        "## Primary",
        "",
        f"Net return {primary['total_return_percent']:.4f}%; annualized return "
        f"{primary['annualized_return_percent']:.4f}%; maximum drawdown "
        f"{primary['maximum_drawdown_percent']:.4f}%; Sharpe "
        f"{primary['sharpe_ratio'] if primary['sharpe_ratio'] is not None else 'n/a'}.",
        f"Turnover {primary['turnover']:.4f}; fees BDT {primary['total_fees_bdt']:.2f}; "
        f"slippage BDT {primary['total_slippage_bdt']:.2f}; average invested exposure "
        f"{primary['average_invested_exposure_percent']:.2f}%.",
        "",
        "## Robustness",
        "",
        f"Positive chronological holdouts: {walk['positive_holdouts']}/"
        f"{walk['holdout_count']} (fixed parameters; no holdout optimization).",
        "Sensitivity returns: "
        + ", ".join(
            f"{name}={result['metrics']['total_return_percent']:.4f}%"
            for name, result in variants.items()
        )
        + ".",
        "Benchmark returns: "
        + ", ".join(
            f"{name}={result['metrics']['total_return_percent']:.4f}%"
            for name, result in benchmarks.items()
        )
        + ".",
        f"Largest absolute symbol contributor: "
        f"{dependence['one_symbol']['largest_absolute_contributor']} "
        f"({dependence['one_symbol']['largest_absolute_contribution_share']:.2%}).",
        "",
        "This result is not real-market evidence, a profitability claim, strategy promotion, "
        "or authorization to create a campaign, session, proposal, order, transaction, or fill.",
    ]
    return "\n".join(lines) + "\n"


def _sqlite_backup() -> tuple[Path, Path]:
    if engine.dialect.name != "sqlite" or not engine.url.database:
        raise RuntimeError(
            "This bounded registration runner requires the canonical SQLite database"
        )
    source = Path(str(engine.url.database)).resolve()
    backup = ROOT / "data" / "backups" / "cross_sectional_momentum_pre_run.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as original, sqlite3.connect(backup) as target:
        original.backup(target)
    return source, backup


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
            archived = (
                MinimalV1Facade(db, repository_root=ROOT).historical_run().model_dump(mode="json")
            )
            chain = audit_status(db)

        code_sha = code_hash(ROOT)
        parameter_sha = parameter_hash()
        registration_id = deterministic_registration_id(
            code_sha256=code_sha,
            parameter_sha256=parameter_sha,
            datasets=datasets,
        )
        run_id = f"cross-sectional-momentum-{registration_id.split('-')[0]}"
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
            "paper_campaign_eligibility": False,
            "execution_outside_authorized_historical_run": False,
        }
        bundle = build_research_bundle(
            bars,
            dataset_identities=datasets,
            validation=validation,
            registration_identity=identity,
            archived_ma_context=archived,
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
                    "research_execution_authorized": True,
                    "execution_authorization": False,
                    "execution_outside_authorized_historical_run": False,
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
                (
                    "strategy.research_registration_created",
                    {**shared, "lifecycle": "research"},
                ),
                (
                    "strategy.research_execution_authorized",
                    {**shared, "scope": "this_historical_research_run_only"},
                ),
                ("strategy.research_pre_run_validated", {**shared, **validation}),
                (
                    "strategy.research_execution_completed",
                    {**shared, "operational_trading_entities_created": False},
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
                "research_execution_authorized": False,
                "artifact_hashes": {
                    **preliminary_hashes,
                    "research_result.json": result_sha,
                },
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
                    "single_run_authorization_consumed": True,
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

from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import csv
import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    PaperSession,
    PaperSessionRun,
    ResearchDataset,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import audit_status, verify_audit_chain  # noqa: E402
from app.services.pilot_research_extension import (  # noqa: E402
    ACTIVE_STATUS,
    BLOCKED_SYMBOLS,
    EXPECTED_ACTIVE_COUNTS,
    EXPECTED_EXCLUSIONS,
    FIVE_SYMBOL_UNIVERSE,
    PACK_MANIFEST_HASH,
    TARGET_SYMBOLS,
    TRANSFORMATION_VERSION,
    assert_strategy_not_promoted,
    build_extension_rows,
    build_five_symbol_research_plan,
    canonical_hash,
    create_extension_dataset_record,
    decision_specs,
    record_decision,
    verify_reconciliation_pack,
    write_jsonl,
)

PACK_DIR = (
    ROOT
    / "reports"
    / "pilot_final_disposition"
    / "pilot_final_7e9fccd005d9225089a70dbc"
)
CANDIDATE_DATABASE = (
    ROOT
    / "reports"
    / "research_data_quality"
    / "canonical_candidate_0a834213759f5a79"
    / "canonical_candidate.sqlite3"
)
PROTECTED_MODELS = (
    ValidationCampaign,
    PaperSession,
    PaperSessionRun,
    Signal,
    Order,
    Transaction,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_fingerprint(path: Path) -> dict[str, Any]:
    components = {"database": _sha256(path)}
    wal = Path(f"{path}-wal")
    if wal.is_file() and wal.stat().st_size:
        components["wal"] = _sha256(wal)
    return {"fingerprint": canonical_hash(components), "components": components}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({field for row in rows for field in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            {
                field: json.dumps(value, sort_keys=True, default=str)
                if isinstance(value, (dict, list, tuple))
                else value
                for field, value in row.items()
            }
            for row in rows
        )


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _protected_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__tablename__: int(
                db.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in PROTECTED_MODELS
        }


def _strategy_states() -> dict[str, str]:
    with SessionLocal() as db:
        return {
            f"{row.strategy_id}@{row.version}": row.lifecycle_state
            for row in db.scalars(select(StrategyRegistration))
        }


def _dataset_snapshot(dataset: ResearchDataset) -> dict[str, Any]:
    return {
        "id": dataset.id,
        "name": dataset.name,
        "symbols": dataset.symbols,
        "dataset_hash": dataset.dataset_hash,
        "source_hash": dataset.source_hash,
        "status": dataset.status,
        "quality_report": dataset.quality_report,
        "normalized_file_path": dataset.normalized_file_path,
    }


def _parent_dataset(db: Session) -> ResearchDataset:
    candidates = list(
        db.scalars(
            select(ResearchDataset).where(
                ResearchDataset.status == "research_dataset_active"
            )
        )
    )
    parents = [
        dataset
        for dataset in candidates
        if set(dataset.symbols) == {"GP", "ACI", "BRACBANK"}
        and dataset.quality_report.get("classification") == ACTIVE_STATUS
    ]
    if len(parents) != 1:
        raise RuntimeError("Expected exactly one active GP/ACI/BRACBANK parent dataset")
    return parents[0]


def _assert_no_existing_target_keys(datasets: list[ResearchDataset]) -> None:
    for dataset in datasets:
        if dataset.status != "research_dataset_active":
            continue
        path = Path(dataset.normalized_file_path)
        if not path.is_file():
            raise RuntimeError(
                f"Active research dataset file is missing: {dataset.name}"
            )
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("symbol") in TARGET_SYMBOLS:
                    raise RuntimeError(
                        f"Target symbol already exists in active dataset: {dataset.name}"
                    )


def _load_observations(path: Path) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM observations WHERE normalized_symbol IN (?,?)",
            TARGET_SYMBOLS,
        )
        return {str(row["source_row_id"]): dict(row) for row in rows}


def _finalize_manifest(output: Path) -> dict[str, Any]:
    files = [
        path
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "manifest.json"
    ]
    manifest: dict[str, Any] = {
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in files
        ],
        "pack_manifest_hash": PACK_MANIFEST_HASH,
        "classification": ACTIVE_STATUS,
        "strategy_execution": False,
        "qualification": "0/60",
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    return manifest


def _markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# BATBC/SQURPHARMA T2 research extension",
        "",
        "Classification: **RESEARCH DATASET ACTIVE**. This is research-only data; it is not exchange-verified, officially lifecycle-verified, paper-candidate, strategy-approved, production-ready, or real-money-ready.",
        "",
        "## Activated rows",
        "",
        f"- BATBC: {summary['active_by_symbol']['BATBC']:,}",
        f"- SQURPHARMA: {summary['active_by_symbol']['SQURPHARMA']:,}",
        f"- total: {summary['active_rows']:,}",
        "- allowed disposition: `tier_2_single_source_high_quality` only",
        "",
        "## Exclusions",
        "",
        f"- T3: BATBC {EXPECTED_EXCLUSIONS['BATBC']['tier_3_research_only']:,}; SQURPHARMA {EXPECTED_EXCLUSIONS['SQURPHARMA']['tier_3_research_only']:,}",
        "- invalid: BATBC 15; SQURPHARMA 15",
        "- duplicate conflict: BATBC 14; SQURPHARMA 14",
        "- lifecycle-held: SQURPHARMA 1; BATBC 0",
        f"- blocked symbols remain inactive: {', '.join(BLOCKED_SYMBOLS)}",
        "",
        "## Boundaries",
        "",
        "Observed research window: 2012-10-01 through 2026-01-22. This is not an official listing-date claim. Lifecycle evidence remains pending. Mendeley known-grain data is primary; DSEStocks and AmarStock are validation references where present. Independence is not claimed, values are not averaged, and corporate actions or missing dates are not reconstructed.",
        "",
        "## Strategy boundary",
        "",
        f"The unexecuted five-symbol plan covers {', '.join(FIVE_SYMBOL_UNIVERSE)}. No strategy, campaign, session, proposal, order, transaction, or fill was created.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Activate the authorized BATBC/SQURPHARMA T2 research extension"
    )
    parser.add_argument("--operator", required=True)
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--pack-dir", type=Path, default=PACK_DIR)
    parser.add_argument("--candidate-db", type=Path, default=CANDIDATE_DATABASE)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "reports" / "pilot_research_extension",
    )
    args = parser.parse_args()
    if _head() != args.expected_head:
        raise RuntimeError("Pinned Git HEAD mismatch")
    settings = get_settings()
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Research extension requires paper/false/disabled safety")
    for required in (
        args.authorization_file,
        args.candidate_db,
        args.pack_dir / "manifest.json",
        args.pack_dir / "final_row_dispositions.json",
    ):
        if not required.is_file():
            raise FileNotFoundError(required)

    verify_reconciliation_pack(args.pack_dir)
    dispositions = json.loads(
        (args.pack_dir / "final_row_dispositions.json").read_text(encoding="utf-8")
    )
    observations = _load_observations(args.candidate_db)
    authorization_text = args.authorization_file.read_text(encoding="utf-8")
    authorization_sha256 = hashlib.sha256(authorization_text.encode()).hexdigest()
    database_fingerprint = _database_fingerprint(args.candidate_db)
    candidate_db_hash = str(database_fingerprint["fingerprint"])
    version_seed = {
        "authorization_sha256": authorization_sha256,
        "candidate_database_sha256": candidate_db_hash,
        "candidate_database_fingerprint_components": database_fingerprint["components"],
        "pack_manifest_hash": PACK_MANIFEST_HASH,
        "transformation_version": TRANSFORMATION_VERSION,
        "git_head": args.expected_head,
    }
    version = "batbc-squrpharma-t2-extension-" + canonical_hash(version_seed)[:16]
    draft_version = "pilot-extension-" + canonical_hash(version_seed)[:16]
    dataset_path = ROOT / "data" / "research_datasets" / f"{version}.jsonl"
    output = args.output_root / version
    if dataset_path.exists() or output.exists():
        raise RuntimeError("Extension output already exists")

    before = _protected_counts()
    strategies_before = _strategy_states()
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        assert_strategy_not_promoted(db)
        audit_before = audit_status(db)
        parent = _parent_dataset(db)
        parent_before = _dataset_snapshot(parent)
        datasets_before = list(db.scalars(select(ResearchDataset)))
        initial_dataset_count = len(datasets_before)
        _assert_no_existing_target_keys(datasets_before)

    placeholder_ids = {
        spec["key"]: spec["key"]
        for spec in decision_specs(authorization_sha256, version=version)
    }
    _, dry_summary = build_extension_rows(
        dispositions,
        observations,
        activation_timestamp="preactivation-validation",
        human_decision_ids=placeholder_ids,
        audit_event_ids=placeholder_ids,
    )
    if dry_summary["active_by_symbol"] != EXPECTED_ACTIVE_COUNTS:
        raise RuntimeError(
            "Pre-activation row counts differ from the authorized maximum"
        )

    decisions: dict[str, Any] = {}
    for spec in decision_specs(authorization_sha256, version=version):
        with SessionLocal() as db:
            approval = record_decision(
                db,
                spec=spec,
                draft_version=draft_version,
                operator_identity=args.operator,
            )
        decisions[spec["key"]] = approval
    human_ids = {key: value.id for key, value in decisions.items()}
    audit_ids = {key: str(value.audit_event_id) for key, value in decisions.items()}
    activation_timestamp = datetime.now(UTC).isoformat()
    rows, summary = build_extension_rows(
        dispositions,
        observations,
        activation_timestamp=activation_timestamp,
        human_decision_ids=human_ids,
        audit_event_ids=audit_ids,
    )
    dataset_hash = write_jsonl(dataset_path, rows)
    if _sha256(dataset_path) != dataset_hash:
        raise RuntimeError("Written dataset hash verification failed")

    final_specs = decision_specs(
        authorization_sha256, version=version, dataset_hash=dataset_hash
    )[-2:]
    for spec in final_specs:
        with SessionLocal() as db:
            approval = record_decision(
                db,
                spec=spec,
                draft_version=draft_version,
                operator_identity=args.operator,
            )
        decisions[spec["key"]] = approval
    source_bundle_hash = canonical_hash(
        {
            "candidate_database_sha256": candidate_db_hash,
            "pack_manifest_hash": PACK_MANIFEST_HASH,
            "symbols": TARGET_SYMBOLS,
            "transformation_version": TRANSFORMATION_VERSION,
        }
    )
    with SessionLocal() as db:
        parent_record = db.get(ResearchDataset, parent_before["id"])
        if parent_record is None or _dataset_snapshot(parent_record) != parent_before:
            raise RuntimeError("Parent research dataset changed")
        activation_approval = decisions["dataset_activation"]
        dataset = create_extension_dataset_record(
            db,
            version=version,
            dataset_path=dataset_path,
            dataset_hash=dataset_hash,
            source_bundle_hash=source_bundle_hash,
            candidate_db_path=args.candidate_db,
            candidate_db_hash=candidate_db_hash,
            parent_dataset=parent_record,
            summary=summary,
            decisions=decisions,
            activation_approval=activation_approval,
            git_head=args.expected_head,
            audit_chain_id=str(audit_before["canonical_chain_id"]),
            operator_identity=args.operator,
        )
        extension_snapshot = _dataset_snapshot(dataset)
        plan = build_five_symbol_research_plan(
            parent_dataset=parent_record, extension_dataset=dataset
        )
        assert_strategy_not_promoted(db)
        audit_after = audit_status(db)
        final_dataset_count = int(
            db.scalar(select(func.count()).select_from(ResearchDataset)) or 0
        )

    after = _protected_counts()
    strategies_after = _strategy_states()
    if before != after or strategies_before != strategies_after:
        raise RuntimeError("Operational trading or strategy state changed")
    if (
        final_dataset_count != initial_dataset_count + 1
        or not audit_after["canonical_valid"]
    ):
        raise RuntimeError("Dataset registration or audit verification failed")
    with SessionLocal() as db:
        parent_after = db.get(ResearchDataset, parent_before["id"])
        if parent_after is None or _dataset_snapshot(parent_after) != parent_before:
            raise RuntimeError("Parent research dataset was modified")

    output.mkdir(parents=True, exist_ok=False)
    decision_result = {
        key: {
            "id": approval.id,
            "status": approval.approval_status,
            "audit_event_id": approval.audit_event_id,
        }
        for key, approval in decisions.items()
    }
    result = {
        "classification": ACTIVE_STATUS,
        "dataset_id": version,
        "registry_id": extension_snapshot["id"],
        "version": version,
        "dataset_hash": dataset_hash,
        "source_bundle_hash": source_bundle_hash,
        "dataset_path": str(dataset_path),
        "parent_dataset": parent_before,
        "candidate_database_sha256": candidate_db_hash,
        "candidate_database_fingerprint_components": database_fingerprint["components"],
        "pack_manifest_hash": PACK_MANIFEST_HASH,
        "transformation_version": TRANSFORMATION_VERSION,
        "git_head": args.expected_head,
        "audit_chain_id": audit_after["canonical_chain_id"],
        "summary": summary,
        "decisions": decision_result,
        "strategy_plan_status": plan["status"],
        "strategy_execution": False,
        "qualification": "0/60",
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_states_before": strategies_before,
        "strategy_states_after": strategies_after,
        "audit_before": audit_before,
        "audit_after": audit_after,
    }
    _write_json(output / "activation_result.json", result)
    _write_json(output / "five_symbol_research_plan.json", plan)
    _write_json(output / "decision_records.json", decision_result)
    _write_csv(
        output / "decision_records.csv",
        [{"key": key, **value} for key, value in decision_result.items()],
    )
    _write_json(
        output / "authorization_record.json",
        {
            "authorization_text": authorization_text,
            "authorization_sha256": authorization_sha256,
            "operator_identity": args.operator,
            "recorded_at": activation_timestamp,
            "decision_ids": sorted(value.id for value in decisions.values()),
        },
    )
    markdown = _markdown(result)
    (output / "activation_report.md").write_text(markdown, encoding="utf-8")
    manifest = _finalize_manifest(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "dataset_id": version,
                "registry_id": extension_snapshot["id"],
                "dataset_hash": dataset_hash,
                "active_rows": summary["active_rows"],
                "manifest_hash": manifest["manifest_hash"],
                "strategy_execution": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

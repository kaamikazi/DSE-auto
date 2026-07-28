from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    PaperSession,
    ResearchDataset,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import audit_status, verify_audit_chain  # noqa: E402
from app.services.research_subset_activation import (  # noqa: E402
    ACTIVE_STATUS,
    ACTIVE_SYMBOLS,
    EXPECTED_PACK_HASH,
    TRANSFORMATION_VERSION,
    assert_strategy_not_promoted,
    build_active_rows,
    build_execution_plan,
    canonical_hash,
    create_research_dataset_record,
    decision_specs,
    record_decision,
    verify_approval_pack,
    write_jsonl,
)
from app.services.target_research_review import (  # noqa: E402
    build_target_subset,
    open_candidate_database,
)

from scripts.build_target_symbol_human_review import _source_metadata  # noqa: E402

PROTECTED_MODELS = (ValidationCampaign, PaperSession, Order, Transaction)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


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
        "portable_html": "blocked_reader_fallback_during_static_chart_extraction",
    }
    manifest["report_hash"] = canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    return manifest


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _protected_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__tablename__: int(
                db.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in PROTECTED_MODELS
        }


def _artifact(result: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "symbol": symbol,
            "rows": coverage["rows"],
            "start": coverage["start"],
            "end": coverage["end"],
            "classification": ACTIVE_STATUS,
        }
        for symbol, coverage in result["summary"]["coverage"].items()
    ]
    decisions = [
        {
            "decision": key,
            "status": value["status"],
            "audit_event_id": value["audit_event_id"],
        }
        for key, value in result["decisions"].items()
    ]
    source = {
        "id": "activation_result",
        "label": "Validated research-subset activation result",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "sql": (
                "SELECT name, json_extract(quality_report, '$.active_rows') AS active_rows, "
                "json_extract(quality_report, '$.qualification') AS qualification, "
                "json_extract(quality_report, '$.coverage.GP.rows') AS gp_rows, "
                "json_extract(quality_report, '$.coverage.ACI.rows') AS aci_rows, "
                "json_extract(quality_report, '$.coverage.BRACBANK.rows') AS bracbank_rows "
                "FROM research_datasets WHERE name = '" + result["version"] + "'"
            ),
            "description": "Deterministic filtered projection of the preserved canonical-candidate ledger.",
            "tables_used": [
                "canonical_candidate.observations",
                "governance_item_approvals",
                "research_datasets",
            ],
            "filters": [
                "GP/ACI/BRACBANK",
                "approvable_after_human_decision",
                "quality tiers 1-3",
            ],
            "metric_definitions": [
                "Rows count immutable JSONL records after all authorized exclusions."
            ],
            "executed_at": result["activation_timestamp"],
        },
        "notes": "Quantitative chart selected because symbol-level active-row mix is material; exact decisions remain tabular.",
    }
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "Research-only target dataset activation",
            "description": "Technical activation evidence for the GP, ACI, and BRACBANK research subset.",
            "generatedAt": result["activation_timestamp"],
            "cards": [
                {
                    "id": "active_rows",
                    "dataset": "summary",
                    "sourceId": "activation_result",
                    "metrics": [
                        {
                            "label": "Active research rows",
                            "field": "active_rows",
                            "format": "number",
                        }
                    ],
                },
                {
                    "id": "qualification",
                    "dataset": "summary",
                    "sourceId": "activation_result",
                    "metrics": [{"label": "Qualification", "field": "qualification"}],
                },
            ],
            "charts": [
                {
                    "id": "rows_by_symbol",
                    "title": "Active rows by symbol",
                    "subtitle": "DSEX and every held or rejected row are absent.",
                    "type": "bar",
                    "dataset": "coverage",
                    "sourceId": "activation_result",
                    "valueFormat": "number",
                    "encodings": {
                        "x": {"field": "symbol", "type": "nominal", "label": "Symbol"},
                        "y": {"field": "rows", "type": "quantitative", "label": "Rows"},
                    },
                }
            ],
            "tables": [
                {
                    "id": "decision_table",
                    "title": "Nine independent operator decisions",
                    "subtitle": "Each decision has a separate canonical audit event.",
                    "dataset": "decisions",
                    "sourceId": "activation_result",
                    "columns": [
                        {"field": "decision", "label": "Decision", "type": "text"},
                        {"field": "status", "label": "Status", "type": "text"},
                        {
                            "field": "audit_event_id",
                            "label": "Audit event",
                            "type": "text",
                        },
                    ],
                }
            ],
            "sources": [source],
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# Research-only target dataset activation",
                },
                {
                    "id": "executive_heading",
                    "type": "markdown",
                    "body": "## Executive Summary",
                },
                {
                    "id": "executive",
                    "type": "markdown",
                    "body": f"{result['summary']['active_rows']:,} GP, ACI, and BRACBANK rows are active for research only. DSEX, unresolved conflicts, calendar holds, corporate-action holds, invalid rows, and unapproved mappings remain excluded. No strategy or trading workflow was activated.",
                    "sourceId": "activation_result",
                },
                {
                    "id": "cards",
                    "type": "metric-strip",
                    "cardIds": ["active_rows", "qualification"],
                },
                {"id": "findings", "type": "markdown", "body": "## Findings"},
                {"id": "chart", "type": "chart", "chartId": "rows_by_symbol"},
                {
                    "id": "controls",
                    "type": "markdown",
                    "body": "## Controls and audit evidence",
                },
                {"id": "decisions", "type": "table", "tableId": "decision_table"},
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": "## Limitations\n\nThis is third-party research data with unknown timestamp trust. It is not exchange-verified, campaign-qualified, production-approved, or real-money evidence. DSEX is unavailable, so the prepared strategy design uses buy-and-hold comparisons only and remains unexecuted.",
                },
                {
                    "id": "recommendations",
                    "type": "markdown",
                    "body": "## Recommendations\n\nKeep all held rows excluded. Obtain separate authorization before any ma_crossover@1.0.0 research execution. Do not infer a calendar or corporate actions from gaps or price movements.",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": result["activation_timestamp"],
            "status": "ready",
            "datasets": {
                "summary": [
                    {
                        "active_rows": result["summary"]["active_rows"],
                        "qualification": "0/60",
                    }
                ],
                "coverage": rows,
                "decisions": decisions,
            },
            "accessIssues": [],
        },
        "sources": [source],
        "package_info": {
            "originUrl": "artifact://research-subset-activation",
            "controls": {"edit": False, "refresh": False},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Activate the authorized research-only target subset"
    )
    parser.add_argument("--operator")
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--finalize-output", type=Path)
    parser.add_argument(
        "--pack-dir",
        type=Path,
        default=ROOT
        / "reports"
        / "target_subset_approval"
        / "approval_9780bfbbfd03e72bae0c9e13",
    )
    parser.add_argument(
        "--candidate-db",
        type=Path,
        default=ROOT
        / "reports"
        / "research_data_quality"
        / "canonical_candidate_0a834213759f5a79"
        / "canonical_candidate.sqlite3",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "reports" / "research_dataset_activation",
    )
    args = parser.parse_args()
    if args.finalize_output:
        print(json.dumps(_finalize_manifest(args.finalize_output), indent=2))
        return 0
    if not args.operator or args.authorization_file is None:
        parser.error("--operator and --authorization-file are required for activation")

    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError("Research activation requires paper/false/disabled safety")
    if not args.authorization_file.is_file() or not args.candidate_db.is_file():
        raise FileNotFoundError(
            "Authorization text or canonical-candidate database is missing"
        )
    manifest = verify_approval_pack(args.pack_dir)
    authorization_text = args.authorization_file.read_text(encoding="utf-8")
    authorization_sha256 = hashlib.sha256(authorization_text.encode()).hexdigest()
    candidate_db_hash = _sha256(args.candidate_db)
    activation_timestamp = datetime.now(UTC).isoformat()
    version_seed = {
        "candidate_database_sha256": candidate_db_hash,
        "approval_pack_hash": EXPECTED_PACK_HASH,
        "authorization_sha256": authorization_sha256,
        "transformation_version": TRANSFORMATION_VERSION,
    }
    version = "gp-aci-bracbank-research-" + canonical_hash(version_seed)[:16]
    draft_version = "research-subset-" + canonical_hash(version_seed)[:16]
    output = args.output_root / version
    dataset_path = ROOT / "data" / "research_datasets" / f"{version}.jsonl"

    before = _protected_counts()
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        assert_strategy_not_promoted(db)
        pre_audit = audit_status(db)
        initial_research_datasets = int(
            db.scalar(select(func.count()).select_from(ResearchDataset)) or 0
        )
        initial_strategies = {
            f"{row.strategy_id}@{row.version}": row.lifecycle_state
            for row in db.scalars(select(StrategyRegistration))
        }

    source_urls, source_scores, _ = _source_metadata()
    connection = open_candidate_database(args.candidate_db)
    subset = build_target_subset(
        connection, source_scores=source_scores, source_urls=source_urls
    )
    connection.close()
    proposal = json.loads(
        (args.pack_dir / "provisional_subset.json").read_text(encoding="utf-8")
    )
    conflicts = json.loads(
        (args.pack_dir / "conflict_approval_records.json").read_text(encoding="utf-8")
    )["rows"]
    if len(conflicts) != 6 or any(row["reviewer_decision"] != "" for row in conflicts):
        raise RuntimeError(
            "The six conflicts are not preserved with blank reviewer decisions"
        )

    specs = decision_specs(authorization_sha256)
    decisions: dict[str, Any] = {}
    with SessionLocal() as db:
        for spec in specs[:7]:
            approval = record_decision(
                db,
                spec=spec,
                draft_version=draft_version,
                operator_identity=args.operator,
            )
            decisions[spec["key"]] = {
                "id": approval.id,
                "status": approval.approval_status,
                "audit_event_id": approval.audit_event_id,
            }

    approval_ids = {symbol: decisions[symbol]["id"] for symbol in ACTIVE_SYMBOLS}
    audit_ids = {key: value["audit_event_id"] for key, value in decisions.items()}
    rows, summary = build_active_rows(
        subset["candidate_rows"],
        proposal["ledger"],
        activation_timestamp=activation_timestamp,
        approval_decision_ids=approval_ids,
        audit_event_ids=audit_ids,
    )
    dataset_hash = write_jsonl(dataset_path, rows)

    with SessionLocal() as db:
        activation = record_decision(
            db,
            spec=specs[7],
            draft_version=draft_version,
            operator_identity=args.operator,
        )
        decisions[specs[7]["key"]] = {
            "id": activation.id,
            "status": activation.approval_status,
            "audit_event_id": activation.audit_event_id,
        }
        dataset = create_research_dataset_record(
            db,
            version=version,
            dataset_path=dataset_path,
            dataset_hash=dataset_hash,
            candidate_db_path=args.candidate_db,
            candidate_db_hash=candidate_db_hash,
            summary=summary,
            activation_approval=activation,
            operator_identity=args.operator,
        )
        prohibited = record_decision(
            db,
            spec=specs[8],
            draft_version=draft_version,
            operator_identity=args.operator,
        )
        decisions[specs[8]["key"]] = {
            "id": prohibited.id,
            "status": prohibited.approval_status,
            "audit_event_id": prohibited.audit_event_id,
        }
        plan = build_execution_plan(
            dataset=dataset,
            summary=summary,
            excluded_dates={
                "GP": [row["date"] for row in conflicts if row["symbol"] == "GP"],
                "ACI": [row["date"] for row in conflicts if row["symbol"] == "ACI"],
                "BRACBANK": [
                    row["date"] for row in conflicts if row["symbol"] == "BRACBANK"
                ],
            },
        )
        assert_strategy_not_promoted(db)
        post_audit = audit_status(db)
        research_dataset_count = int(
            db.scalar(select(func.count()).select_from(ResearchDataset)) or 0
        )
        final_strategies = {
            f"{row.strategy_id}@{row.version}": row.lifecycle_state
            for row in db.scalars(select(StrategyRegistration))
        }

    after = _protected_counts()
    if before != after or initial_strategies != final_strategies:
        raise RuntimeError("Protected trading or strategy state changed")
    if (
        research_dataset_count != initial_research_datasets + 1
        or not post_audit["canonical_valid"]
    ):
        raise RuntimeError(
            "Dataset registration or canonical audit verification failed"
        )

    result = {
        "classification": ACTIVE_STATUS,
        "version": version,
        "dataset_hash": dataset_hash,
        "dataset_path": str(dataset_path),
        "activation_timestamp": activation_timestamp,
        "operator_identity": args.operator,
        "authorization_sha256": authorization_sha256,
        "approval_pack_hash": EXPECTED_PACK_HASH,
        "approval_pack_manifest_files": len(manifest["files"]),
        "candidate_database_sha256": candidate_db_hash,
        "summary": summary,
        "decisions": decisions,
        "audit_before": pre_audit,
        "audit_after": post_audit,
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_states_before": initial_strategies,
        "strategy_states_after": final_strategies,
        "qualification": "0/60",
        "git_head": _git_head(),
    }
    _write_json(output / "activation_result.json", result)
    _write_json(output / "strategy_execution_plan.json", plan)
    _write_json(
        output / "authorization_record.json",
        {
            "authorization_text": authorization_text,
            "authorization_sha256": authorization_sha256,
            "operator_identity": args.operator,
            "recorded_at": activation_timestamp,
            "decisions": decisions,
        },
    )
    _write_json(output / "artifact.json", _artifact(result))
    manifest_out = _finalize_manifest(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "version": version,
                "dataset_hash": dataset_hash,
                "active_rows": len(rows),
                "report_hash": manifest_out["report_hash"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

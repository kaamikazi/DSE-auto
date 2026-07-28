from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal, resolved_database_url  # noqa: E402
from app.models import (  # noqa: E402
    DatasetImportRun,
    ExtractedClaim,
    GovernedDataset,
    NormalizedDailyBar,
    Order,
    PaperSession,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.operational_provenance_audit import (  # noqa: E402
    historical_record_ledger,
    inspect_database_artifacts,
    ledger_summary,
)
from app.services.report_provenance import (  # noqa: E402
    build_report_provenance,
    csv_provenance_columns,
    html_provenance,
    markdown_provenance,
    provenance_status,
)
from app.services.target_research_review import (  # noqa: E402
    audit_corporate_action_queue,
    build_review_samples,
    build_target_subset,
    open_candidate_database,
    segment_cross_source_conflicts,
)


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError(
            "Database provenance review requires permanent paper-only safety"
        )


def _operational_counts(db: Session) -> dict[str, int]:
    return {
        "campaigns": int(
            db.scalar(select(func.count()).select_from(ValidationCampaign)) or 0
        ),
        "sessions": int(db.scalar(select(func.count()).select_from(PaperSession)) or 0),
        "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
        "fills": int(db.scalar(select(func.count()).select_from(Transaction)) or 0),
        "promoted_strategies": int(
            db.scalar(
                select(func.count())
                .select_from(StrategyRegistration)
                .where(
                    StrategyRegistration.lifecycle_state.in_(
                        ["paper_candidate", "paper_active"]
                    )
                )
            )
            or 0
        ),
        "active_research_bars": int(
            db.scalar(
                select(func.count())
                .select_from(NormalizedDailyBar)
                .where(NormalizedDailyBar.active_for_research.is_(True))
            )
            or 0
        ),
        "activated_datasets": int(
            db.scalar(
                select(func.count())
                .select_from(GovernedDataset)
                .where(GovernedDataset.review_status != "registered")
            )
            or 0
        ),
        "non_review_previews": int(
            db.scalar(
                select(func.count())
                .select_from(DatasetImportRun)
                .where(DatasetImportRun.state != "review_required")
            )
            or 0
        ),
        "approved_rule_claims": int(
            db.scalar(
                select(func.count())
                .select_from(ExtractedClaim)
                .where(ExtractedClaim.reviewer_status != "under_review")
            )
            or 0
        ),
    }


def _write_json(
    path: Path, provenance: dict[str, Any], payload: dict[str, Any]
) -> None:
    path.write_text(
        json.dumps(
            {"provenance": provenance, **payload}, indent=2, sort_keys=True, default=str
        ),
        encoding="utf-8",
    )


def _csv_value(value: Any) -> Any:
    return (
        json.dumps(value, sort_keys=True, default=str)
        if isinstance(value, (dict, list, set))
        else value
    )


def _write_csv(
    path: Path,
    provenance: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    provenance_columns = csv_provenance_columns(provenance)
    normalized = [
        provenance_columns | {key: _csv_value(value) for key, value in row.items()}
        for row in rows
    ]
    fields = list(provenance_columns)
    for row in normalized:
        fields.extend(key for key in row if key not in fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(normalized or [provenance_columns])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_metadata() -> tuple[dict[str, str], dict[str, float]]:
    catalog = json.loads(
        (ROOT / "config" / "public_dse_source_catalog.json").read_text()
    )
    urls = {
        item["sha256"]: item["source_url"]
        for item in catalog["sources"]
        if item.get("sha256")
    }
    pack = (
        ROOT
        / "reports"
        / "research_data_quality"
        / "canonical_candidate_0a834213759f5a79"
    )
    inventory = json.loads((pack / "dataset_inventory.json").read_text())
    quality = json.loads((pack / "source_quality_scores.json").read_text())
    physical_by_logical = {
        item["logical_name"]: item["source_name"] for item in inventory
    }
    scores = {
        physical_by_logical.get(item["logical_name"], item["logical_name"]): float(
            item["score"]
        )
        for item in quality
    }
    return urls, scores


def _report_conflicts() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    json_reports = {
        "public_source_registration": ROOT
        / "reports"
        / "evidence_workspace"
        / "public_sources"
        / "registration_result.json",
        "canonical_candidate_manifest": ROOT
        / "reports"
        / "research_data_quality"
        / "canonical_candidate_0a834213759f5a79"
        / "manifest.json",
    }
    for name, path in json_reports.items():
        payload = json.loads(path.read_text(encoding="utf-8"))
        findings.append(
            {
                "report": name,
                "absolute_path": str(path.resolve()),
                **provenance_status(payload),
                "recorded_counts": payload.get("operational_before"),
                "interpretation": "Counts are usable only with separately reconstructed database provenance.",
            }
        )
    findings.extend(
        [
            {
                "report": "docs/PRE_CAMPAIGN_APPROVAL.md",
                "absolute_path": str(
                    (ROOT / "docs" / "PRE_CAMPAIGN_APPROVAL.md").resolve()
                ),
                "status": "legacy_unverified",
                "missing_fields": "all mandatory database provenance fields",
                "recorded_counts": {
                    "campaigns": 0,
                    "sessions": 0,
                    "orders": 0,
                    "fills": 0,
                },
                "interpretation": (
                    "The wording described the prospective approval-pack scope, not total preserved "
                    "operational history; as a global database assertion it is false."
                ),
            },
            {
                "report": "focused no-activation tests",
                "absolute_path": str((ROOT / "backend" / "tests").resolve()),
                "status": "test_scoped",
                "missing_fields": [],
                "recorded_counts": {
                    "campaigns": 0,
                    "sessions": 0,
                    "orders": 0,
                    "fills": 0,
                },
                "interpretation": (
                    "Pytest recreates an isolated test database. Zero counts prove test side effects, "
                    "not operational-database emptiness."
                ),
            },
        ]
    )
    return findings


def _source_of_truth_policy() -> dict[str, Any]:
    return {
        "operational": {
            "role": "operational",
            "location": str(
                (ROOT / "backend" / "data" / "dse_autotrader.db").resolve()
            ),
            "policy": "Only this role may contain preserved operator paper history.",
        },
        "research": {
            "role": "research",
            "location": str((ROOT / "reports" / "research_data_quality").resolve()),
            "policy": "Inactive transformed candidates only; never an order source.",
        },
        "test": {
            "role": "test",
            "location": "%TEMP%/dse_autotrader_pytest_<pid>.db",
            "policy": "Per-process database; startup refuses the operational SQLite path.",
        },
        "recovery": {
            "role": "recovery",
            "location": "data/backups and reports/recovery",
            "policy": "Immutable backup/restore evidence; never silently merged.",
        },
        "postgres_verification": {
            "role": "postgres_verification",
            "location": "127.0.0.1:5432/dse_autotrader and 127.0.0.1:15432/dse_autotrader_test",
            "policy": "Explicit alias and role required; currently unavailable because Docker is offline.",
        },
        "simulation": {
            "role": "simulation",
            "location": "incident/campaign-specific databases and PostgreSQL validation databases",
            "policy": "Must not resolve to the operational SQLite file without explicit override.",
        },
        "mandatory_identity": [
            "database_role",
            "absolute location or redacted connection alias",
            "environment",
            "audit chain ID",
            "migration revision",
        ],
        "legacy_rule": "Any report without the complete provenance header is legacy_unverified.",
    }


def _markdown(payload: dict[str, Any]) -> str:
    provenance = payload["provenance"]
    summary = payload["historical_ledger_summary"]
    lines = [
        "# Database provenance and four-symbol research review",
        "",
        "**INACTIVE - HUMAN REVIEW REQUIRED - QUALIFICATION 0/60**",
        "",
        markdown_provenance(provenance),
        "## Finding",
        "",
        "The count discrepancy was a scope/provenance error, not a newly created trading event. ",
        "Test and prospective-approval assertions were reported as if they described the operational ",
        "database, while a relative SQLite URL could also resolve to a separate empty root-level file.",
        "",
        "## Historical records",
        "",
        f"- Record types: `{json.dumps(summary['records_by_type'], sort_keys=True)}`",
        f"- Classifications: `{json.dumps(summary['records_by_classification'], sort_keys=True)}`",
        "- Real broker connections: 0",
        "- Real order submissions: 0",
        "",
        "## Detector audit",
        "",
        payload["corporate_action_audit"]["false_positive_finding"],
        "",
        f"Revised labels: `{json.dumps(payload['corporate_action_audit']['revised_classification_counts'], sort_keys=True)}`",
        "",
        "## Target readiness",
        "",
    ]
    lines.extend(
        f"- {item['symbol']}: {item['research_readiness_status']}; "
        f"{item['candidate_rows']} inactive candidates; {item['held_rows']} held groups"
        for item in payload["target_subset"]["target_readiness"]
    )
    lines.extend(
        [
            "",
            "## Unresolved human decisions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in payload["unresolved_decisions"])
    lines.extend(
        [
            "",
            "No dataset, rule, fee, limit, strategy, campaign, session, proposal, order, transaction,",
            "or fill was created or activated by this review.",
        ]
    )
    return "\n".join(lines) + "\n"


def _html(payload: dict[str, Any]) -> str:
    readiness = "".join(
        f"<tr><td>{escape(row['symbol'])}</td><td>{row['candidate_rows']}</td>"
        f"<td>{row['held_rows']}</td><td>{escape(row['research_readiness_status'])}</td></tr>"
        for row in payload["target_subset"]["target_readiness"]
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>DSE provenance review</title></head>"
        "<body><h1>Database provenance and four-symbol research review</h1>"
        "<p><strong>INACTIVE - HUMAN REVIEW REQUIRED - QUALIFICATION 0/60</strong></p>"
        f"{html_provenance(payload['provenance'])}"
        "<h2>Target readiness</h2><table><tr><th>Symbol</th><th>Candidates</th>"
        f"<th>Held</th><th>Status</th></tr>{readiness}</table>"
        f"<pre>{escape(json.dumps(payload['historical_ledger_summary'], indent=2))}</pre>"
        "<p>No activation or new trading record occurred.</p></body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit database provenance and build target review"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "reports" / "database_provenance_review",
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
    args = parser.parse_args()
    _assert_safety()
    settings = get_settings()
    generated_at = datetime.now(UTC)
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        before = _operational_counts(db)
        datasets = list(
            db.scalars(select(GovernedDataset.id).order_by(GovernedDataset.id))
        )
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=resolved_database_url,
            dataset_ids=datasets,
        )
        ledger = historical_record_ledger(db)
    git_head = provenance["git_head"]
    inventory = inspect_database_artifacts(
        git_head=git_head, generated_at=generated_at.isoformat()
    )
    source_urls, source_scores = _source_metadata()
    candidate = open_candidate_database(args.candidate_db)
    action_audit = audit_corporate_action_queue(candidate)
    conflict_audit = segment_cross_source_conflicts(candidate)
    subset = build_target_subset(
        candidate, source_scores=source_scores, source_urls=source_urls
    )
    samples = build_review_samples(
        candidate, subset, action_audit, source_urls=source_urls
    )
    candidate.close()
    with SessionLocal() as db:
        after = _operational_counts(db)
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain changed or became invalid")
    delta = {key: after[key] - before[key] for key in before}
    if any(delta.values()):
        raise RuntimeError(f"Review changed operational state: {delta}")
    ledger_stats = ledger_summary(ledger)
    if ledger_stats["unknown_or_suspicious"]:
        raise RuntimeError(
            "Unknown/suspicious historical records require an operator incident"
        )
    unresolved = [
        "Approve or reject 00DSEX to DSEX mapping with effective-date evidence.",
        "Approve a source hierarchy separately for adjusted and unadjusted research series.",
        "Resolve eligible same-grain source conflicts; no averaging is permitted.",
        "Confirm price/volume units and licensing for each third-party source.",
        "Supply official announcement, ex-date, and record-date evidence for corporate actions.",
        "Approve an authoritative DSE calendar before interpreting missing dates.",
        "Decide whether legacy zero-count governance wording should be formally superseded.",
        "Record application Git/version directly in future operational records.",
    ]
    payload = {
        "provenance": provenance,
        "database_discrepancy_cause": {
            "primary": "test/prospective scope was conflated with preserved operational history",
            "contributing": (
                "sqlite:///./data/dse_autotrader.db previously depended on current working directory, "
                "creating an empty root-level shadow while backend execution used the populated backend file"
            ),
            "operational_counts": before,
        },
        "database_inventory": inventory,
        "report_conflicts": _report_conflicts(),
        "source_of_truth_policy": _source_of_truth_policy(),
        "historical_ledger": ledger,
        "historical_ledger_summary": ledger_stats,
        "corporate_action_audit": {
            key: value for key, value in action_audit.items() if key != "rows"
        },
        "conflict_segmentation": conflict_audit,
        "target_subset": {
            key: value for key, value in subset.items() if not key.endswith("_rows")
        },
        "unresolved_decisions": unresolved,
        "operational_before": before,
        "operational_after": after,
        "operational_delta": delta,
        "audit_valid": True,
        "activation_blocked": True,
        "qualification": "0/60",
    }
    run_id = provenance["report_id"]
    output = args.output_root / f"review_{run_id}"
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "review_summary.json", provenance, payload)
    _write_json(
        output / "database_inventory.json",
        provenance,
        {"database_inventory": inventory},
    )
    _write_csv(output / "database_inventory.csv", provenance, inventory)
    _write_json(
        output / "historical_record_ledger.json", provenance, {"records": ledger}
    )
    _write_csv(output / "historical_record_ledger.csv", provenance, ledger)
    _write_json(
        output / "corporate_action_detector_audit.json",
        provenance,
        {key: value for key, value in action_audit.items() if key != "rows"},
    )
    _write_csv(
        output / "corporate_action_review_queue.csv", provenance, action_audit["rows"]
    )
    _write_json(
        output / "cross_source_tolerance_audit.json",
        provenance,
        {"conflict_segmentation": conflict_audit},
    )
    _write_json(
        output / "target_subset.json",
        provenance,
        {"policy": subset["policy"], "candidate_rows": subset["candidate_rows"]},
    )
    _write_csv(output / "target_subset.csv", provenance, subset["candidate_rows"])
    _write_json(output / "review_samples.json", provenance, {"samples": samples})
    _write_csv(output / "review_samples.csv", provenance, samples)
    (output / "human_review.md").write_text(_markdown(payload), encoding="utf-8")
    (output / "human_review.html").write_text(_html(payload), encoding="utf-8")
    content_files = sorted(output.iterdir())
    file_hashes = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in content_files
    }
    manifest = {
        "provenance": provenance,
        "files": file_hashes,
        "operational_delta": delta,
        "audit_valid": True,
        "activation_blocked": True,
        "qualification": "0/60",
    }
    manifest_hash = hashlib.sha256(
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
    manifest["manifest_hash"] = manifest_hash
    _write_json(output / "manifest.json", provenance, manifest)
    _write_csv(
        output / "manifest.csv",
        provenance,
        [{"filename": name, **details} for name, details in file_hashes.items()],
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "report_id": run_id,
                "manifest_hash": manifest_hash,
                "ledger": ledger_stats,
                "candidate_counts": subset["candidate_counts"],
                "held_counts": subset["held_counts"],
                "operational_delta": delta,
                "audit_valid": True,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

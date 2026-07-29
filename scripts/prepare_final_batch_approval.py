from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    Order,
    PaperSession,
    ResearchDataset,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain  # noqa: E402
from app.services.final_batch_review import (  # noqa: E402
    PROFILE_SQL,
    TARGET_SYMBOLS,
    approval_decisions,
    build_final_batch_review,
)

EXPECTED_HEAD = "34a859904b858507d103c039b24614e8f733a4ed"
REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
PACK_SCHEMA = "final_batch_approval_v2"
SOURCE_HIERARCHY_POLICY = "minimum_252_quality_coverage_conflict_invalid_v2"
EVIDENCE_DIR = (
    ROOT / "reports" / "research_data_quality" / "canonical_candidate_0a834213759f5a79"
)
CANONICAL_DATABASE = EVIDENCE_DIR / "canonical_candidate.sqlite3"
REPORT_BUILDER = (
    Path.home()
    / ".codex"
    / "plugins"
    / "cache"
    / "openai-curated-remote"
    / "data-analytics"
    / "0.2.8-13ceeea1f599"
    / "skills"
    / "build-report"
    / "scripts"
    / "deliver_portable_artifact.mjs"
)
PROTECTED = (
    ResearchDataset,
    ValidationCampaign,
    PaperSession,
    Signal,
    Order,
    Transaction,
)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in PROTECTED
    }


def _source_sql() -> str:
    literals = ",".join(f"'{symbol}'" for symbol in TARGET_SYMBOLS)
    return PROFILE_SQL.format(placeholders=literals)


def _markdown(review: dict[str, Any], decisions: list[dict[str, Any]]) -> str:
    diversity = review["portfolio_diversity"]
    lines = [
        "# Final 12-symbol expanded research-batch approval pack",
        "",
        "> **INACTIVE. EVERY INCLUSION PERMISSION IS REJECTED / NOT GRANTED.**",
        "",
        "## Technical summary",
        "",
        f"All {len(review['symbols'])} symbols require human review. The pack preserves "
        f"{len(review['conflicts']):,} unresolved cross-source comparisons and "
        f"{len(review['corporate_actions']):,} corporate-action candidates. Official lifecycle "
        "evidence is unavailable, no candidate row is active, and ma_crossover was not run.",
        "",
        "## Every symbol remains blocked pending review",
        "",
        "| Symbol | Sector | Valid | Invalid | Conflicts | Action-held | Readiness | Inclusion |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    lines.extend(
        f"| {row['symbol']} | {row['sector']} | {row['valid_rows']} | {row['invalid_rows']} | "
        f"{row['eligible_cross_source_conflicts']} | {row['corporate_action_held_rows']} | "
        f"{row['readiness_status']} | REJECTED / NOT GRANTED |"
        for row in review["symbols"]
    )
    lines.extend(
        [
            "",
            "## Source roles are proposals, not approvals",
            "",
            "Each symbol has separate adjusted, unadjusted, validation, fallback, rejected, and "
            "unresolved roles. Quality, coverage, conflict burden, invalid burden, license notes, "
            "rationale, risk, and blank human-approval fields are retained in JSON/CSV.",
            "",
            "## Lifecycle bounds remain observational",
            "",
            "First and last valid dates are not described as listing or delisting dates. Every symbol "
            "is `lifecycle_evidence_pending`; long gaps are not treated as verified suspensions.",
            "",
            "## Portfolio diversity does not cure evidence gaps",
            "",
            f"Equal symbol weight would be {diversity['equal_symbol_weight_percent']:.4f}% and the "
            f"maximum provisional sector weight would be {diversity['maximum_sector_weight_percent']:.2f}%. "
            f"Common observed coverage begins {diversity['common_observed_coverage']['start']}. "
            "Survivorship risk remains high because lifecycle evidence is pending.",
            "",
            "## Decisions remain independent",
            "",
            f"The pack contains {len(decisions)} separate decision records. All reviewer and operator "
            "fields are blank; all 12 inclusion permissions default to REJECTED / NOT GRANTED.",
            "",
            "## Limitations and next step",
            "",
            "Sources are registered research evidence, not exchange-verified lifecycle truth. "
            "Conflicts and corporate-action rows remain excluded. Review must proceed symbol by symbol; "
            "DSEX and the 13 secondary symbols remain unchanged and outside this review.",
            "",
            "Qualification remains 0/60.",
        ]
    )
    return "\n".join(lines) + "\n"


def _source_metadata() -> dict[str, Any]:
    return {
        "label": "Canonical candidate observations",
        "query": {
            "sql": _source_sql(),
            "description": "Profile the exact 12-symbol review scope without performance fields.",
            "engine": "sqlite",
            "language": "sql",
            "tables_used": ["canonical_candidate.observations"],
            "filters": {
                "symbols": list(TARGET_SYMBOLS),
                "performance_fields": "excluded",
            },
            "executed_at": datetime.now(UTC).isoformat(),
        },
    }


def _artifact(review: dict[str, Any]) -> dict[str, Any]:
    readiness = [
        {
            "symbol": row["symbol"],
            "sector": row["sector"],
            "valid_rows": row["valid_rows"],
            "conflicts": row["eligible_cross_source_conflicts"],
            "action_held": row["corporate_action_held_rows"],
            "readiness": row["readiness_status"],
            "inclusion": "REJECTED / NOT GRANTED",
        }
        for row in review["symbols"]
    ]
    sectors = [
        {"sector": key, "symbols": value, "batch_size": 12}
        for key, value in sorted(review["portfolio_diversity"]["sector_counts"].items())
    ]
    source = _source_metadata()
    return {
        "surface": "report",
        "manifest": {
            "surface": "report",
            "version": 1,
            "title": "Final 12-symbol expanded research-batch approval pack",
            "description": "Human review evidence; no activation or strategy execution.",
            "generatedAt": datetime.now(UTC).isoformat(),
            "sources": [],
            "cards": [],
            "charts": [
                {
                    "id": "sector_mix",
                    "type": "bar",
                    "title": "Provisional sector composition",
                    "subtitle": "Twelve-symbol equal-weight batch; sector labels require confirmation.",
                    "dataset": "sector_mix",
                    "source": source,
                    "encodings": {
                        "x": {"field": "sector", "type": "nominal", "label": "Sector"},
                        "y": {
                            "field": "symbols",
                            "type": "quantitative",
                            "label": "Symbols",
                        },
                        "tooltip": [
                            {
                                "field": "symbols",
                                "type": "quantitative",
                                "label": "Symbols",
                            },
                            {
                                "field": "batch_size",
                                "type": "quantitative",
                                "label": "Batch size",
                            },
                        ],
                    },
                    "valueFormat": "number",
                }
            ],
            "tables": [
                {
                    "id": "readiness_table",
                    "title": "Symbol review evidence",
                    "subtitle": "Exact scope; every inclusion decision defaults to rejected.",
                    "dataset": "readiness",
                    "source": source,
                    "defaultSort": {"field": "symbol", "direction": "asc"},
                    "columns": [
                        {"field": "symbol", "label": "Symbol", "type": "text"},
                        {"field": "sector", "label": "Sector", "type": "text"},
                        {
                            "field": "valid_rows",
                            "label": "Valid rows",
                            "type": "number",
                        },
                        {"field": "conflicts", "label": "Conflicts", "type": "number"},
                        {
                            "field": "action_held",
                            "label": "Action-held",
                            "type": "number",
                        },
                        {"field": "readiness", "label": "Readiness", "type": "text"},
                        {"field": "inclusion", "label": "Inclusion", "type": "text"},
                    ],
                }
            ],
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# Final 12-symbol expanded research-batch approval pack",
                },
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": f"## Technical summary\n\nAll 12 symbols remain blocked. The review preserves {len(review['conflicts']):,} conflicts and {len(review['corporate_actions']):,} corporate-action candidates; lifecycle evidence is pending and inclusion is rejected.",
                },
                {
                    "id": "sector_heading",
                    "type": "markdown",
                    "body": "## Sector spread limits concentration but does not establish readiness\n\nThe chart shows equal-weight composition; source and lifecycle uncertainty remain binding.",
                },
                {"id": "sector_chart", "type": "chart", "chartId": "sector_mix"},
                {
                    "id": "readiness_heading",
                    "type": "markdown",
                    "body": "## Every symbol requires conflict and lifecycle review\n\nExact counts support human decisions; no status is an activation.",
                },
                {"id": "readiness", "type": "table", "tableId": "readiness_table"},
                {
                    "id": "definitions",
                    "type": "markdown",
                    "body": "## Scope and definitions\n\nValid rows pass canonical OHLC checks. Conflict and corporate-action rows remain held. First/last valid dates are observational bounds, not listing evidence.",
                },
                {
                    "id": "method",
                    "type": "markdown",
                    "body": "## Method preserves unresolved evidence\n\nThe review profiles registered observations, duplicates, source comparisons, mappings, and action candidates. Strategy returns and trade results are excluded.",
                },
                {
                    "id": "limits",
                    "type": "markdown",
                    "body": "## Limitations and next step\n\nHuman reviewers must decide each source role, conflict, action, lifecycle assumption, and inclusion independently. DSEX remains separate and rejected; qualification remains 0/60.",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "accessIssues": [],
            "datasets": {"readiness": readiness, "sector_mix": sectors},
        },
        "package_info": {
            "controls": {"edit": False, "refresh": False},
            "originUrl": "artifact://final-expanded-batch-review",
        },
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args()
    settings = get_settings()
    if _head() != EXPECTED_HEAD:
        raise RuntimeError("Pinned Git HEAD mismatch")
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Paper-only safety mismatch")
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        if registration is None:
            raise RuntimeError("Strategy registration missing")
        strategy_before = {
            "lifecycle": registration.lifecycle_state,
            "promotion": registration.evidence.get("promotion_status"),
            "campaign_eligibility": registration.evidence.get("campaign_eligibility"),
        }
        if strategy_before != {
            "lifecycle": "research",
            "promotion": "blocked",
            "campaign_eligibility": False,
        }:
            raise RuntimeError("Strategy governance mismatch")
        before = _counts(db)
        audit_before = audit_status(db)

    review = build_final_batch_review(CANONICAL_DATABASE, EVIDENCE_DIR)
    decisions = approval_decisions()
    run_id = (
        "review_"
        + _canonical_hash(
            {"head": EXPECTED_HEAD, "scope": TARGET_SYMBOLS, "schema": PACK_SCHEMA}
        )[:24]
    )
    output = ROOT / "reports" / "final_batch_approval" / run_id
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        key: value
        for key, value in review.items()
        if key not in {"conflicts", "corporate_actions"}
    }
    _write_json(output / "review_summary.json", summary)
    _write_json(output / "unresolved_conflicts.json", review["conflicts"])
    _write_json(output / "corporate_action_review.json", review["corporate_actions"])
    _write_json(output / "approval_decisions.json", decisions)
    _write_csv(
        output / "symbol_review.csv",
        review["symbols"],
        [
            "symbol",
            "sector",
            "valid_rows",
            "invalid_rows",
            "adjusted_valid_rows",
            "unadjusted_valid_rows",
            "duplicate_groups",
            "exact_duplicates",
            "conflicting_duplicates",
            "eligible_cross_source_conflicts",
            "missing_date_gaps",
            "weekend_rows",
            "corporate_action_held_rows",
            "suspension_candidates",
            "readiness_status",
            "lifecycle_status",
            "inclusion_permission",
        ],
    )
    hierarchy_rows = [
        {"symbol": symbol["symbol"], **role}
        for symbol in review["symbols"]
        for role in symbol["provisional_source_hierarchy"]
    ]
    _write_csv(
        output / "source_hierarchy.csv",
        hierarchy_rows,
        [
            "symbol",
            "role",
            "source",
            "quality_score",
            "conflict_burden",
            "invalid_row_burden",
            "adjustment_status",
            "rationale",
            "risk",
            "human_approval",
        ],
    )
    conflict_rows = [
        {
            **row,
            "source_a_values": json.dumps(row["source_a_values"], sort_keys=True),
            "source_b_values": json.dumps(row["source_b_values"], sort_keys=True),
            "percentage_difference": json.dumps(
                row["percentage_difference"], sort_keys=True
            ),
        }
        for row in review["conflicts"]
    ]
    _write_csv(
        output / "unresolved_conflicts.csv",
        conflict_rows,
        [
            "symbol",
            "date",
            "source_a",
            "source_b",
            "source_a_values",
            "source_b_values",
            "adjustment_a",
            "adjustment_b",
            "source_a_quality",
            "source_b_quality",
            "percentage_difference",
            "previous_valid_date",
            "next_valid_date",
            "corporate_action_relationship",
            "recommended_action",
            "reviewer_decision",
            "operator_decision",
        ],
    )
    _write_csv(
        output / "corporate_action_review.csv",
        review["corporate_actions"],
        [
            "symbol",
            "date",
            "source_dataset_id",
            "candidate_type",
            "classification",
            "previous_close",
            "current_close",
            "adjusted_close",
            "unadjusted_close",
            "volume_change",
            "review_status",
            "reconstructed",
            "future_inclusion",
        ],
    )
    tier_rows = [
        {"symbol": row["symbol"], **row["candidate_tiers"]} for row in review["symbols"]
    ]
    _write_csv(
        output / "candidate_tiers.csv",
        tier_rows,
        [
            "symbol",
            "tier_1_cross_source_confirmed",
            "tier_2_single_source_high_quality",
            "tier_3_research_only",
            "held_for_review",
            "rejected_invalid",
        ],
    )
    _write_csv(
        output / "approval_decisions.csv",
        decisions,
        [
            "decision_id",
            "symbol",
            "decision",
            "default",
            "reviewer_decision",
            "operator_decision",
        ],
    )
    (output / "approval_pack.md").write_text(
        _markdown(review, decisions), encoding="utf-8"
    )
    artifact = output / "artifact.json"
    _write_json(artifact, _artifact(review))
    builder = subprocess.run(
        [
            "node",
            str(REPORT_BUILDER),
            "--input",
            str(artifact),
            "--output",
            str(output / "approval_pack.html"),
        ],
        cwd=REPORT_BUILDER.parents[3],
        capture_output=True,
        text=True,
    )
    _write_json(
        output / "html_builder_receipt.json",
        {
            "returncode": builder.returncode,
            "stdout": builder.stdout,
            "stderr": builder.stderr,
        },
    )
    html_status = (
        "portable_builder_completed"
        if builder.returncode == 0 and (output / "approval_pack.html").is_file()
        else "blocked_after_bounded_builder_attempt"
    )
    evidence_hashes = {
        path.name: _hash(path)
        for path in sorted(output.iterdir())
        if path.is_file() and path.name not in {"audit_record.json", "manifest.json"}
    }
    with SessionLocal() as db:
        prior_event = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "research.final_batch_review_prepared")
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        event = append_audit(
            db,
            actor=args.operator,
            event_type="research.final_batch_review_prepared",
            entity_type="research_batch",
            entity_id=run_id,
            new_state={
                "scope": list(TARGET_SYMBOLS),
                "activation": False,
                "strategy_execution": False,
                "inclusion_permission": "REJECTED / NOT GRANTED",
                "output_hashes": evidence_hashes,
                "qualification": "0/60",
                "source_hierarchy_policy": SOURCE_HIERARCHY_POLICY,
                "pack_schema": PACK_SCHEMA,
                "supersedes_audit_event_id": prior_event.id if prior_event else None,
            },
        )
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        after = _counts(db)
        strategy_after = {
            "lifecycle": registration.lifecycle_state if registration else None,
            "promotion": registration.evidence.get("promotion_status")
            if registration
            else None,
            "campaign_eligibility": registration.evidence.get("campaign_eligibility")
            if registration
            else None,
        }
        if (
            before != after
            or strategy_before != strategy_after
            or not verify_audit_chain(db)
        ):
            raise RuntimeError("Protected state or audit verification failed")
        audit_after = audit_status(db)
    _write_json(
        output / "audit_record.json",
        {
            "event_id": event.id,
            "event_hash": event.integrity_hash,
            "audit_before": audit_before,
            "audit_after": audit_after,
        },
    )
    manifest = {
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": _hash(path)}
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        ],
        "html_status": html_status,
        "protected_counts_before": before,
        "protected_counts_after": after,
        "strategy_before": strategy_before,
        "strategy_after": strategy_after,
        "activation": False,
        "strategy_execution": False,
        "qualification": "0/60",
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(output),
                "manifest_hash": manifest["manifest_hash"],
                "html_status": html_status,
                "conflicts": len(review["conflicts"]),
                "corporate_actions": len(review["corporate_actions"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

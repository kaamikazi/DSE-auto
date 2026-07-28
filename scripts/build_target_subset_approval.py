from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal, resolved_database_url  # noqa: E402
from app.core.database_identity import sha256_file  # noqa: E402
from app.models import GovernedDataset  # noqa: E402
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.report_provenance import build_report_provenance  # noqa: E402
from app.services.target_research_review import (  # noqa: E402
    audit_corporate_action_queue,
    build_target_subset,
    open_candidate_database,
)
from app.services.target_subset_approval import (  # noqa: E402
    approval_decisions,
    build_conflict_approval_records,
    build_dsex_forensics,
    build_dsex_invalid_review,
    build_dsex_volume_semantics,
    build_subset_status_proposal,
    calendar_decision_pack,
    corporate_action_statuses,
    final_source_hierarchies,
    research_readiness,
    source_role_decision,
    source_role_recommendations,
    validate_pack_invariants,
)
from app.services.target_symbol_human_review import (  # noqa: E402
    build_calendar_review,
    build_corporate_action_review,
    build_rounding_review,
    build_source_hierarchy_review,
    build_unexplained_conflict_review,
    build_volume_unit_review,
)

from scripts.build_target_symbol_human_review import (  # noqa: E402
    _operational_counts,
    _source_metadata,
)


def _sha256(path: Path) -> str:
    return sha256_file(path)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _flatten(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        fields.extend(key for key in row if key not in fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            [{key: _flatten(value) for key, value in row.items()} for row in rows]
        )


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError("Target-subset approval pack requires paper-only safety")


def _markdown(payload: dict[str, Any]) -> str:
    dsex = payload["dsex_forensics"]
    invalid = payload["dsex_invalid_review"]
    volume = payload["dsex_volume_semantics"]
    lines = [
        "# Final target-subset review resolution and research-activation approval pack",
        "",
        "**INACTIVE - ACTIVATION REJECTED / NOT GRANTED - QUALIFICATION 0/60**",
        "",
        "## Technical summary",
        "",
        (
            f"The scoped review preserves {dsex['population']:,} `00DSEX` rows. "
            f"{dsex['classification_counts']['unresolved']:,} remain unresolved, "
            f"{invalid['row_count']:,} are OHLC-invalid, and DSEX volume is "
            f"`{volume['outcome']}`. No source, mapping, calendar, corporate action, "
            "dataset, rule, strategy, campaign, session, proposal, order, or fill was activated."
        ),
        "",
        "## DSEX mapping and invalid-row findings",
        "",
        f"- Mapping classifications: `{json.dumps(dsex['classification_counts'], sort_keys=True)}`",
        f"- Unresolved causes: `{json.dumps(dsex['unresolved_cause_counts'], sort_keys=True)}`",
        f"- Invalid primary classifications: `{json.dumps(invalid['primary_classification_counts'], sort_keys=True)}`",
        "- Automatic mapping or repair: false",
        "",
        "## DSEX volume semantics",
        "",
        (
            f"The {volume['scoped_conflicts']} scoped comparisons show a stable approximate "
            "100x relationship, but registered official DSE documentation separates instrument "
            "quantity/value/trades and defines no volume in the index table. The field is therefore "
            "not comparable and is excluded rather than rescaled."
        ),
        "",
        "## Six separate conflict decisions",
        "",
        "| ID | Symbol | Date | Recommendation | Confidence |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {row['approval_record_id']} | {row['symbol']} | {row['date']} | "
        f"{row['recommendation']} | {row['confidence']} |"
        for row in payload["conflict_approval_records"]
    )
    lines.extend(
        [
            "",
            "## Source hierarchy proposals",
            "",
            "| Grain | Primary | Validation | Status |",
            "|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {row['grain']} | {row['primary_source']} | {row['validation_source']} | "
        f"{row['approval_status']} |"
        for row in payload["source_hierarchies"]
    )
    lines.extend(
        [
            "",
            "## Corporate actions and calendar",
            "",
            f"- Corporate-action statuses: `{json.dumps(payload['corporate_action_summary'], sort_keys=True)}`",
            "- The 12 suspension/resumption candidates have no issuer/date-specific official match; none is verified.",
            "- Observed calendar behavior is separated from official evidence; holidays and historical weekend regimes remain unresolved.",
            "",
            "## Readiness",
            "",
        ]
    )
    lines.extend(
        f"- {row['symbol']}: **{row['status']}** - {', '.join(row['human_decisions'])}"
        for row in payload["readiness"]
    )
    lines.extend(
        [
            "",
            "## Human approval decisions",
            "",
            "Every field below is intentionally blank; absence of a signed decision means no approval.",
            "",
            "| ID | Decision | Default | Reviewer | Operator |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {row['decision_id']} | {row['decision']} | {row['recommended_action']} |  |  |"
        for row in payload["approval_decisions"]
    )
    lines.extend(
        [
            "",
            "## Limitations and next step",
            "",
            "This is an evidence and decision pack, not approval. Human reviewers must resolve each decision independently. Research activation remains rejected even if a subset of source or conflict decisions is accepted.",
            "",
            f"Git HEAD: `{payload['provenance']['git_head']}`  ",
            f"Database fingerprint: `{payload['provenance']['database_fingerprint']}`  ",
            f"Canonical audit chain: `{payload['provenance']['audit_chain_id']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifact(payload: dict[str, Any]) -> dict[str, Any]:
    generated = payload["provenance"]["generated_at"]
    decision_rows = [
        {
            "id": row["decision_id"],
            "decision": row["decision"],
            "default": row["recommended_action"],
            "status": row["status"],
        }
        for row in payload["approval_decisions"]
    ]
    dsex_rows = [
        {"classification": key, "rows": value, "scope": "00DSEX mapping review"}
        for key, value in payload["dsex_forensics"]["classification_counts"].items()
    ]
    readiness_rows = [
        {
            "symbol": row["symbol"],
            "status": row["status"],
            "activation": "rejected",
        }
        for row in payload["readiness"]
    ]
    source = {
        "id": "candidate_review_sql",
        "label": "Preserved canonical-candidate review queries",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "sql": (
                "SELECT normalized_symbol, mapping_approval_status, accepted_for_candidate, "
                "COUNT(*) AS rows FROM observations WHERE normalized_symbol IN "
                "('GP','ACI','BRACBANK','DSEX') GROUP BY 1,2,3"
            ),
            "description": "Deterministic aggregate over the preserved candidate observations.",
            "tables_used": ["canonical_candidate.observations"],
            "filters": ["GP, ACI, BRACBANK, DSEX only", "no activation writes"],
            "metric_definitions": [
                "Rows are preserved observation records grouped by review classification."
            ],
            "executed_at": generated,
        },
    }
    title = "Final target-subset research-activation approval pack"
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": title,
            "description": "Technical evidence pack; every activation decision defaults to rejected.",
            "generatedAt": generated,
            "cards": [],
            "charts": [
                {
                    "id": "dsex_classifications",
                    "title": "DSEX mapping review classifications",
                    "subtitle": "Exact preserved row counts; no alias was approved.",
                    "type": "bar",
                    "dataset": "dsex_classifications",
                    "sourceId": "candidate_review_sql",
                    "valueFormat": "number",
                    "encodings": {
                        "x": {
                            "field": "classification",
                            "type": "nominal",
                            "label": "Classification",
                        },
                        "y": {"field": "rows", "type": "quantitative", "label": "Rows"},
                        "tooltip": [
                            {"field": "scope", "type": "nominal", "label": "Scope"}
                        ],
                    },
                }
            ],
            "tables": [
                {
                    "id": "readiness_table",
                    "title": "Target research readiness",
                    "subtitle": "Activation remains rejected for every target.",
                    "dataset": "readiness",
                    "sourceId": "candidate_review_sql",
                    "defaultSort": {"field": "symbol", "direction": "asc"},
                    "columns": [
                        {"field": "symbol", "label": "Symbol", "type": "text"},
                        {"field": "status", "label": "Readiness", "type": "text"},
                        {"field": "activation", "label": "Activation", "type": "text"},
                    ],
                },
                {
                    "id": "decision_table",
                    "title": "Fifteen independent human decisions",
                    "subtitle": "Blank reviewer and operator fields remain in the full CSV/JSON pack.",
                    "dataset": "decisions",
                    "sourceId": "candidate_review_sql",
                    "defaultSort": {"field": "id", "direction": "asc"},
                    "columns": [
                        {"field": "id", "label": "ID", "type": "text"},
                        {"field": "decision", "label": "Decision", "type": "text"},
                        {"field": "default", "label": "Default", "type": "text"},
                        {"field": "status", "label": "Status", "type": "text"},
                    ],
                },
            ],
            "sources": [
                {
                    "id": "candidate_review_sql",
                    "label": source["label"],
                    "path": "canonical_candidate.observations",
                }
            ],
            "blocks": [
                {"id": "title", "type": "markdown", "body": f"# {title}"},
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": (
                        "## Technical summary\n\nDSEX retains 5,295 unresolved alias rows and "
                        "680 invalid rows. Its volume field is not comparable. Six conflicts and "
                        "15 human decisions remain separate. **Research activation is REJECTED / NOT GRANTED.**"
                    ),
                    "sourceId": "candidate_review_sql",
                },
                {
                    "id": "dsex_heading",
                    "type": "markdown",
                    "body": "## DSEX forensics show unresolved alias and field semantics",
                },
                {
                    "id": "dsex_chart",
                    "type": "chart",
                    "chartId": "dsex_classifications",
                },
                {
                    "id": "readiness_heading",
                    "type": "markdown",
                    "body": "## No target is active or approved",
                },
                {"id": "readiness", "type": "table", "tableId": "readiness_table"},
                {
                    "id": "decision_heading",
                    "type": "markdown",
                    "body": "## Every required decision remains independent",
                },
                {"id": "decisions", "type": "table", "tableId": "decision_table"},
                {
                    "id": "limits",
                    "type": "markdown",
                    "body": (
                        "## Limitations and next step\n\nThe sources are third-party research unless explicitly "
                        "official documents. The pack does not establish profitability, market timestamp trust, "
                        "or research activation permission. Human review is the only next step."
                    ),
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated,
            "status": "ready",
            "datasets": {
                "dsex_classifications": dsex_rows,
                "readiness": readiness_rows,
                "decisions": decision_rows,
            },
            "accessIssues": [],
        },
        "sources": [source],
        "package_info": {
            "originUrl": "artifact://target-subset-approval-pack",
            "controls": {"edit": False, "refresh": False},
        },
    }


def _finalize(output: Path) -> None:
    files = sorted(
        path
        for path in output.iterdir()
        if path.is_file() and path.name not in {"manifest.json", "manifest.csv"}
    )
    rows = [
        {"filename": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in files
    ]
    manifest: dict[str, Any] = {
        "files": rows,
        "activation_permission": "REJECTED / NOT GRANTED",
        "qualification": "0/60",
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write_json(output / "manifest.json", manifest)
    _write_csv(output / "manifest.csv", rows)
    print(
        json.dumps(
            {
                "output": str(output),
                "manifest_hash": manifest["manifest_hash"],
                "files": len(rows),
            },
            indent=2,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build inactive target-subset approval evidence"
    )
    parser.add_argument("--finalize", type=Path)
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
        default=ROOT / "reports" / "target_subset_approval",
    )
    args = parser.parse_args()
    if args.finalize:
        _finalize(args.finalize)
        return 0

    _assert_safety()
    settings = get_settings()
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        before = _operational_counts(db)
        dataset_ids = list(
            db.scalars(select(GovernedDataset.id).order_by(GovernedDataset.id))
        )
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=resolved_database_url,
            dataset_ids=dataset_ids,
        )
    source_urls, source_scores, source_catalog = _source_metadata()
    connection = open_candidate_database(args.candidate_db)
    action_audit = audit_corporate_action_queue(connection)
    subset = build_target_subset(
        connection, source_scores=source_scores, source_urls=source_urls
    )
    hierarchy_rows = build_source_hierarchy_review(
        connection, source_scores=source_scores, source_catalog=source_catalog
    )
    unexplained = build_unexplained_conflict_review(connection, source_urls=source_urls)
    rounding = build_rounding_review(connection)
    volume_review = build_volume_unit_review(connection)
    dsex = build_dsex_forensics(connection)
    invalid = build_dsex_invalid_review(connection)
    volume = build_dsex_volume_semantics(connection, volume_review)
    conflict_records = build_conflict_approval_records(
        unexplained["rows"], rounding["rows"], source_scores
    )
    calendar = calendar_decision_pack(build_calendar_review(connection))
    corporate = corporate_action_statuses(
        build_corporate_action_review(
            connection,
            action_audit["rows"],
            subset["candidate_rows"],
            source_urls=source_urls,
        )
    )
    subset_proposal = build_subset_status_proposal(
        connection,
        subset=subset,
        conflicts=conflict_records,
        calendar=calendar,
        corporate_actions=corporate,
    )
    candidate_db_hash = _sha256(args.candidate_db)
    connection.close()

    with SessionLocal() as db:
        after = _operational_counts(db)
        audit_valid = verify_audit_chain(db)
    delta = {key: after[key] - before[key] for key in before}
    if any(delta.values()) or not audit_valid:
        raise RuntimeError(f"Protected state changed: {delta}; audit={audit_valid}")

    source_roles = {
        symbol: source_role_recommendations(hierarchy_rows, symbol=symbol)
        for symbol in ("GP", "ACI", "BRACBANK")
    }
    source_role_decisions = {
        symbol: source_role_decision(symbol) for symbol in ("GP", "ACI", "BRACBANK")
    }
    source_hashes = sorted(
        {
            str(row["source_file_hash"])
            for rows in dsex["mapping_ledger"]
            for row in [rows]
            if row.get("source_file_hash")
        }
        | {
            str(item.get("sha256"))
            for item in source_catalog.values()
            if item.get("sha256")
        }
    )
    subset_version_seed = {
        "git_head": provenance["git_head"],
        "candidate_database_sha256": candidate_db_hash,
        "source_hashes": source_hashes,
        "status_counts": subset_proposal["candidate_status_counts"],
    }
    subset_proposal.update(
        {
            "subset_version": "target-subset-inactive-"
            + hashlib.sha256(
                json.dumps(subset_version_seed, sort_keys=True).encode()
            ).hexdigest()[:16],
            "transformation_version": "target-subset-approval-v1",
            "source_hierarchy_proposal": final_source_hierarchies(),
            "unresolved_conflicts": conflict_records,
            "mapping_status": dsex["classification_counts"],
            "dataset_hashes": source_hashes,
            "candidate_database_sha256": candidate_db_hash,
            "git_head": provenance["git_head"],
            "database_fingerprint": provenance["database_fingerprint"],
            "audit_chain_id": provenance["audit_chain_id"],
            "report_hashes": {
                path.name: _sha256(path)
                for path in sorted(
                    (
                        ROOT
                        / "reports"
                        / "target_symbol_human_review"
                        / "review_655ec146704dc08b19b67b9a"
                    ).glob("*.json")
                )
            },
            "final_output_hashes_reference": "manifest.json",
        }
    )
    corporate_summary = dict(Counter(row["final_evidence_status"] for row in corporate))
    payload = {
        "provenance": provenance,
        "scope": list(("GP", "ACI", "BRACBANK", "DSEX")),
        "dsex_forensics": dsex,
        "dsex_invalid_review": invalid,
        "dsex_volume_semantics": volume,
        "source_role_tables": source_roles,
        "source_role_decisions": source_role_decisions,
        "conflict_approval_records": conflict_records,
        "source_hierarchies": subset_proposal["source_hierarchy_proposal"],
        "corporate_actions": corporate,
        "corporate_action_summary": corporate_summary,
        "calendar": calendar,
        "provisional_subset": subset_proposal,
        "approval_decisions": approval_decisions(),
        "readiness": research_readiness(),
        "activation_permission": "REJECTED / NOT GRANTED",
        "qualification": "0/60",
        "operational_before": before,
        "operational_after": after,
        "operational_delta": delta,
        "audit_valid": audit_valid,
    }
    validate_pack_invariants(payload)
    output = args.output_root / f"approval_{provenance['report_id']}"
    output.mkdir(parents=True, exist_ok=True)

    _write_json(output / "dsex_forensics.json", dsex)
    _write_csv(output / "dsex_mapping_ledger.csv", dsex["mapping_ledger"])
    _write_json(output / "dsex_invalid_review.json", invalid)
    _write_csv(output / "dsex_invalid_rows.csv", invalid["rows"])
    _write_json(output / "dsex_volume_semantics.json", volume)
    _write_json(output / "conflict_approval_records.json", {"rows": conflict_records})
    _write_csv(output / "conflict_approval_records.csv", conflict_records)
    _write_json(output / "source_role_tables.json", source_roles)
    _write_json(output / "source_role_decisions.json", source_role_decisions)
    _write_csv(
        output / "source_role_tables.csv",
        [row for rows in source_roles.values() for row in rows],
    )
    _write_json(
        output / "source_hierarchy_proposal.json",
        {"rows": payload["source_hierarchies"]},
    )
    _write_json(output / "corporate_action_decisions.json", {"rows": corporate})
    _write_csv(output / "corporate_action_decisions.csv", corporate)
    _write_json(output / "calendar_decision_pack.json", {"rows": calendar})
    _write_json(output / "provisional_subset.json", subset_proposal)
    _write_csv(output / "provisional_subset_ledger.csv", subset_proposal["ledger"])
    _write_json(output / "approval_pack.json", payload)
    _write_csv(output / "approval_decisions.csv", payload["approval_decisions"])
    (output / "approval_pack.md").write_text(_markdown(payload), encoding="utf-8")
    _write_json(output / "artifact.json", _artifact(payload))
    print(
        json.dumps(
            {
                "output": str(output),
                "artifact": str(output / "artifact.json"),
                "candidate_status_counts": subset_proposal["candidate_status_counts"],
                "dsex_unresolved_causes": dsex["unresolved_cause_counts"],
                "volume_outcome": volume["outcome"],
                "audit_valid": audit_valid,
                "operational_delta": delta,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

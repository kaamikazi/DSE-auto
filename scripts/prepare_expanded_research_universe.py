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
from app.services.expanded_research_universe import (  # noqa: E402
    build_universe_candidate,
    expanded_research_plan,
)

EXPECTED_HEAD = "d97e1e190ee8701d34c182962dff0e5ddd12410f"
REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
CANONICAL_DATABASE = (
    ROOT
    / "reports"
    / "research_data_quality"
    / "canonical_candidate_0a834213759f5a79"
    / "canonical_candidate.sqlite3"
)
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


def _hash_file(path: Path) -> str:
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


def _markdown(payload: dict[str, Any], plan: dict[str, Any]) -> str:
    selected = payload["proposed_universe"]
    lines = [
        "# Expanded DSE equity research-universe approval pack",
        "",
        "> **INACTIVE. EVERY ACTIVATION DECISION IS REJECTED / NOT GRANTED.**",
        "",
        "## Technical summary",
        "",
        f"A transparent data-quality screen proposed {len(selected)} equities across "
        f"{len(payload['sector_counts'])} provisional sectors. No return, strategy, or "
        "historical-performance field was read. Official listing, delisting, and suspension "
        "evidence is absent, so observation bounds are provisional and no symbol is activated.",
        "",
        "## Proposed universe quality and decision",
        "",
        "| Symbol | Provisional sector | Valid rows | Quality | Readiness | Activation |",
        "|---|---|---:|---:|---|---|",
    ]
    lines.extend(
        f"| {row['symbol']} | {row['sector']} | {row['valid_rows']} | {row['quality_score']:.2f} | "
        f"{row['research_readiness_status']} | rejected/not granted |"
        for row in selected
    )
    lines.extend(
        [
            "",
            "## Survivorship controls remain fail-closed",
            "",
            "No current-only universe is projected backward. A symbol cannot be used before a "
            "verified listing date, after a verified delisting date, or through a known suspension. "
            "Until official lifecycle evidence is supplied, first/last valid observations are only "
            "review bounds and not activation permission.",
            "",
            "## DSEX remains a separate rejected track",
            "",
            "`00DSEX`/`DSEX` alias evidence remains unofficial; non-comparable volume is excluded, "
            "malformed rows are preserved, and price continuity has not passed. DSEX is not mixed "
            "with the equity candidate.",
            "",
            "## Expanded strategy study is prepared but not run",
            "",
            "The frozen plan includes per-symbol, equal-weight, sector-balanced, leave-one-symbol-out, "
            "leave-one-sector-out, rolling walk-forward, untouched holdout, benchmark, cost, tier, "
            "corporate-action, and parameter checks. It explicitly removes BRACBANK, the future "
            "best-performing symbol, every sector in turn, weaker-quality symbols, and applies "
            "stricter costs.",
            "",
            f"Plan status: `{plan['status']}`. Qualification remains 0/60.",
            "",
            "## Limitations and next decision",
            "",
            "Sector labels are provisional review metadata, missing weekday counts are not an "
            "official trading calendar, and corporate-action candidates are unresolved. Reviewers "
            "must decide each symbol independently; blanket approval is not requested.",
        ]
    )
    return "\n".join(lines) + "\n"


def _artifact(payload: dict[str, Any]) -> dict[str, Any]:
    selected = payload["proposed_universe"]
    sector_rows = [
        {"sector": sector, "symbols": count}
        for sector, count in sorted(payload["sector_counts"].items())
    ]
    readiness_rows = [
        {
            "symbol": row["symbol"],
            "sector": row["sector"],
            "valid_rows": row["valid_rows"],
            "quality_score": row["quality_score"],
            "readiness": row["research_readiness_status"],
            "activation": "REJECTED / NOT GRANTED",
        }
        for row in selected
    ]
    source = {
        "id": "candidate_quality_sql",
        "label": "Canonical candidate observations and review ledgers",
        "path": "canonical_candidate.observations",
    }
    return {
        "surface": "report",
        "manifest": {
            "surface": "report",
            "version": 1,
            "title": "Expanded DSE equity research-universe approval pack",
            "description": "Data-quality-only selection; activation rejected for every symbol.",
            "generatedAt": datetime.now(UTC).isoformat(),
            "sources": [source],
            "cards": [],
            "charts": [
                {
                    "id": "sector_coverage",
                    "type": "bar",
                    "title": "Provisional sector coverage",
                    "subtitle": "Selected symbol count; sectors require human confirmation.",
                    "dataset": "sector_coverage",
                    "sourceId": "candidate_quality_sql",
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
                            }
                        ],
                    },
                    "valueFormat": "number",
                }
            ],
            "tables": [
                {
                    "id": "readiness_table",
                    "title": "Proposed symbol readiness",
                    "subtitle": "Exact quality evidence; all activation decisions default to rejected.",
                    "dataset": "readiness",
                    "sourceId": "candidate_quality_sql",
                    "defaultSort": {"field": "quality_score", "direction": "desc"},
                    "columns": [
                        {"field": "symbol", "label": "Symbol", "type": "text"},
                        {
                            "field": "sector",
                            "label": "Provisional sector",
                            "type": "text",
                        },
                        {
                            "field": "valid_rows",
                            "label": "Valid rows",
                            "type": "number",
                        },
                        {
                            "field": "quality_score",
                            "label": "Quality",
                            "type": "number",
                        },
                        {"field": "readiness", "label": "Readiness", "type": "text"},
                        {"field": "activation", "label": "Activation", "type": "text"},
                    ],
                }
            ],
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# Expanded DSE equity research-universe approval pack",
                },
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": f"## Technical summary\n\n{len(selected)} equities across {len(sector_rows)} provisional sectors passed the non-performance screen. Official lifecycle evidence remains incomplete, so every activation decision is **REJECTED / NOT GRANTED**.",
                    "sourceId": "candidate_quality_sql",
                },
                {
                    "id": "sector_heading",
                    "type": "markdown",
                    "body": "## Sector quotas prevent one-industry concentration\n\nNo provisional sector contributes more than three symbols; labels still require human confirmation.",
                },
                {"id": "sector_chart", "type": "chart", "chartId": "sector_coverage"},
                {
                    "id": "quality_heading",
                    "type": "markdown",
                    "body": "## Quality evidence supports review, not activation\n\nCoverage and validity passed the screen, but listing, suspension, conflict, and corporate-action evidence remains unresolved.",
                },
                {"id": "readiness", "type": "table", "tableId": "readiness_table"},
                {
                    "id": "method",
                    "type": "markdown",
                    "body": "## Method excludes strategy performance\n\nThe selector uses valid coverage, OHLC validity, duplicate/conflict burden, adjustment availability, mapping confidence, volume-field availability, and sector caps only. It reads no strategy return or trade result.",
                },
                {
                    "id": "limits",
                    "type": "markdown",
                    "body": "## Limitations and next step\n\nDSEX is separate and rejected. The expanded study is prepared but not executed. Independent, symbol-by-symbol human review is required; qualification remains 0/60.",
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "accessIssues": [],
            "datasets": {"sector_coverage": sector_rows, "readiness": readiness_rows},
        },
        "package_info": {
            "controls": {"edit": False, "refresh": False},
            "originUrl": "artifact://expanded-research-universe",
        },
    }


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
    if not CANONICAL_DATABASE.is_file():
        raise RuntimeError("Canonical candidate database missing")
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        if registration is None:
            raise RuntimeError("Pinned strategy registration missing")
        strategy_before = {
            "lifecycle": registration.lifecycle_state,
            "promotion_status": registration.evidence.get("promotion_status"),
            "campaign_eligibility": registration.evidence.get("campaign_eligibility"),
        }
        if strategy_before != {
            "lifecycle": "research",
            "promotion_status": "blocked",
            "campaign_eligibility": False,
        }:
            raise RuntimeError("Strategy governance state mismatch")
        protected_before = _counts(db)
        audit_before = audit_status(db)

    payload = build_universe_candidate(CANONICAL_DATABASE)
    symbols = [str(row["symbol"]) for row in payload["proposed_universe"]]
    sectors = sorted(str(value) for value in payload["sector_counts"])
    plan = expanded_research_plan(symbols, sectors)
    run_id = (
        "review_" + _canonical_hash({"head": EXPECTED_HEAD, "symbols": symbols})[:24]
    )
    output = ROOT / "reports" / "expanded_research_universe" / run_id
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "universe_candidate.json", payload)
    _write_json(output / "dsex_only_approval_pack.json", payload["dsex_track"])
    _write_json(output / "expanded_research_plan.json", plan)
    markdown = _markdown(payload, plan)
    (output / "approval_pack.md").write_text(markdown, encoding="utf-8")
    with (output / "universe_quality.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        fields = [
            "symbol",
            "sector",
            "coverage_start",
            "coverage_end",
            "valid_rows",
            "invalid_rows",
            "duplicate_rows",
            "eligible_conflicts",
            "corporate_action_held_rows",
            "quality_score",
            "research_readiness_status",
            "proposed_activation_decision",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(payload["proposed_universe"] + payload["excluded_candidates"])
    artifact = output / "artifact.json"
    _write_json(artifact, _artifact(payload))
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
        else "blocked_after_bounded_artifact_contract_corrections"
    )

    evidence_hashes = {
        path.name: _hash_file(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    with SessionLocal() as db:
        prior_candidate_event = db.scalar(
            select(AuditEvent)
            .where(
                AuditEvent.event_type
                == "research.expanded_universe_candidate_prepared",
                AuditEvent.entity_id == REGISTRATION_ID,
            )
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
        event = append_audit(
            db,
            actor=args.operator,
            event_type="research.expanded_universe_candidate_prepared",
            entity_type="strategy_registration",
            entity_id=REGISTRATION_ID,
            new_state={
                "proposed_symbols": symbols,
                "activation_permission": "rejected_not_granted",
                "strategy_execution": False,
                "report_hashes": evidence_hashes,
                "qualification": "0/60",
                "selection_policy_version": "continuity-anchors-sector-minimums-quality-v1",
                "supersedes_audit_event_id": (
                    prior_candidate_event.id if prior_candidate_event else None
                ),
            },
        )
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        protected_after = _counts(db)
        strategy_after = {
            "lifecycle": registration.lifecycle_state if registration else None,
            "promotion_status": registration.evidence.get("promotion_status")
            if registration
            else None,
            "campaign_eligibility": registration.evidence.get("campaign_eligibility")
            if registration
            else None,
        }
        if (
            protected_before != protected_after
            or strategy_before != strategy_after
            or not verify_audit_chain(db)
        ):
            raise RuntimeError("Protected state or audit changed unexpectedly")
        audit_after = audit_status(db)
    audit_record = {
        "audit_event_id": event.id,
        "audit_event_hash": event.integrity_hash,
        "audit_before": audit_before,
        "audit_after": audit_after,
    }
    _write_json(output / "audit_record.json", audit_record)
    manifest = {
        "files": [
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": _hash_file(path),
            }
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        ],
        "protected_counts_before": protected_before,
        "protected_counts_after": protected_after,
        "strategy_before": strategy_before,
        "strategy_after": strategy_after,
        "dataset_activated": False,
        "strategy_executed": False,
        "activation_permission": "REJECTED / NOT GRANTED",
        "html_status": html_status,
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(output),
                "symbols": symbols,
                "manifest_hash": manifest["manifest_hash"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

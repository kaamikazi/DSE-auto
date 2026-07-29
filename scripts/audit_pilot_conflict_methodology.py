from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
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
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import append_audit, audit_status, verify_audit_chain  # noqa: E402
from app.services.pilot_conflict_methodology import (  # noqa: E402
    PILOT_SYMBOLS,
    build_pilot_methodology_audit,
)

EXPECTED_HEAD = "c8d8d6665a1c3dd44be36c4c8cfb9326265318ab"
REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
PACK_SCHEMA = "pilot_conflict_methodology_v1"
EVIDENCE_DIR = (
    ROOT / "reports" / "research_data_quality" / "canonical_candidate_0a834213759f5a79"
)
DATABASE = EVIDENCE_DIR / "canonical_candidate.sqlite3"
CONFLICT_EXPORT = (
    ROOT
    / "reports"
    / "final_batch_approval"
    / "review_1a1d23469e1c22d1c24cf5a6"
    / "unresolved_conflicts.json"
)
SOURCE_QUALITY = EVIDENCE_DIR / "source_quality_scores.json"
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


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()


def _counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in PROTECTED
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _csv_value(value: object) -> object:
    return (
        json.dumps(value, sort_keys=True, default=str)
        if isinstance(value, (dict, list, tuple, set))
        else value
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in row.items()} for row in rows
        )


def _source() -> dict[str, Any]:
    symbols = ",".join(f"'{symbol}'" for symbol in PILOT_SYMBOLS)
    return {
        "label": "Preserved canonical-candidate observations and prior conflict export",
        "query": {
            "sql": (
                "SELECT * FROM canonical_candidate.observations "
                f"WHERE normalized_symbol IN ({symbols});"
            ),
            "description": "Read-only five-symbol, field-semantic conflict-methodology audit.",
            "engine": "sqlite",
            "language": "sql",
            "tables_used": [
                "canonical_candidate.observations",
                "canonical_candidate.corporate_action_candidates",
            ],
            "filters": {
                "symbols": list(PILOT_SYMBOLS),
                "activation": False,
                "strategy_performance": "excluded",
            },
            "executed_at": datetime.now(UTC).isoformat(),
        },
    }


def _artifact(review: dict[str, Any]) -> dict[str, Any]:
    baseline_by_symbol = Counter(
        str(row["symbol"]) for row in review["baseline_conflicts"]
    )
    before_after = []
    for row in review["symbol_summary"]:
        before_after.extend(
            [
                {
                    "symbol": row["symbol"],
                    "phase": "Prior unresolved",
                    "conflicts": baseline_by_symbol[row["symbol"]],
                },
                {
                    "symbol": row["symbol"],
                    "phase": "Corrected genuine",
                    "conflicts": row["genuine_conflicts"],
                },
            ]
        )
    source = _source()
    total = review["totals"]
    return {
        "surface": "report",
        "manifest": {
            "surface": "report",
            "version": 1,
            "title": "Five-symbol DSE conflict-methodology audit",
            "description": "Read-only reconciliation audit; no activation or strategy execution.",
            "generatedAt": datetime.now(UTC).isoformat(),
            "sources": [],
            "cards": [],
            "charts": [
                {
                    "id": "conflict_before_after",
                    "type": "bar",
                    "title": "Conflict counts before and after eligibility correction",
                    "subtitle": "Five-symbol pilot; OHLC only, 0.1% relative tolerance.",
                    "dataset": "before_after",
                    "source": source,
                    "encodings": {
                        "x": {"field": "symbol", "type": "nominal", "label": "Symbol"},
                        "y": {
                            "field": "conflicts",
                            "type": "quantitative",
                            "label": "Conflicts",
                        },
                        "color": {
                            "field": "phase",
                            "type": "nominal",
                            "label": "Method",
                        },
                        "tooltip": [
                            {"field": "phase", "type": "nominal", "label": "Method"},
                            {
                                "field": "conflicts",
                                "type": "quantitative",
                                "label": "Conflicts",
                            },
                        ],
                    },
                    "valueFormat": "number",
                }
            ],
            "tables": [
                {
                    "id": "symbol_results",
                    "title": "Corrected pilot candidate results",
                    "subtitle": "Inactive logical rows after exact deduplication and eligibility checks.",
                    "dataset": "symbols",
                    "source": source,
                    "defaultSort": {"field": "symbol", "direction": "asc"},
                    "columns": [
                        {"field": "symbol", "label": "Symbol", "type": "text"},
                        {"field": "raw_rows", "label": "Raw rows", "type": "number"},
                        {
                            "field": "genuine_conflicts",
                            "label": "Genuine conflicts",
                            "type": "number",
                        },
                        {
                            "field": "tier_1_cross_source_confirmed",
                            "label": "T1",
                            "type": "number",
                        },
                        {
                            "field": "tier_2_single_source_high_quality",
                            "label": "T2",
                            "type": "number",
                        },
                        {
                            "field": "tier_3_research_only",
                            "label": "T3",
                            "type": "number",
                        },
                        {"field": "invalid_rows", "label": "Invalid", "type": "number"},
                    ],
                }
            ],
            "blocks": [
                {
                    "id": "title",
                    "type": "markdown",
                    "body": "# Five-symbol DSE conflict-methodology audit",
                },
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": (
                        "## Technical summary\n\nThe previous method overstated conflict: "
                        f"{total['baseline_conflicts']:,} records reduce to "
                        f"{total['genuine_conflicts']:,} eligible same-grain OHLC disagreements. "
                        f"The human queue contains {total['human_review_queue']} items and remains "
                        "methodology-only; no row is active."
                    ),
                },
                {
                    "id": "finding",
                    "type": "markdown",
                    "body": (
                        "## Cross-grain comparisons caused the explosion\n\nAdjusted/unadjusted "
                        "and known/unknown-grain comparisons are now rejected explicitly. Volume is "
                        "excluded because registered unit semantics are absent. The chart uses exact "
                        "prior and corrected counts; the large baseline does not establish bad prices."
                    ),
                },
                {
                    "id": "conflict_chart",
                    "type": "chart",
                    "chartId": "conflict_before_after",
                },
                {
                    "id": "definitions",
                    "type": "markdown",
                    "body": (
                        "## Scope, grain, and definitions\n\nThe cohort is IDLC, LANKABAFIN, "
                        "BATBC, SQURPHARMA, and POWERGRID. A genuine conflict requires aligned date, "
                        "high-confidence mapping, distinct raw files, matching adjustment grain, "
                        "collapsed exact duplicates, and an OHLC difference above 0.1%."
                    ),
                },
                {
                    "id": "candidate_heading",
                    "type": "markdown",
                    "body": (
                        "## Candidate tiers are evidence labels, not activation\n\nT1 requires "
                        "distinct-file price agreement. T2 uses a registered score of at least 70 "
                        "and at least 252 source observations. T3 remains research-only."
                    ),
                },
                {"id": "candidate_table", "type": "table", "tableId": "symbol_results"},
                {
                    "id": "method",
                    "type": "markdown",
                    "body": (
                        "## Method preserves rejected comparisons\n\nEvery rejected pair has reason codes; "
                        "duplicate lineage retains source row IDs, raw-file hashes, group IDs, and "
                        "representatives. Large price movement alone is insufficient corporate-action "
                        "evidence."
                    ),
                },
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": (
                        "## Limitations and robustness boundary\n\nSource independence, volume units, "
                        "official lifecycle evidence, and corporate actions remain unverified. Observed "
                        "bounds are not listing dates. The five symbols remain in methodology review."
                    ),
                },
                {
                    "id": "next",
                    "type": "markdown",
                    "body": (
                        "## Recommended next step\n\nHuman reviewers should resolve the four OHLC "
                        "disagreements and five lifecycle decisions, then separately approve source "
                        "semantics before any activation review."
                    ),
                },
                {
                    "id": "questions",
                    "type": "markdown",
                    "body": (
                        "## Further questions\n\nObtain official symbol lifecycle records, a "
                        "field-unit data dictionary, and evidence that the registered validation files "
                        "are independently derived."
                    ),
                },
            ],
        },
        "snapshot": {
            "version": 1,
            "status": "ready",
            "accessIssues": [],
            "datasets": {
                "before_after": before_after,
                "symbols": review["symbol_summary"],
            },
        },
        "package_info": {
            "controls": {"edit": False, "refresh": False},
            "originUrl": "artifact://pilot-conflict-methodology-audit",
        },
    }


def _markdown(review: dict[str, Any]) -> str:
    totals = review["totals"]
    roots = review["baseline_root_causes"]
    actions = review["corporate_action_audit_counts"]
    lines = [
        "# Five-symbol DSE conflict-methodology audit",
        "",
        "This is a read-only methodology audit. Qualification remains **0/60**; no data, strategy, campaign, session, proposal, order, transaction, or fill was activated.",
        "",
        "## Result",
        "",
        f"The prior five-symbol queue contained {totals['baseline_conflicts']:,} conflicts. Correct same-grain, same-date, distinct-file OHLC reconciliation leaves {totals['genuine_conflicts']} genuine disagreements. The human queue contains {totals['human_review_queue']} items and is manageable (<=500).",
        "",
        "## Baseline root causes",
        "",
        f"- adjusted versus unadjusted: {roots['adjusted_unadjusted_comparison']:,}",
        f"- known versus unknown/incompatible grain: {roots['incompatible_adjustment_grain']:,}",
        f"- volume-unit-only: {roots['unverified_volume_unit_only']:,}",
        f"- genuine same-grain OHLC disagreement: {roots['genuine_same_grain_source_disagreement']:,}",
        f"- duplicate logical dataset was a contributing flag on {review['baseline_contributing_causes']['duplicate_logical_dataset']:,} adjusted/unadjusted comparisons",
        "- same-source, turnover/value, exact-duplicate, alias, date-shift, rounding-only, malformed, and unknown primary causes: 0",
        "",
        "## Corrected comparison contract",
        "",
        "Only aligned, high-confidence, distinct-file, same-adjustment OHLC fields are eligible. Exact duplicates collapse first with complete lineage. Volume, turnover/value, and trade counts remain excluded until registered units and semantics match. Every ineligible pair retains one or more reason codes.",
        "",
        "## Corporate-action false-positive audit",
        "",
        f"The {totals['corporate_action_candidates']:,} heuristic candidates comprise {actions['ordinary_price_movement']:,} ordinary movements, {actions['missing_session_discontinuity']:,} missing-session discontinuities, {actions['long_source_gap']:,} long gaps, {actions['adjusted_unadjusted_divergence']:,} adjustment divergences, and {actions['duplicate_source_divergence']:,} duplicate-source divergences. Supported registered evidence: {actions['supported_by_registered_evidence']}; approved actions: 0.",
        "",
        "## Lifecycle and concentration",
        "",
        "All five symbols remain `lifecycle_evidence_pending`. Conservative windows use accepted known-adjustment observation bounds only and make no listing-date claim. Distinct validation files overlap, but independence is not proven; the long-coverage Mendeley source is the only usable source for most periods.",
        "",
        "## Per-symbol inactive candidates",
        "",
        "| Symbol | Raw | Logical | Deduped | Ineligible pairs | Genuine | Lifecycle hold | Invalid | T1 | T2 | T3 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in review["symbol_summary"]:
        lines.append(
            f"| {row['symbol']} | {row['raw_rows']} | {row['eligible_logical_rows']} | {row['exact_duplicates_collapsed']} | {row['ineligible_comparisons']} | {row['genuine_conflicts']} | {row['lifecycle_holds']} | {row['invalid_rows']} | {row['tier_1_cross_source_confirmed']} | {row['tier_2_single_source_high_quality']} | {row['tier_3_research_only']} |"
        )
    lines.extend(
        [
            "",
            "No symbol is recommended for activation review. T1/T2/T3 are inactive evidence labels; source independence, lifecycle, field units, and the four genuine disagreements remain unresolved.",
        ]
    )
    return "\n".join(lines) + "\n"


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
    for required in (DATABASE, CONFLICT_EXPORT, SOURCE_QUALITY):
        if not required.is_file():
            raise RuntimeError(f"Required preserved evidence missing: {required.name}")
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
        protected_before = _counts(db)
        audit_before = audit_status(db)

    review = build_pilot_methodology_audit(DATABASE, CONFLICT_EXPORT, SOURCE_QUALITY)
    run_id = (
        "pilot_"
        + _canonical_hash(
            {"head": EXPECTED_HEAD, "schema": PACK_SCHEMA, "scope": PILOT_SYMBOLS}
        )[:24]
    )
    output = ROOT / "reports" / "pilot_conflict_methodology" / run_id
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        key: value
        for key, value in review.items()
        if key
        not in {
            "baseline_conflicts",
            "duplicate_collapse_ledger",
            "ineligible_comparisons",
            "corrected_comparisons",
            "corporate_action_audit",
            "candidates",
        }
    }
    _write_json(output / "summary.json", summary)
    exports = {
        "baseline_conflict_audit": review["baseline_conflicts"],
        "comparison_eligibility_matrix": review["comparison_eligibility_matrix"],
        "duplicate_collapse_ledger": review["duplicate_collapse_ledger"],
        "ineligible_comparisons": review["ineligible_comparisons"],
        "corrected_comparisons": review["corrected_comparisons"],
        "corporate_action_audit": review["corporate_action_audit"],
        "lifecycle_evidence": review["lifecycle_evidence"],
        "source_overlap": review["source_overlap"],
        "pilot_candidates": review["candidates"],
        "human_review_queue": review["human_review_queue"],
    }
    for name, rows in exports.items():
        _write_json(output / f"{name}.json", rows)
        _write_csv(output / f"{name}.csv", rows)
    (output / "pilot_methodology_report.md").write_text(
        _markdown(review), encoding="utf-8"
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
            str(output / "pilot_methodology_report.html"),
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
        if builder.returncode == 0
        and (output / "pilot_methodology_report.html").is_file()
        else "blocked_after_bounded_builder_attempt"
    )
    evidence_hashes = {
        path.name: _hash(path)
        for path in sorted(output.iterdir())
        if path.is_file() and path.name not in {"audit_record.json", "manifest.json"}
    }
    with SessionLocal() as db:
        event = append_audit(
            db,
            actor=args.operator,
            event_type="research.pilot_conflict_methodology_audited",
            entity_type="research_methodology",
            entity_id=run_id,
            new_state={
                "scope": list(PILOT_SYMBOLS),
                "activation": False,
                "strategy_execution": False,
                "qualification": "0/60",
                "pack_schema": PACK_SCHEMA,
                "baseline_conflicts": review["totals"]["baseline_conflicts"],
                "genuine_conflicts": review["totals"]["genuine_conflicts"],
                "human_review_queue": review["totals"]["human_review_queue"],
                "output_hashes": evidence_hashes,
            },
        )
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        protected_after = _counts(db)
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
            protected_before != protected_after
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
        "protected_counts_before": protected_before,
        "protected_counts_after": protected_after,
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
                "baseline_conflicts": review["totals"]["baseline_conflicts"],
                "genuine_conflicts": review["totals"]["genuine_conflicts"],
                "human_review_queue": review["totals"]["human_review_queue"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

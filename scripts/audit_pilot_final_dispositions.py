from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import subprocess
import sys
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
    TIER_3_REASON_CODES,
    build_pilot_methodology_audit,
)

REGISTRATION_ID = "4faf2623-f458-4d96-93d0-e70e8af8f7f6"
PACK_SCHEMA = "pilot_final_disposition_v1"
SOURCE_PACK = (
    ROOT / "reports" / "pilot_conflict_methodology" / "pilot_e5003bc95233252838ac7307"
)
SOURCE_MANIFEST_HASH = (
    "1649bc90bcf6dae3b4f9ce22508775d010f30bc024d74521a6d4e33138156c38"
)
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


def _counts(db: Any) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count()).select_from(model)) or 0
        )
        for model in PROTECTED
    }


def _markdown(review: dict[str, Any]) -> str:
    totals = review["totals"]
    lines = [
        "# Pilot final-disposition reconciliation",
        "",
        "Read-only evidence audit. Qualification remains **0/60**. Activation default: **REJECTED / NOT GRANTED**.",
        "",
        "## Why the previous table did not reconcile",
        "",
        "The previous table placed row counts beside pair counts, excluded 132 invalid rows from its logical denominator, and combined 68 conflicting same-source duplicate keys with four genuine cross-source conflicts. It also labelled 1,199 distinct-file agreements T1 although derivation independence was not proven.",
        "",
        "## Count grains",
        "",
        f"- raw source rows: {totals['raw_source_rows']:,}",
        f"- logical deduplicated rows: {totals['logical_rows']:,}",
        f"- duplicate groups: {totals['duplicate_groups']:,}",
        f"- comparison pairs: {totals['comparison_pairs']:,}",
        f"- ineligible comparison pairs: {totals['ineligible_comparisons']:,}",
        f"- genuine conflict pairs: {totals['genuine_conflict_pairs']:,}",
        "",
        "## Exact row reconciliation",
        "",
        "| Symbol | Logical | T1 | T2 | T3 | Genuine | Lifecycle | Corp action | Mapping | Invalid | Duplicate conflict | Other | Balanced |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in review["symbol_summary"]:
        counts = row["final_disposition_counts"]
        lines.append(
            f"| {row['symbol']} | {row['logical_rows']} | {counts['tier_1_cross_source_confirmed']} | {counts['tier_2_single_source_high_quality']} | {counts['tier_3_research_only']} | {counts['held_genuine_conflict']} | {counts['held_lifecycle']} | {counts['held_corporate_action']} | {counts['held_mapping']} | {counts['rejected_invalid']} | {counts['rejected_duplicate_conflict']} | {counts['rejected_other']} | {row['reconciliation_equation']['balanced']} |"
        )
    counts = totals["final_disposition_counts"]
    lines.append(
        f"| **Combined** | **{totals['logical_rows']}** | **{counts['tier_1_cross_source_confirmed']}** | **{counts['tier_2_single_source_high_quality']}** | **{counts['tier_3_research_only']}** | **{counts['held_genuine_conflict']}** | **{counts['held_lifecycle']}** | **{counts['held_corporate_action']}** | **{counts['held_mapping']}** | **{counts['rejected_invalid']}** | **{counts['rejected_duplicate_conflict']}** | **{counts['rejected_other']}** | **{totals['reconciliation_equation']['balanced']}** |"
    )
    lines.extend(
        [
            "",
            "## Formal tiers",
            "",
            "T1 requires complete lineage, structurally valid OHLC, known adjustment grain, high-confidence mapping, proven independent eligible cross-source agreement, and no conflict or hold. T2 requires the same lineage/OHLC/grain/mapping controls, a high-quality primary source, unavailable independent validation, and no conflict or hold. T3 requires complete lineage and no structural OHLC failure, but must carry at least one exact weakness reason and is ineligible by default.",
            "",
            "## Tier-3 reason segmentation",
            "",
            "Counts are diagnostic flags and may exceed Tier-3 rows because a row can carry multiple reasons.",
            "",
            "| Symbol | " + " | ".join(TIER_3_REASON_CODES) + " |",
            "|---|" + "---:|" * len(TIER_3_REASON_CODES),
        ]
    )
    for row in review["symbol_summary"]:
        reasons = row["tier_3_reason_counts"]
        lines.append(
            f"| {row['symbol']} | "
            + " | ".join(str(reasons[reason]) for reason in TIER_3_REASON_CODES)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Human decisions and readiness",
            "",
            "Four conflict approval records and five lifecycle approval records are independent and blank. Priority reports for BATBC and SQURPHARMA, followed by IDLC, LANKABAFIN, and POWERGRID, all remain `human_decision_required`. No symbol is activated or recommended as ready.",
            "",
            "## Proposed policy",
            "",
            "T1/T2 would be eligible by default only under a separately activated policy. T3, every held status, and every rejected status are ineligible. Tier-3 inclusion would require explicit human authorization by reason category. This proposal is **REJECTED / NOT GRANTED**.",
        ]
    )
    return "\n".join(lines) + "\n"


def _html(review: dict[str, Any], markdown: str) -> str:
    payload = {
        "totals": review["totals"],
        "tier_definitions": review["tier_definitions"],
        "symbol_summary": review["symbol_summary"],
        "human_review_queue": review["human_review_queue"],
        "symbol_readiness": review["symbol_readiness"],
        "proposed_activation_policy": review["proposed_activation_policy"],
    }
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Pilot final-disposition reconciliation</title>"
        "<style>body{font:15px system-ui;max-width:1180px;margin:2rem auto;padding:0 1rem;color:#17202a}"
        "pre{white-space:pre-wrap;background:#f5f7f9;padding:1rem;border-radius:8px}"
        "details{margin:1rem 0}strong{color:#8b0000}</style></head><body>"
        "<h1>Pilot final-disposition reconciliation</h1>"
        "<p><strong>REJECTED / NOT GRANTED — no activation; qualification 0/60.</strong></p>"
        f"<pre>{html.escape(markdown)}</pre>"
        "<details><summary>Complete machine-readable review evidence</summary>"
        f"<pre>{html.escape(json.dumps(payload, indent=2, sort_keys=True, default=str))}</pre>"
        "</details></body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args()
    if _head() != args.expected_head:
        raise RuntimeError("Pinned Git HEAD mismatch")
    settings = get_settings()
    if (
        settings.TRADING_MODE,
        settings.LIVE_TRADING_ENABLED,
        settings.BROKER_ADAPTER,
    ) != ("paper", False, "disabled"):
        raise RuntimeError("Paper-only safety mismatch")
    for required in (
        DATABASE,
        CONFLICT_EXPORT,
        SOURCE_QUALITY,
        SOURCE_PACK / "manifest.json",
    ):
        if not required.is_file():
            raise RuntimeError(f"Required preserved evidence missing: {required}")
    source_manifest = json.loads(
        (SOURCE_PACK / "manifest.json").read_text(encoding="utf-8")
    )
    if source_manifest.get("manifest_hash") != SOURCE_MANIFEST_HASH:
        raise RuntimeError("Previous evidence-pack manifest linkage failed")
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit invalid")
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        strategy_before = {
            "lifecycle": registration.lifecycle_state if registration else None,
            "promotion": registration.evidence.get("promotion_status")
            if registration
            else None,
            "campaign_eligibility": registration.evidence.get("campaign_eligibility")
            if registration
            else None,
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
    if not review["totals"]["reconciliation_equation"]["balanced"]:
        raise RuntimeError("Final row reconciliation failed")
    if len(review["human_review_queue"]) != 9:
        raise RuntimeError("Expected exactly nine independent human decisions")
    run_id = (
        "pilot_final_"
        + _canonical_hash(
            {"head": args.expected_head, "schema": PACK_SCHEMA, "scope": PILOT_SYMBOLS}
        )[:24]
    )
    output = ROOT / "reports" / "pilot_final_disposition" / run_id
    output.mkdir(parents=True, exist_ok=False)

    excluded = {
        "baseline_conflicts",
        "duplicate_collapse_ledger",
        "ineligible_comparisons",
        "corrected_comparisons",
        "corporate_action_audit",
        "candidates",
    }
    _write_json(
        output / "summary.json", {k: v for k, v in review.items() if k not in excluded}
    )
    exports: dict[str, list[dict[str, Any]]] = {
        "final_row_dispositions": review["candidates"],
        "conflict_approval_records": review["conflict_approval_records"],
        "lifecycle_approval_records": review["lifecycle_approval_records"],
        "human_review_queue": review["human_review_queue"],
        "symbol_readiness": review["symbol_readiness"],
        "row_reconciliation": review["symbol_summary"],
    }
    tier_3_rows = [
        {"symbol": row["symbol"], "reason_code": reason, "count": count}
        for row in review["symbol_summary"]
        for reason, count in row["tier_3_reason_counts"].items()
    ]
    exports["tier_3_segmentation"] = tier_3_rows
    for name, rows in exports.items():
        _write_json(output / f"{name}.json", rows)
        _write_csv(output / f"{name}.csv", rows)
    markdown = _markdown(review)
    (output / "pilot_final_disposition_report.md").write_text(
        markdown, encoding="utf-8"
    )
    (output / "pilot_final_disposition_report.html").write_text(
        _html(review, markdown), encoding="utf-8"
    )
    _write_json(
        output / "source_linkage.json",
        {
            "source_pack": str(SOURCE_PACK),
            "source_manifest_hash": SOURCE_MANIFEST_HASH,
            "verified": True,
        },
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
            event_type="research.pilot_final_dispositions_reconciled",
            entity_type="research_methodology",
            entity_id=run_id,
            new_state={
                "scope": list(PILOT_SYMBOLS),
                "activation": False,
                "strategy_execution": False,
                "qualification": "0/60",
                "pack_schema": PACK_SCHEMA,
                "logical_rows": review["totals"]["logical_rows"],
                "human_review_queue": 9,
                "activation_default": "REJECTED / NOT GRANTED",
                "source_manifest_hash": SOURCE_MANIFEST_HASH,
                "output_hashes": evidence_hashes,
            },
        )
        registration = db.get(StrategyRegistration, REGISTRATION_ID)
        strategy_after = {
            "lifecycle": registration.lifecycle_state if registration else None,
            "promotion": registration.evidence.get("promotion_status")
            if registration
            else None,
            "campaign_eligibility": registration.evidence.get("campaign_eligibility")
            if registration
            else None,
        }
        protected_after = _counts(db)
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
    manifest: dict[str, Any] = {
        "schema": PACK_SCHEMA,
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": _hash(path)}
            for path in sorted(output.iterdir())
            if path.is_file() and path.name != "manifest.json"
        ],
        "source_manifest_hash": SOURCE_MANIFEST_HASH,
        "protected_counts_before": protected_before,
        "protected_counts_after": protected_after,
        "strategy_before": strategy_before,
        "strategy_after": strategy_after,
        "activation": False,
        "strategy_execution": False,
        "qualification": "0/60",
        "activation_default": "REJECTED / NOT GRANTED",
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "output": str(output),
                "manifest_hash": manifest["manifest_hash"],
                "logical_rows": review["totals"]["logical_rows"],
                "reconciliation_balanced": True,
                "human_review_queue": 9,
                "activation": False,
                "strategy_execution": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

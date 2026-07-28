from __future__ import annotations

import sys

if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import csv
import hashlib
import json
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

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
from app.services.report_provenance import (  # noqa: E402
    build_report_provenance,
    csv_provenance_columns,
    html_provenance,
    markdown_provenance,
)
from app.services.target_research_review import (  # noqa: E402
    audit_corporate_action_queue,
    build_target_subset,
    open_candidate_database,
)
from app.services.target_symbol_human_review import (  # noqa: E402
    build_calendar_review,
    build_corporate_action_review,
    build_dsex_mapping_review,
    build_review_samples,
    build_rounding_review,
    build_source_hierarchy_review,
    build_unexplained_conflict_review,
    build_volume_unit_review,
    provisional_policies,
    readiness_statuses,
)


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError(
            "Target review requires paper-only safety with broker disabled"
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


def _source_metadata() -> tuple[
    dict[str, str], dict[str, float], dict[str, dict[str, Any]]
]:
    catalog_payload = json.loads(
        (ROOT / "config" / "public_dse_source_catalog.json").read_text(encoding="utf-8")
    )
    urls = {
        item["sha256"]: item["source_url"]
        for item in catalog_payload["sources"]
        if item.get("sha256")
    }
    catalog_by_title = {
        str(item["title"]): item
        for item in catalog_payload["sources"]
        if item.get("title")
    }
    pack = (
        ROOT
        / "reports"
        / "research_data_quality"
        / "canonical_candidate_0a834213759f5a79"
    )
    inventory = json.loads(
        (pack / "dataset_inventory.json").read_text(encoding="utf-8")
    )
    quality = json.loads(
        (pack / "source_quality_scores.json").read_text(encoding="utf-8")
    )
    physical_by_logical = {
        item["logical_name"]: item["source_name"] for item in inventory
    }
    scores = {
        physical_by_logical.get(item["logical_name"], item["logical_name"]): float(
            item["score"]
        )
        for item in quality
    }
    catalog_by_source: dict[str, dict[str, Any]] = {}
    for item in inventory:
        logical = str(item["logical_name"])
        physical = str(item["source_name"])
        metadata = catalog_by_title.get(logical)
        if metadata is None:
            metadata = next(
                (
                    value
                    for title, value in catalog_by_title.items()
                    if title in logical or title in physical
                ),
                {},
            )
        catalog_by_source[physical] = metadata
    return urls, scores, catalog_by_source


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
    if isinstance(value, (dict, list, set, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _write_csv(
    path: Path, provenance: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    prefix = csv_provenance_columns(provenance)
    normalized = [
        prefix | {key: _csv_value(value) for key, value in row.items()} for row in rows
    ]
    fields = list(prefix)
    for row in normalized:
        fields.extend(key for key in row if key not in fields)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(normalized or [prefix])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Target-symbol human review and research-approval preparation",
        "",
        "**INACTIVE - HUMAN REVIEW REQUIRED - QUALIFICATION 0/60**",
        "",
    ]
    lines.extend(markdown_provenance(payload["provenance"]).rstrip().splitlines())
    lines.extend(
        [
            "",
            "## Scope finding",
            "",
            (
                "The legacy counts 690 volume-unit, 99 rounding, and 229 unexplained conflicts "
                "cover the global candidate database. The mandated GP/ACI/BRACBANK/DSEX scope "
                "contains 231, 1, and 5 respectively; out-of-scope rows remain unchanged."
            ),
            "",
            "## DSEX mapping",
            "",
            f"- Under-review rows: {payload['dsex_mapping']['total_rows']}",
            f"- Classifications: `{json.dumps(payload['dsex_mapping']['classification_counts'], sort_keys=True)}`",
            f"- Quality: `{json.dumps(payload['dsex_mapping']['quality_counts'], sort_keys=True)}`",
            "- Automatic merge: false",
            "",
            "## Conflict review",
            "",
            f"- Target volume cases: {payload['volume_review']['target_scope_count']}",
            f"- Target legacy-rounding cases: {payload['rounding_review']['target_scope_count']}",
            f"- Target unexplained cases: {payload['unexplained_conflicts']['target_scope_count']}",
            "",
            "## Readiness",
            "",
        ]
    )
    lines.extend(
        f"- {item['symbol']}: {item['status']} ({', '.join(item['blockers'])})"
        for item in payload["readiness"]
    )
    lines.extend(
        [
            "",
            "## Human approvals required",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in payload["human_approvals_required"])
    lines.extend(
        [
            "",
            "No mapping, hierarchy, dataset, policy, rule, fee, limit, strategy, campaign,",
            "session, proposal, order, transaction, or fill was approved, created, or activated.",
        ]
    )
    return "\n".join(lines) + "\n"


def _html(payload: dict[str, Any]) -> str:
    readiness = "".join(
        f"<tr><td>{escape(row['symbol'])}</td><td>{escape(row['status'])}</td>"
        f"<td>{escape(', '.join(row['blockers']))}</td></tr>"
        for row in payload["readiness"]
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Target DSE review</title></head>"
        "<body><h1>Target-symbol human review</h1>"
        "<p><strong>INACTIVE - HUMAN REVIEW REQUIRED - QUALIFICATION 0/60</strong></p>"
        f"{html_provenance(payload['provenance'])}"
        "<h2>Readiness</h2><table><tr><th>Symbol</th><th>Status</th><th>Blockers</th></tr>"
        f"{readiness}</table>"
        f"<pre>{escape(json.dumps(payload['summary_counts'], indent=2))}</pre>"
        "<p>No approval, activation, strategy run, campaign, order, or fill occurred.</p>"
        "</body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build inactive target-symbol review evidence"
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
        default=ROOT / "reports" / "target_symbol_human_review",
    )
    args = parser.parse_args()
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
    candidate = open_candidate_database(args.candidate_db)
    action_audit = audit_corporate_action_queue(candidate)
    subset = build_target_subset(
        candidate, source_scores=source_scores, source_urls=source_urls
    )
    dsex = build_dsex_mapping_review(candidate)
    volume = build_volume_unit_review(candidate)
    rounding = build_rounding_review(candidate)
    unexplained = build_unexplained_conflict_review(candidate, source_urls=source_urls)
    source_hierarchy = build_source_hierarchy_review(
        candidate, source_scores=source_scores, source_catalog=source_catalog
    )
    calendar = build_calendar_review(candidate)
    corporate = build_corporate_action_review(
        candidate,
        action_audit["rows"],
        subset["candidate_rows"],
        source_urls=source_urls,
    )
    policies = provisional_policies()
    conflicts_by_symbol = dict(Counter(row["symbol"] for row in unexplained["rows"]))
    source_approvals = dict(Counter(row["symbol"] for row in source_hierarchy))
    readiness = readiness_statuses(
        dsex_mapping_rows=dsex["total_rows"],
        conflicts_by_symbol=conflicts_by_symbol,
        source_approvals_by_symbol=source_approvals,
    )
    samples = build_review_samples(
        candidate,
        subset=subset,
        unexplained_rows=unexplained["rows"],
        corporate_rows=corporate,
        source_urls=source_urls,
    )
    candidate.close()
    with SessionLocal() as db:
        after = _operational_counts(db)
        audit_valid = verify_audit_chain(db)
    delta = {key: after[key] - before[key] for key in before}
    if any(delta.values()) or not audit_valid:
        raise RuntimeError(
            f"Target review changed protected state: {delta}; audit={audit_valid}"
        )
    approvals = [
        "Approve/reject 00DSEX as a DSEX alias and separately reject 680 OHLC-invalid rows.",
        "Approve a primary/secondary role for every target and adjustment grain.",
        "Establish volume field semantics before considering the observed factor of 100.",
        "Resolve five target price conflicts and the GP material volume disagreement.",
        "Provide issuer/ex-date evidence for target corporate-action candidates.",
        "Approve an authoritative DSE calendar before classifying gaps or holidays.",
        "Approve each provisional target policy before research activation can be proposed.",
    ]
    payload = {
        "provenance": provenance,
        "scope": ["GP", "ACI", "BRACBANK", "DSEX"],
        "dsex_mapping": {key: value for key, value in dsex.items() if key != "ledger"},
        "volume_review": {key: value for key, value in volume.items() if key != "rows"},
        "rounding_review": {
            key: value for key, value in rounding.items() if key != "rows"
        },
        "unexplained_conflicts": {
            key: value for key, value in unexplained.items() if key != "rows"
        },
        "corporate_action_summary": {
            "rows": len(corporate),
            "by_symbol": dict(Counter(row["normalized_symbol"] for row in corporate)),
            "by_classification": dict(
                Counter(row["human_classification"] for row in corporate)
            ),
            "confirmed": 0,
        },
        "calendar": calendar,
        "source_hierarchy": source_hierarchy,
        "provisional_policies": policies,
        "readiness": readiness,
        "human_approvals_required": approvals,
        "summary_counts": {
            "source_recommendations": len(source_hierarchy),
            "dsex_mapping_rows": dsex["total_rows"],
            "target_volume_rows": volume["target_scope_count"],
            "target_rounding_rows": rounding["target_scope_count"],
            "target_unexplained_rows": unexplained["target_scope_count"],
            "corporate_action_rows": len(corporate),
            "review_samples": len(samples),
        },
        "operational_before": before,
        "operational_after": after,
        "operational_delta": delta,
        "audit_valid": audit_valid,
        "activation_blocked": True,
        "qualification": "0/60",
    }
    output = args.output_root / f"review_{provenance['report_id']}"
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "review_summary.json", provenance, payload)
    _write_json(
        output / "source_hierarchy_review.json", provenance, {"rows": source_hierarchy}
    )
    _write_csv(output / "source_hierarchy_review.csv", provenance, source_hierarchy)
    _write_json(
        output / "dsex_mapping_review.json",
        provenance,
        {"summary": payload["dsex_mapping"], "rows": dsex["ledger"]},
    )
    _write_csv(output / "dsex_mapping_review.csv", provenance, dsex["ledger"])
    _write_json(
        output / "volume_unit_review.json",
        provenance,
        {"summary": payload["volume_review"], "rows": volume["rows"]},
    )
    _write_csv(output / "volume_unit_review.csv", provenance, volume["rows"])
    _write_json(
        output / "rounding_review.json",
        provenance,
        {"summary": payload["rounding_review"], "rows": rounding["rows"]},
    )
    _write_csv(output / "rounding_review.csv", provenance, rounding["rows"])
    _write_json(
        output / "unexplained_conflicts.json",
        provenance,
        {"summary": payload["unexplained_conflicts"], "rows": unexplained["rows"]},
    )
    _write_csv(output / "unexplained_conflicts.csv", provenance, unexplained["rows"])
    _write_json(
        output / "corporate_action_review.json", provenance, {"rows": corporate}
    )
    _write_csv(output / "corporate_action_review.csv", provenance, corporate)
    _write_json(output / "calendar_review.json", provenance, {"rows": calendar})
    _write_csv(output / "calendar_review.csv", provenance, calendar)
    _write_json(output / "provisional_policies.json", provenance, {"rows": policies})
    _write_csv(output / "provisional_policies.csv", provenance, policies)
    _write_json(output / "review_samples.json", provenance, {"rows": samples})
    _write_csv(output / "review_samples.csv", provenance, samples)
    (output / "human_review.md").write_text(_markdown(payload), encoding="utf-8")
    (output / "human_review.html").write_text(_html(payload), encoding="utf-8")
    files = sorted(output.iterdir())
    file_hashes = {
        path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in files
    }
    manifest = {
        "provenance": provenance,
        "files": file_hashes,
        "operational_delta": delta,
        "audit_valid": audit_valid,
        "activation_blocked": True,
        "qualification": "0/60",
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()
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
                "manifest_hash": manifest["manifest_hash"],
                "summary_counts": payload["summary_counts"],
                "readiness": readiness,
                "operational_delta": delta,
                "audit_valid": audit_valid,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import sys

# Avoid shadowing Python's stdlib ``operator`` module with scripts/operator.py.
if sys.path and sys.path[0].lower().rstrip("\\/").endswith("scripts"):
    sys.path.pop(0)

import argparse
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuthoritativeEvidence,
    DatasetImportRun,
    ExtractedClaim,
    GovernedDataset,
    Order,
    PaperSession,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import verify_audit_chain  # noqa: E402
from app.services.authoritative_evidence import intake_evidence_file  # noqa: E402
from app.services.governed_research_data import register_dataset  # noqa: E402
from app.services.public_source_collection import (  # noqa: E402
    SourceAttempt,
    compare_csv_sources,
    extract_rule_candidates,
    inspect_csv_stream,
    inspect_zip_csv,
    record_schema_preview,
    register_under_review_claims,
    validate_archive_manifest,
    write_attempt_manifest,
)


def _assert_safety() -> None:
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError(
            "Public-source collection requires paper-only safety settings"
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
    }


def _catalog() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (ROOT / "config" / "public_dse_source_catalog.json").read_text(
                encoding="utf-8"
            )
        ),
    )


def _verify_file(entry: dict[str, Any], raw_root: Path) -> Path:
    path = raw_root / str(entry["local_path"])
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if size != entry["file_size"] or digest != entry["sha256"]:
        raise RuntimeError(f"Immutable source verification failed: {path}")
    if path.suffix.lower() == ".zip":
        validate_archive_manifest(path)
    return path


def _publication_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _dataset_category(path: Path) -> str:
    normalized = path.as_posix()
    if normalized.startswith("mendeley_"):
        return "mendeley"
    if normalized.startswith("dsestocks/"):
        return "dse_stocks_archive"
    if normalized.startswith("amarstock/"):
        return "amarstock"
    raise ValueError(f"No dataset category for {path}")


def _register_dataset(
    db: Session,
    entry: dict[str, Any],
    source_path: Path,
    retained_root: Path,
    operator: str,
) -> GovernedDataset:
    existing = db.scalar(
        select(GovernedDataset).where(GovernedDataset.raw_sha256 == entry["sha256"])
    )
    if existing:
        return existing
    return register_dataset(
        db,
        filename=source_path.name,
        raw=source_path.read_bytes(),
        raw_dir=retained_root,
        source_category=_dataset_category(Path(str(entry["local_path"]))),
        source_name=str(entry["title"]),
        source_reference=str(entry["source_url"]),
        publisher=str(entry["publisher"]),
        publication_date=_publication_date(entry["publication_date"]),
        license_note=str(entry["license_note"]),
        operator=operator,
        timestamp_trust=str(entry["timestamp_trust"]),
        source_trust=str(entry["source_trust"]),
        stated_date_coverage=str(entry["stated_date_coverage"]),
        adjustment_status=str(entry["adjustment_status"]),
        notes="Public research source; not exchange-verified; not activated; no redistribution.",
    )


def _register_evidence(
    db: Session,
    entry: dict[str, Any],
    source_path: Path,
    retained_root: Path,
    operator: str,
) -> AuthoritativeEvidence:
    existing = db.scalar(
        select(AuthoritativeEvidence).where(
            AuthoritativeEvidence.file_hash == entry["sha256"]
        )
    )
    if existing:
        return existing
    is_official = entry["source_trust"] == "official_document"
    return intake_evidence_file(
        db,
        category="market_rule_evidence" if is_official else "dataset_metadata",
        title=str(entry["title"]),
        source_organization=str(entry["publisher"]),
        source_type=str(entry["source_trust"]),
        source_reference=str(entry["source_url"]),
        collected_by=operator,
        source_description="Public source retained unchanged; source status does not imply verification.",
        operator_attestation=(
            "Downloaded from the recorded public URL without access-control bypass; content and "
            "applicability remain unverified."
        ),
        filename=source_path.name,
        raw=source_path.read_bytes(),
        raw_dir=retained_root,
        declared_type=str(entry["mime_type"]),
        document_date=_publication_date(entry["publication_date"]),
        extraction={
            "source_trust": entry["source_trust"],
            "timestamp_trust": entry["timestamp_trust"],
            "license_note": entry["license_note"],
            "human_verified": False,
        },
    )


def _pdf_pages(path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "pypdf is required for deterministic public-document extraction"
        ) from exc
    reader = PdfReader(path)
    return [page.extract_text() or "" for page in reader.pages]


def _claims(
    db: Session,
    evidence: AuthoritativeEvidence,
    path: Path,
    entry: dict[str, Any],
    operator: str,
) -> list[ExtractedClaim]:
    existing = list(
        db.scalars(
            select(ExtractedClaim).where(ExtractedClaim.evidence_id == evidence.id)
        )
    )
    if existing:
        return existing
    candidates = extract_rule_candidates(_pdf_pages(path))
    return register_under_review_claims(
        db,
        evidence,
        candidates,
        source_url=str(entry["source_url"]),
        actor=operator,
    )


def _preview(db: Session, dataset: GovernedDataset, path: Path) -> DatasetImportRun:
    existing = db.scalar(
        select(DatasetImportRun).where(DatasetImportRun.dataset_id == dataset.id)
    )
    if existing:
        return existing
    inspection = (
        inspect_zip_csv(path)
        if path.suffix.lower() == ".zip"
        else inspect_csv_stream(path)
    )
    return record_schema_preview(db, dataset, inspection)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register retained approved public DSE sources"
    )
    parser.add_argument("--operator", default="public-source-collector")
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=ROOT / "data" / "evidence_workspace" / "public_sources" / "raw",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "evidence_workspace" / "public_sources",
    )
    args = parser.parse_args()
    _assert_safety()
    catalog = _catalog()
    args.output.mkdir(parents=True, exist_ok=True)
    database_path = ROOT / "backend" / "data" / "dse_autotrader.db"
    backup_path = args.output / "pre_public_source_collection.db"
    if database_path.is_file() and not backup_path.exists():
        shutil.copy2(database_path, backup_path)
    attempts: list[SourceAttempt] = []
    previews: list[dict[str, Any]] = []
    evidence_items: list[dict[str, Any]] = []
    with SessionLocal() as db:
        before = _operational_counts(db)
        for entry in catalog["sources"]:
            local_path = entry["local_path"]
            audit_linkage: list[str] = []
            if entry["result"] == "downloaded":
                source_path = _verify_file(entry, args.raw_root)
                if source_path.suffix.lower() in {".csv", ".zip"}:
                    dataset = _register_dataset(
                        db,
                        entry,
                        source_path,
                        ROOT / "data" / "research_datasets" / "raw",
                        args.operator,
                    )
                    preview = _preview(db, dataset, source_path)
                    audit_linkage = list(dataset.audit_event_ids)
                    previews.append(
                        {
                            "dataset_id": dataset.id,
                            "title": entry["title"],
                            "preview_id": preview.id,
                            "state": preview.state,
                            "activated": False,
                        }
                    )
                else:
                    evidence = _register_evidence(
                        db,
                        entry,
                        source_path,
                        ROOT / "data" / "evidence_workspace" / "raw",
                        args.operator,
                    )
                    claims = (
                        _claims(db, evidence, source_path, entry, args.operator)
                        if source_path.suffix.lower() == ".pdf"
                        else []
                    )
                    audit_linkage = list(evidence.audit_event_ids) + [
                        event_id
                        for claim in claims
                        for event_id in claim.audit_event_ids
                    ]
                    evidence_items.append(
                        {
                            "evidence_id": evidence.id,
                            "title": entry["title"],
                            "verification_status": evidence.verification_status,
                            "claim_count": len(claims),
                        }
                    )
            attempts.append(
                SourceAttempt(
                    source_url=str(entry["source_url"]),
                    publisher=str(entry["publisher"]),
                    title=str(entry["title"]),
                    result=str(entry["result"]),
                    accessed_at=str(catalog["accessed_at"]),
                    license_note=str(entry["license_note"]),
                    source_trust=str(entry["source_trust"]),
                    timestamp_trust=str(entry["timestamp_trust"]),
                    publication_date=entry["publication_date"],
                    local_path=local_path,
                    file_size=entry["file_size"],
                    mime_type=entry["mime_type"],
                    sha256=entry["sha256"],
                    stated_date_coverage=str(entry["stated_date_coverage"]),
                    stated_symbol_coverage=str(entry["stated_symbol_coverage"]),
                    adjustment_status=str(entry["adjustment_status"]),
                    audit_linkage=audit_linkage,
                    detail=(
                        "Downloaded source remains inactive and under review."
                        if entry["result"] == "downloaded"
                        else str(entry["license_note"])
                    ),
                )
            )
        after = _operational_counts(db)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise RuntimeError(
                f"Fail closed: source registration changed operational state: {delta}"
            )
        audit_valid = verify_audit_chain(db)
        if not audit_valid:
            raise RuntimeError("Canonical audit verification failed")
    comparisons = [
        compare_csv_sources(
            args.raw_root / "mendeley_5mww8rb9td_v1" / "DSE_Data.csv",
            args.raw_root / "dsestocks" / "Dhaka-Stock-Exchange-DSE-2021.csv",
            left_label="Mendeley 5mww8rb9td v1",
            right_label="DSE Stocks 2021 archive",
            output_dir=args.output / "discrepancies" / "mendeley_vs_dsestocks_2021",
            date_start="2021-01-01",
            date_end="2021-12-31",
        ),
        compare_csv_sources(
            args.raw_root / "amarstock" / "AmarStock_0_20230416.csv",
            args.raw_root / "amarstock" / "AmarStock_1_20230416.csv",
            left_label="AmarStock unadjusted 2023-04-16",
            right_label="AmarStock adjusted 2023-04-16",
            output_dir=args.output / "discrepancies" / "amarstock_adjustment_20230416",
        ),
    ]
    manifest_hash = write_attempt_manifest(
        attempts, args.output / "collection_manifest.json"
    )
    result = {
        "manifest_hash": manifest_hash,
        "sources_attempted": len(attempts),
        "downloaded_files": sum(item.result == "downloaded" for item in attempts),
        "manual_or_blocked": sum(item.result != "downloaded" for item in attempts),
        "previews": previews,
        "official_evidence": evidence_items,
        "comparisons": [
            {
                "report_hash": item["report_hash"],
                "overlap_rows": item["overlap_rows"],
                "counts": item["counts"],
                "output_paths": item["output_paths"],
            }
            for item in comparisons
        ],
        "operational_before": before,
        "operational_after": after,
        "operational_delta": delta,
        "audit_valid": True,
        "qualification": "0/60",
        "automatic_activation": False,
    }
    (args.output / "registration_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

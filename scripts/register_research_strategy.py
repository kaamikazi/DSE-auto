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
from app.core.database import SessionLocal, resolved_database_url  # noqa: E402
from app.models import (  # noqa: E402
    Order,
    PaperSession,
    Signal,
    StrategyRegistration,
    Transaction,
    ValidationCampaign,
)
from app.services.audit import audit_status, verify_audit_chain  # noqa: E402
from app.services.report_provenance import build_report_provenance  # noqa: E402
from app.services.research_strategy_registration import (  # noqa: E402
    APPROVED_DATASET_ID,
    execution_readiness,
    inspect_sqlite_registration,
    register_research_strategy,
    verify_strategy_artifacts,
)

PROTECTED_MODELS = (ValidationCampaign, PaperSession, Signal, Order, Transaction)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _protected_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__tablename__: int(
                db.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in PROTECTED_MODELS
        }


def _legacy_review() -> dict[str, Any]:
    sqlite_paths = sorted(
        {
            ROOT / "backend" / "data" / "dse_autotrader.db.pre_restore",
            *(ROOT / "reports" / "recovery").glob("**/operational.sqlite3"),
            *(ROOT / "reports" / "disaster_recovery").glob("**/*.db"),
        }
    )
    sqlite_results = [
        inspect_sqlite_registration(path) for path in sqlite_paths if path.is_file()
    ]
    reusable = [
        match
        for result in sqlite_results
        for match in result["matches"]
        if result["classification"] == "same_operational_database_identity"
    ]
    dumps = [
        {
            "path": str(path),
            "classification": "unverifiable_without_isolated_restore",
            "restored": False,
            "reason": "custom database dump was not restored blindly",
        }
        for path in sorted((ROOT / "reports" / "recovery").glob("*.dump"))
    ]
    governance_evidence = [
        {
            "path": "reports/evidence_workspace/approval_packs/ma_crossover_promotion_approval_pack_0189a5b5dcbd.json",
            "classification": "rewritten_history_evidence_only",
            "finding": "no registration identifier; promotion remained blocked",
        },
        {
            "path": "scripts/generate_pre_campaign_evidence.py",
            "classification": "template_not_identity_evidence",
            "finding": "registration-id default is UNASSIGNED",
        },
        {
            "path": "canonical audit chain through event 201",
            "classification": "same_operational_database_identity",
            "finding": "execution prohibition exists; no prior registration creation event or ID",
        },
    ]
    result: dict[str, Any] = {
        "reusable_registration_ids": reusable,
        "decision": "create_new_uuid",
        "reason": "No prior ID is proven to belong to the current operational registration identity.",
        "sqlite_evidence": sqlite_results,
        "custom_dump_evidence": dumps,
        "governance_evidence": governance_evidence,
    }
    result["review_hash"] = _canonical_hash(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the authorized ma_crossover research registration"
    )
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "reports" / "research_strategy_registration",
    )
    args = parser.parse_args()
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise RuntimeError("Research registration requires paper/false/disabled safety")
    if _git_head() != "a5e04272e7d75dcaf8836bce11b7e9d64b4a2daa":
        raise RuntimeError(
            "Registration authorization is bound to the expected Git HEAD"
        )
    if not args.authorization_file.is_file():
        raise FileNotFoundError("Registration authorization file is missing")

    artifact_identity = verify_strategy_artifacts(ROOT)
    authorization_text = args.authorization_file.read_text(encoding="utf-8")
    authorization_sha256 = hashlib.sha256(authorization_text.encode()).hexdigest()
    legacy_review = _legacy_review()
    registered_at = datetime.now(UTC).isoformat()
    before = _protected_counts()
    with SessionLocal() as db:
        if not verify_audit_chain(db):
            raise RuntimeError("Canonical audit chain is invalid")
        provenance = build_report_provenance(
            db,
            database_role=settings.DATABASE_ROLE,
            environment=settings.APP_ENV,
            database_url=resolved_database_url,
            dataset_ids=[APPROVED_DATASET_ID],
            strategy_version="ma_crossover@1.0.0 research",
        )
        audit_before = audit_status(db)
        registrations_before = int(
            db.scalar(select(func.count()).select_from(StrategyRegistration)) or 0
        )

    with SessionLocal() as db:
        registration, created = register_research_strategy(
            db,
            artifact_identity=artifact_identity,
            provenance=provenance,
            authorization_sha256=authorization_sha256,
            legacy_review_hash=legacy_review["review_hash"],
            registered_at=registered_at,
        )
        readiness = execution_readiness(registration)
        audit_after = audit_status(db)
        audit_valid = verify_audit_chain(db)
        registrations_after = int(
            db.scalar(select(func.count()).select_from(StrategyRegistration)) or 0
        )
    after = _protected_counts()
    if before != after:
        raise RuntimeError(
            "A protected campaign, session, signal, order, or fill state changed"
        )
    if created and registrations_after != registrations_before + 1:
        raise RuntimeError("Registration count did not increase by exactly one")
    if not created and registrations_after != registrations_before:
        raise RuntimeError("Idempotent registration changed the registration count")
    if (
        not audit_valid
        or readiness["status"] != "ready_for_research_execution_authorization"
    ):
        raise RuntimeError("Registration readiness or audit verification failed")
    expected_event_delta = 5 if created else 0
    if (
        audit_after["canonical_events"] - audit_before["canonical_events"]
        != expected_event_delta
    ):
        raise RuntimeError("Unexpected canonical audit event count")

    result = {
        "created": created,
        "registration_id": registration.id,
        "strategy": "ma_crossover@1.0.0",
        "lifecycle_state": registration.lifecycle_state,
        "artifact_identity": artifact_identity,
        "dataset_linkage": {
            "id": registration.evidence["approved_dataset_id"],
            "name": registration.evidence["approved_dataset_name"],
            "sha256": registration.evidence["approved_dataset_hash"],
        },
        "governance": {
            "promotion_status": registration.evidence["promotion_status"],
            "campaign_eligibility": registration.evidence["campaign_eligibility"],
            "execution_eligibility": registration.evidence["execution_eligibility"],
            "research_execution_authorized": registration.evidence[
                "research_execution_authorized"
            ],
            "qualification": registration.evidence["qualification"],
        },
        "readiness": readiness,
        "legacy_identity_review": legacy_review,
        "provenance": provenance,
        "authorization_sha256": authorization_sha256,
        "audit_before": audit_before,
        "audit_after": audit_after,
        "audit_valid": audit_valid,
        "protected_counts_before": before,
        "protected_counts_after": after,
        "registration_count_before": registrations_before,
        "registration_count_after": registrations_after,
        "strategy_executed": False,
        "no_real_money_authorization": registration.evidence[
            "no_real_money_authorization"
        ],
    }
    output = args.output_root / registration.id
    _write_json(output / "registration_result.json", result)
    _write_json(
        output / "authorization_record.json",
        {
            "authorization_text": authorization_text,
            "authorization_sha256": authorization_sha256,
            "registered_at": registered_at,
            "registration_id": registration.id,
        },
    )
    files = sorted(path for path in output.iterdir() if path.is_file())
    manifest: dict[str, Any] = {
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in files
        ]
    }
    manifest["manifest_hash"] = _canonical_hash(manifest)
    _write_json(output / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "registration_id": registration.id,
                "created": created,
                "readiness": readiness["status"],
                "audit_events_added": expected_event_delta,
                "manifest_hash": manifest["manifest_hash"],
                "output": str(output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

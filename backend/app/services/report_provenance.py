from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.database_identity import (
    REPOSITORY_ROOT,
    database_name,
    redacted_database_alias,
    sha256_file,
    sqlite_path_from_url,
)

PROVENANCE_FIELDS = (
    "report_id",
    "generated_at",
    "git_head",
    "application_version",
    "database_role",
    "database_engine",
    "database_location_or_alias",
    "database_fingerprint",
    "postgresql_database_name",
    "migration_revision",
    "audit_chain_id",
    "canonical_event_count",
    "legacy_archive_count",
    "dataset_ids",
    "rule_set_version",
    "fee_profile_version",
    "strategy_version",
    "execution_mode",
    "environment",
)


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _application_version() -> str:
    for line in (
        (REPOSITORY_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    ):
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def _scalar_if_table(db: Session, table: str, query: str) -> Any:
    bind = db.get_bind()
    if table not in inspect(bind).get_table_names():
        return None
    return db.execute(text(query)).scalar()


def build_report_provenance(
    db: Session,
    *,
    database_role: str,
    environment: str,
    database_url: str,
    dataset_ids: list[str] | None = None,
    rule_set_version: str = "not_activated",
    fee_profile_version: str = "not_activated",
    strategy_version: str = "not_activated",
    execution_mode: str = "paper_research_only",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    bind = db.get_bind()
    engine_name = bind.dialect.name
    sqlite_path = sqlite_path_from_url(database_url)
    if engine_name == "sqlite" and sqlite_path and sqlite_path.is_file():
        fingerprint = f"sha256:{sha256_file(sqlite_path)}"
    else:
        alias = redacted_database_alias(database_url)
        fingerprint = "connection-alias-sha256:" + hashlib.sha256(alias.encode()).hexdigest()
    migration = _scalar_if_table(db, "alembic_version", "SELECT version_num FROM alembic_version")
    chain_id = _scalar_if_table(
        db, "audit_chains", "SELECT id FROM audit_chains WHERE status='active' LIMIT 1"
    )
    canonical_events = 0
    legacy_events = 0
    if "audit_events" in inspect(bind).get_table_names():
        canonical_events = int(
            db.execute(
                text("SELECT COUNT(*) FROM audit_events WHERE chain_id IS NOT NULL")
            ).scalar()
            or 0
        )
        legacy_events = int(
            db.execute(text("SELECT COUNT(*) FROM audit_events WHERE chain_id IS NULL")).scalar()
            or 0
        )
    timestamp = (generated_at or datetime.now(UTC)).isoformat()
    core: dict[str, Any] = {
        "generated_at": timestamp,
        "git_head": _git_head(),
        "application_version": _application_version(),
        "database_role": database_role,
        "database_engine": engine_name,
        "database_location_or_alias": redacted_database_alias(database_url),
        "database_fingerprint": fingerprint,
        "postgresql_database_name": database_name(database_url),
        "migration_revision": migration or "unversioned",
        "audit_chain_id": chain_id,
        "canonical_event_count": canonical_events,
        "legacy_archive_count": legacy_events,
        "dataset_ids": sorted(dataset_ids or []),
        "rule_set_version": rule_set_version,
        "fee_profile_version": fee_profile_version,
        "strategy_version": strategy_version,
        "execution_mode": execution_mode,
        "environment": environment,
    }
    report_seed = json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)
    return {"report_id": hashlib.sha256(report_seed.encode()).hexdigest()[:24], **core}


def provenance_status(payload: dict[str, Any]) -> dict[str, Any]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return {
            "status": "legacy_unverified",
            "missing_fields": list(PROVENANCE_FIELDS),
        }
    missing = [field for field in PROVENANCE_FIELDS if field not in provenance]
    return {
        "status": "verified_provenance" if not missing else "legacy_unverified",
        "missing_fields": missing,
    }


def markdown_provenance(provenance: dict[str, Any]) -> str:
    lines = ["## Report provenance", "", "| Field | Value |", "|---|---|"]
    for field in PROVENANCE_FIELDS:
        value = provenance.get(field)
        rendered = (
            json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        )
        escaped = rendered.replace("|", "\\|")
        lines.append(f"| {field} | {escaped} |")
    return "\n".join(lines) + "\n"


def html_provenance(provenance: dict[str, Any]) -> str:
    from html import escape

    rows = "".join(
        f"<tr><th>{escape(field)}</th><td>{escape(str(provenance.get(field)))}</td></tr>"
        for field in PROVENANCE_FIELDS
    )
    return f"<section><h2>Report provenance</h2><table>{rows}</table></section>"


def csv_provenance_columns(provenance: dict[str, Any]) -> dict[str, str]:
    return {
        f"provenance_{field}": (
            json.dumps(provenance.get(field), sort_keys=True)
            if isinstance(provenance.get(field), (dict, list))
            else str(provenance.get(field))
        )
        for field in PROVENANCE_FIELDS
    }

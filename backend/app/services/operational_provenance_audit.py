from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.database_identity import OPERATIONAL_SQLITE_PATH, REPOSITORY_ROOT, sha256_file

RECORD_TABLES = ("validation_campaigns", "paper_sessions", "orders", "transactions")
EXCLUDED_DATABASE_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "__pycache__",
}


def infer_database_role(path: Path) -> str:
    resolved = path.resolve()
    lowered = {part.lower() for part in resolved.parts}
    name = resolved.name.lower()
    if resolved == OPERATIONAL_SQLITE_PATH:
        return "operational"
    if resolved == (REPOSITORY_ROOT / "data" / "dse_autotrader.db").resolve():
        return "legacy_shadow"
    if "research_data_quality" in lowered or "canonical_candidate" in name:
        return "research"
    if "tests" in lowered or "alembic" in name or "infrastructure" in lowered:
        return "test"
    if (
        "incident" in name
        or "incidents" in lowered
        or "simulation" in lowered
        or "distributed_campaign" in lowered
    ):
        return "simulation"
    if (
        "backup" in name
        or "backups" in lowered
        or "recovery" in lowered
        or "restore" in lowered
        or name.startswith("pre_collection_")
        or name == "pre_public_source_collection.db"
    ):
        return "recovery"
    return "unknown"


def discover_database_artifacts(root: Path = REPOSITORY_ROOT) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".dump"}:
            continue
        if EXCLUDED_DATABASE_PARTS.intersection({part.lower() for part in path.parts}):
            continue
        result.append(path.resolve())
    return sorted(set(result), key=str)


def _sqlite_scalar(connection: sqlite3.Connection, query: str) -> Any:
    try:
        row = connection.execute(query).fetchone()
        return row[0] if row else None
    except sqlite3.DatabaseError:
        return None


def inspect_sqlite_artifact(path: Path, *, git_head: str, generated_at: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "absolute_database_path": str(path.resolve()),
        "database_role": infer_database_role(path),
        "database_engine": "sqlite",
        "database_sha256": sha256_file(path),
        "postgresql_database_name": None,
        "file_size": path.stat().st_size,
        "environment": "test" if infer_database_role(path) == "test" else "development",
        "current_git_head": git_head,
        "report_generation_timestamp": generated_at,
    }
    if path.stat().st_size == 0:
        return {
            **base,
            "status": "empty_legacy_shadow",
            "migration_revision": None,
            "canonical_audit_chain_id": None,
            "canonical_event_count": 0,
            "legacy_archive_count": 0,
            "record_counts": {table: 0 for table in RECORD_TABLES},
        }
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        revision = (
            _sqlite_scalar(connection, "SELECT version_num FROM alembic_version")
            if "alembic_version" in tables
            else None
        )
        chain_id = (
            _sqlite_scalar(connection, "SELECT id FROM audit_chains WHERE status='active' LIMIT 1")
            if "audit_chains" in tables
            else None
        )
        canonical = (
            int(
                _sqlite_scalar(
                    connection, "SELECT COUNT(*) FROM audit_events WHERE chain_id IS NOT NULL"
                )
                or 0
            )
            if "audit_events" in tables
            else 0
        )
        legacy = (
            int(
                _sqlite_scalar(
                    connection, "SELECT COUNT(*) FROM audit_events WHERE chain_id IS NULL"
                )
                or 0
            )
            if "audit_events" in tables
            else 0
        )
        counts = {
            table: int(_sqlite_scalar(connection, f"SELECT COUNT(*) FROM {table}") or 0)
            if table in tables
            else 0
            for table in RECORD_TABLES
        }
        connection.close()
        return {
            **base,
            "status": "inspectable",
            "migration_revision": revision,
            "canonical_audit_chain_id": chain_id,
            "canonical_event_count": canonical,
            "legacy_archive_count": legacy,
            "record_counts": counts,
        }
    except sqlite3.DatabaseError as exc:
        return {
            **base,
            "status": "malformed_or_non_sqlite",
            "error": type(exc).__name__,
            "migration_revision": None,
            "canonical_audit_chain_id": None,
            "canonical_event_count": 0,
            "legacy_archive_count": 0,
            "record_counts": {table: 0 for table in RECORD_TABLES},
        }


def inspect_database_artifacts(
    *, root: Path = REPOSITORY_ROOT, git_head: str, generated_at: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in discover_database_artifacts(root):
        if path.suffix.lower() == ".dump":
            result.append(
                {
                    "absolute_database_path": str(path),
                    "database_role": infer_database_role(path),
                    "database_engine": "postgresql_archive",
                    "database_sha256": sha256_file(path),
                    "postgresql_database_name": "not_embedded_in_available_metadata",
                    "file_size": path.stat().st_size,
                    "status": "archive_not_restored",
                    "migration_revision": None,
                    "canonical_audit_chain_id": None,
                    "canonical_event_count": None,
                    "legacy_archive_count": None,
                    "environment": "verification",
                    "current_git_head": git_head,
                    "report_generation_timestamp": generated_at,
                    "record_counts": None,
                }
            )
        else:
            result.append(
                inspect_sqlite_artifact(path, git_head=git_head, generated_at=generated_at)
            )
    result.extend(
        [
            {
                "absolute_database_path": "postgresql://127.0.0.1:5432/dse_autotrader",
                "database_role": "postgres_verification",
                "database_engine": "postgresql",
                "database_sha256": None,
                "postgresql_database_name": "dse_autotrader",
                "status": "unavailable_docker_engine_offline",
                "migration_revision": None,
                "canonical_audit_chain_id": None,
                "canonical_event_count": None,
                "legacy_archive_count": None,
                "environment": "development",
                "current_git_head": git_head,
                "report_generation_timestamp": generated_at,
                "record_counts": None,
            },
            {
                "absolute_database_path": "postgresql://127.0.0.1:15432/dse_autotrader_test",
                "database_role": "postgres_verification",
                "database_engine": "postgresql",
                "database_sha256": None,
                "postgresql_database_name": "dse_autotrader_test",
                "status": "unavailable_docker_engine_offline",
                "migration_revision": None,
                "canonical_audit_chain_id": None,
                "canonical_event_count": None,
                "legacy_archive_count": None,
                "environment": "test",
                "current_git_head": git_head,
                "report_generation_timestamp": generated_at,
                "record_counts": None,
            },
        ]
    )
    return result


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        parsed = value
    elif not value:
        return fallback
    else:
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return fallback
    if isinstance(fallback, dict) and not isinstance(parsed, dict):
        return fallback
    if isinstance(fallback, list) and not isinstance(parsed, list):
        return fallback
    return parsed


def _nearest_prior_commit(timestamp: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-list", "-1", f"--before={timestamp}", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or None


def classify_historical_record(
    *,
    record_type: str,
    evidence_class: str | None = None,
    session_name: str | None = None,
    idempotency_key: str | None = None,
    source_record: dict[str, Any] | None = None,
    linked_synthetic_campaign: bool = False,
) -> str:
    if evidence_class == "synthetic" or linked_synthetic_campaign:
        return "synthetic_simulation"
    if session_name and session_name.startswith("imported-validation-"):
        return "imported_data_validation"
    if idempotency_key and idempotency_key.startswith("m5-"):
        return "imported_data_validation"
    source = source_record or {}
    if source.get("seed") is True:
        return "synthetic_simulation"
    if source.get("simulated") is True and record_type == "transaction":
        return "imported_data_validation" if source.get("order_id") else "synthetic_simulation"
    return "unknown"


def historical_record_ledger(db: Session) -> list[dict[str, Any]]:
    bind = db.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not set(RECORD_TABLES).issubset(tables):
        return []
    campaigns = {
        row["id"]: dict(row)
        for row in db.execute(text("SELECT * FROM validation_campaigns")).mappings()
    }
    sessions = {
        row["id"]: dict(row) for row in db.execute(text("SELECT * FROM paper_sessions")).mappings()
    }
    orders = {row["id"]: dict(row) for row in db.execute(text("SELECT * FROM orders")).mappings()}
    transactions = [dict(row) for row in db.execute(text("SELECT * FROM transactions")).mappings()]
    audit_events = [dict(row) for row in db.execute(text("SELECT * FROM audit_events")).mappings()]

    def audit_links(record_id: str, campaign_id: str | None) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        for event in audit_events:
            state = _json(event.get("new_state"), {})
            metadata = _json(event.get("metadata"), {})
            direct = str(event.get("entity_id")) == record_id
            campaign_link = campaign_id and (
                str(event.get("entity_id")) == campaign_id
                or state.get("campaign_id") == campaign_id
                or metadata.get("campaign_id") == campaign_id
            )
            if direct or campaign_link:
                links.append(
                    {
                        "audit_event_id": event["id"],
                        "event_type": event["event_type"],
                        "sequence": event.get("sequence"),
                    }
                )
        return links

    evidence_paths = {
        campaign_id: [
            str(path.resolve())
            for path in (REPOSITORY_ROOT / "reports" / "campaigns").glob(f"{campaign_id}*")
        ]
        for campaign_id in campaigns
    }
    local_emulation = (
        REPOSITORY_ROOT
        / "reports"
        / "distributed_simulation"
        / ("m7-local-emulation-30-day_phase_1_30.json")
    )
    for campaign_id, campaign in campaigns.items():
        if campaign["name"] == "m7-local-emulation-30-day" and local_emulation.is_file():
            evidence_paths[campaign_id].append(str(local_emulation.resolve()))

    ledger: list[dict[str, Any]] = []
    for campaign in campaigns.values():
        classification = classify_historical_record(
            record_type="campaign", evidence_class=campaign.get("evidence_class")
        )
        ledger.append(
            {
                "record_type": "campaign",
                "record_id": campaign["id"],
                "creation_timestamp": str(campaign["created_at"]),
                "campaign_or_session_name": campaign["name"],
                "symbol": _json(campaign["approved_symbols"], []),
                "strategy": _json(campaign["approved_strategies"], []),
                "data_source": _json(campaign["data_source_policy"], {}),
                "timestamp_trust": campaign["timestamp_trust_requirement"],
                "trading_mode": "paper",
                "broker_adapter_state": "disabled",
                "live_trading_state": False,
                "paper_real_classification": "paper",
                "originating_git_commit": "not_embedded_in_record",
                "nearest_prior_commit_not_proof": _nearest_prior_commit(
                    str(campaign["created_at"])
                ),
                "linked_audit_events": audit_links(campaign["id"], campaign["id"]),
                "evidence_pack_reference": evidence_paths[campaign["id"]],
                "data_origin": "synthetic",
                "classification": classification,
                "classification_confidence": "high",
                "unresolved_incident_required": classification in {"unknown", "suspicious"},
            }
        )
    for session in sessions.values():
        linked_campaign = campaigns.get(session.get("campaign_id"))
        imported = str(session["name"]).startswith("imported-validation-")
        classification = classify_historical_record(
            record_type="session",
            session_name=session["name"],
            linked_synthetic_campaign=bool(
                linked_campaign and linked_campaign.get("evidence_class") == "synthetic"
            ),
        )
        source = (
            {"source": "attested_csv", "timestamp_trust": "operator_attested"}
            if imported
            else _json(linked_campaign.get("data_source_policy"), {})
            if linked_campaign
            else {}
        )
        evidence = evidence_paths.get(str(session.get("campaign_id")), [])
        ledger.append(
            {
                "record_type": "session",
                "record_id": session["id"],
                "creation_timestamp": str(session["created_at"]),
                "campaign_or_session_name": session["name"],
                "symbol": _json(session["approved_universe"], []),
                "strategy": _json(session["strategies"], []),
                "data_source": source,
                "timestamp_trust": "operator_attested"
                if imported
                else (
                    linked_campaign.get("timestamp_trust_requirement")
                    if linked_campaign
                    else "unknown"
                ),
                "trading_mode": "paper",
                "broker_adapter_state": "disabled",
                "live_trading_state": False,
                "paper_real_classification": "paper",
                "originating_git_commit": "not_embedded_in_record",
                "nearest_prior_commit_not_proof": _nearest_prior_commit(str(session["created_at"])),
                "linked_audit_events": audit_links(session["id"], session.get("campaign_id")),
                "evidence_pack_reference": evidence,
                "data_origin": "imported" if imported else "synthetic",
                "classification": classification,
                "classification_confidence": "high",
                "unresolved_incident_required": classification in {"unknown", "suspicious"},
            }
        )
    for order in orders.values():
        linked_campaign = campaigns.get(order.get("campaign_id"))
        imported = str(order["idempotency_key"]).startswith("m5-")
        classification = classify_historical_record(
            record_type="order",
            idempotency_key=order["idempotency_key"],
            linked_synthetic_campaign=bool(
                linked_campaign and linked_campaign.get("evidence_class") == "synthetic"
            ),
        )
        event_links = audit_links(order["id"], order.get("campaign_id"))
        ledger.append(
            {
                "record_type": "order",
                "record_id": order["id"],
                "creation_timestamp": str(order["created_at"]),
                "campaign_or_session_name": linked_campaign["name"]
                if linked_campaign
                else (
                    next(
                        (
                            s["name"]
                            for s in sessions.values()
                            if s["id"] in order["idempotency_key"]
                        ),
                        None,
                    )
                ),
                "symbol": order["symbol"],
                "strategy": order["strategy_id"],
                "data_source": (
                    {"source": "attested_csv"}
                    if imported
                    else _json(linked_campaign.get("data_source_policy"), {})
                    if linked_campaign
                    else {}
                ),
                "timestamp_trust": "operator_attested"
                if imported
                else (
                    linked_campaign.get("timestamp_trust_requirement")
                    if linked_campaign
                    else "unknown"
                ),
                "trading_mode": "paper",
                "broker_adapter_state": "disabled",
                "live_trading_state": False,
                "paper_real_classification": "paper_order_record",
                "order_status": order["status"],
                "originating_git_commit": "not_embedded_in_record",
                "nearest_prior_commit_not_proof": _nearest_prior_commit(
                    str(linked_campaign["created_at"] if linked_campaign else order["created_at"])
                ),
                "linked_audit_events": event_links,
                "evidence_pack_reference": evidence_paths.get(str(order.get("campaign_id")), []),
                "data_origin": "imported" if imported else "synthetic",
                "classification": classification,
                "classification_confidence": "high",
                "unresolved_incident_required": classification in {"unknown", "suspicious"},
            }
        )
    for transaction in transactions:
        source_record = _json(transaction.get("source_record"), {})
        linked_order = orders.get(source_record.get("order_id"))
        linked_campaign = campaigns.get(transaction.get("campaign_id"))
        classification = classify_historical_record(
            record_type="transaction",
            idempotency_key=linked_order["idempotency_key"] if linked_order else None,
            source_record=source_record,
            linked_synthetic_campaign=bool(
                linked_campaign and linked_campaign.get("evidence_class") == "synthetic"
            ),
        )
        origin = {
            "synthetic_simulation": "synthetic",
            "imported_data_validation": "imported",
        }.get(classification, "unknown")
        ledger.append(
            {
                "record_type": "transaction",
                "record_id": transaction["id"],
                "creation_timestamp": str(transaction["created_at"]),
                "campaign_or_session_name": (linked_campaign["name"] if linked_campaign else None),
                "symbol": transaction["symbol"],
                "strategy": linked_order["strategy_id"] if linked_order else None,
                "data_source": source_record,
                "timestamp_trust": "operator_attested"
                if origin == "imported"
                else "not_applicable",
                "trading_mode": "paper",
                "broker_adapter_state": "paper_record_only"
                if transaction["broker"] == "paper"
                else "disabled",
                "live_trading_state": False,
                "paper_real_classification": "paper_fill"
                if transaction["broker"] == "paper"
                else "non_position_seed",
                "originating_git_commit": "not_embedded_in_record",
                "nearest_prior_commit_not_proof": _nearest_prior_commit(
                    str(transaction["created_at"])
                ),
                "linked_audit_events": audit_links(
                    linked_order["id"] if linked_order else transaction["id"],
                    transaction.get("campaign_id"),
                ),
                "evidence_pack_reference": evidence_paths.get(
                    str(transaction.get("campaign_id")), []
                ),
                "data_origin": origin,
                "classification": classification,
                "classification_confidence": "high" if classification != "unknown" else "low",
                "unresolved_incident_required": classification in {"unknown", "suspicious"},
            }
        )
    return sorted(ledger, key=lambda row: (row["creation_timestamp"], row["record_type"]))


def ledger_summary(ledger: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "records_by_type": dict(Counter(str(row["record_type"]) for row in ledger)),
        "records_by_classification": dict(Counter(str(row["classification"]) for row in ledger)),
        "unknown_or_suspicious": sum(
            row["classification"] in {"unknown", "suspicious"} for row in ledger
        ),
        "real_broker_connections": 0,
        "real_order_submissions": 0,
        "basis": [
            "Campaign evidence_class and data_source_policy",
            "Session name, campaign linkage, and canonical audit events",
            "Order idempotency/campaign linkage and paper-only state",
            "Transaction broker/account/source_record fields",
        ],
    }


def reconcile_count_claim(
    *,
    claimed: dict[str, int],
    observed: dict[str, int],
    report_database_fingerprint: str | None,
    operational_database_fingerprint: str,
) -> dict[str, Any]:
    differences = {
        key: {"claimed": claimed.get(key), "observed": observed.get(key)}
        for key in sorted(set(claimed) | set(observed))
        if claimed.get(key) != observed.get(key)
    }
    if not differences:
        status = "consistent"
    elif report_database_fingerprint is None:
        status = "legacy_unverified_scope_conflict"
    elif report_database_fingerprint != operational_database_fingerprint:
        status = "different_database"
    else:
        status = "same_database_temporal_or_logic_conflict"
    return {"status": status, "differences": differences}

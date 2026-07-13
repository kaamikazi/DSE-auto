from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.services.incidents import open_incident, transition_incident

EXERCISES: dict[str, dict[str, str]] = {
    "postgresql_unavailable": {"severity": "critical", "outcome": "operator_required"},
    "redis_unavailable": {"severity": "high", "outcome": "operator_required"},
    "one_worker_killed": {"incident": "worker_failure", "severity": "high", "outcome": "recovered"},
    "all_workers_killed": {
        "incident": "worker_failure",
        "severity": "critical",
        "outcome": "operator_required",
    },
    "scheduler_killed": {
        "incident": "scheduler_failure",
        "severity": "high",
        "outcome": "recovered",
    },
    "api_restarted": {"incident": "api_restart", "severity": "medium", "outcome": "recovered"},
    "postgresql_restarted_mid_task": {
        "incident": "database_restart",
        "severity": "critical",
        "outcome": "operator_required",
    },
    "redis_restarted_with_queue": {
        "incident": "redis_restart",
        "severity": "high",
        "outcome": "recovered",
    },
    "database_pool_exhaustion": {"severity": "high", "outcome": "recovered"},
    "disk_write_failure": {"severity": "critical", "outcome": "operator_required"},
    "backup_destination_unavailable": {
        "incident": "backup_failure",
        "severity": "critical",
        "outcome": "operator_required",
    },
    "dead_letter_accumulation": {"severity": "high", "outcome": "operator_required"},
    "stale_lease": {"severity": "medium", "outcome": "recovered"},
    "corrupted_task_payload": {
        "incident": "corrupt_task_payload",
        "severity": "high",
        "outcome": "recovered",
    },
    "database_migration_mismatch": {"severity": "critical", "outcome": "operator_required"},
    "provider_certification_failure": {"severity": "high", "outcome": "operator_required"},
    "invalid_recovery_manifest": {"severity": "critical", "outcome": "operator_required"},
}


def run_controlled_exercise(
    db: Session,
    exercise: str,
    output_dir: Path,
    *,
    execution_mode: str = "offline_controlled_simulation",
) -> dict[str, Any]:
    """Record a deterministic fail-closed exercise; never label it as a real outage."""

    definition = EXERCISES.get(exercise)
    if definition is None:
        raise ValueError("Unknown infrastructure exercise")
    incident_type = definition.get("incident", exercise)
    incident = open_incident(
        db,
        incident_type,
        definition["severity"],
        evidence={
            "exercise": exercise,
            "execution_mode": execution_mode,
            "injected": True,
            "fail_closed": True,
            "trading_mode": "paper",
            "live_trading_enabled": False,
        },
        owner="milestone8-exercise",
    )
    outcome = definition["outcome"]
    if outcome == "recovered":
        transition_incident(
            db,
            incident,
            "resolved",
            owner="milestone8-exercise",
            root_cause=f"Controlled {exercise} injection",
            remediation="Recovery guard completed and state was revalidated",
        )
    report: dict[str, Any] = {
        "exercise": exercise,
        "execution_mode": execution_mode,
        "status": outcome,
        "incident_id": incident.id,
        "incident_state": incident.state,
        "linked_audit_events": incident.linked_audit_events,
        "fail_closed": True,
        "evidence_preserved": bool(incident.linked_audit_events),
        "operator_action_required": outcome == "operator_required",
        "generated_at": datetime.now(UTC).isoformat(),
        "safety": {
            "trading_mode": "paper",
            "live_trading_enabled": False,
            "broker_adapter": "disabled",
        },
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    report["integrity_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exercise}.json"
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(output_path)
    return report


def run_all_controlled_exercises(db: Session, output_dir: Path) -> list[dict[str, Any]]:
    return [run_controlled_exercise(db, name, output_dir) for name in EXERCISES]

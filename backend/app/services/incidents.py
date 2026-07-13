from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import OperationalIncident
from app.notifications.telegram import send_telegram_alert
from app.services.audit import append_audit
from app.services.events import emit_event

INCIDENT_TYPES = {
    "provider_outage",
    "stale_data",
    "timestamp_trust_failure",
    "reconciliation_mismatch",
    "audit_failure",
    "missed_scheduler_job",
    "missed_eod",
    "campaign_drawdown_breach",
    "backup_failure",
    "database_failure",
    "notification_failure",
    "unexpected_process_restart",
}
INCIDENT_STATES = {"open", "acknowledged", "mitigated", "resolved", "accepted_risk"}


def open_incident(
    db: Session,
    incident_type: str,
    severity: str,
    *,
    campaign_id: str | None = None,
    evidence: dict[str, Any] | None = None,
    owner: str | None = None,
) -> OperationalIncident:
    if incident_type not in INCIDENT_TYPES:
        raise ValueError("Unknown incident type")
    if severity not in {"low", "medium", "high", "critical"}:
        raise ValueError("Unknown incident severity")
    incident = OperationalIncident(
        campaign_id=campaign_id,
        incident_type=incident_type,
        state="open",
        severity=severity,
        owner=owner,
        evidence=evidence or {},
    )
    db.add(incident)
    db.flush()
    outbox = emit_event(
        db,
        "incident_opened",
        aggregate_type="operational_incident",
        aggregate_id=incident.id,
        payload={"type": incident_type, "severity": severity, "campaign_id": campaign_id},
        idempotency_key=f"incident-opened:{incident.id}",
        correlation_id=campaign_id,
    )
    audit = append_audit(
        db,
        actor="operations",
        event_type="incident.opened",
        entity_type="operational_incident",
        entity_id=incident.id,
        new_state={"type": incident_type, "severity": severity, "campaign_id": campaign_id},
    )
    incident.linked_audit_events = [audit.id]
    outbox.audit_event_id = audit.id
    db.commit()
    if severity == "critical":
        send_telegram_alert(f"CRITICAL PAPER-OPS INCIDENT: {incident_type} ({incident.id})")
    return incident


def transition_incident(
    db: Session,
    incident: OperationalIncident,
    target_state: str,
    *,
    owner: str | None = None,
    root_cause: str | None = None,
    remediation: str | None = None,
) -> OperationalIncident:
    if target_state not in INCIDENT_STATES:
        raise ValueError("Unknown incident state")
    allowed = {
        "open": {"acknowledged", "mitigated", "resolved", "accepted_risk"},
        "acknowledged": {"mitigated", "resolved", "accepted_risk"},
        "mitigated": {"resolved", "accepted_risk", "open"},
        "resolved": set(),
        "accepted_risk": {"open", "resolved"},
    }
    if target_state not in allowed[incident.state]:
        raise ValueError(f"Invalid incident transition {incident.state} -> {target_state}")
    previous = incident.state
    incident.state = target_state
    incident.owner = owner or incident.owner
    incident.root_cause = root_cause or incident.root_cause
    incident.remediation = remediation or incident.remediation
    now = datetime.now(UTC)
    if target_state == "acknowledged":
        incident.acknowledged_at = now
    if target_state == "resolved":
        incident.resolved_at = now
    audit = append_audit(
        db,
        actor="operator",
        event_type="incident.state_changed",
        entity_type="operational_incident",
        entity_id=incident.id,
        previous_state={"state": previous},
        new_state={"state": target_state, "owner": incident.owner},
    )
    incident.linked_audit_events = [*incident.linked_audit_events, audit.id]
    db.commit()
    return incident

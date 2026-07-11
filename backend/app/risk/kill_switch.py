from sqlalchemy.orm import Session

from app.models import RiskState
from app.services.audit import append_audit

VALID_STATES = {"healthy", "warning", "trading_paused", "reconciliation_required", "emergency_stop"}


def get_state(db: Session) -> RiskState:
    state = db.get(RiskState, 1)
    if state is None:
        state = RiskState(
            id=1, state="reconciliation_required", reason="Startup reconciliation required"
        )
        db.add(state)
        db.flush()
    return state


def set_state(db: Session, state: str, reason: str, actor: str = "system") -> RiskState:
    if state not in VALID_STATES:
        raise ValueError(f"Invalid kill-switch state: {state}")
    record = get_state(db)
    previous = {"state": record.state, "reason": record.reason}
    record.state, record.reason = state, reason
    append_audit(
        db,
        actor=actor,
        event_type="risk.kill_switch_changed",
        entity_type="risk_state",
        entity_id="1",
        previous_state=previous,
        new_state={"state": state, "reason": reason},
    )
    db.commit()
    return record

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FeeProfile, MarketRuleSet, PaperAccount, PaperSession, PaperSessionRun
from app.services.audit import append_audit

ACTIVE_STATES = {"warming_up", "running", "paused", "degraded", "reconciliation_required"}
TRANSITIONS = {
    "configured": {"warming_up", "stopped"},
    "warming_up": {"running", "degraded", "reconciliation_required", "failed", "stopped"},
    "running": {"paused", "degraded", "reconciliation_required", "stopped", "completed"},
    "paused": {"running", "stopped", "reconciliation_required"},
    "degraded": {"running", "reconciliation_required", "stopped", "failed"},
    "reconciliation_required": {"running", "stopped", "failed"},
    "stopped": set(),
    "completed": set(),
    "failed": set(),
}


def create_session(
    db: Session,
    name: str,
    universe: list[str],
    strategies: list[str],
    risk_profile: dict[str, Any],
    fill_model: str = "pessimistic",
    account_id: int = 1,
) -> PaperSession:
    if fill_model not in {"pessimistic", "balanced", "optimistic"}:
        raise ValueError("Unknown fill model")
    account = db.get(PaperAccount, account_id)
    if account is None:
        raise ValueError("Paper account is not initialized")
    rule_set = db.scalar(
        select(MarketRuleSet)
        .where(MarketRuleSet.verification_status != "deprecated")
        .order_by(MarketRuleSet.effective_date.desc())
        .limit(1)
    )
    fee_profile = db.scalar(select(FeeProfile).order_by(FeeProfile.effective_date.desc()).limit(1))
    session = PaperSession(
        name=name,
        account_id=account_id,
        starting_cash=account.cash,
        approved_universe=sorted(set(universe)),
        strategies=sorted(set(strategies)),
        risk_profile=risk_profile,
        fill_model=fill_model,
        market_rule_set_id=rule_set.id if rule_set else None,
        fee_profile_id=fee_profile.id if fee_profile else None,
    )
    db.add(session)
    append_audit(
        db,
        actor="operator",
        event_type="paper_session.configured",
        entity_type="paper_session",
        entity_id=session.id,
        new_state={
            "name": name,
            "account_id": account_id,
            "fill_model": fill_model,
            "market_rule_set_id": session.market_rule_set_id,
            "fee_profile_id": session.fee_profile_id,
        },
    )
    db.commit()
    db.refresh(session)
    return session


def transition_session(
    db: Session, session: PaperSession, target: str, reason: str, actor: str = "operator"
) -> PaperSession:
    if target not in TRANSITIONS.get(session.state, set()):
        raise ValueError(f"Invalid paper-session transition {session.state} -> {target}")
    if target in ACTIVE_STATES:
        duplicate = db.scalar(
            select(PaperSession).where(
                PaperSession.account_id == session.account_id,
                PaperSession.state.in_(ACTIVE_STATES),
                PaperSession.id != session.id,
            )
        )
        if duplicate:
            raise ValueError(f"Paper account already used by active session {duplicate.name}")
    previous = session.state
    session.state = target
    now = datetime.now(UTC)
    session.heartbeat_at = now
    if target == "warming_up":
        session.started_at = now
    if target in {"stopped", "completed", "failed"}:
        session.stopped_at = now
    db.add(
        PaperSessionRun(
            session_id=session.id,
            run_type="state_transition",
            status=target,
            reason=reason,
            finished_at=now,
        )
    )
    append_audit(
        db,
        actor=actor,
        event_type=f"paper_session.{target}",
        entity_type="paper_session",
        entity_id=session.id,
        previous_state={"state": previous},
        new_state={"state": target, "reason": reason},
    )
    db.commit()
    return session


def heartbeat(db: Session, session: PaperSession, metrics: dict[str, Any] | None = None) -> None:
    session.heartbeat_at = datetime.now(UTC)
    db.add(
        PaperSessionRun(
            session_id=session.id,
            run_type="heartbeat",
            status=session.state,
            metrics=metrics or {},
            finished_at=session.heartbeat_at,
        )
    )
    db.commit()


def recover_stale_sessions(db: Session, stale_after_seconds: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    sessions = db.scalars(
        select(PaperSession).where(
            PaperSession.state.in_({"warming_up", "running"}), PaperSession.heartbeat_at < cutoff
        )
    ).all()
    for session in sessions:
        transition_session(
            db, session, "reconciliation_required", "stale_session_recovered", "startup_recovery"
        )
    return len(sessions)


def summary(session: PaperSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "name": session.name,
        "state": session.state,
        "starting_cash": str(session.starting_cash),
        "universe": session.approved_universe,
        "strategies": session.strategies,
        "risk_profile": session.risk_profile,
        "fill_model": session.fill_model,
        "market_rule_set_id": session.market_rule_set_id,
        "fee_profile_id": session.fee_profile_id,
        "heartbeat_at": session.heartbeat_at.isoformat() if session.heartbeat_at else None,
    }

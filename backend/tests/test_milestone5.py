from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import AuditEvent
from app.services.audit import (
    append_audit,
    audit_status,
    initialize_canonical_chain,
    verify_audit_chain,
)
from app.services.shadow_portfolio import compare_shadow_portfolios


def test_audit_archive_new_chain_dry_run_preserves_legacy(db: Session, tmp_path: Path) -> None:
    append_audit(db, actor="legacy", event_type="legacy.event", entity_type="test")
    db.commit()
    before = db.query(AuditEvent).count()
    result = initialize_canonical_chain(
        db,
        tmp_path,
        "Operator approves evidence-preserving recovery",
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert not Path(result["archive_path"]).exists()
    assert db.query(AuditEvent).count() == before


def test_concurrent_canonical_audit_writers_are_serialized(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(
        db,
        tmp_path,
        "Operator approves canonical concurrency test",
    )

    def write(index: int) -> None:
        with SessionLocal() as session:
            append_audit(
                session,
                actor=f"writer-{index}",
                event_type="stress.concurrent",
                entity_type="test",
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    db.expire_all()
    status = audit_status(db)
    assert status["canonical_valid"] is True
    assert status["canonical_events"] == 41
    assert verify_audit_chain(db)


def test_audit_crash_rollback_does_not_leave_gap(db: Session, tmp_path: Path) -> None:
    initialize_canonical_chain(db, tmp_path, "Operator approves crash rollback test")
    append_audit(db, actor="crash", event_type="crash.pending", entity_type="test")
    db.rollback()
    append_audit(db, actor="recovery", event_type="crash.recovered", entity_type="test")
    db.commit()
    assert verify_audit_chain(db)
    assert audit_status(db)["canonical_events"] == 3


def test_shadow_portfolio_comparison_has_required_metrics() -> None:
    result = compare_shadow_portfolios(
        {
            "reference_imported": [100, 102, 101],
            "buy_and_hold": [100, 101, 103],
            "dsex": [100, 99, 101],
            "strategy_ma": [100, 103, 102],
            "combined_paper": [100, 102, 104],
        }
    )
    metrics = result["portfolios"]["combined_paper"]
    assert {
        "return",
        "drawdown",
        "volatility",
        "sharpe",
        "sortino",
        "turnover",
        "fees",
        "slippage",
        "rejected_trades",
        "missed_trades",
        "risk_interventions",
    } <= metrics.keys()
    assert result["ranking_policy"].startswith("No portfolio")

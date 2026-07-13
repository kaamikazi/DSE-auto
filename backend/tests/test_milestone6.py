from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models import (
    AuditEvent,
    ImportBatch,
    JobExecution,
    MarketBar,
    OperationalIncident,
    PaperAccount,
    RiskState,
    ValidationCampaign,
)
from app.services.attested_imports import (
    ATTESTATION,
    activate_attested_import,
    preview_attested_import,
    rollback_attested_import,
)
from app.services.campaign_analytics import campaign_metrics
from app.services.campaign_simulation import run_accelerated_campaign
from app.services.campaigns import (
    archive_campaign,
    change_campaign_rule_set,
    create_campaign,
    detect_missed_trading_days,
    recover_campaigns_after_restart,
    recover_missed_eod,
    start_campaign_day,
    transition_campaign,
)
from app.services.governance import (
    create_fee_profile,
    create_rule_set,
    evaluate_strategy_suspension,
    promote_strategy,
    register_strategy,
    trade_cost_breakdown,
)
from app.services.incidents import open_incident, transition_incident
from app.services.observability import scheduler_lag_seconds


def _rules(holidays: list[str] | None = None) -> dict[str, object]:
    return {
        "market_timezone": "Asia/Dhaka",
        "weekly_trading_days": ["sunday", "monday", "tuesday", "wednesday", "thursday"],
        "trading_sessions": [{"start": "10:00", "end": "14:30"}],
        "auction_periods": {},
        "holidays": holidays or [],
        "tick_sizes": [{"minimum": 0, "tick": 0.1}],
        "price_bands": {"default_percent": 10},
        "settlement_assumptions": {"equity": "T+2"},
        "short_selling_policy": "blocked",
        "leverage_policy": "blocked",
        "minimum_order_quantity": 1,
        "order_expiry_rules": "end_of_session",
        "transaction_fee_assumptions": "fee_profile",
        "tax_assumptions": "conservative",
        "liquidity_thresholds": {"minimum_daily_volume": 1000},
    }


def _setup_campaign(
    db: Session,
    *,
    name: str = "campaign-one",
    start: date = date(2026, 1, 4),
    end: date = date(2026, 2, 1),
) -> ValidationCampaign:
    if db.get(PaperAccount, 1) is None:
        db.add(PaperAccount(id=1, cash=Decimal("1000000"), starting_cash=Decimal("1000000")))
    if db.get(RiskState, 1) is None:
        db.add(RiskState(id=1, state="healthy", reason="test"))
    db.commit()
    suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
    rule_set = create_rule_set(
        db,
        version=f"rules-{suffix}",
        effective_date=start,
        source_reference="Test assumptions",
        verification_status="assumed",
        operator_approval="Operator approves test assumptions",
        rules=_rules(),
    )
    fees = create_fee_profile(
        db,
        name=f"fees-{suffix}",
        version="1",
        effective_date=start,
        configuration={},
    )
    strategy = register_strategy(
        db,
        strategy_id=f"strategy-{suffix}",
        version="1",
        code_hash=hashlib.sha256(name.encode()).hexdigest(),
        parameters={},
        data_requirements={"bars": 50},
        minimum_sample_size=20,
        evidence={
            "backtest_report": "present",
            "walk_forward_report": "present",
            "sensitivity_report": "present",
            "risk_review": "present",
            "sample_size": 100,
        },
    )
    promote_strategy(db, strategy, "research", "Operator research decision")
    promote_strategy(db, strategy, "paper_candidate", "Operator approves candidate")
    promote_strategy(db, strategy, "paper_active", "Operator approves paper activation")
    return create_campaign(
        db,
        name=name,
        start_date=start,
        planned_end_date=end,
        approved_symbols=["GP", "ACI", "BRACBANK"],
        approved_strategies=[f"{strategy.strategy_id}@1"],
        starting_capital=Decimal("1000000"),
        risk_profile={"max_drawdown": 0.1},
        data_source_policy={"approved": ["operator_attested"]},
        timestamp_trust_requirement="operator_attested",
        fill_model="pessimistic",
        benchmark="DSEX",
        operator_notes="Sustained paper validation",
        active_rule_set_id=rule_set.id,
        active_fee_profile_id=fees.id,
    )


def test_multi_day_campaign_lifecycle_and_single_controller(db: Session) -> None:
    campaign = _setup_campaign(db)
    transition_campaign(db, campaign, "active", "Operator starts validation")
    second = _setup_campaign(db, name="campaign-two")
    with pytest.raises(ValueError, match="controlled"):
        transition_campaign(db, second, "active", "Competing activation")
    ready = {"ready": True, "checks": {"test": {"passed": True}}}
    first = start_campaign_day(
        db, campaign, get_settings(), date(2026, 1, 4), readiness_override=ready
    )
    second_day = start_campaign_day(
        db, campaign, get_settings(), date(2026, 1, 5), readiness_override=ready
    )
    assert first.session_id != second_day.session_id
    assert first.campaign_id == second_day.campaign_id == campaign.id
    with pytest.raises(ValueError, match="already exists"):
        start_campaign_day(db, campaign, get_settings(), date(2026, 1, 5), readiness_override=ready)


def test_duplicate_campaign_name_is_rejected(db: Session) -> None:
    existing = _setup_campaign(db, name="duplicate-name")
    with pytest.raises(IntegrityError):
        create_campaign(
            db,
            name=existing.name,
            start_date=existing.start_date,
            planned_end_date=existing.planned_end_date,
            approved_symbols=existing.approved_symbols,
            approved_strategies=existing.approved_strategies,
            starting_capital=existing.starting_capital,
            risk_profile=existing.risk_profile,
            data_source_policy=existing.data_source_policy,
            timestamp_trust_requirement=existing.timestamp_trust_requirement,
            fill_model=existing.fill_model,
            benchmark=existing.benchmark,
            operator_notes=existing.operator_notes,
            active_rule_set_id=existing.active_rule_set_id,
            active_fee_profile_id=existing.active_fee_profile_id,
        )
    db.rollback()


def test_missed_trading_day_and_restart_recovery(db: Session) -> None:
    campaign = _setup_campaign(db)
    transition_campaign(db, campaign, "active", "Operator starts validation")
    missed = detect_missed_trading_days(db, campaign, date(2026, 1, 5))
    assert [item.market_date for item in missed] == [date(2026, 1, 4), date(2026, 1, 5)]
    ready = {"ready": True, "checks": {"test": {"passed": True}}}
    live_day = start_campaign_day(
        db, campaign, get_settings(), date(2026, 1, 6), readiness_override=ready
    )
    assert recover_campaigns_after_restart(db, date(2026, 1, 7)) == [campaign.id]
    assert campaign.state == "reconciliation_required"
    assert live_day.state == "reconciliation_required"


def test_missed_eod_recovery_generates_evidence_and_backup(db: Session, tmp_path: Path) -> None:
    campaign = _setup_campaign(db)
    transition_campaign(db, campaign, "active", "Operator starts validation")
    ready = {"ready": True, "checks": {"test": {"passed": True}}}
    day = start_campaign_day(
        db, campaign, get_settings(), date(2026, 1, 4), readiness_override=ready
    )
    recovered = recover_missed_eod(
        db,
        campaign,
        get_settings(),
        as_of=date(2026, 1, 5),
        evidence_dir=tmp_path / "evidence",
        backup_dir=tmp_path / "backups",
    )
    assert recovered == [day.id]
    assert day.eod_completed and Path(day.evidence_path or "").exists()
    assert list((tmp_path / "backups").glob("*.db"))
    assert campaign.state == "paused"


def test_attested_import_validation_duplicate_activation_and_rollback(
    db: Session, tmp_path: Path
) -> None:
    campaign = _setup_campaign(db)
    valid = (
        b"symbol,timestamp,last_price,volume,source\nGP,2026-01-04T14:30:00+06:00,250,1000,manual\n"
    )
    preview = preview_attested_import(
        db,
        filename="quotes.csv",
        raw=valid,
        import_kind="quote",
        market_date=date(2026, 1, 4),
        operator_attestation=ATTESTATION,
        raw_dir=tmp_path,
        campaign_id=campaign.id,
    )
    assert preview["activation_allowed"] and preview["exchange_verified"] is False
    batch = db.get(ImportBatch, str(preview["batch_id"]))
    assert batch is not None
    activate_attested_import(db, batch, "Operator reviewed and approves activation")
    bar = db.scalar(select(MarketBar).where(MarketBar.import_batch_id == batch.id))
    assert bar is not None and bar.timestamp_provenance == "operator_attested"
    with pytest.raises(ValueError, match="Duplicate batch"):
        preview_attested_import(
            db,
            filename="copy.csv",
            raw=valid,
            import_kind="quote",
            market_date=date(2026, 1, 4),
            operator_attestation=ATTESTATION,
            raw_dir=tmp_path,
        )
    retained = Path(batch.raw_file_path or "")
    rollback_attested_import(db, batch, "Operator found a source correction")
    assert retained.exists()
    assert db.scalar(select(MarketBar).where(MarketBar.import_batch_id == batch.id)) is None


def test_import_requires_exact_attestation_and_valid_timestamp(db: Session, tmp_path: Path) -> None:
    raw = b"symbol,timestamp,last_price,source\nGP,2026-01-04T14:30:00,250,manual\n"
    with pytest.raises(ValueError, match="confirm exactly"):
        preview_attested_import(
            db,
            filename="bad.csv",
            raw=raw,
            import_kind="quote",
            market_date=date(2026, 1, 4),
            operator_attestation="yes",
            raw_dir=tmp_path,
        )
    preview = preview_attested_import(
        db,
        filename="bad.csv",
        raw=raw,
        import_kind="quote",
        market_date=date(2026, 1, 4),
        operator_attestation=ATTESTATION,
        raw_dir=tmp_path,
    )
    assert preview["status"] == "rejected"
    assert "UTC offset" in preview["errors"][0]["error"]


def test_rule_versioning_and_active_campaign_lock(db: Session) -> None:
    campaign = _setup_campaign(db)
    original = campaign.active_rule_set_id
    replacement = create_rule_set(
        db,
        version="rules-replacement",
        effective_date=date(2026, 1, 5),
        source_reference="Replacement evidence",
        verification_status="partially_verified",
        operator_approval="Operator approves replacement version",
        rules=_rules(),
        change_history=[{"from": original, "reason": "new evidence"}],
    )
    assert replacement.integrity_hash
    transition_campaign(db, campaign, "active", "Operator starts validation")
    with pytest.raises(ValueError, match="locked"):
        change_campaign_rule_set(db, campaign, replacement.id, "Operator change approval")
    assert campaign.active_rule_set_id == original


def test_fee_calculation_is_conservative_and_asymmetric(db: Session) -> None:
    profile = create_fee_profile(
        db,
        name="fees",
        version="1",
        effective_date=date(2026, 1, 1),
        configuration={"tax_sell_percent": 0.2, "flat_sell_charge": 3},
    )
    buy = trade_cost_breakdown(profile, "buy", Decimal("10000"))
    sell = trade_cost_breakdown(profile, "sell", Decimal("10000"))
    assert buy["brokerage"] == Decimal("50.00")
    assert sell["tax"] == Decimal("20.00")
    assert sell["total"] > buy["total"]


def test_strategy_promotion_and_automatic_suspension(db: Session) -> None:
    strategy = register_strategy(
        db,
        strategy_id="governed",
        version="1",
        code_hash="a" * 64,
        parameters={},
        data_requirements={},
        minimum_sample_size=50,
        evidence={"sample_size": 10},
    )
    promote_strategy(db, strategy, "research", "Operator research review")
    with pytest.raises(ValueError, match="evidence missing"):
        promote_strategy(db, strategy, "paper_candidate", "Operator candidate approval")
    strategy.evidence = {
        "backtest_report": "x",
        "walk_forward_report": "x",
        "sensitivity_report": "x",
        "risk_review": "x",
        "sample_size": 60,
    }
    promote_strategy(db, strategy, "paper_candidate", "Operator candidate approval")
    promote_strategy(db, strategy, "paper_active", "Operator activation approval")
    assert (
        evaluate_strategy_suspension(db, strategy, {"data_failures": 3}) == "repeated_data_failures"
    )
    assert strategy.lifecycle_state == "suspended"


def test_campaign_analytics_separates_operational_effects() -> None:
    metrics = campaign_metrics(
        [100, 101, 99, 103],
        [100, 100.5, 100, 101],
        [{"pnl": 10}, {"pnl": -5}],
        {
            "fees": 5,
            "slippage": 2,
            "execution_effects": {"partial_fill_cost": 1},
            "data_quality_effects": {"missed_signals": 2},
            "risk_engine_effects": {"rejections": 1},
            "operator_decisions": {"pauses": 1},
        },
    )
    assert metrics["maximum_drawdown"] < 0
    assert metrics["profit_factor"] == 2
    assert metrics["execution_effects"]["partial_fill_cost"] == 1
    assert metrics["interpretation"].startswith("Short paper campaigns")


def test_incident_lifecycle_and_scheduler_lag(db: Session) -> None:
    incident = open_incident(db, "provider_outage", "high", evidence={"provider": "test"})
    transition_incident(db, incident, "acknowledged", owner="operator")
    transition_incident(db, incident, "mitigated", remediation="attested import fallback")
    transition_incident(db, incident, "resolved", root_cause="upstream outage")
    assert incident.resolved_at is not None
    db.add(
        JobExecution(
            job_name="stale_job",
            status="success",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
    )
    db.commit()
    lag = scheduler_lag_seconds(db)
    assert lag is not None and lag >= 29 * 60


def test_backup_and_audit_failures_open_incidents(db: Session, tmp_path: Path) -> None:
    campaign = _setup_campaign(db)
    transition_campaign(db, campaign, "active", "Operator starts validation")
    ready = {"ready": True, "checks": {"test": {"passed": True}}}
    day = start_campaign_day(
        db, campaign, get_settings(), date(2026, 1, 4), readiness_override=ready
    )
    non_sqlite = Settings(DATABASE_URL="postgresql://invalid/never-connected")
    from app.services.campaigns import complete_campaign_day

    with pytest.raises(ValueError, match="backup failed"):
        complete_campaign_day(
            db,
            campaign,
            day,
            non_sqlite,
            evidence_dir=tmp_path / "evidence",
            backup_dir=tmp_path / "backup",
        )
    assert db.scalar(
        select(OperationalIncident).where(OperationalIncident.incident_type == "backup_failure")
    )

    # Use a fresh campaign/day because backup failure intentionally degraded the first.
    second = _setup_campaign(db, name="audit-failure-campaign")
    campaign.state = "failed"
    db.commit()
    transition_campaign(db, second, "active", "Operator starts audit test")
    second_day = start_campaign_day(
        db, second, get_settings(), date(2026, 1, 4), readiness_override=ready
    )
    event = db.scalar(select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(1))
    assert event is not None
    event.integrity_hash = "f" * 64
    db.commit()
    with pytest.raises(ValueError, match="audit verification"):
        complete_campaign_day(
            db,
            second,
            second_day,
            get_settings(),
            evidence_dir=tmp_path / "evidence2",
            backup_dir=tmp_path / "backup2",
        )
    assert db.scalar(
        select(OperationalIncident).where(OperationalIncident.incident_type == "audit_failure")
    )


def test_campaign_completion_and_archive(db: Session) -> None:
    campaign = _setup_campaign(db)
    transition_campaign(db, campaign, "active", "Operator starts validation")
    transition_campaign(db, campaign, "completed", "Planned evidence window completed")
    archive_campaign(db, campaign, "Operator archives immutable campaign record")
    assert campaign.state == "archived"


def test_accelerated_twenty_day_campaign(db: Session, tmp_path: Path) -> None:
    result = cast(
        dict[str, Any],
        run_accelerated_campaign(db, get_settings(), tmp_path / "reports", tmp_path / "backups"),
    )
    assert result["trading_days"] == 20
    assert len(result["symbols"]) >= 3
    assert len(result["strategies"]) >= 2
    assert {
        "provider_outage",
        "stale_data",
        "partial_fill",
        "rejected_order",
        "missed_eod_recovery",
        "restart_recovery",
        "drawdown_intervention",
    } <= set(result["events"])
    assert result["final_reconciliation"]["healthy"] is True
    assert result["audit_valid"] is True
    assert result["campaign_state"] == "completed"
    assert result["evidence_pack_generated"] is True
    assert Path(str(result["report_path"])).exists()
    cumulative = result["summary"]["cumulative"]
    assert cumulative["rejected_trades"] == 1
    assert cumulative["partial_fills"] == 1

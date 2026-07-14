from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.data.providers.fake_certified import FakeCertifiedFeedAdapter
from app.models import (
    CampaignDay,
    DataQualityReport,
    OperationalIncident,
)
from app.services.data_quality import (
    generate_data_quality_report,
    measure_data_quality,
    persist_observations,
)
from app.services.database_migration import migrate_sqlite_to_postgresql
from app.services.disaster_recovery import run_sqlite_disaster_recovery_exercise
from app.services.distributed_simulation import run_distributed_simulation_phase
from app.services.evidence_review import queue_campaign_day_review, submit_review
from app.services.qualification import calculate_qualification
from app.services.risk_validation import validate_risk_controls
from app.services.task_queue import InMemoryBroker


def _completed_day(db: Session, campaign_id: str, market_date: date) -> CampaignDay:
    day = CampaignDay(
        campaign_id=campaign_id,
        market_date=market_date,
        state="completed",
        premarket_completed=True,
        eod_completed=True,
        summary={
            "audit_valid": True,
            "reconciliation": {"healthy": True},
            "backup": {"successful": True, "sha256": "a" * 64},
        },
        completed_at=datetime.now(UTC),
    )
    db.add(day)
    db.commit()
    return day


def _quality_report(db: Session, campaign_id: str, market_date: date) -> None:
    db.add(
        DataQualityReport(
            scope="daily",
            campaign_id=campaign_id,
            start_date=market_date,
            end_date=market_date,
            metrics={"passed": True},
            json_path="daily.json",
            csv_path="daily.csv",
            chart_path="daily.svg",
            integrity_hash=(market_date.isoformat().replace("-", "") + "0" * 64)[:64],
            passed=True,
        )
    )
    db.commit()


def test_fake_certified_adapter_declares_full_test_only_contract() -> None:
    adapter = FakeCertifiedFeedAdapter(now=datetime(2026, 7, 13, 5, tzinfo=UTC))
    descriptor = adapter.descriptor()
    assert descriptor.licensing_status == "test_only"
    assert descriptor.timestamp_trust.value == "exchange_verified"
    assert {
        "streaming_quotes",
        "polling_quotes",
        "historical_data",
        "dsex_index",
        "market_depth",
        "corporate_actions",
        "price_sensitive_news",
    } == set(descriptor.capabilities)
    assert adapter.get_capabilities().suitable_for_order_approval


def test_data_latency_calculation_and_evidence_exports(db: Session, tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 5, tzinfo=UTC)
    adapter = FakeCertifiedFeedAdapter(now=now)
    quotes = [adapter.get_quote(symbol) for symbol in ("GP", "ACI", "BRACBANK")]
    metrics = measure_data_quality(
        quotes,
        expected_symbols={"GP", "ACI", "BRACBANK"},
        now=now + timedelta(seconds=2),
        activated_at=now + timedelta(seconds=3),
        expected_updates=3,
    )
    assert metrics["passed"] is True
    assert metrics["quote_age_seconds_max"] == 2
    assert metrics["activation_latency_seconds_mean"] == 3
    persist_observations(db, quotes, metrics, campaign_id="quality-campaign")
    report = generate_data_quality_report(
        db,
        scope="daily",
        start_date=now.date(),
        end_date=now.date(),
        campaign_id="quality-campaign",
        output_dir=tmp_path,
    )
    assert report.passed
    assert Path(report.json_path).is_file()
    assert Path(report.csv_path).is_file()
    assert Path(report.chart_path).is_file()


def test_daily_review_rejection_and_qualification_math(db: Session) -> None:
    campaign_id = "review-campaign"
    first = _completed_day(db, campaign_id, date(2026, 7, 12))
    second = _completed_day(db, campaign_id, date(2026, 7, 13))
    _quality_report(db, campaign_id, first.market_date)
    _quality_report(db, campaign_id, second.market_date)
    accepted = queue_campaign_day_review(db, first)
    rejected = queue_campaign_day_review(db, second)
    submit_review(
        db,
        accepted,
        reviewer="reviewer-one",
        reviewer_role="reviewer",
        target_state="accepted",
        data_quality_verdict="pass",
        strategy_behavior_verdict="pass",
        risk_engine_verdict="pass",
        execution_model_verdict="pass",
        incidents_reviewed=[],
        comments="Evidence checks passed.",
        approval_decision="accept",
    )
    submit_review(
        db,
        rejected,
        reviewer="reviewer-one",
        reviewer_role="reviewer",
        target_state="rejected",
        data_quality_verdict="concern",
        strategy_behavior_verdict="fail",
        risk_engine_verdict="pass",
        execution_model_verdict="concern",
        incidents_reviewed=[],
        comments="Strategy behavior requires investigation.",
        approval_decision="reject",
    )
    result = calculate_qualification(db, campaign_id, target_days=2)
    assert result.counts["reviewed_days"] == 2
    assert result.counts["rejected_days"] == 1
    assert result.counts["qualifying_days"] == 1
    assert result.remaining_qualifying_days == 1
    assert not result.qualifying


def test_unresolved_critical_incident_fails_qualification(db: Session) -> None:
    campaign_id = "incident-campaign"
    day = _completed_day(db, campaign_id, date(2026, 7, 13))
    _quality_report(db, campaign_id, day.market_date)
    review = queue_campaign_day_review(db, day)
    submit_review(
        db,
        review,
        reviewer="operator",
        reviewer_role="operator",
        target_state="accepted",
        data_quality_verdict="pass",
        strategy_behavior_verdict="pass",
        risk_engine_verdict="pass",
        execution_model_verdict="pass",
        incidents_reviewed=[],
        comments="Accepted before incident check.",
        approval_decision="accept",
    )
    db.add(
        OperationalIncident(
            campaign_id=campaign_id,
            incident_type="database_failure",
            state="open",
            severity="critical",
        )
    )
    db.commit()
    result = calculate_qualification(db, campaign_id, target_days=1)
    assert "unresolved_critical_incidents" in result.failure_reasons
    assert not result.qualifying


def test_risk_control_effectiveness_report_covers_required_controls(db: Session) -> None:
    run = validate_risk_controls(db, campaign_id="risk-campaign")
    assert run.report["all_controls_effective"] is True
    assert not run.report["missed_risk_candidates"]
    assert set(run.report["controls"]) == {
        "position_limits",
        "concentration_limits",
        "daily_loss_limits",
        "campaign_drawdown_limits",
        "liquidity_limits",
        "stale_data_blocking",
        "provider_disagreement_blocking",
        "repeated_loss_cooldown",
        "strategy_suspension",
        "emergency_stop",
        "restart_recovery",
        "reconciliation_mismatch",
    }


def test_sqlite_migration_copy_has_matching_counts_and_hashes(db: Session, tmp_path: Path) -> None:
    db.add(
        OperationalIncident(incident_type="provider_outage", state="resolved", severity="medium")
    )
    db.commit()
    destination = tmp_path / "destination.db"
    result = migrate_sqlite_to_postgresql(
        Settings().DATABASE_URL,
        f"sqlite:///{destination.as_posix()}",
        dry_run=False,
        allow_test_destination=True,
    )
    assert result["copied"] is True
    assert result["count_match"] is True
    assert result["hash_match"] is True
    assert result["verified"] is True
    assert result["sequence_state"] == {}
    assert result["sequence_state_valid"] is True


def test_full_sqlite_disaster_recovery_exercise(db: Session, tmp_path: Path) -> None:
    run = run_sqlite_disaster_recovery_exercise(
        db,
        Settings(),
        exercise_dir=tmp_path / "exercise",
        configuration_files=(Path("../.env.example"),),
    )
    assert run.status == "passed"
    assert run.checks["sqlite_quick_check"] == "ok"
    assert run.checks["audit_valid"] is True
    assert run.checks["secrets_excluded"] is True


def test_reviewer_can_read_audit_but_cannot_pause(client) -> None:  # type: ignore[no-untyped-def]
    reviewer = {"X-API-Key": "development-reviewer-secret-change-me"}
    assert client.get("/api/v1/audit").status_code == 401
    assert client.get("/api/v1/audit", headers=reviewer).status_code == 200
    assert client.post("/api/v1/risk/pause", headers=reviewer).status_code == 403


def test_accelerated_30_day_simulation_is_explicit_local_emulation(
    db: Session, tmp_path: Path
) -> None:
    report = run_distributed_simulation_phase(
        db,
        InMemoryBroker(),
        campaign_name="test-m7-30-day",
        start_day=1,
        end_day=30,
        output_dir=tmp_path,
    )
    assert report["execution_mode"] == "local_emulation"
    assert report["infrastructure_verified"] is False
    assert report["symbols"] == ["GP", "ACI", "BRACBANK"]
    assert len(report["strategies"]) == 2
    assert set(report["task_states"]) == {"succeeded"}
    qualification = report["final_qualification"]
    assert qualification["counts"]["completed_days"] == 30
    assert qualification["counts"]["reviewed_days"] == 30
    assert qualification["counts"]["rejected_days"] == 1
    assert qualification["counts"]["qualifying_days"] == 29
    assert qualification["remaining_qualifying_days"] == 31
    assert report["final_reconciliation"]["healthy"] is True
    assert report["profitability_claimed"] is False

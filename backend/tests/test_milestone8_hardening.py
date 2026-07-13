from __future__ import annotations

import json
import shutil
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.data.adapters.certification import certify_adapter, provider_may_activate
from app.data.providers.fake_certified import FakeCertifiedFeedAdapter
from app.models import (
    CampaignDay,
    DataQualityReport,
    EvidenceReview,
    RiskState,
    ValidationCampaign,
)
from app.services.audit import append_audit, verify_audit_chain
from app.services.database_migration import compare_database_fingerprints
from app.services.infrastructure_doctor import run_infrastructure_doctor
from app.services.infrastructure_incidents import run_controlled_exercise
from app.services.qualification import calculate_qualification
from app.services.recovery_bundle import create_recovery_bundle, verify_recovery_bundle

ROOT = Path(__file__).resolve().parents[2]


def test_legacy_audit_writes_have_monotonic_ordering(db: Session) -> None:
    events = [
        append_audit(
            db,
            actor="ordering-test",
            event_type="audit.ordering_test",
            entity_type="test",
            entity_id=str(index),
        )
        for index in range(50)
    ]
    db.commit()
    timestamps = [event.timestamp for event in events]
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert verify_audit_chain(db)


def test_infrastructure_doctor_reports_machine_and_human_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "C:/Program Files/Docker/docker.exe")

    def runner(command: list[str] | tuple[str, ...]) -> tuple[int, str]:
        joined = " ".join(command)
        if joined == "docker version":
            return 0, "Client: Docker\nServer: Docker Engine"
        if joined == "docker compose version":
            return 0, "Docker Compose version v5"
        if joined == "docker compose ps --format json db db_test redis":
            return 0, "\n".join(
                json.dumps({"Service": service, "State": "running", "Health": "healthy"})
                for service in ("db", "db_test", "redis")
            )
        if joined == "wsl.exe --status":
            return 0, "Default Version: 2"
        if "Get-Service" in joined:
            return 0, "Running"
        return 1, "unexpected"

    report = run_infrastructure_doctor(
        tmp_path,
        runner=runner,
        tcp_probe=lambda _, port: port in {5432, 6379},
        resource_reader=lambda: {
            "disk_free_gb": 100,
            "memory_total_gb": 16,
            "memory_free_gb": 8,
            "virtualization_available": True,
        },
    )
    assert report["ready"] is True
    assert report["safety"]["configuration_changed"] is False
    assert Path(report["json_path"]).is_file()
    assert "READY" in Path(report["human_report_path"]).read_text(encoding="utf-8")


def test_infrastructure_doctor_accepts_wsl2_engine_when_windows_service_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "C:/Program Files/Docker/docker.exe")

    def runner(command: list[str] | tuple[str, ...]) -> tuple[int, str]:
        joined = " ".join(command)
        if joined == "docker version":
            return 0, "Client: Docker\nServer: Docker Desktop\nOS/Arch: linux/amd64"
        if joined == "docker compose version":
            return 0, "Docker Compose version v5"
        if joined == "docker compose ps --format json db db_test redis":
            return 0, json.dumps(
                [
                    {"Service": service, "State": "running", "Health": "healthy"}
                    for service in ("db", "db_test", "redis")
                ]
            )
        if joined == "wsl.exe --status":
            return 0, "Default Version: 2"
        if "Get-Service" in joined:
            return 0, "Stopped"
        return 1, "unexpected"

    report = run_infrastructure_doctor(
        tmp_path,
        runner=runner,
        tcp_probe=lambda _, port: port in {5432, 6379},
        resource_reader=lambda: {
            "disk_free_gb": 100,
            "memory_total_gb": 16,
            "memory_free_gb": 8,
            "virtualization_available": True,
        },
    )

    service = next(check for check in report["checks"] if check["name"] == "docker_service_running")
    containers = next(
        check for check in report["checks"] if check["name"] == "required_containers_healthy"
    )
    assert report["ready"] is True
    assert service["passed"] is False
    assert service["required"] is False
    assert containers["passed"] is True
    assert "WARN" in Path(report["human_report_path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "exercise",
    [
        "postgresql_unavailable",
        "redis_unavailable",
        "one_worker_killed",
        "scheduler_killed",
        "redis_restarted_with_queue",
        "database_pool_exhaustion",
        "backup_destination_unavailable",
        "dead_letter_accumulation",
        "stale_lease",
        "corrupted_task_payload",
        "database_migration_mismatch",
        "provider_certification_failure",
        "invalid_recovery_manifest",
    ],
)
def test_controlled_infrastructure_exercises_fail_closed(
    db: Session, tmp_path: Path, exercise: str
) -> None:
    report = run_controlled_exercise(db, exercise, tmp_path)
    assert report["fail_closed"] is True
    assert report["evidence_preserved"] is True
    assert report["status"] in {"recovered", "operator_required"}
    assert Path(report["report_path"]).is_file()


def test_database_fingerprint_mismatch_is_blocking(tmp_path: Path) -> None:
    source = create_engine(f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    destination = create_engine(f"sqlite:///{(tmp_path / 'destination.db').as_posix()}")
    Base.metadata.create_all(source)
    Base.metadata.create_all(destination)
    with Session(source) as session:
        session.add(RiskState(id=1, state="healthy"))
        session.commit()
    result = compare_database_fingerprints(source, destination)
    assert result["verified"] is False
    assert result["fail_closed"] is True
    assert "risk_state" in result["count_mismatches"]


def test_dependency_inputs_are_exact_and_locks_are_hashed() -> None:
    requirements = ROOT / "backend/requirements"
    for input_path in requirements.glob("*.in"):
        for line in input_path.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("-r"):
                assert "==" in line
    for name in ("runtime", "development", "testing", "providers"):
        lock = (requirements / f"{name}.lock.txt").read_text(encoding="utf-8")
        assert "--hash=sha256:" in lock


def test_clean_machine_bundle_excludes_secrets_and_restores(db: Session, tmp_path: Path) -> None:
    del db
    bundle = tmp_path / "recovery.zip"
    result = create_recovery_bundle(
        ROOT,
        Path(__file__).parent / "test.db",
        bundle,
        evidence_roots=(),
    )
    assert result["passed"] is True
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        assert not any(Path(name).name == ".env" for name in names)
        assert not any("node_modules" in Path(name).parts for name in names)
        assert "MANIFEST.json" in names
    restored = verify_recovery_bundle(bundle, tmp_path / "isolated-restore")
    assert restored["passed"] is True
    assert restored["checks"]["database_quick_check"] == "ok"


def test_modified_recovery_manifest_payload_fails_closed(db: Session, tmp_path: Path) -> None:
    del db
    bundle = tmp_path / "recovery.zip"
    create_recovery_bundle(ROOT, Path(__file__).parent / "test.db", bundle)
    extracted = tmp_path / "tampered"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extracted)
    state = extracted / "state" / "operational-state.json"
    state.write_text("{}", encoding="utf-8")
    tampered = Path(shutil.make_archive(str(tmp_path / "tampered-recovery"), "zip", extracted))
    result = verify_recovery_bundle(tampered, tmp_path / "tampered-restore")
    assert result["passed"] is False
    assert "state/operational-state.json" in result["hash_mismatches"]


def test_test_only_provider_fails_licensed_activation(db: Session, tmp_path: Path) -> None:
    adapter = FakeCertifiedFeedAdapter(now=datetime.now(UTC))
    certification = certify_adapter(db, adapter, ["GP", "ACI"], tmp_path)
    assert certification.status == "failed"
    assert provider_may_activate(certification) is False
    persisted = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    licensing = next(
        item for item in persisted["checks"] if item["name"] == "licensed_for_operational_use"
    )
    assert licensing["passed"] is False
    checks = {item["name"]: item["passed"] for item in persisted["checks"]}
    assert {
        "quote_schema",
        "timestamp_provenance",
        "symbol_coverage",
        "freshness",
        "event_ordering",
        "duplicate_handling",
        "reconnect_behavior",
        "missing_updates",
        "latency",
        "historical_consistency",
        "corporate_actions_contract",
        "dsex_coverage",
    } <= checks.keys()
    assert all(passed for name, passed in checks.items() if name != "licensed_for_operational_use")


def test_synthetic_day_never_counts_as_real_market_qualification(db: Session) -> None:
    campaign = ValidationCampaign(
        name="synthetic-isolation",
        start_date=date(2026, 7, 13),
        planned_end_date=date(2026, 9, 30),
        approved_symbols=["GP"],
        approved_strategies=["buy_hold@1"],
        starting_capital=Decimal("1000000"),
        risk_profile={},
        data_source_policy={"provider": "synthetic"},
        timestamp_trust_requirement="exchange_verified",
        fill_model="pessimistic",
        benchmark="DSEX",
        active_rule_set_id="rule",
        active_fee_profile_id="fee",
        evidence_class="synthetic",
    )
    db.add(campaign)
    db.flush()
    day = CampaignDay(
        campaign_id=campaign.id,
        market_date=campaign.start_date,
        state="completed",
        premarket_completed=True,
        eod_completed=True,
        evidence_class="synthetic",
        summary={
            "audit_valid": True,
            "reconciliation": {"healthy": True},
            "backup": {"successful": True},
            "provider_certified": True,
        },
    )
    db.add(day)
    db.flush()
    db.add(
        DataQualityReport(
            scope="daily",
            campaign_id=campaign.id,
            start_date=day.market_date,
            end_date=day.market_date,
            metrics={},
            json_path="quality.json",
            csv_path="quality.csv",
            chart_path="quality.svg",
            integrity_hash="c" * 64,
            passed=True,
        )
    )
    db.add(
        EvidenceReview(
            campaign_day_id=day.id,
            campaign_id=campaign.id,
            state="accepted",
            reviewer="reviewer",
            evidence_pack_hash="d" * 64,
        )
    )
    db.commit()
    paper = calculate_qualification(db, campaign.id, target_days=1)
    assert paper.counts["qualifying_days"] == 1
    real_market = calculate_qualification(
        db, campaign.id, target_days=1, qualification_scope="real_market"
    )
    assert real_market.counts["qualifying_days"] == 0
    assert "campaign_not_real_market" in real_market.failure_reasons

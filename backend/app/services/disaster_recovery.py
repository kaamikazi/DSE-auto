from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import AuditChain, DisasterRecoveryRun
from app.services.audit import verify_audit_chain
from app.services.backups import backup_database


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_logical_hash(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        dump = "\n".join(connection.iterdump())
    return hashlib.sha256(dump.encode()).hexdigest()


def run_sqlite_disaster_recovery_exercise(
    db: Session,
    settings: Settings,
    *,
    exercise_dir: Path,
    evidence_roots: tuple[Path, ...] = (),
    configuration_files: tuple[Path, ...] = (),
) -> DisasterRecoveryRun:
    if not settings.DATABASE_URL.startswith("sqlite:///"):
        raise ValueError("Use PostgreSQL backup/restore scripts for PostgreSQL recovery exercises")
    started = time.perf_counter()
    exercise_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_database(db, settings, exercise_dir / "backups")
    backup_path = Path(str(backup["path"])).resolve()
    rpo_seconds = max(datetime.now(UTC).timestamp() - backup_path.stat().st_mtime, 0)
    restore_path = exercise_dir / "restore" / "restored.db"
    restore_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(backup_path) as source, sqlite3.connect(restore_path) as target:
        source.backup(target)
    with sqlite3.connect(restore_path) as restored:
        quick_check = str(restored.execute("PRAGMA quick_check").fetchone()[0])
        table_count = int(
            restored.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
    copied_evidence: list[str] = []
    for root in evidence_roots:
        if root.exists():
            destination = exercise_dir / "evidence" / root.name
            if root.is_dir():
                shutil.copytree(root, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root, destination)
            copied_evidence.append(str(destination))
    copied_config: list[str] = []
    for config_source in configuration_files:
        if config_source.is_file() and config_source.name != ".env":
            destination = exercise_dir / "configuration" / config_source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_source, destination)
            copied_config.append(str(destination))
    chain = db.query(AuditChain).filter_by(status="active").first()
    archive_preserved = False
    if chain and Path(chain.legacy_archive_path).is_file():
        destination = exercise_dir / "audit" / Path(chain.legacy_archive_path).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chain.legacy_archive_path, destination)
        archive_preserved = _sha256(destination) == chain.legacy_archive_hash
    restored_engine = create_engine(f"sqlite:///{restore_path.as_posix()}")
    restored_session = sessionmaker(bind=restored_engine, expire_on_commit=False)
    with restored_session() as restored_db:
        audit_valid = verify_audit_chain(restored_db)
    restored_engine.dispose()
    rto_seconds = time.perf_counter() - started
    checks: dict[str, Any] = {
        "backup_hash_match": _sqlite_logical_hash(backup_path)
        == _sqlite_logical_hash(restore_path),
        "sqlite_quick_check": quick_check,
        "restored_table_count": table_count,
        "audit_valid": audit_valid,
        "audit_archive_preserved": archive_preserved if chain else True,
        "evidence_preserved": copied_evidence,
        "configuration_preserved": copied_config,
        "secrets_excluded": all(Path(item).name != ".env" for item in copied_config),
        "redis_recovery_source": "task_records and outbox_events in restored database",
    }
    checks["passed"] = bool(
        checks["backup_hash_match"]
        and quick_check == "ok"
        and table_count > 0
        and audit_valid
        and checks["audit_archive_preserved"]
    )
    report_path = exercise_dir / "disaster_recovery_report.json"
    report = {
        "status": "passed" if checks["passed"] else "failed",
        "recovery_point_seconds": rpo_seconds,
        "recovery_time_seconds": rto_seconds,
        "checks": checks,
        "backup": backup,
        "restore_path": str(restore_path),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    report_path.write_text(
        json.dumps(report | {"integrity_hash": digest}, indent=2, default=str), encoding="utf-8"
    )
    record = DisasterRecoveryRun(
        status=str(report["status"]),
        recovery_point_seconds=rpo_seconds,
        recovery_time_seconds=rto_seconds,
        checks=checks,
        evidence_path=str(report_path),
        integrity_hash=digest,
    )
    db.add(record)
    db.commit()
    return record

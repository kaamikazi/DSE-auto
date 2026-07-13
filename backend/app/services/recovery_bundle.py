from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.audit import verify_audit_chain

FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "node_modules",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
FORBIDDEN_NAMES = {".env", "credentials.json", "secrets.json"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(relative: Path) -> bool:
    name = relative.name.lower()
    parts = {part.lower() for part in relative.parts}
    return name not in FORBIDDEN_NAMES and not (parts & FORBIDDEN_PARTS)


def _tracked_files(repository_root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]


def _copy_tree_files(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if not _safe(relative):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def create_recovery_bundle(
    repository_root: Path,
    database_path: Path,
    output_path: Path,
    *,
    evidence_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    database_path = database_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    if (
        settings.TRADING_MODE != "paper"
        or settings.LIVE_TRADING_ENABLED
        or settings.BROKER_ADAPTER != "disabled"
    ):
        raise ValueError("Recovery bundles require paper-only settings and a disabled broker")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_clean = not bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    with tempfile.TemporaryDirectory(prefix="dse-recovery-stage-") as temp:
        stage = Path(temp) / "dse-autotrader-recovery"
        source_dir = stage / "source"
        for relative in _tracked_files(repository_root):
            if not _safe(relative):
                continue
            source = repository_root / relative
            if not source.is_file():
                continue
            target = source_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        backup_path = stage / "state" / "operational.sqlite3"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            closing(sqlite3.connect(database_path)) as source_db,
            closing(sqlite3.connect(backup_path)) as target_db,
        ):
            source_db.backup(target_db)

        for root in evidence_roots:
            resolved = root.resolve()
            label = resolved.name
            _copy_tree_files(resolved, stage / "evidence" / label)

        with closing(sqlite3.connect(backup_path)) as restored:
            tables = {
                str(row[0])
                for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            counts = {
                table: int(restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in sorted(
                    tables
                    & {
                        "audit_events",
                        "audit_chains",
                        "validation_campaigns",
                        "campaign_days",
                        "evidence_reviews",
                        "paper_qualifications",
                        "market_rule_sets",
                        "fee_profiles",
                    }
                )
            }
            migration = (
                restored.execute("SELECT version_num FROM alembic_version").fetchone()
                if "alembic_version" in tables
                else None
            )

        state_export = {
            "source_revision": revision,
            "source_clean": source_clean,
            "migration_revision": migration[0] if migration else None,
            "record_counts": counts,
            "paper_only": True,
            "live_trading_enabled": False,
            "broker_adapter": "disabled",
        }
        export_path = stage / "state" / "operational-state.json"
        export_path.write_text(json.dumps(state_export, indent=2), encoding="utf-8")
        files = {
            item.relative_to(stage).as_posix(): _sha256(item)
            for item in sorted(stage.rglob("*"))
            if item.is_file()
        }
        manifest = {
            "format": "dse-paper-recovery-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": revision,
            "source_clean": source_clean,
            "secrets_excluded": True,
            "files": files,
            "state": state_export,
        }
        (stage / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(stage.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(stage).as_posix())
    result = verify_recovery_bundle(output_path)
    return result | {"bundle_path": str(output_path), "source_revision": revision}


def verify_recovery_bundle(
    bundle_path: Path, restore_directory: Path | None = None
) -> dict[str, Any]:
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if restore_directory is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="dse-recovery-verify-")
        restore_directory = Path(owned_temp.name)
    restore_directory.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            names = [Path(name) for name in archive.namelist()]
            if any(not _safe(name) or name.is_absolute() or ".." in name.parts for name in names):
                raise ValueError("Recovery bundle contains a forbidden or unsafe path")
            archive.extractall(restore_directory)
        try:
            manifest = json.loads((restore_directory / "MANIFEST.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "passed": False,
                "checks": {"manifest_structure_valid": False},
                "hash_mismatches": [],
                "error": f"{type(exc).__name__}: {exc}",
                "restore_directory": str(restore_directory),
            }
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != "dse-paper-recovery-v1"
            or not isinstance(manifest.get("files"), dict)
            or not isinstance(manifest.get("state"), dict)
        ):
            return {
                "passed": False,
                "checks": {"manifest_structure_valid": False},
                "hash_mismatches": [],
                "error": "Unsupported or incomplete recovery manifest",
                "restore_directory": str(restore_directory),
            }
        mismatches = [
            relative
            for relative, expected in manifest["files"].items()
            if not (restore_directory / relative).is_file()
            or _sha256(restore_directory / relative) != expected
        ]
        database = restore_directory / "state" / "operational.sqlite3"
        with closing(sqlite3.connect(database)) as restored:
            quick_check = str(restored.execute("PRAGMA quick_check").fetchone()[0])
            tables = {
                str(row[0])
                for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            audit_rows = (
                int(restored.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
                if "audit_events" in tables
                else 0
            )
            archive_rows = (
                list(
                    restored.execute(
                        "SELECT legacy_archive_path, legacy_archive_hash FROM audit_chains"
                    )
                )
                if "audit_chains" in tables
                else []
            )
            migration = (
                restored.execute("SELECT version_num FROM alembic_version").fetchone()
                if "alembic_version" in tables
                else None
            )
            restored_counts = {
                table: int(restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in manifest["state"]["record_counts"]
            }
        restored_engine = create_engine(f"sqlite:///{database.as_posix()}")
        try:
            with Session(restored_engine) as session:
                audit_valid = verify_audit_chain(session)
        finally:
            restored_engine.dispose()
        archive_checks = []
        for archive_path, expected_hash in archive_rows:
            archived = restore_directory / "evidence" / "audit_archives" / Path(archive_path).name
            archive_checks.append(archived.is_file() and _sha256(archived) == str(expected_hash))
        forbidden = [name.as_posix() for name in names if not _safe(name)]
        checks = {
            "manifest_structure_valid": True,
            "manifest_hashes_valid": not mismatches,
            "database_quick_check": quick_check,
            "audit_records_preserved": audit_rows,
            "canonical_audit_valid": audit_valid,
            "legacy_audit_archives_valid": all(archive_checks),
            "campaign_state_preserved": "validation_campaigns" in tables,
            "qualification_tracker_preserved": "paper_qualifications" in tables,
            "record_counts_match": restored_counts == manifest["state"]["record_counts"],
            "migration_version_match": (migration[0] if migration else None)
            == manifest["state"]["migration_revision"],
            "dependency_locks_present": any(
                name.as_posix().endswith("requirements/runtime.lock.txt") for name in names
            ),
            "secrets_excluded": not forbidden and not any(name.name == ".env" for name in names),
            "paper_only_safety": {
                "trading_mode": "paper" if manifest["state"].get("paper_only") else "unsafe",
                "live_trading_enabled": manifest["state"].get("live_trading_enabled"),
                "broker_adapter": manifest["state"].get("broker_adapter"),
            },
        }
        safety_valid = bool(
            manifest["state"].get("paper_only") is True
            and manifest["state"].get("live_trading_enabled") is False
            and manifest["state"].get("broker_adapter") == "disabled"
        )
        passed = bool(
            not mismatches
            and quick_check == "ok"
            and checks["secrets_excluded"]
            and checks["dependency_locks_present"]
            and audit_valid
            and checks["legacy_audit_archives_valid"]
            and checks["record_counts_match"]
            and checks["migration_version_match"]
            and safety_valid
        )
        return {
            "passed": passed,
            "checks": checks,
            "hash_mismatches": mismatches,
            "restore_directory": str(restore_directory),
        }
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()

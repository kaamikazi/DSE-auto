from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.services.audit import append_audit


def backup_database(
    db: Session, settings: Settings, backup_dir: Path = Path("../data/backups")
) -> dict[str, Any]:
    if not settings.DATABASE_URL.startswith("sqlite:///"):
        raise ValueError("This backup command currently supports SQLite only")
    source = Path(settings.DATABASE_URL.removeprefix("sqlite:///"))
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = (
        backup_dir / f"dse_autotrader_backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.db"
    )
    append_audit(
        db,
        actor="operator",
        event_type="database.backup_requested",
        entity_type="database",
        new_state={"destination": str(destination)},
    )
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "successful": True,
        "path": str(destination),
        "sha256": digest,
        "bytes": destination.stat().st_size,
    }

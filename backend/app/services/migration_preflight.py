from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


def migration_preflight(
    engine: Engine, alembic_ini: Path = Path(__file__).resolve().parents[2] / "alembic.ini"
) -> dict[str, Any]:
    config = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(config)
    expected = script.get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return {
        "current_revision": current,
        "expected_revision": expected,
        "at_head": bool(current and current == expected),
        "safe_to_start": bool(current and current == expected),
    }


def require_migration_head(engine: Engine) -> None:
    result = migration_preflight(engine)
    if not result["safe_to_start"]:
        raise RuntimeError(
            "Database migration preflight failed: "
            f"current={result['current_revision']} expected={result['expected_revision']}"
        )

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

DatabaseRole = Literal[
    "operational",
    "research",
    "test",
    "recovery",
    "postgres_verification",
    "simulation",
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent
OPERATIONAL_SQLITE_PATH = (BACKEND_ROOT / "data" / "dse_autotrader.db").resolve()


def sqlite_path_from_url(database_url: str, *, base_dir: Path = BACKEND_ROOT) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw = database_url.removeprefix("sqlite:///").split("?", 1)[0]
    if raw == ":memory:":
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def resolve_database_url(database_url: str, *, base_dir: Path = BACKEND_ROOT) -> str:
    path = sqlite_path_from_url(database_url, base_dir=base_dir)
    if path is None:
        return database_url
    return f"sqlite:///{path.as_posix()}"


def redacted_database_alias(database_url: str, *, base_dir: Path = BACKEND_ROOT) -> str:
    path = sqlite_path_from_url(database_url, base_dir=base_dir)
    if path is not None:
        return str(path)
    parsed = urlsplit(database_url.replace("postgresql+psycopg://", "postgresql://", 1))
    hostname = parsed.hostname or "unknown-host"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "unknown-database"
    return urlunsplit((parsed.scheme or "unknown", f"{hostname}{port}", database, "", ""))


def database_name(database_url: str) -> str | None:
    if database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        return urlsplit(
            database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        ).path.lstrip("/")
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def database_role_violation(
    *,
    app_env: str,
    database_role: DatabaseRole,
    database_url: str,
    allow_override: bool,
) -> str | None:
    if allow_override:
        return None
    resolved = sqlite_path_from_url(database_url)
    points_to_operational = (
        resolved == OPERATIONAL_SQLITE_PATH or database_name(database_url) == "dse_autotrader"
    )
    non_operational_process = app_env == "test" or database_role in {"test", "simulation"}
    if non_operational_process and points_to_operational:
        return "Test or simulation process refuses the operational SQLite database"
    if app_env == "test" and database_role not in {"test", "postgres_verification"}:
        return "Test environment requires an explicit test or postgres_verification database role"
    return None

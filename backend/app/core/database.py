from __future__ import annotations

import time
from collections.abc import Callable, Generator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine_options: dict[str, Any] = {
    "connect_args": connect_args,
    "pool_pre_ping": True,
    "pool_recycle": settings.DATABASE_POOL_RECYCLE_SECONDS,
}
if settings.DATABASE_URL.startswith(("postgresql://", "postgresql+psycopg://")):
    engine_options.update(
        {
            "pool_size": settings.DATABASE_POOL_SIZE,
            "max_overflow": settings.DATABASE_MAX_OVERFLOW,
            "pool_timeout": settings.DATABASE_POOL_TIMEOUT_SECONDS,
            "isolation_level": settings.DATABASE_ISOLATION_LEVEL,
        }
    )
engine = create_engine(settings.DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def is_retryable_database_error(exc: BaseException) -> bool:
    """Return whether a transaction can safely be retried from its beginning."""

    if not isinstance(exc, (OperationalError, DBAPIError)):
        return False
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate in {"40001", "40P01"}:  # serialization failure, deadlock detected
        return True
    message = str(original or exc).lower()
    return "database is locked" in message or "deadlock" in message


def run_transaction[T](
    operation: Callable[[Session], T],
    *,
    attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> T:
    """Run a whole idempotent transaction with bounded deadlock/serialization retries."""

    limit = settings.DATABASE_TRANSACTION_RETRIES if attempts is None else attempts
    delay = settings.DATABASE_RETRY_BACKOFF_SECONDS if backoff_seconds is None else backoff_seconds
    for attempt in range(limit + 1):
        with SessionLocal() as session:
            try:
                result = operation(session)
                session.commit()
                return result
            except Exception as exc:
                session.rollback()
                if attempt >= limit or not is_retryable_database_error(exc):
                    raise
        time.sleep(delay * (2**attempt))
    raise RuntimeError("unreachable transaction retry state")


def database_health_metadata(db: Session) -> dict[str, Any]:
    """Return portable health plus PostgreSQL replication-readiness metadata."""

    dialect = db.bind.dialect.name if db.bind is not None else "unknown"
    metadata: dict[str, Any] = {
        "healthy": True,
        "dialect": dialect,
        "pool": engine.pool.status(),
        "production_supported": dialect == "postgresql",
    }
    try:
        db.execute(text("SELECT 1"))
        if dialect == "postgresql":
            row = (
                db.execute(
                    text(
                        "SELECT current_setting('server_version') AS server_version, "
                        "current_setting('transaction_isolation') AS transaction_isolation, "
                        "current_setting('transaction_read_only') AS transaction_read_only, "
                        "pg_is_in_recovery() AS is_replica"
                    )
                )
                .mappings()
                .one()
            )
            metadata.update(dict(row))
            metadata["replication_ready"] = True
        else:
            metadata.update(
                {
                    "server_version": None,
                    "transaction_isolation": "SQLite serialized writes",
                    "transaction_read_only": False,
                    "is_replica": False,
                    "replication_ready": False,
                }
            )
    except Exception as exc:
        metadata.update({"healthy": False, "error": type(exc).__name__})
    return metadata

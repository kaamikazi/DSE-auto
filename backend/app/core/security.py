from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import AuthSession, LoginAttempt


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    session_id: str | None = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _principal(
    db: Session,
    x_api_key: str,
    authorization: str,
) -> Principal:
    settings = get_settings()
    if x_api_key and hmac.compare_digest(x_api_key, settings.API_SECRET_KEY):
        return Principal(actor="operator-api-key", role="operator")
    if x_api_key and hmac.compare_digest(x_api_key, settings.REVIEWER_API_SECRET_KEY):
        return Principal(actor="reviewer-api-key", role="reviewer")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        session = db.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
        if (
            session is not None
            and session.revoked_at is None
            and _aware(session.expires_at) > datetime.now(UTC)
        ):
            return Principal(actor=session.actor, role=session.role, session_id=session.id)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


def require_operator(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Principal:
    principal = _principal(db, x_api_key, authorization)
    if principal.role != "operator":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operator role required")
    return principal


def require_reviewer(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Principal:
    principal = _principal(db, x_api_key, authorization)
    if principal.role not in {"operator", "reviewer"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reviewer role required")
    return principal


require_api_key = require_operator


def authenticate_and_create_session(
    db: Session,
    *,
    role: str,
    supplied_key: str,
    actor: str,
    fingerprint: str,
) -> tuple[str, AuthSession]:
    if role not in {"operator", "reviewer"}:
        raise ValueError("Unknown authentication role")
    settings = get_settings()
    fingerprint_hash = _token_hash(fingerprint)
    since = datetime.now(UTC) - timedelta(seconds=settings.LOGIN_WINDOW_SECONDS)
    failures = db.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.fingerprint == fingerprint_hash,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.attempted_at >= since,
        )
    )
    if int(failures or 0) >= settings.LOGIN_MAX_ATTEMPTS:
        raise PermissionError("Login throttled")
    expected = settings.API_SECRET_KEY if role == "operator" else settings.REVIEWER_API_SECRET_KEY
    succeeded = hmac.compare_digest(supplied_key, expected)
    db.add(
        LoginAttempt(
            fingerprint=fingerprint_hash,
            role=role,
            succeeded=succeeded,
        )
    )
    if not succeeded:
        db.commit()
        raise PermissionError("Invalid credentials")
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        token_hash=_token_hash(token),
        role=role,
        actor=actor,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.SESSION_TTL_SECONDS),
    )
    db.add(session)
    db.commit()
    return token, session
